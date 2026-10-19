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

<!-- The section below was brought over from the cloud branch on 2026-10-19. Commands that referred to the cloud branch's separate job system were updated to this branch's worker (backend/worker.py). Items marked as pending application work remain pending; see docs/cloud-integration-status.md. -->

## Deploy to AWS

> **Status (2026-10-19): designed, validated, and rehearsed locally; not deployed.** The
> Terraform below runs the API task with `JOB_EXECUTION_MODE=thread`, so one API task runs
> analysis, benchmark, and training jobs itself against RDS. The separate worker service
> defaults to 0 tasks because ECS tasks do not share the SQLite job store. SQS delivery,
> worker interruption recovery, and per-job S3 artifacts are therefore not exercised in
> AWS. See `docs/cloud-integration-status.md` for the full picture and costs.

Phase 3 moves the API and worker to ECS Fargate with RDS, SQS, S3, Secrets Manager
and CloudWatch. The infrastructure lives under `infra/` as Terraform and is applied in
stages: the foundation (networking, security groups, ECR, the artifact bucket, the job
queue and dead-letter queue, secrets, log groups, IAM roles, a cost budget), a private
RDS for MySQL instance with a one-off database bootstrap task, and the ECS Fargate API
and worker services behind an Application Load Balancer with CloudWatch alarms.

The application adapters that use SQS, S3 and a shared job store are separate work and
are not in the backend yet. Until they land, the deployed API serves the dashboard, the
query and recommendation endpoints and accepts benchmark jobs into its own task-local
SQLite store, which the worker task cannot see. The ECS stage is therefore verifiable
(health, dashboard, secrets, TLS to RDS, worker start-up) but end-to-end benchmarks in
AWS wait for those adapters. The local Docker workflow above is unchanged and remains
the default.

### Layout

| Path | Purpose |
| --- | --- |
| `infra/bootstrap/` | One-time stack that creates the encrypted, versioned S3 bucket for Terraform state. Uses local state on purpose. |
| `infra/terraform/` | Main stack. Remote state in the bootstrap bucket with native S3 locking. |
| `infra/terraform/environments/dev.tfvars.example` | Copy to `dev.tfvars` (gitignored) and edit. |
| `infra/terraform/backend.hcl.example` | Copy to `backend.hcl` (gitignored) and fill in the bootstrap outputs. |
| `infra/certs/rds-global-bundle.pem` | Amazon RDS certificate authorities, copied into the image at `/app/certs/` so `DB_TLS_VERIFY=true` works against RDS. |
| `scripts/aws_bootstrap.py` | Idempotent database bootstrap, run as the ECS bootstrap task (or locally). |
| `scripts/aws_db_validate.py` | Post-bootstrap checks: databases, tables, row counts, leftover indexes, grants, TLS. |
| `scripts/aws/run_bootstrap_task.ps1` | Starts the bootstrap or validation task on ECS and streams its logs. |
| `scripts/aws/build_push_image.ps1` | Builds the image and pushes it to ECR tagged with the current Git commit. |
| `scripts/aws/check_health.ps1` | Calls the health, API and dashboard URLs through the ALB and reports ECS service state. |
| `scripts/aws/teardown.ps1` | Applies the teardown flags, destroys the main stack, leaves the state bucket. |
| `scripts/aws/post_destroy_check.ps1` | Read-only sweep for snapshots, ENIs, log groups, load balancers and other leftovers. |
| `scripts/aws/smoke_test.py` | Submits a job through the API, polls to completion, fetches the result and artifact, checks `cleanup_verified`. |
| `scripts/aws/interruption_test.ps1` | Stops the worker task mid-benchmark and checks redelivery, recovery and index absence. |
| `scripts/aws/dlq_test.ps1` | Drives a poison message into the dead-letter queue and checks the alarm. |
| `docs/aws-validation.md` | Compatibility matrix against the application code, acceptance checklist, and a full local rehearsal. |

State files, `backend.hcl`, `*.tfvars` and `.terraform/` are gitignored. Only the
`.example` inputs and the provider lock files are committed. Database passwords are
generated by Terraform and exist only in Secrets Manager and the encrypted remote state;
never put them in tfvars.

### Prerequisites

Install Terraform 1.10 or newer and the AWS CLI, then authenticate with an IAM identity
that can create the resources above:

```powershell
winget install --id Hashicorp.Terraform
winget install --id Amazon.AWSCLI
aws configure            # or: aws configure sso
aws sts get-caller-identity
```

Without installing Terraform, the read-only checks below also work through Docker. From
the repository root, replace `terraform` with:

```powershell
docker run --rm -v "${PWD}\infra:/infra" -w /infra/terraform hashicorp/terraform:1.10
```

### Validate without an AWS account

```powershell
cd infra\terraform
terraform fmt -check -recursive ..
terraform init -backend=false
terraform validate
cd ..\bootstrap
terraform init -backend=false
terraform validate
```

### 1. Create the state bucket (once per account and region)

```powershell
cd infra\bootstrap
terraform init
terraform plan -var="region=us-east-1"
terraform apply -var="region=us-east-1"
terraform output
```

Copy `state_bucket_name` and `state_bucket_region` into `infra/terraform/backend.hcl`.
The bucket has `prevent_destroy` set; delete it manually, last, if you ever retire the
environment.

### 2. Configure and plan the main stack

```powershell
cd infra\terraform
Copy-Item backend.hcl.example backend.hcl
Copy-Item environments\dev.tfvars.example environments\dev.tfvars
# Edit backend.hcl (bucket, key, region) and environments\dev.tfvars
# (alb_allowed_cidrs and budget_alert_emails at minimum).
terraform init -backend-config=backend.hcl
terraform plan -var-file=environments/dev.tfvars -out=dev.tfplan
```

Review the plan. `alb_allowed_cidrs` must list your own addresses; `0.0.0.0/0` is
rejected unless you also set `alb_allow_public_ingress = true`, because the API has no
authentication.

### 3. Apply and read the outputs

```powershell
terraform apply dev.tfplan
terraform output
terraform output -json container_environment
```

`container_environment` lists the non-secret variables the ECS stage will hand to the
containers (`JOB_QUEUE_URL`, `JOB_ARTIFACT_BUCKET`, `JOB_ARTIFACT_PREFIX`, `DB_*`). The
`secret_arns` output gives the Secrets Manager ARNs used for ECS secret injection; the
values themselves are never printed.

### 4. Build and push the image to ECR

The image is the same one used locally. It is tagged with the Git commit because the
repository rejects tag overwrites, so every deployed revision maps to exactly one commit.

```powershell
.\scripts\aws\build_push_image.ps1                       # linux/amd64, matches image_cpu_architecture = "X86_64"
.\scripts\aws\build_push_image.ps1 -Platform linux/arm64  # only with image_cpu_architecture = "ARM64"
```

The script refuses to build from a dirty working tree (`-AllowDirty` overrides) and
prints the tag to put into `image_tag`. Rebuilding the same commit needs `-Force`, which
appends a suffix instead of failing on the immutable tag.

### 5. Create the database and run the bootstrap task

Set `image_tag` in `environments/dev.tfvars` to the tag you just pushed, then apply
again. This creates the bootstrap task definition (it is skipped while `image_tag` is
empty). The RDS instance itself is created on the first apply and takes about ten
minutes.

```powershell
terraform plan -var-file=environments/dev.tfvars -out=dev.tfplan
terraform apply dev.tfplan
cd ..\..
.\scripts\aws\run_bootstrap_task.ps1
.\scripts\aws\run_bootstrap_task.ps1 -Mode validate
```

The bootstrap task is the only process that ever receives the RDS master credential.
It creates three databases and three users on the instance and then stops:

| Database | Variable | User | Grants |
| --- | --- | --- | --- |
| `queryscale` | `DB_NAME` | `queryscale_app` (`DB_USER`) | `SELECT` |
| `queryscale_test` | `DB_TEST_NAME` | `queryscale_benchmark` (`DB_TEST_USER`) | `SELECT, INDEX` |
| `queryscale_jobs` | `DB_JOBS_NAME` | `queryscale_jobs` (`DB_JOBS_USER`) | `SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX` |

Every user requires TLS, and the server enforces `require_secure_transport`. The
bootstrap is idempotent: it loads `database/schema.sql` only when the source database
has none of the five tables, seeds synthetic data only when every table is empty, and
clones the benchmark database only when it is missing or incomplete. It never drops the
source or jobs database. The explicit, destructive overrides are `--reinitialize-schema`,
`--reseed` and `--rebuild-test-db`, passed through `-ScriptArgs`:

```powershell
.\scripts\aws\run_bootstrap_task.ps1 -ScriptArgs "--rebuild-test-db"
.\scripts\aws\run_bootstrap_task.ps1 -ScriptArgs "--customers","1000","--transactions","5000"
```

Rebuild the benchmark database whenever a job fails with `candidate_index_exists`. The
validation task reports a leftover candidate index as a failure for exactly that reason.

Both scripts also run against the local MySQL container, using `DB_USER` as the
administrative account and `BOOTSTRAP_REQUIRE_TLS=false` because local MySQL has no
trusted certificate:

```powershell
$env:BOOTSTRAP_REQUIRE_TLS = "false"
$env:DB_APP_PASSWORD = "..."; $env:DB_BENCHMARK_PASSWORD = "..."; $env:DB_JOBS_PASSWORD = "..."
py -m scripts.aws_bootstrap --customers 100 --accounts 150 --transactions 500 --loans 50
py -m scripts.aws_db_validate --no-require-tls
```

**Interface dependency.** `DB_JOBS_NAME`, `DB_JOBS_USER` and `DB_JOBS_PASSWORD` are the
proposed names for the shared job store. They are created and granted now so the
MySQL-backed job store can use them when it lands; until then nothing reads them.

### 6. Deploy the API and worker services

The API and worker task definitions and services are created by the same apply that
sets `image_tag` (step 5). Each later deployment is: push a new image, change
`image_tag`, plan, apply. ECS performs a rolling update with the deployment circuit
breaker enabled, so a revision whose tasks never pass the health checks is rolled back
to the previous revision automatically.

```powershell
.\scripts\aws\build_push_image.ps1
# edit infra\terraform\environments\dev.tfvars: image_tag = "<printed tag>"
cd infra\terraform
terraform plan -var-file=environments/dev.tfvars -out=dev.tfplan
terraform apply dev.tfplan
cd ..\..
.\scripts\aws\check_health.ps1
```

What the services look like:

| Service | Command | Deployment | Credentials received |
| --- | --- | --- | --- |
| `api` | Dockerfile `CMD` (uvicorn on port 8000) | 100% minimum healthy, 200% maximum, ALB health check on `/health/ready`, container health check on `/health/live` | `DB_PASSWORD` of `queryscale_app` |
| `worker` | `python -m backend.worker` | 0% minimum healthy, 100% maximum, so the old task stops before the new one starts; 120 s stop timeout | `DB_PASSWORD` and `DB_TEST_PASSWORD` of `queryscale_benchmark` |

The worker settings exist because there is one benchmark database and the worker's lock
and crash recovery assume a single active process; `worker_desired_count` is capped at 1.
Neither service can read the RDS master secret. Passwords reach containers only as
Secrets Manager references resolved by the execution role at start-up; they appear in
no task-definition environment variable, output or source file.

**Health checks.** `check_health.ps1` runs these and prints the service rollout state:

```powershell
$url = terraform -chdir=infra/terraform output -raw api_url
Invoke-WebRequest "$url/health/live"          # process is up
Invoke-WebRequest "$url/health/ready"         # SELECT 1 against RDS succeeded
Invoke-WebRequest "$url/api/recommendations"  # CSV data baked into the image
Start-Process "$url/dashboard/"
```

**Rollback.** Terraform keeps the previous task definition revisions. To roll back
without a Terraform change, point the service at an earlier revision; to roll back with
Terraform, set `image_tag` back to the previous tag and apply.

```powershell
$cluster = terraform -chdir=infra/terraform output -raw ecs_cluster_name
$region  = (terraform -chdir=infra/terraform output -json container_environment | ConvertFrom-Json).AWS_REGION
aws ecs list-task-definitions --region $region --family-prefix queryscale-dev-api --sort DESC
aws ecs update-service --region $region --cluster $cluster --service queryscale-dev-api --task-definition queryscale-dev-api:<previous revision>
aws ecs update-service --region $region --cluster $cluster --service queryscale-dev-worker --task-definition queryscale-dev-worker:<previous revision>
aws ecs wait services-stable --region $region --cluster $cluster --services queryscale-dev-api queryscale-dev-worker
```

A manual `update-service` drifts from Terraform; the next apply moves the service back
to the revision in state, so follow up by fixing `image_tag`. To pause everything
without destroying it, set `api_desired_count` and `worker_desired_count` to 0 and apply.

**Troubleshooting.**

| Symptom | Where to look | Usual cause |
| --- | --- | --- |
| `terraform output api_url` works but requests time out | `alb_allowed_cidrs` in `dev.tfvars` | Your current public IP is not allowlisted. |
| Service events say `CannotPullContainerError` | `aws ecs describe-services` events | `image_tag` does not exist in ECR, wrong `image_cpu_architecture`, or tasks are in private subnets without NAT. |
| Tasks stop with `ResourceInitializationError: unable to pull secrets` | Same events | Execution role or the secret ARN; tasks in private subnets without NAT cannot reach Secrets Manager. |
| `/health/live` is 200 but `/health/ready` is 503 and targets flap | `aws logs tail /queryscale/dev/api --follow` | RDS unreachable or TLS failing: check `DB_HOST`, the `rds` security group, and that `DB_TLS_CA` exists in the image. |
| Deployment rolled back by the circuit breaker | Service events plus the API log group | The new image fails its health check; run it locally with the same environment first. |
| Worker log shows `candidate_index_exists` | Worker log group | A previous run left its index; run the bootstrap task with `-ScriptArgs "--rebuild-test-db"`. |
| Alarm `candidate-index-cleanup-failed` fires | Worker log group, then the validation task | Cleanup failed mid-benchmark; rebuild the benchmark database before the next job. |
| Jobs queued from the dashboard never leave `queued` | Expected for now | The shared job store and queue adapters are not in the backend yet (see the note at the top of this section). |
| Email alarms never arrive | SNS console | The subscription confirmation email was not accepted. |

Useful log commands:

```powershell
aws logs tail /queryscale/dev/api --region $region --follow
aws logs tail /queryscale/dev/worker --region $region --since 30m
aws ecs describe-services --region $region --cluster $cluster --services queryscale-dev-api --query "services[0].events[:5]"
```

### 7. Validate the deployment

The acceptance gate, the compatibility matrix against the application code, and the
worker-interruption and dead-letter-queue procedures are in
[docs/aws-validation.md](docs/aws-validation.md). The short version:

```powershell
.\scripts\aws\check_health.ps1
py scripts/aws/smoke_test.py --base-url (terraform -chdir=infra/terraform output -raw api_url)
.\scripts\aws\interruption_test.ps1
.\scripts\aws\dlq_test.ps1
```

The smoke test distinguishes `FAIL` from `PENDING-APP`: the latter marks checks that
depend on the cloud job adapters not yet in the backend, so a review cannot mistake a
missing feature for a passing test.

### 8. Operations: alarms, budget, pause versus destroy

**Alarms.** Every alarm notifies the SNS topic in `alarm_topic_arn`; add addresses to
`alarm_emails` and confirm the subscription email. Names are prefixed `queryscale-dev-`.

| Alarm | Source | Meaning |
| --- | --- | --- |
| `dlq-has-messages` | SQS dead-letter queue visible messages > 0 | A job message was received more than `BENCHMARK_JOB_MAX_ATTEMPTS + 1` times. |
| `job-queue-stalled` | SQS oldest message age > `alarm_queue_age_seconds` | Nothing is consuming the queue. |
| `api-5xx` | ALB target plus load-balancer 5xx > `alarm_alb_5xx_threshold` in 5 min | The API is erroring. |
| `api-no-healthy-targets` | ALB healthy host count < 1 for 3 min | No API task passes `/health/ready`. |
| `api-unhealthy-targets` | ALB unhealthy host count > 0 for 5 min | At least one API task is failing readiness. |
| `api-tasks-below-desired`, `worker-tasks-below-desired` | ECS `RunningTaskCount` < desired, only with `ecs_container_insights = true` | A service cannot keep its tasks running. |
| `worker-not-running` | No `AWS/ECS` CPU samples for 10 min, only without Container Insights | Free proxy for the worker count. |
| `rds-cpu-high` | RDS CPU > 80% for 15 min | Benchmark load or a runaway query. |
| `rds-storage-low` | RDS free storage < `alarm_rds_free_storage_bytes` | Raise `db_allocated_storage` or enable autoscaling. |
| `candidate-index-cleanup-failed` | Worker log line `Candidate-index cleanup failed for job <id>` from `backend/worker.py` | The benchmark database may hold a stray index; rebuild it before the next job. |

The cleanup filter matches the exact message already logged by the Phase 2 worker. If
that text changes in `backend/worker.py`, the pattern in `infra/terraform/alarms.tf`
must change with it.

**Budget.** `budget_monthly_limit_usd` with percent thresholds in
`budget_alert_thresholds_percent`, absolute dollar thresholds in
`budget_alert_absolute_usd`, and a forecast alert. A budget only sends email. It does not
cap spending, stop tasks, or delete anything; the only ways to stop charges are to pause
or destroy resources as described next.

**Pause versus destroy.**

| | Pause | Destroy |
| --- | --- | --- |
| How | `api_desired_count = 0`, `worker_desired_count = 0`, apply; then `aws rds stop-db-instance --db-instance-identifier queryscale-dev-mysql` | `.\scripts\aws\teardown.ps1 -Environment dev` |
| Keeps | Everything: data, images, secrets, state, URLs | Only the Terraform state bucket and, by default, a final RDS snapshot |
| Still bills | Load balancer (about USD 17/month), RDS storage, NAT gateway if enabled, S3, ECR, secrets, log storage | Final snapshot storage until you delete it; state bucket (cents) |
| Caveats | AWS restarts a stopped RDS instance after seven days | Irreversible except by restoring the snapshot |
| Resume | Set counts back, apply; `aws rds start-db-instance` | Full redeploy from step 1 |

Pausing is for gaps of days. For anything longer, destroy; the load balancer alone costs
more per month than the rest of an idle stack.

### Cost and teardown

The foundation resources cost close to nothing while idle: SQS, S3, Secrets Manager
(about USD 0.40 per secret per month), CloudWatch log storage and ECR storage. The
running services add roughly USD 9 per month for each 0.25 vCPU / 0.5 GiB Fargate task
and about USD 17 per month for the load balancer, which bills while it exists even with
zero tasks. The RDS instance is the next line item: `db.t4g.micro` with 20 GiB gp3 is
roughly USD 14 per month, or free for twelve months in free-tier-eligible accounts. The
NAT gateway is off by default (`enable_nat_gateway = false`); turning it on adds roughly
USD 33 per month. Log groups expire after `log_retention_days` (14) and benchmark
artifacts after `artifact_expiration_days` (30).

`terraform destroy` is safe by default and will **refuse** to remove resources that still
hold data: the artifact bucket keeps its objects, the ECR repository its images, secrets
get a seven-day recovery window, and RDS has deletion protection plus a final snapshot.
The teardown script flips those flags for one run, applies them (AWS enforces RDS
deletion protection at the API, so the apply must precede the destroy), scales the
services to zero, then destroys. It never touches the state bucket.

```powershell
.\scripts\aws\teardown.ps1 -Environment dev -PlanOnly     # show both plans, change nothing
.\scripts\aws\teardown.ps1 -Environment dev               # keeps a final RDS snapshot
.\scripts\aws\teardown.ps1 -Environment dev -SkipFinalSnapshot
.\scripts\aws\post_destroy_check.ps1 -Environment dev     # anything still billing?
```

The equivalent manual flags in `dev.tfvars`, if you prefer not to use the script:

```hcl
artifact_bucket_force_destroy = true   # delete the artifact bucket with its objects
ecr_force_delete              = true   # delete the ECR repository with its images
secret_recovery_window_days   = 0      # delete secrets immediately instead of after 7 days
db_deletion_protection        = false  # AWS refuses to delete the instance until this is applied
db_skip_final_snapshot        = true   # otherwise a final snapshot is kept (and billed)
alb_deletion_protection       = false
```

Apply once, then `terraform destroy -var-file=environments/dev.tfvars`.

The post-destroy check sweeps the region for leftovers that commonly survive a destroy
and keep billing or clutter: RDS snapshots, network interfaces, NAT gateways and elastic
IPs, load balancers and target groups, log groups (including the RDS and Container
Insights groups AWS creates on its own), secrets scheduled for deletion, alarms, budgets,
and anything else carrying the `Project` and `Environment` tags. It exits non-zero when
something other than the state bucket remains. Unconfirmed SNS email subscriptions
cannot be deleted by Terraform and expire by themselves after three days.

Delete the state bucket last and by hand, only when the environment is retired for good;
`infra/bootstrap` sets `prevent_destroy` so `terraform destroy` refuses on purpose.

### Security notes for this milestone

- Security groups form a chain: allowlisted CIDRs, then the ALB, then ECS tasks, then
  RDS. The RDS group accepts MySQL only from the ECS task group and has no egress.
- Task roles are split: the API may only send to the queue and read artifacts; the
  worker may only receive, delete and write artifacts; the bootstrap task has no AWS API
  access at all. Execution roles may read only the two application secrets.
- Buckets block public access, require TLS and encrypted uploads. Queues require TLS.
- RDS manages the master credential in Secrets Manager. Only the bootstrap execution
  role may read it; the API and worker roles cannot, and their task definitions never
  reference it.
- RDS is in the private subnets, not publicly accessible, encrypted at rest, and its
  parameter group sets `require_secure_transport`. `DB_TLS_VERIFY` stays `true` and
  `DB_TLS_CA` points at the bundled `/app/certs/rds-global-bundle.pem`.
- Deletion protection and a final snapshot are on by default for the instance.
- Each task kind has its own execution role that can read only the secrets its task
  definition references and write only its own log group. The API and worker task roles
  reach only the job queue and the artifact prefix. Passwords never appear in environment
  variables, outputs or source.
- Without a certificate the ALB listener is plain HTTP, so the allowlist in
  `alb_allowed_cidrs` is the only access control. Set `acm_certificate_arn` once you
  have a domain; port 80 then redirects to HTTPS.
- A `trivy config` scan of `infra/` reports five findings that are accepted on purpose
  and explained in comments beside the resources: the internet-facing ALB and its HTTP
  listener (above), the tasks' outbound HTTPS to any address (no NAT or endpoints),
  SSE-S3 instead of a customer-managed key on the artifact bucket, and the unencrypted
  alarm topic (CloudWatch alarms cannot publish to a topic using the AWS-managed key).

