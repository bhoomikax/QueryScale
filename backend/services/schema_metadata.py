"""Database schema metadata used by feature extraction and candidate generation.

Three sources are supported, in order of preference for ``METADATA_SOURCE=auto``:

1. ``mysql``: read-only queries against INFORMATION_SCHEMA of the primary database.
2. ``snapshot``: a JSON file previously written from a live collection.
3. ``schema``: parsing ``database/schema.sql``. This yields columns, types, and
   indexes only. Row counts and cardinalities are left as ``None`` because they
   are unavailable, never invented.

Every statistic carries a ``*_source`` field so downstream consumers can tell
estimates from exact counts.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from backend.config import ROOT_DIR, settings


logger = logging.getLogger(__name__)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NON_INDEXABLE_TYPES = {"text", "tinytext", "mediumtext", "longtext", "blob", "tinyblob", "mediumblob", "longblob", "json", "geometry"}


@dataclass
class ColumnMeta:
    name: str
    data_type: str
    column_type: str
    nullable: bool = True
    char_max_length: Optional[int] = None
    numeric_precision: Optional[int] = None
    auto_increment: bool = False
    cardinality: Optional[int] = None
    cardinality_source: Optional[str] = None


@dataclass
class IndexMeta:
    name: str
    columns: List[str]
    unique: bool = False
    primary: bool = False


@dataclass
class TableMeta:
    name: str
    columns: Dict[str, ColumnMeta] = field(default_factory=dict)
    indexes: List[IndexMeta] = field(default_factory=list)
    row_count: Optional[int] = None
    row_count_source: Optional[str] = None
    data_bytes: Optional[int] = None
    index_bytes: Optional[int] = None

    def column(self, name: str) -> Optional[ColumnMeta]:
        if name in self.columns:
            return self.columns[name]
        lowered = name.lower()
        for column_name, column in self.columns.items():
            if column_name.lower() == lowered:
                return column
        return None

    @property
    def primary_key(self) -> List[str]:
        for index in self.indexes:
            if index.primary:
                return list(index.columns)
        return []


@dataclass
class SchemaMetadata:
    database: str
    source: str
    collected_at: str
    tables: Dict[str, TableMeta] = field(default_factory=dict)
    server_version: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

    def table(self, name: Optional[str]) -> Optional[TableMeta]:
        if not name:
            return None
        if name in self.tables:
            return self.tables[name]
        lowered = name.lower()
        for table_name, table in self.tables.items():
            if table_name.lower() == lowered:
                return table
        return None

    def canonical_table_name(self, name: Optional[str]) -> Optional[str]:
        table = self.table(name)
        return table.name if table else None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "SchemaMetadata":
        tables: Dict[str, TableMeta] = {}
        for name, table in (payload.get("tables") or {}).items():
            columns = {cname: ColumnMeta(**column) for cname, column in (table.get("columns") or {}).items()}
            indexes = [IndexMeta(**index) for index in table.get("indexes") or []]
            tables[name] = TableMeta(
                name=table.get("name", name),
                columns=columns,
                indexes=indexes,
                row_count=table.get("row_count"),
                row_count_source=table.get("row_count_source"),
                data_bytes=table.get("data_bytes"),
                index_bytes=table.get("index_bytes"),
            )
        return cls(
            database=payload.get("database", ""),
            source=payload.get("source", "snapshot"),
            collected_at=payload.get("collected_at", ""),
            tables=tables,
            server_version=payload.get("server_version"),
            warnings=list(payload.get("warnings") or []),
        )


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# schema.sql parsing (no statistics)
# ---------------------------------------------------------------------------

_CREATE_TABLE = re.compile(r"CREATE\s+TABLE\s+`?(\w+)`?\s*\((.*?)\)\s*ENGINE", re.IGNORECASE | re.DOTALL)
_COLUMN_LINE = re.compile(r"^`(\w+)`\s+([A-Za-z]+)(\([^)]*\))?(.*)$")
_KEY_LINE = re.compile(r"^(PRIMARY\s+KEY|UNIQUE\s+KEY\s+`(\w+)`|KEY\s+`(\w+)`|INDEX\s+`(\w+)`)\s*\(([^)]*)\)", re.IGNORECASE)


def _split_definitions(body: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    current: List[str] = []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if "".join(current).strip():
        parts.append("".join(current).strip())
    return parts


def parse_schema_sql(path: Path | str | None = None, database: str | None = None) -> SchemaMetadata:
    """Build metadata from CREATE TABLE statements. Row counts and cardinalities stay unavailable."""

    schema_path = Path(path) if path else ROOT_DIR / "database" / "schema.sql"
    text = schema_path.read_text(encoding="utf-8")
    metadata = SchemaMetadata(database=database or settings.db_name, source="schema", collected_at=_now())
    for table_name, body in _CREATE_TABLE.findall(text):
        table = TableMeta(name=table_name)
        for definition in _split_definitions(body):
            column_match = _COLUMN_LINE.match(definition)
            if column_match:
                name, data_type, args, rest = column_match.groups()
                args_values = [int(v) for v in re.findall(r"\d+", args or "")]
                lowered_type = data_type.lower()
                table.columns[name] = ColumnMeta(
                    name=name,
                    data_type=lowered_type,
                    column_type=f"{lowered_type}{args or ''}",
                    nullable="NOT NULL" not in rest.upper(),
                    char_max_length=args_values[0] if lowered_type in {"varchar", "char"} and args_values else None,
                    numeric_precision=args_values[0] if lowered_type == "decimal" and args_values else None,
                    auto_increment="AUTO_INCREMENT" in rest.upper(),
                )
                continue
            key_match = _KEY_LINE.match(definition)
            if key_match:
                columns = [c.strip().strip("`") for c in key_match.group(5).split(",") if c.strip()]
                head = key_match.group(1).upper()
                if head.startswith("PRIMARY"):
                    table.indexes.append(IndexMeta("PRIMARY", columns, unique=True, primary=True))
                else:
                    name = key_match.group(2) or key_match.group(3) or key_match.group(4)
                    table.indexes.append(IndexMeta(name, columns, unique=head.startswith("UNIQUE")))
        metadata.tables[table_name] = table
    metadata.warnings.append("Row counts and column cardinality are unavailable from schema.sql.")
    return metadata


# ---------------------------------------------------------------------------
# Live MySQL collection (read-only)
# ---------------------------------------------------------------------------

def _canonical_names(names: Iterable[str]) -> Dict[str, str]:
    """Map lower-case table names (lower_case_table_names=1) back to schema.sql spelling."""

    try:
        known = list(parse_schema_sql().tables)
    except OSError:
        known = []
    lookup = {name.lower(): name for name in known}
    return {name: lookup.get(name.lower(), name) for name in names}


def collect_mysql_metadata(
    connection: Any,
    database: str,
    exact_stats_max_rows: int | None = None,
    tables: Sequence[str] | None = None,
) -> SchemaMetadata:
    """Collect metadata with SELECT-only statements. Does not run ANALYZE or modify the database.

    Exact ``COUNT(*)`` and ``COUNT(DISTINCT col)`` are used only when the
    INFORMATION_SCHEMA row estimate is at or below ``exact_stats_max_rows``.
    Larger tables keep the InnoDB estimate and are labelled as such.
    """

    limit = settings.metadata_exact_stats_max_rows if exact_stats_max_rows is None else exact_stats_max_rows
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT VERSION()")
        version_row = cursor.fetchone()
        metadata = SchemaMetadata(
            database=database,
            source="mysql",
            collected_at=_now(),
            server_version=str(version_row[0]) if version_row else None,
        )

        cursor.execute(
            "SELECT TABLE_NAME, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'",
            (database,),
        )
        table_rows = cursor.fetchall()
        names = _canonical_names(row[0] for row in table_rows)
        wanted = {name.lower() for name in tables} if tables else None
        for raw_name, estimate, data_length, index_length in table_rows:
            name = names[raw_name]
            if wanted is not None and name.lower() not in wanted:
                continue
            metadata.tables[name] = TableMeta(
                name=name,
                row_count=int(estimate) if estimate is not None else None,
                row_count_source="information_schema_estimate" if estimate is not None else None,
                data_bytes=int(data_length) if data_length is not None else None,
                index_bytes=int(index_length) if index_length is not None else None,
            )

        cursor.execute(
            "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, CHARACTER_MAXIMUM_LENGTH, "
            "NUMERIC_PRECISION, EXTRA FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = %s "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION",
            (database,),
        )
        for raw_table, column, data_type, column_type, nullable, char_len, precision, extra in cursor.fetchall():
            table = metadata.table(names.get(raw_table, raw_table))
            if table is None:
                continue
            table.columns[column] = ColumnMeta(
                name=column,
                data_type=str(data_type).lower(),
                column_type=str(column_type).lower(),
                nullable=str(nullable).upper() == "YES",
                char_max_length=int(char_len) if char_len is not None else None,
                numeric_precision=int(precision) if precision is not None else None,
                auto_increment="auto_increment" in str(extra or "").lower(),
            )

        cursor.execute(
            "SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, CARDINALITY "
            "FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = %s "
            "ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX",
            (database,),
        )
        index_map: Dict[tuple[str, str], IndexMeta] = {}
        for raw_table, index_name, non_unique, seq, column, cardinality in cursor.fetchall():
            table = metadata.table(names.get(raw_table, raw_table))
            if table is None or column is None:
                continue
            key = (table.name, index_name)
            if key not in index_map:
                index_map[key] = IndexMeta(
                    name=index_name,
                    columns=[],
                    unique=int(non_unique) == 0,
                    primary=index_name == "PRIMARY",
                )
                table.indexes.append(index_map[key])
            index_map[key].columns.append(column)
            column_meta = table.column(column)
            if int(seq) == 1 and column_meta is not None and cardinality is not None:
                column_meta.cardinality = int(cardinality)
                column_meta.cardinality_source = "index_statistics_estimate"

        for table in metadata.tables.values():
            if table.row_count is None or table.row_count > limit:
                continue
            quoted_table = _quote(table.name)
            cursor.execute(f"SELECT COUNT(*) FROM {quoted_table}")
            table.row_count = int(cursor.fetchone()[0])
            table.row_count_source = "exact_count"
            for column in table.columns.values():
                if column.data_type in NON_INDEXABLE_TYPES:
                    continue
                cursor.execute(f"SELECT COUNT(DISTINCT {_quote(column.name)}) FROM {quoted_table}")
                column.cardinality = int(cursor.fetchone()[0])
                column.cardinality_source = "exact_count"
        return metadata
    finally:
        cursor.close()


def _quote(identifier: str) -> str:
    if not _IDENTIFIER.fullmatch(identifier):
        raise ValueError(f"Invalid MySQL identifier: {identifier!r}")
    return f"`{identifier}`"


# ---------------------------------------------------------------------------
# Snapshot persistence and source selection
# ---------------------------------------------------------------------------

def save_snapshot(metadata: SchemaMetadata, path: Path | str | None = None) -> Path:
    target = Path(path) if path else settings.resolve_path(settings.schema_metadata_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(metadata.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return target


def load_snapshot(path: Path | str | None = None) -> Optional[SchemaMetadata]:
    target = Path(path) if path else settings.resolve_path(settings.schema_metadata_path)
    if not target.exists():
        return None
    try:
        metadata = SchemaMetadata.from_dict(json.loads(target.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Ignoring unreadable schema metadata snapshot %s: %s", target, exc)
        return None
    metadata.source = "snapshot"
    return metadata


def _connect_primary_read_only(timeout_seconds: int = 3) -> Any:
    import mysql.connector

    config = settings.mysql_config()
    config["connection_timeout"] = timeout_seconds
    config["raise_on_warnings"] = False
    return mysql.connector.connect(**config)


def load_metadata(source: str | None = None, snapshot_path: Path | str | None = None, save: bool = True) -> SchemaMetadata:
    """Load metadata from the configured source, degrading gracefully.

    ``auto`` tries live MySQL first (saving a snapshot), then the snapshot, then schema.sql.
    """

    selected = (source or settings.metadata_source or "auto").lower()
    errors: List[str] = []

    if selected in {"auto", "mysql"}:
        try:
            connection = _connect_primary_read_only()
            try:
                metadata = collect_mysql_metadata(connection, settings.db_name)
            finally:
                connection.close()
            if save:
                save_snapshot(metadata, snapshot_path)
            return metadata
        except Exception as exc:  # connection, permission, or driver errors
            message = f"Live MySQL metadata unavailable: {type(exc).__name__}: {exc}"
            if selected == "mysql":
                raise RuntimeError(message) from exc
            errors.append(message)
            logger.info(message)

    if selected in {"auto", "snapshot", "snapshot_or_schema"}:
        snapshot = load_snapshot(snapshot_path)
        if snapshot is not None:
            snapshot.warnings.extend(errors)
            snapshot.warnings.append(f"Using metadata snapshot collected at {snapshot.collected_at}.")
            return snapshot
        if selected == "snapshot":
            raise RuntimeError("Schema metadata snapshot not found")

    metadata = parse_schema_sql()
    metadata.warnings.extend(errors)
    return metadata


def column_byte_estimate(column: ColumnMeta) -> int:
    """Approximate on-disk key bytes for one column, used only for labelled storage estimates."""

    data_type = column.data_type
    fixed = {
        "tinyint": 1, "smallint": 2, "mediumint": 3, "int": 4, "integer": 4, "bigint": 8,
        "float": 4, "double": 8, "date": 3, "time": 3, "year": 1, "datetime": 5, "timestamp": 4,
        "bit": 1, "bool": 1, "boolean": 1,
    }
    if data_type in fixed:
        size = fixed[data_type]
    elif data_type == "decimal":
        precision = column.numeric_precision or 10
        size = (precision // 9) * 4 + (0, 1, 1, 2, 2, 3, 3, 4, 4, 4)[precision % 9]
    elif data_type in {"varchar", "char", "varbinary", "binary"}:
        # Average stored length is unknown; assume half the declared length (ASCII) plus length bytes.
        size = max(1, (column.char_max_length or 32) // 2) + 2
    else:
        size = 16
    return size + (1 if column.nullable else 0)
