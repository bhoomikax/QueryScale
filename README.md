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
stages. **Milestone 1 (this branch) creates the foundation only**: networking, security
groups, ECR, the artifact bucket, the job queue and dead-letter queue, secret containers,
log groups, IAM roles and a cost budget. RDS, the ECS cluster and services, the load
balancer and alarms follow in later milestones, and the application adapters that use
SQS, S3 and a shared job store are separate work. The local Docker workflow above is
unchanged and remains the default; the application does not read any AWS setting yet.

### Layout

| Path | Purpose |
| --- | --- |
| `infra/bootstrap/` | One-time stack that creates the encrypted, versioned S3 bucket for Terraform state. Uses local state on purpose. |
| `infra/terraform/` | Main stack. Remote state in the bootstrap bucket with native S3 locking. |
| `infra/terraform/environments/dev.tfvars.example` | Copy to `dev.tfvars` (gitignored) and edit. |
| `infra/terraform/backend.hcl.example` | Copy to `backend.hcl` (gitignored) and fill in the bootstrap outputs. |

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

The image is the same one used locally. Tag it with the git commit, because the
repository rejects tag overwrites.

```powershell
cd infra\terraform
$region = "us-east-1"                      # same as region in dev.tfvars
$repo   = terraform output -raw ecr_repository_url
$tag    = git rev-parse --short HEAD
aws ecr get-login-password --region $region | docker login --username AWS --password-stdin $repo.Split("/")[0]
docker build -t "${repo}:${tag}" ..\..
docker push "${repo}:${tag}"
```

### Cost and teardown

The milestone 1 resources cost close to nothing while idle: SQS, S3, Secrets Manager
(about USD 0.40 per secret per month), CloudWatch log storage and ECR storage. The NAT
gateway is off by default (`enable_nat_gateway = false`); turning it on adds roughly
USD 33 per month. The budget sends email alerts at the configured thresholds.

`terraform destroy` is safe by default and will **refuse** to remove resources that still
hold data. For a disposable development environment set these in `dev.tfvars` first:

```hcl
artifact_bucket_force_destroy = true   # delete the artifact bucket with its objects
ecr_force_delete              = true   # delete the ECR repository with its images
secret_recovery_window_days   = 0      # delete secrets immediately instead of after 7 days
```

Then run `terraform destroy -var-file=environments/dev.tfvars`. Budget email
notifications are removed with the budget. Finally, if the environment is being retired
for good, empty and delete the state bucket by hand; Terraform will not do it.

### Security notes for this milestone

- Security groups form a chain: allowlisted CIDRs, then the ALB, then ECS tasks, then
  RDS. The RDS group accepts MySQL only from the ECS task group and has no egress.
- Task roles are split: the API may only send to the queue and read artifacts; the
  worker may only receive, delete and write artifacts; the bootstrap task has no AWS API
  access at all. Execution roles may read only the two application secrets.
- Buckets block public access, require TLS and encrypted uploads. Queues require TLS.
- The RDS master credential is not created here. The RDS milestone will have RDS manage
  it and grant read access only to the bootstrap execution role.
- `DB_TLS_VERIFY` stays `true` in the cloud. The Dockerfile does not yet ship the Amazon
  RDS CA bundle; that change belongs with the RDS milestone.

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
