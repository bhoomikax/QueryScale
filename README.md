# QueryScale (DBOpt-AI)

QueryScale is an index recommendation and performance optimization platform for banking SQL workloads on MySQL. It analyzes a captured workload, proposes index candidates, ranks them with an explainable rule engine and an optional scikit-learn model, validates candidates on an isolated test database, and shows measured before/after results in a dashboard.

## Architecture

```text
SQL workload (scripts/workload.py) ──> data/query_logs.csv
        │
        ▼
Query analysis (sqlglot)             backend/services/query_analyzer.py
Query features                       backend/services/feature_extractor.py   ──> data/query_features.csv
Schema metadata (read-only)          backend/services/schema_metadata.py     ──> data/schema_metadata.json
        │
        ▼
Candidate generation                 backend/services/candidate_generator.py ──> data/candidate_features.csv
Rule-based scoring                   backend/services/rule_scorer.py         ──> data/index_candidates.csv
ML ranking (optional, validated)     backend/services/ml/                    ──> models/
Recommendations + explanations       backend/services/recommender.py         ──> data/recommendations.csv
        │
        ▼
Isolated test DB benchmark           backend/services/benchmark.py           ──> data/benchmark_results.csv
                                                                              ──> data/benchmark_history.jsonl
        │
        ▼
FastAPI (backend/app.py) + background jobs (backend/services/jobs.py) ──> dashboard/
```

All stages are plain Python modules in one FastAPI application. There are no extra services. Background work runs in-process with job state persisted in SQLite (`runtime/queryscale_jobs.sqlite3`).

### Safety rules

- Indexes are created only in `DB_TEST_NAME`. If the two names are equal, compared case-insensitively as MySQL on Windows does, the configuration is rejected before any connection opens.
- Analysis, metadata collection, and benchmarks only read `DB_NAME`. Workload write statements run inside a transaction that is always rolled back.
- The full pipeline is destructive for the project databases. Seeding always empties and refills the five tables in `DB_NAME`. `--initialize-schema` drops and recreates them. Cloning drops and recreates `DB_TEST_NAME`. Run it only against disposable databases.
- Each benchmark resets the test database to the primary's index set, creates one candidate, measures, and drops it again. Cleanup status is recorded.

## What works where

| Capability | Works offline | Needs MySQL | Needs AWS |
| --- | --- | --- | --- |
| Analysis of an existing query log, candidates, rule ranking | Yes | Better with it (row counts, cardinality) | No |
| API, dashboard, job status | Yes | No | No |
| Model training on synthetic demo data | Yes | No | No |
| Workload capture, rows examined | No | Yes | No |
| Benchmarks, measured labels, model training on real labels | No | Yes | No |
| S3 artifact mirroring | No | No | Yes (`ARTIFACT_STORE=s3`) |

## Installation

Python 3.11 or newer.

```powershell
py -m venv .venv
.venv\Scripts\activate
py -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set `DB_USER` and `DB_PASSWORD`. Credentials are read only from the environment and are never logged or returned by the API.

### Environment variables

| Variable | Purpose |
| --- | --- |
| `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_CHARSET` | MySQL connection |
| `DB_NAME` | Primary database, read-only for QueryScale |
| `DB_TEST_NAME` | Isolated benchmark database, must differ from `DB_NAME` |
| `QUERY_LOG_PATH`, `QUERY_FEATURE_PATH`, `FEATURE_PATH`, `CANDIDATE_PATH`, `RECOMMENDATION_PATH` | Analysis artifacts |
| `BENCHMARK_RESULT_PATH`, `BENCHMARK_HISTORY_PATH` | Latest per-query benchmark rows and the full run history |
| `SCHEMA_METADATA_PATH`, `METADATA_SOURCE` | Metadata snapshot and source: `auto`, `mysql`, `snapshot`, `schema` |
| `METADATA_EXACT_STATS_MAX_ROWS` | Tables up to this estimated size get exact `COUNT(*)` and `COUNT(DISTINCT)` |
| `MAX_INDEX_CANDIDATES`, `MAX_CANDIDATES_PER_TABLE`, `MAX_INDEX_COLUMNS` | Bounds on candidate generation |
| `RULE_WEIGHTS_PATH` | Optional JSON file overriding rule weights and priority thresholds |
| `MODEL_DIR`, `ML_MIN_TRAINING_SAMPLES`, `ML_MIN_TRAINING_GROUPS`, `ML_ALLOW_SYNTHETIC_MODEL` | Model storage and training gates |
| `BENCHMARK_QUERY_TIMEOUT_MS`, `BENCHMARK_WRITE_PROBE_SAMPLES`, `BENCHMARK_SEED` | Benchmark behavior |
| `JOB_DATABASE_PATH` | Job status store |
| `JOB_EXECUTION_MODE`, `WORKER_POLL_SECONDS` | `thread` runs jobs in the API process; `queue` needs `python -m backend.worker` |
| `MYSQL_HOST_PORT`, `API_HOST_PORT` | Docker host ports, defaults 3307 and 8000 |
| `ARTIFACT_STORE`, `S3_ARTIFACT_BUCKET`, `S3_ARTIFACT_PREFIX` | Optional S3 mirroring of model artifacts |
| `LOG_LEVEL`, `LOG_FORMAT` | `LOG_FORMAT=json` emits one JSON object per line |

## Running with Docker

`compose.yaml` runs three services on one Docker network named `queryscale`:

| Service | Image | Host port | Purpose |
| --- | --- | --- | --- |
| `queryscale-mysql` | `mysql:8.4` | `127.0.0.1:3307` to container 3306 | Database, persistent volume `queryscale_queryscale-mysql-data` |
| `queryscale-api` | `queryscale-app:local` | `127.0.0.1:8000` | FastAPI and the dashboard |
| `queryscale-worker` | `queryscale-app:local` | none | Single background worker for analysis, benchmark, and training jobs |

Containers reach MySQL as `queryscale-mysql:3306`. Host port 3307 lets the Windows `MySQL80` service keep 3306. Both ports are bound to localhost only.

The API runs with `JOB_EXECUTION_MODE=queue`: it records jobs, and the worker claims and runs them one at a time from the shared job store in the `queryscale_queryscale-runtime` volume. `data/` and `models/` are bind-mounted from the repository, so the containers and direct Python runs share the same artifacts. Run exactly one worker; a second worker exits because it cannot take the worker lock.

Containers never initialize, seed, clone, or reset databases on startup.

### First start

1. Copy `.env.example` to `.env` and set `DB_PASSWORD`. Compose refuses to start without it.
2. Start the stack:

   ```powershell
   docker compose up -d --build
   docker compose ps
   ```

3. Open <http://localhost:8000/dashboard/>. Readiness, including database reachability and worker status, is at <http://localhost:8000/health/ready?check_db=true>.

`DB_PASSWORD` becomes the MySQL root password only when the `queryscale_queryscale-mysql-data` volume is first created. Editing `.env` later does not change the stored password. Change it inside MySQL with `ALTER USER`, or, only if the volume holds nothing you need, remove it with `docker compose down -v`, which deletes all data in it.

### Running the pipeline in Docker

The pipeline is destructive for the project databases (see Safety rules). Run it on demand in a one-off container on the same network:

```powershell
docker compose run --rm queryscale-worker python scripts/run_local_pipeline.py --initialize-schema --iterations 200 --seed 42 --collect-labels 12
docker compose run --rm queryscale-worker python scripts/train_model.py
```

### Direct Python on Windows

Python run on the host reads `.env` directly:

- Against the Docker MySQL: `DB_HOST=127.0.0.1`, `DB_PORT=3307`, `DB_USER=root`, and the same `DB_PASSWORD`.
- Against the Windows `MySQL80` service: `DB_HOST=127.0.0.1`, `DB_PORT=3306`, and that server's own credentials.

Direct runs use `JOB_EXECUTION_MODE=thread`, so the API executes jobs itself and no worker is needed. Do not run host benchmarks while a container benchmark is running; the two do not share a benchmark lock.

## Database setup without Docker

Use any MySQL 8 server and point `.env` at it. The pipeline creates the tables in `DB_NAME` from `database/schema.sql`, replacing that file's hard-coded `queryscale` name with `DB_NAME`.

### Permissions

The Docker setup uses `root` inside the isolated container. For any shared server, use a dedicated user scoped to the two project databases. No global privileges are needed:

```sql
CREATE USER 'queryscale_app'@'%' IDENTIFIED BY '<strong password>';
-- Primary: schema creation, seeding, and rolled-back workload writes
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, REFERENCES, INDEX ON queryscale.* TO 'queryscale_app'@'%';
-- Test database: DROP/CREATE DATABASE when cloning, CREATE/DROP INDEX, ANALYZE TABLE, rolled-back write probes
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, INDEX, ALTER ON queryscale_test.* TO 'queryscale_app'@'%';
-- Optional measurements; without them rows examined and measured index size are reported as unavailable
GRANT SELECT ON performance_schema.events_statements_history TO 'queryscale_app'@'%';
GRANT SELECT ON mysql.innodb_index_stats TO 'queryscale_app'@'%';
```

Database-level `CREATE` and `DROP` on `queryscale_test.*` are enough for the clone step to drop and recreate that one database.

## Running the complete pipeline

`--initialize-schema` drops and recreates the source schema. Use it only on a disposable development database.

```powershell
py scripts/run_local_pipeline.py --initialize-schema --iterations 200 --seed 42 --serve
```

Stages: initialize schema, seed data, run the workload (writes rolled back), collect schema statistics read-only, analyze and rank, clone the test database, benchmark the top recommendation, and start the API. Add `--collect-labels 10 --train` to benchmark several candidates and train the model in the same run.

Open <http://127.0.0.1:8000/dashboard/>.

## Running stages individually

```powershell
py scripts/seed.py
py scripts/workload.py --iterations 200 --seed 42
py -m backend.services.cli --metadata-source auto
py -m backend.services.test_database
py -m backend.services.benchmark --candidate-id IDX_TRANSACTIONS_ACCOUNT_ID_TXN_DATE --table Transactions --columns account_id,txn_date --iterations 200 --repeats 2
py scripts/collect_benchmark_labels.py --clone --limit 10 --iterations 200 --repeats 2
py scripts/train_model.py
py -m backend.app
```

The CLI defaults to `--metadata-source snapshot_or_schema`, which never connects to MySQL. Use `auto` or `mysql` for live statistics.

## Query features

Per query template (`data/query_features.csv`, `GET /api/queries/summary`):

- Normalized SQL, query type, tables, WHERE, JOIN, ORDER BY, and GROUP BY columns, and each predicate's kind: equality, IN, IS NULL, range, LIKE, function-wrapped, or disjunctive.
- Execution count, workload share, time share, mean, median, minimum, and maximum latency.
- P95 latency only with at least 20 observations.
- Rows returned and rows examined when captured.
- First and last observation and the observation window.

Per table: reads, writes, and write ratio in the captured workload. Unavailable metrics are empty in CSV, `null` in JSON, and NaN in pandas. They are never replaced with zero.

Malformed SQL does not stop the pipeline. A regex fallback recovers what it can and the query is marked `fallback` or `error`.

## Candidate generation

Candidates follow MySQL/InnoDB B-tree behavior:

- Equality columns first, ordered by known cardinality and then by name.
- Then one range column, or ORDER BY or GROUP BY columns, which can avoid a filesort.
- Join lookup columns, and covering indexes when the query reads few columns.
- Secondary indexes implicitly end with the primary key, so `(account_id)` already covers `(account_id, txn_id)`.

Every column is validated against schema metadata. Candidates covered by an existing index are kept as evidence but never recommended. Candidates are deduplicated, evidence is merged across queries, and output is bounded by the configured limits. Each candidate records supporting query IDs, observation counts, read/write evidence, drawbacks, a heuristic storage estimate when the row count is known, and an explanation.

## Rule-based baseline

`backend/services/rule_scorer.py` computes a deterministic heuristic score from 0 to 100. The inputs are workload time share, frequency share, predicate strength, table size, and leading-column selectivity. Write ratio and storage add penalties, and overlap with an existing index prefix adds a discount. Unknown inputs use a neutral value and are listed as unavailable. Default priority thresholds are HIGH at 50 or above and MEDIUM at 25 or above. Recommendations include HIGH and MEDIUM candidates. A candidate that is a left prefix of a higher-ranked recommendation on the same table is dropped as superseded.

The score is a heuristic priority. It is not a measured speedup or a probability, and the API labels it `heuristic_rule_score`. Rule-based ranking is always available and is the fallback whenever ML is unavailable or unsuitable.

## ML pipeline

Code is in `backend/services/ml/`.

**Features** (`features.py`): 21 numeric features derived from pre-index candidate evidence, such as column count, equality prefix length, ORDER BY coverage, time share, log latency, usage ratios, log table rows, selectivity, write ratio, existing-prefix ratio, and log estimated size. Missing values are median-imputed with indicator columns. Measured index size is excluded because it only exists after a benchmark.

**Label** (`dataset.py`): `workload_benefit`. For one benchmark run of candidate `c`, the same seeded replay runs before and after creating `c`:

```text
workload_benefit = Σ_{q ∈ S} n_q · (median_before_q − median_after_q)  /  Σ_{q ∈ all} n_q · median_before_q
```

`n_q` is the number of executions of template `q`. Medians are taken over all timed replays, excluding warm-up. `S` is the set of templates whose post-index EXPLAIN uses `c`, or the candidate's supporting queries when EXPLAIN is unavailable. Storage and write overheads are recorded separately and are not part of the label.

**Comparability**: only completed runs with the same benchmark protocol version and workload signature are combined. `--strict-dataset` also requires an identical dataset signature, based on exact row counts.

**Training** (`trainer.py`):

- Training is refused below `ML_MIN_TRAINING_SAMPLES` labeled runs or `ML_MIN_TRAINING_GROUPS` distinct candidates. The report then explains why.
- The split is grouped by candidate, so repeated benchmarks of one index never appear in both train and test.
- Ridge, decision tree, random forest, and gradient boosting are compared by grouped cross-validated MAE, with a mean predictor as the baseline.
- Held-out MAE and RMSE are reported for every model.
- NDCG@k, Precision@k, and Spearman compare rule and ML rankings within each dataset group.
- A model is marked suitable only when its held-out MAE beats the baseline.

**Persistence**: `models/index_ranker.joblib` holds the pipeline. `models/index_ranker.json` holds the version, timestamp, feature names, target definition, metrics, provenance, scikit-learn version, and SHA-256 checksum. `models/evaluation.json` holds the full report.

**Prediction** (`predictor.py`): the model is used only when the checksum matches, the feature schema matches, the model is suitable, and labels were measured. Predictions are labelled as predictions. No confidence interval is reported.

**Synthetic demo**: `py scripts/train_model.py --synthetic-demo` trains on formula-generated labels into `models/demo/`. It verifies the code path only. It is never used for ranking, and its metrics say nothing about real recommendation quality.

### Reproducing the measured ML experiment

All commands run on the host against the Docker MySQL (`.env` with `DB_PORT=3307`). Steps 1 and 2 are destructive for `DB_NAME` and `DB_TEST_NAME` only.

```powershell
# 1. Schema, seed (50 branches, 10k customers, 15k accounts, 100k transactions, 5k loans),
#    200-iteration workload, live metadata, candidates, test-database clone, one benchmark
py scripts/run_local_pipeline.py --initialize-schema --iterations 200 --seed 42

# 2. Measured labels: every scored candidate, same workload signature each pass.
#    Repeat the command to add passes; repeated runs of one candidate share a group.
py scripts/collect_benchmark_labels.py --limit 12 --iterations 200 --repeats 2
py scripts/collect_benchmark_labels.py --limit 12 --iterations 200 --repeats 2

# 3. Train from measured history only, then inspect
py scripts/train_model.py
Get-Content models\evaluation.json
Get-Content models\index_ranker.json
Invoke-RestMethod http://localhost:8000/api/ml/model | ConvertTo-Json -Depth 6
```

```powershell
# 4. Diagnostic: leave-one-candidate-out evaluation over the same measured records (trains nothing)
py scripts/evaluate_leave_one_out.py
Get-Content models\evaluation_logo.json

# 5. Tests (no database needed)
py -m unittest discover -s tests -p "test_*.py"
```

Only runs whose `workload.signature` matches the most common signature are combined (same templates, iterations, seed, repeats, protocol). Runs with a different `repeats` value, such as the single `repeats=1` run from step 1, stay in the history but are excluded from training and listed under `records_excluded`. Training refuses to run below `ML_MIN_TRAINING_SAMPLES` (20) rows or `ML_MIN_TRAINING_GROUPS` (5) candidates. Previous `models/evaluation.json` and `models/index_ranker.*` files are moved to `models/archive/` before a new model is written.

Pass `--seed` to `scripts/seed.py` or `scripts/run_local_pipeline.py` to make the generated rows reproducible; the 2026-10-18 data was generated before this option existed, so a re-seed reproduces its row counts but not its exact values. The experiment record for this repository's own run is in `docs/ml-experiment-2026-10-18.md`, and the generated files it was read from are frozen under `docs/evidence/2026-10-18/`.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /health`, `GET /health/ready[?check_db=true]` | Liveness and readiness |
| `GET /api/queries`, `GET /api/queries/slow`, `GET /api/queries/summary` | Query log rows, slow queries, per-query features |
| `POST /api/analysis`, `GET /api/analysis/status` | Start analysis as a 202 job, latest analysis status |
| `GET /api/jobs`, `GET /api/jobs/{job_id}` | Job status: PENDING, RUNNING, COMPLETED, FAILED |
| `GET /api/candidates[?include_covered=true]` | Scored candidates with evidence |
| `GET /api/recommendations`, `GET /api/recommendations/{id}` | Ranked recommendations, explanation, supporting query evidence |
| `POST /api/benchmark/{id}[?background=true&iterations=&repeats=]` | Benchmark on the test DB, synchronous by default or as a 202 job |
| `GET /api/benchmark/results` | Per-query rows of the latest successful run |
| `GET /api/benchmark/history`, `GET /api/benchmark/history/{run_id}` | All runs, including failed ones, and full run detail with EXPLAIN plans |
| `GET /api/ml/model`, `GET /api/ml/evaluation`, `POST /api/ml/train` | Model status, evaluation reports, training job |

Only one job of each kind runs at a time. Repeating an identical request returns the active job. A conflicting request returns 409. Benchmark failures return structured errors with codes such as `test_database_missing`, `authentication_failed`, `permission_denied`, `query_timeout`, `connection_error`, and `benchmark_in_progress`.

## Dashboard

The existing dashboard at `/dashboard/` keeps its layout. Additions:

- A "Run analysis" button and analysis status.
- Recommendation cards with the rule score labelled heuristic, the ML prediction labelled prediction, supporting queries, the storage estimate, write ratio, and an expandable explanation.
- A per-card benchmark button with job progress.
- Measured workload benefit, index size, and write overhead in the performance panel.
- Rows examined in the before/after chart.
- An ML evaluation panel.

Unavailable values show `--` or "unavailable".

## Tests

```powershell
py -m unittest discover -s tests -p "test_*.py"
```

Tests do not need MySQL and never connect to the primary database. Database steps are mocked or replaced by temporary files.

## AWS readiness

The application runs unchanged locally and is configured for AWS through environment variables:

- **ECS Fargate**: run `uvicorn backend.app:app --host 0.0.0.0 --port 8000`. Use `/health` for liveness and `/health/ready` for readiness.
- **RDS MySQL**: point `DB_HOST` and `DB_NAME` at the primary and `DB_TEST_NAME` at a separate benchmark database. Inject `DB_USER` and `DB_PASSWORD` from Secrets Manager as ECS task secrets.
- **S3**: set `ARTIFACT_STORE=s3`, `S3_ARTIFACT_BUCKET`, and `S3_ARTIFACT_PREFIX`, and install `boto3`. Trained models are uploaded to versioned keys with a `latest.json` pointer, using the task role.
- **CloudWatch**: set `LOG_FORMAT=json`. Logs go to stdout with request IDs and durations.
- **SQS**: not wired on this branch. Jobs run in-process with idempotent submission and persisted status, which suits a single API task. For several tasks, move execution to a queue worker such as the one on the `cloud` branch and keep the job record as shared state.

**Deployment status: not deployed.** No AWS resources have been created from any branch. The Terraform, ECS, RDS, SQS, and S3 definitions live on the `cloud` branch and were rehearsed only against local Docker; see `docs/cloud-integration-status.md` for what integrating them with this branch requires.

## Known limitations

- Benchmarks run on one MySQL instance without concurrent load. Warm-ups reduce but do not remove cache effects, so differences of a few percent can be noise.
- The model needs measured labels from many benchmark runs. Until enough exist, ranking is rule-based. Metrics from small held-out sets are preliminary.
- Storage estimates before benchmarking are heuristics. Measured size depends on `mysql.innodb_index_stats` access.
- Write overhead is measured with single-row operations in a rolled-back transaction, so commit cost is excluded.
- InnoDB row-count estimates are used for tables larger than `METADATA_EXACT_STATS_MAX_ROWS`.
- Candidate generation covers SELECT, UPDATE, and DELETE predicates, joins, ordering, grouping, and simple covering indexes. It does not generate prefix, functional, or full-text indexes.
