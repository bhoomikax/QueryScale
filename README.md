# QueryScale

QueryScale is an intelligent database performance optimization system for banking workloads.

## Local requirements

- Python 3.11 or newer
- MySQL 8.x, MariaDB, or Docker Desktop
- PowerShell on Windows, or an equivalent shell

Install Python dependencies from the repository root:

```powershell
py -m pip install -r requirements.txt
Copy-Item .env.example .env
```

The example file contains local-development values only. `DB_TLS_VERIFY` is explicitly
disabled there to work with the self-signed TLS setup commonly used by local MySQL
containers. `backend.config.Settings` enables certificate and hostname verification by
default when that option is omitted. Keep verification enabled for deployed databases;
set `DB_TLS_CA` to the trusted CA bundle path when the database requires a specific CA.

Connection and read/write timeouts are configurable with `DB_CONNECTION_TIMEOUT`,
`DB_READ_TIMEOUT`, and `DB_WRITE_TIMEOUT`. The API bind address and port use `API_HOST`
and `API_PORT` (defaults `0.0.0.0` and `8000`).

## Run the API in Docker

Build the image from the repository root:

```powershell
docker build -t queryscale:local .
```

For a MySQL server running on the host, copy `.env.example` to `.env`, set its local
database name and credentials, and start the API container:

```powershell
Copy-Item .env.example .env
docker run --rm --name queryscale-api --env-file .env `
    -e DB_HOST=host.docker.internal -p 8000:8000 queryscale:local
```

For MySQL also running in Docker, put both containers on the same user-defined Docker
network and set `DB_HOST` to the MySQL container name instead. The dashboard is at
<http://127.0.0.1:8000/dashboard/>. Check process liveness at `/health/live` and database
readiness at `/health/ready`; the existing `/health` response remains available.

The image includes the repository's current `data/` CSV artifacts as a starting snapshot.
The benchmark endpoint can write `BENCHMARK_RESULT_PATH` inside the container, but that
output is ephemeral when the container is removed. Mount a writable volume at the result
path if you need to keep local Docker benchmark output. This phase does not make the CSV
persistence distributed or durable.

## Asynchronous benchmark jobs (Phase 2)

The existing `POST /api/benchmark/{recommendation_id}` now validates the selected
recommendation and returns `202 Accepted` with a job ID and `queued` status. The local
SQLite job store records state transitions and structured results. The local queue is
SQLite-backed, so API and worker processes must use the same `JOB_DATABASE_PATH`. Result
CSV artifacts are stored under `JOB_ARTIFACT_DIRECTORY` and their local reference is
included in job status. Keep the `runtime/` directory on persistent shared storage if
state and artifacts must survive container removal.

### Start locally

Use a disposable local MySQL database containing synthetic QueryScale data. Create or
refresh `DB_TEST_NAME` once before starting jobs:

```powershell
py -m backend.services.test_database
```

**This command drops and recreates only `DB_TEST_NAME` before cloning the configured
source schema/data. Point `DB_NAME` and `DB_TEST_NAME` only at disposable local databases;
do not point either at production data.** The API and worker do not invoke this command.
The worker only creates and removes a validated candidate index in the explicitly
configured `DB_TEST_NAME` and rejects a test database equal to `DB_NAME`.

Use `DB_USER`/`DB_PASSWORD` for one-time source/test database setup and ordinary API
database checks. Configure `DB_TEST_USER`/`DB_TEST_PASSWORD` for benchmark connections;
grant that account only the read and index-management permissions needed on
`DB_TEST_NAME`. If the test credentials are omitted, they fall back to `DB_USER` for
backward-compatible local setups, which is broader than the recommended split.

Start the API and worker in separate terminals from the project root:

```powershell
py -m backend.app
```

```powershell
py -m backend.jobs.worker
```

The first process to use the job store creates the SQLite database and tables. Both
processes must share the same `JOB_DATABASE_PATH` and `JOB_ARTIFACT_DIRECTORY`; defaults
are `runtime/queryscale_jobs.sqlite3` and `runtime/job_artifacts`. Set
`BENCHMARK_JOB_MAX_ITERATIONS`, `BENCHMARK_JOB_TIMEOUT_SECONDS`, and
`BENCHMARK_JOB_MAX_ATTEMPTS` to bound work and worker-crash retries. The defaults are
1000 iterations, 900 seconds per job, and three worker attempts.

Open `/dashboard/` and choose **Queue benchmark** on a recommendation. The dashboard
shows recent job IDs and states, polls at a modest interval while jobs are queued/running,
and stops after terminal states. It displays the result summary or a safe failure message.
The API also provides `GET /api/benchmark/jobs`, `GET /api/benchmark/jobs/{job_id}`, and
`GET /api/benchmark/jobs/{job_id}/result`. The existing
`GET /api/benchmark/results` continues to include legacy CSV rows and successful job
measurements.

The local worker takes a kernel-managed file lock beside the SQLite database, so multiple
worker processes sharing the same local filesystem cannot benchmark the same test DB at
the same time. A worker timeout exits that worker process; restart the worker to recover
an interrupted job. If the worker stopped after applying an index, recovery removes only
that job's deterministic candidate index from `DB_TEST_NAME` before retrying. Repeated
worker crashes fail the job after the configured attempt limit. This lock is for local
shared filesystems and is not an ECS or network-wide lock.

### Run API and worker as Docker containers

Build the image as described above, create a named volume, and mount that same volume at
`/app/runtime` in both containers:

```powershell
docker volume create queryscale-runtime
docker run --rm --name queryscale-api --env-file .env `
    -e DB_HOST=host.docker.internal -p 8000:8000 `
    --mount source=queryscale-runtime,target=/app/runtime queryscale:local
```

In another terminal:

```powershell
docker run --rm --name queryscale-worker --env-file .env `
    -e DB_HOST=host.docker.internal `
    --mount source=queryscale-runtime,target=/app/runtime `
    --entrypoint python queryscale:local -m backend.jobs.worker
```

If MySQL is another container, put all containers on one user-defined network and set
`DB_HOST` to the MySQL container name instead. The local SQLite store and file lock need
a filesystem with working SQLite WAL and OS file-lock semantics; a Docker named volume is
appropriate for this single-host local workflow. Phase 3 must replace these adapters with
shared cloud job storage/queue and a lock mechanism that coordinates across ECS tasks;
local filesystem artifact references must be replaced by S3 object references.

### CSV persistence work for the asynchronous cloud phase

These CSV paths are environment-configurable but still assume a local filesystem:

- `QUERY_LOG_PATH`: read by `backend/routes/queries.py`, produced by `scripts/workload.py`.
- `RECOMMENDATION_PATH`: read by `backend/routes/recommendations.py`, produced by the
  rule-based pipeline (`backend/services/recommender.py`).
- `BENCHMARK_RESULT_PATH`: read by `backend/routes/benchmark.py` and overwritten by
  `backend/services/benchmark.py`.
- Feature and candidate CSVs are intermediate pipeline files in
  `backend/services/feature_extractor.py`, `candidate_generator.py`, `recommender.py`,
  `backend/services/cli.py`, and `scripts/run_optimization.py`.

For asynchronous cloud processing, job state and dashboard-visible results must move to
shared durable storage; report exports can go to S3. Worker inputs and intermediate
artifacts must not rely on one task's local disk. The API will need to retrieve results
through that durable store, and the dashboard will need job submission/status behavior.
Those persistence and async changes are intentionally deferred from this container phase.

## MySQL setup

The application expects MySQL at `127.0.0.1:3306`. With Docker Desktop, start a local server:

```powershell
docker run --name queryscale-mysql `
    -e MYSQL_ROOT_PASSWORD=your_password `
    -e MYSQL_DATABASE=queryscale `
    -p 3306:3306 `
    -d mysql:8.4
```

Wait until the container log says `ready for connections`, then set the matching values in `.env`.
The complete pipeline can initialize the schema itself. The `--initialize-schema` flag resets the local source schema, so use it only for a disposable development database.

## Complete local demo

Run the full local pipeline from MySQL to the dashboard:

```powershell
py scripts/run_local_pipeline.py --initialize-schema --iterations 100 --serve
```

This performs the following stages:

1. Initializes `queryscale` from `database/schema.sql`.
2. Resets and seeds synthetic banking data.
3. Executes the real workload and records timings.
4. Extracts features and generates index recommendations.
5. Rebuilds the isolated `queryscale_test` database.
6. Applies the selected recommendation only to `queryscale_test`.
7. Warms up and runs before/after benchmarks, then writes measured results.
8. Starts the API and dashboard when `--serve` is provided.

For a smaller local run, keep enough workload iterations to sample recommendation candidates:

```powershell
py scripts/run_local_pipeline.py --initialize-schema --iterations 100 `
    --customers 100 --accounts 150 --transactions 500 --loans 50 --serve
```

Open the dashboard at <http://127.0.0.1:8000/dashboard/>.

## Individual workflow

Use these commands when running stages separately:

```powershell
py scripts/seed.py
py scripts/workload.py --iterations 100
py -m backend.services.test_database
py scripts/run_optimization.py --iterations 100
py -m backend.app
```

The analysis pipeline can also be run directly through the existing CLI:

```powershell
py -m backend.services.cli --log-path data/query_logs.csv
```

The workload and analysis stages produce these generated files:

- `data/query_logs.csv`
- `data/candidate_features.csv`
- `data/index_candidates.csv`
- `data/recommendations.csv`
- `data/benchmark_results.csv`

These files contain local measurements and should not be staged unless they are intentionally being shared as a fixture.

## API

Start the API with `py -m backend.app`. Available endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Application health check |
| `GET /api/queries` | Measured query log rows |
| `GET /api/queries/slow` | Queries above a latency threshold |
| `GET /api/recommendations` | Generated index recommendations |
| `GET /api/recommendations/{id}` | One recommendation by candidate ID |
| `POST /api/benchmark/{id}` | Queue a recommendation benchmark (202 Accepted) |
| `GET /api/benchmark/jobs` | List recent benchmark jobs |
| `GET /api/benchmark/jobs/{job_id}` | Get job status |
| `GET /api/benchmark/jobs/{job_id}/result` | Get a completed benchmark result |
| `GET /api/benchmark/results` | Persisted benchmark measurements |

The APIs read generated project data and never substitute hardcoded dashboard metrics.

## Dashboard

The dashboard is served by FastAPI at `/dashboard/`. It displays:

- Query, slow-query, recommendation, and priority counts
- Top recommendation cards
- Before/after benchmark comparisons weighted by measured query executions
- Recommendation score bars
- Query frequency and measured latency

It shows explicit empty or unavailable states until the corresponding local artifacts exist.

## Tests

Run the complete test suite without requiring MySQL:

```powershell
py -m unittest discover -s tests -p "test_*.py"
```

The live pipeline requires MySQL and should be run separately with the complete demo command above.

## Deploy to AWS (Phase 3)

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
| `worker` | `python -m backend.jobs.worker` | 0% minimum healthy, 100% maximum, so the old task stops before the new one starts; 120 s stop timeout | `DB_PASSWORD` and `DB_TEST_PASSWORD` of `queryscale_benchmark` |

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
| `candidate-index-cleanup-failed` | Worker log line `Candidate-index cleanup failed for job <id>` from `backend/jobs/worker.py` | The benchmark database may hold a stray index; rebuild it before the next job. |

The cleanup filter matches the exact message already logged by the Phase 2 worker. If
that text changes in `backend/jobs/worker.py`, the pattern in `infra/terraform/alarms.tf`
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

## Ownership and future work

### Person A: database and query intelligence

- MySQL schema and synthetic banking data
- Workload query definitions and query logs
- Query analysis and feature extraction
- Candidate generation and rule-based recommendations

### Person B: benchmark, API, and dashboard

- Isolated test database management
- Safe index application
- Before/after benchmark engine
- FastAPI routes and local dashboard
- End-to-end local integration and tests

### Later integration points

The generated feature and recommendation datasets are the input boundary for future ML experiments. The local FastAPI application and generated benchmark artifacts are the boundary for future cloud deployment. AWS and ML integration are intentionally out of scope for the current local milestone.

## Repository structure

```text
QueryScale/
├── backend/
│   ├── app.py
│   ├── config.py
│   ├── database.py
│   ├── routes/
│   └── services/
├── dashboard/
│   ├── dashboard.js
│   ├── index.html
│   └── style.css
├── database/
│   ├── README.md
│   ├── queries.sql
│   └── schema.sql
├── infra/
│   ├── bootstrap/
│   ├── certs/
│   └── terraform/
├── scripts/
│   ├── run_local_pipeline.py
│   ├── run_optimization.py
│   ├── seed.py
│   └── workload.py
├── tests/
├── .env.example
└── requirements.txt
```
