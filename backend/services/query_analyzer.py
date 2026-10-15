"""SQL query analyzer for the QueryScale banking workload.

The analyzer parses SQL with sqlglot (MySQL dialect) and returns structured
predicate, join, ordering, and grouping information. The legacy keys
``tables``, ``where_columns``, ``join_columns``, and ``order_columns`` are kept
for backward compatibility with existing callers.

If sqlglot cannot parse a statement, a conservative regular-expression
fallback is used and the result is marked ``parse_status="fallback"``. If
nothing useful can be recovered the result is marked ``parse_status="error"``
and contains empty column lists instead of raising.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError


# Predicate kinds, ordered roughly by how useful they are as a B-tree index prefix.
EQUALITY = "equality"
IN_LIST = "in"
NULL_CHECK = "null"
RANGE = "range"
LIKE = "like"
INEQUALITY = "inequality"
FUNCTION = "function"
DISJUNCTIVE = "disjunctive"

SARGABLE_KINDS = {EQUALITY, IN_LIST, NULL_CHECK, RANGE, LIKE}
EQUALITY_KINDS = {EQUALITY, IN_LIST, NULL_CHECK}

_PLACEHOLDER_PATTERN = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|%s|%\([A-Za-z_][A-Za-z0-9_]*\)s")


def normalize_placeholders(sql: str) -> str:
    """Replace DB-API placeholders (``%s`` and ``%(name)s``) outside string literals with ``?``."""

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.startswith(("'", '"')):
            return token
        return "?"

    return _PLACEHOLDER_PATTERN.sub(_replace, sql)


def normalize_sql(sql: str) -> str:
    """Return a whitespace-collapsed, placeholder-normalized SQL string without a trailing semicolon."""

    collapsed = " ".join(normalize_placeholders(sql).strip().split())
    return collapsed.rstrip(";").strip()


# ---------------------------------------------------------------------------
# Regex fallback (original implementation, retained for unparseable SQL)
# ---------------------------------------------------------------------------

def _normalize_table_name(name: str) -> str:
    cleaned = name.strip()
    if cleaned.startswith("`") and cleaned.endswith("`"):
        return cleaned[1:-1]
    return cleaned


def _regex_tables(sql: str) -> List[str]:
    match = re.search(r"FROM\s+([a-zA-Z0-9_`]+)(?:\s+(?:AS\s+)?[a-zA-Z0-9_]+)?", sql, flags=re.IGNORECASE)
    tables: List[str] = []
    if match:
        tables.append(_normalize_table_name(match.group(1)))
    for join_table in re.findall(r"JOIN\s+([a-zA-Z0-9_`]+)", sql, flags=re.IGNORECASE):
        table_name = _normalize_table_name(join_table)
        if table_name not in tables:
            tables.append(table_name)
    return tables


def _regex_where_columns(sql: str) -> List[str]:
    where_match = re.search(r"WHERE\s+(.*?)(?:GROUP\s+BY|ORDER\s+BY|LIMIT|$)", sql, flags=re.IGNORECASE | re.DOTALL)
    if not where_match:
        return []
    names = re.findall(
        r"([a-zA-Z_][a-zA-Z0-9_\.]*)\s*(?:=|!=|<>|<|>|LIKE|IN|BETWEEN|IS\s+NULL|IS\s+NOT\s+NULL)",
        where_match.group(1),
        flags=re.IGNORECASE,
    )
    return _unique(name.split(".")[-1] for name in names)


def _regex_order_columns(sql: str) -> List[str]:
    order_match = re.search(r"ORDER\s+BY\s+(.+?)(?:LIMIT|$)", sql, flags=re.IGNORECASE | re.DOTALL)
    if not order_match:
        return []
    names = re.findall(r"([a-zA-Z_][a-zA-Z0-9_\.]*)", order_match.group(1))
    return _unique(name.split(".")[-1] for name in names if name.lower() not in {"asc", "desc"})


def _unique(values) -> List[str]:
    result: List[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


# ---------------------------------------------------------------------------
# sqlglot-based analysis
# ---------------------------------------------------------------------------

def _query_type(tree: exp.Expression) -> str:
    if isinstance(tree, (exp.Select, exp.Union)):
        return "SELECT"
    if isinstance(tree, exp.Insert):
        return "INSERT"
    if isinstance(tree, exp.Update):
        return "UPDATE"
    if isinstance(tree, exp.Delete):
        return "DELETE"
    return "OTHER"


def _collect_tables(tree: exp.Expression) -> tuple[List[str], Dict[str, str]]:
    tables: List[str] = []
    aliases: Dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        name = table.name
        if not name:
            continue
        if name not in tables:
            tables.append(name)
        aliases[name] = name
        if table.alias:
            aliases[table.alias] = name
    return tables, aliases


def _resolve_table(column: exp.Column, aliases: Dict[str, str], tables: List[str]) -> Optional[str]:
    qualifier = column.table
    if qualifier:
        return aliases.get(qualifier, qualifier)
    if len(tables) == 1:
        return tables[0]
    return None


def _flatten_and(condition: exp.Expression) -> List[exp.Expression]:
    if isinstance(condition, exp.Paren):
        return _flatten_and(condition.this)
    if isinstance(condition, exp.And):
        return _flatten_and(condition.left) + _flatten_and(condition.right)
    return [condition]


def _is_bare_column(node: Optional[exp.Expression]) -> bool:
    return isinstance(node, exp.Column)


def _contains_column(node: Optional[exp.Expression]) -> bool:
    return node is not None and next(node.find_all(exp.Column), None) is not None


def _classify(conjunct: exp.Expression) -> Optional[str]:
    if isinstance(conjunct, (exp.EQ, exp.NullSafeEQ)):
        return EQUALITY
    if isinstance(conjunct, exp.In):
        return IN_LIST
    if isinstance(conjunct, exp.Is):
        return NULL_CHECK
    if isinstance(conjunct, (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)):
        return RANGE
    if isinstance(conjunct, (exp.Like, exp.ILike)):
        return LIKE
    if isinstance(conjunct, exp.NEQ):
        return INEQUALITY
    if isinstance(conjunct, exp.Not):
        return INEQUALITY
    return None


def _analyze_condition(
    condition: Optional[exp.Expression],
    aliases: Dict[str, str],
    tables: List[str],
    predicates: List[Dict[str, Any]],
    join_pairs: List[Dict[str, Any]],
    source: str,
) -> None:
    if condition is None:
        return
    for conjunct in _flatten_and(condition):
        kind = _classify(conjunct)
        left = conjunct.args.get("this")
        right = conjunct.args.get("expression")

        # column = column across different tables is a join predicate.
        if kind == EQUALITY and _is_bare_column(left) and _is_bare_column(right):
            left_table = _resolve_table(left, aliases, tables)
            right_table = _resolve_table(right, aliases, tables)
            if left_table != right_table or left_table is None:
                join_pairs.append({
                    "left_table": left_table,
                    "left_column": left.name,
                    "right_table": right_table,
                    "right_column": right.name,
                    "source": source,
                })
                continue

        if kind is None:
            # OR or other compound expressions: record columns as non-conjunctive.
            for column in conjunct.find_all(exp.Column):
                predicates.append({
                    "table": _resolve_table(column, aliases, tables),
                    "column": column.name,
                    "kind": DISJUNCTIVE if isinstance(conjunct, exp.Or) else FUNCTION,
                    "source": source,
                })
            continue

        if _is_bare_column(left):
            predicates.append({
                "table": _resolve_table(left, aliases, tables),
                "column": left.name,
                "kind": kind,
                "source": source,
            })
        elif _is_bare_column(right) and kind in {EQUALITY, RANGE}:
            predicates.append({
                "table": _resolve_table(right, aliases, tables),
                "column": right.name,
                "kind": kind,
                "source": source,
            })
        elif _contains_column(left) or _contains_column(right):
            # Column wrapped in a function or expression: not usable by a plain B-tree index.
            for column in conjunct.find_all(exp.Column):
                predicates.append({
                    "table": _resolve_table(column, aliases, tables),
                    "column": column.name,
                    "kind": FUNCTION,
                    "source": source,
                })


def _select_aliases(select: exp.Expression) -> set[str]:
    return {
        projection.alias
        for projection in getattr(select, "expressions", []) or []
        if isinstance(projection, exp.Alias) and projection.alias
    }


def _analyze_select(
    select: exp.Expression,
    aliases: Dict[str, str],
    tables: List[str],
    result: Dict[str, Any],
) -> None:
    _analyze_condition(
        select.args.get("where").this if select.args.get("where") else None,
        aliases, tables, result["predicates"], result["join_pairs"], "where",
    )
    for join in select.args.get("joins") or []:
        _analyze_condition(join.args.get("on"), aliases, tables, result["predicates"], result["join_pairs"], "join")
        using = join.args.get("using") or []
        for identifier in using:
            join_table = join.this.name if isinstance(join.this, exp.Table) else None
            result["join_pairs"].append({
                "left_table": None,
                "left_column": identifier.name,
                "right_table": join_table,
                "right_column": identifier.name,
                "source": "join",
            })

    select_aliases = _select_aliases(select)
    order = select.args.get("order")
    if order is not None:
        for ordered in order.expressions:
            node = ordered.this
            direction = "DESC" if ordered.args.get("desc") else "ASC"
            if isinstance(node, exp.Column) and not node.table and node.name in select_aliases:
                result["order_by"].append({"table": None, "column": node.name, "direction": direction, "indexable": False})
            elif isinstance(node, exp.Column):
                result["order_by"].append({
                    "table": _resolve_table(node, aliases, tables),
                    "column": node.name,
                    "direction": direction,
                    "indexable": True,
                })
            else:
                for column in node.find_all(exp.Column):
                    result["order_by"].append({
                        "table": _resolve_table(column, aliases, tables),
                        "column": column.name,
                        "direction": direction,
                        "indexable": False,
                    })

    group = select.args.get("group")
    if group is not None:
        for node in group.expressions:
            if isinstance(node, exp.Column):
                result["group_by"].append({
                    "table": _resolve_table(node, aliases, tables),
                    "column": node.name,
                    "indexable": node.name not in select_aliases,
                })
            else:
                for column in node.find_all(exp.Column):
                    result["group_by"].append({
                        "table": _resolve_table(column, aliases, tables),
                        "column": column.name,
                        "indexable": False,
                    })


def _projection(select: exp.Expression, aliases: Dict[str, str], tables: List[str]) -> Dict[str, Any]:
    star = False
    columns: List[Dict[str, Any]] = []
    for projection in select.expressions:
        if isinstance(projection, exp.Star) or (isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)):
            star = True
            continue
        for column in projection.find_all(exp.Column):
            entry = {"table": _resolve_table(column, aliases, tables), "column": column.name}
            if entry not in columns:
                columns.append(entry)
    return {"star": star, "columns": columns}


def _empty_result(sql: str) -> Dict[str, Any]:
    return {
        "normalized_sql": normalize_sql(sql),
        "query_type": "UNKNOWN",
        "tables": [],
        "where_columns": [],
        "join_columns": [],
        "order_columns": [],
        "group_columns": [],
        "predicates": [],
        "join_pairs": [],
        "order_by": [],
        "group_by": [],
        "projection": {"star": False, "columns": []},
        "has_limit": False,
        "parse_status": "error",
        "parse_error": None,
    }


def _finalize_legacy_fields(result: Dict[str, Any]) -> Dict[str, Any]:
    result["where_columns"] = _unique(p["column"] for p in result["predicates"] if p["source"] == "where")
    join_columns: List[str] = []
    for pair in result["join_pairs"]:
        for column in (pair["left_column"], pair["right_column"]):
            if column not in join_columns:
                join_columns.append(column)
                break
    result["join_columns"] = join_columns
    result["order_columns"] = _unique(item["column"] for item in result["order_by"])
    result["group_columns"] = _unique(item["column"] for item in result["group_by"])
    return result


def analyze_query(sql: str) -> Dict[str, Any]:
    """Analyze one SQL statement. Never raises for malformed SQL."""

    result = _empty_result(sql or "")
    if not sql or not sql.strip():
        result["parse_error"] = "Empty SQL statement"
        return result

    try:
        statements = [s for s in sqlglot.parse(normalize_placeholders(sql), read="mysql") if s is not None]
        if len(statements) != 1:
            raise ValueError(f"Expected exactly one SQL statement, found {len(statements)}")
        tree = statements[0]
    except (SqlglotError, ValueError) as exc:
        return _regex_fallback(sql, result, str(exc).splitlines()[0])

    result["query_type"] = _query_type(tree)
    tables, aliases = _collect_tables(tree)
    result["tables"] = tables

    if isinstance(tree, (exp.Update, exp.Delete)):
        where = tree.args.get("where")
        _analyze_condition(where.this if where else None, aliases, tables, result["predicates"], result["join_pairs"], "where")
    else:
        for select in tree.find_all(exp.Select):
            _analyze_select(select, aliases, tables, result)
        outer = tree if isinstance(tree, exp.Select) else next(tree.find_all(exp.Select), None)
        if outer is not None:
            result["projection"] = _projection(outer, aliases, tables)
            result["has_limit"] = outer.args.get("limit") is not None

    result["parse_status"] = "ok" if tables else "error"
    if not tables:
        result["parse_error"] = "No tables referenced"
    return _finalize_legacy_fields(result)


def _regex_fallback(sql: str, result: Dict[str, Any], error: str) -> Dict[str, Any]:
    normalized = " ".join(sql.strip().split())
    tables = _regex_tables(normalized)
    result["parse_error"] = error
    if not tables:
        return result
    single = tables[0] if len(tables) == 1 else None
    result["tables"] = tables
    result["query_type"] = normalized.split(" ", 1)[0].upper()
    result["predicates"] = [
        {"table": single, "column": column, "kind": "unknown", "source": "where"}
        for column in _regex_where_columns(normalized)
    ]
    result["order_by"] = [
        {"table": single, "column": column, "direction": "ASC", "indexable": True}
        for column in _regex_order_columns(normalized)
    ]
    result["parse_status"] = "fallback"
    return _finalize_legacy_fields(result)
