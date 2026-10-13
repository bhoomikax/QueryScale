# QueryScale AWS validation checklist

This checklist is the acceptance gate for a reviewed deployment. Each row names the
command that proves it and the state it must reach. "Pending application work" rows
depend on the cloud adapters (shared job store, SQS queue, S3 artifacts, the
`cleanup_verified` result field) that are not in the backend yet; they are listed so a
review cannot mistake their absence for a pass.

Scripts referenced here live in `scripts/aws/`. All of them read Terraform outputs from
`infra/terraform`, so run them from the repository root after `terraform apply`.

## Compatibility between Terraform and the application

Verified by reading the code on the `cloud` branch; the Terraform side was adjusted to
match the application wherever both existed.

| Area | Application (as implemented) | Infrastructure | Status |
| --- | --- | --- | --- |
| Job backend switch | No `JOB_BACKEND` setting; `backend/jobs/runtime.py` always builds the SQLite store, local queue and local artifact store | Task definitions pass only implemented variables; `api_extra_environment` / `worker_extra_environment` carry future ones; `container_environment` output lists proposed values | Pending application work: adapter selection |
| Local fallback | SQLite under `JOB_DATABASE_PATH` (default `runtime/…`, writable in the image) | Unchanged; local Docker commands in the README still work | Compatible |
| Queue | Not consumed or produced by the application | FIFO queue, one message group, visibility 1200 s > 900 s job timeout, `maxReceiveCount = BENCHMARK_JOB_MAX_ATTEMPTS + 1`, FIFO DLQ | Pending application work: body `{"job_id": "<uuid>"}`, `MessageGroupId=benchmark`, `MessageDeduplicationId=<job_id>` |
| Job metadata | SQLite rows carry `owner`, `attempts`, `phase`, `recovery_required` (`backend/jobs/store.py`) | `queryscale_jobs` database and `queryscale_jobs` user with `SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX`; secret wired through `jobs_db_consumers` | Pending application work: MySQL store with the same fields |
| Artifacts | `local-job-artifact:<job_id>/results.csv`; worker checks the file exists on local disk (`backend/jobs/worker.py`) | Bucket, prefix `jobs/`, worker role `s3:PutObject`, API role `s3:GetObject` for presigning, 30-day expiry | Pending application work: S3 adapter, `GET /api/benchmark/jobs/{id}/artifact` |
| Database names | `DB_NAME` and `DB_TEST_NAME` from `backend/config.py`; `DB_TEST_NAME != DB_NAME` enforced in three places | Bootstrap creates `queryscale`, `queryscale_test`, `queryscale_jobs`; refuses non-distinct names | Compatible |
| Database users | API uses `DB_USER` for `/health/ready` only; worker uses `DB_TEST_USER` for every benchmark connection; workload is `SELECT`-only (`scripts/workload.py`) | `queryscale_app`: `SELECT` on source; `queryscale_benchmark`: `SELECT, INDEX` on test DB; every user `REQUIRE SSL` | Compatible |
| TLS | `DB_TLS_VERIFY` defaults to true; `DB_TLS_CA` is a file path passed as `ssl_ca` | RDS enforces `require_secure_transport`; image ships `/app/certs/rds-global-bundle.pem`; tasks get `DB_TLS_VERIFY=true` and `DB_TLS_CA` | Compatible |
| API command and port | Dockerfile `CMD` runs uvicorn on `API_HOST:API_PORT`, `EXPOSE 8000` | Container port 8000, `API_HOST=0.0.0.0`, `API_PORT=8000` | Compatible |
| Worker command | `python -m backend.jobs.worker` (README, `backend/jobs/worker.py`) | Task definition `command` identical | Compatible |
| Health checks | `/health/live` (no dependencies), `/health/ready` (`SELECT 1`) | Container health check on `/health/live`; ALB target health on `/health/ready` | Compatible |
| Timeouts and retries | `BENCHMARK_JOB_TIMEOUT_SECONDS=900`, `BENCHMARK_JOB_MAX_ATTEMPTS=3`, timeout exits the process | Same values passed to both containers; ECS restarts the worker; SQS visibility and redrive derived from them | Compatible |
| IAM | Application makes no AWS API calls today | API role: `sqs:SendMessage`, `s3:GetObject`; worker role: `sqs:ReceiveMessage/DeleteMessage/ChangeMessageVisibility`, `s3:PutObject`; each execution role reads only its own secrets | Compatible; sufficient for the proposed adapters |
| Cleanup verification | Worker removes the candidate index in `finally` and logs `Candidate-index cleanup failed for job <id>` on failure; it does not record a `cleanup_verified` field | Log metric filter and alarm on that exact message; `scripts/aws_db_validate.py` checks for leftover indexes | Pending application work: `cleanup_verified` in the result |
| Crash recovery | `recover_interrupted` requeues every `running` row on worker start, assuming the file lock makes any such row orphaned | Single worker task, 0/100 percent deployments, 120 s stop timeout | Pending application work: per-job recovery on claim, since a MySQL store is shared across restarts and the file lock no longer applies |

## Procedures

| # | Gate | Command | Must observe |
| --- | --- | --- | --- |
| 1 | Image | `.\scripts\aws\build_push_image.ps1` | Tag equals the current commit; push succeeds; `image_tag` updated in `dev.tfvars` and applied |
| 2 | Bootstrap | `.\scripts\aws\run_bootstrap_task.ps1` | Exit 0; log ends with `Bootstrap complete` |
| 3 | Database validation | `.\scripts\aws\run_bootstrap_task.ps1 -Mode validate` | Exit 0; every check `PASS`, including `benchmark:no-leftover-indexes`, `grants:*`, `ssl-required:*`, `tls:require_secure_transport`, and the `probe:*` denials |
| 4 | Health readiness | `.\scripts\aws\check_health.ps1` | `/health/live` and `/health/ready` return 200; both services `rollout=COMPLETED` with `running=desired` |
| 5 | Job persistence, queue delivery, result storage, cleanup | `py scripts/aws/smoke_test.py --base-url (terraform -chdir=infra/terraform output -raw api_url)` | All checks `PASS`. Today: submission, listing and health pass; the job stays `queued` in AWS (reported as FAIL with the reason) until the shared job store is deployed |
| 6 | Worker interruption | `.\scripts\aws\interruption_test.ps1` | Job reaches `running`, task is stopped, job ends terminal with `attempts >= 2`, validation task shows no leftover index. Today: `PENDING-APP` at the claim step |
| 7 | Dead-letter queue | `.\scripts\aws\dlq_test.ps1` | Poison message lands in the DLQ after `maxReceiveCount` receives; `dlq-has-messages` alarm reaches `ALARM`; message deleted afterwards. Runs today, independent of the application |
| 8 | Alarms wired | `aws cloudwatch describe-alarms --alarm-name-prefix queryscale-dev --query "MetricAlarms[].[AlarmName,StateValue]"` | Every alarm present, none `INSUFFICIENT_DATA` after 15 minutes of traffic except the Container Insights ones when disabled |
| 9 | Teardown rehearsal | `.\scripts\aws\teardown.ps1 -Environment dev -PlanOnly` then, when retiring, without `-PlanOnly`, then `.\scripts\aws\post_destroy_check.ps1 -Environment dev` | Plans show only this stack; post-destroy check reports nothing but the state bucket |

## Local rehearsal

Everything except the queue and S3 paths can be rehearsed on one machine with Docker.
The same scripts run against local containers, with TLS verification off because local
MySQL has no trusted certificate:

```powershell
docker network create qs-local
docker run -d --name qs-local-mysql --network qs-local -e MYSQL_ROOT_PASSWORD=localroot -e MYSQL_DATABASE=queryscale mysql:8.4
docker build -t queryscale:local .
$env_common = @("-e","DB_HOST=qs-local-mysql","-e","DB_TLS_VERIFY=false")
# bootstrap and validate as the administrative user
docker run --rm --network qs-local @env_common -e DB_USER=root -e DB_PASSWORD=localroot -e BOOTSTRAP_REQUIRE_TLS=false `
  -e DB_APP_PASSWORD=app-local -e DB_BENCHMARK_PASSWORD=bench-local -e DB_JOBS_PASSWORD=jobs-local `
  queryscale:local python -m scripts.aws_bootstrap --branches 10 --customers 200 --accounts 300 --transactions 2000 --loans 100
docker run --rm --network qs-local @env_common -e DB_USER=root -e DB_PASSWORD=localroot `
  -e DB_APP_PASSWORD=app-local -e DB_BENCHMARK_PASSWORD=bench-local -e DB_JOBS_PASSWORD=jobs-local `
  queryscale:local python -m scripts.aws_db_validate --no-require-tls
# API and worker with the exact credentials their ECS task definitions receive
docker volume create qs-local-runtime
docker run -d --name qs-local-api --network qs-local -p 8000:8000 @env_common -e DB_USER=queryscale_app -e DB_PASSWORD=app-local `
  --mount source=qs-local-runtime,target=/app/runtime queryscale:local
docker run -d --name qs-local-worker --network qs-local @env_common -e DB_USER=queryscale_benchmark -e DB_PASSWORD=bench-local `
  -e DB_TEST_USER=queryscale_benchmark -e DB_TEST_PASSWORD=bench-local `
  --mount source=qs-local-runtime,target=/app/runtime queryscale:local python -m backend.jobs.worker
py scripts/aws/smoke_test.py --base-url http://127.0.0.1:8000 --expect-local-artifacts --tolerate-missing-adapters
# clean up
docker rm -f qs-local-api qs-local-worker qs-local-mysql; docker volume rm qs-local-runtime; docker network rm qs-local
```

## Observed results of the local rehearsal (2026-10-14, `cloud` branch)

Run on one machine with Docker, MySQL 8.4.11 and the repository image. Nothing was
run against AWS; rows 5 to 8 of the procedure table remain unexecuted there.

| Step | Command | Observed |
| --- | --- | --- |
| Bootstrap, first run | `python -m scripts.aws_bootstrap --branches 10 --customers 200 --accounts 300 --transactions 2000 --loans 100` as `root` | Exit 0. TLS session, three databases, three users converged, schema loaded, seeded, test database cloned |
| Bootstrap, second run | same command, no flags | Exit 0. `skipping` for schema, seed and clone; no destructive statement issued |
| Validation | `python -m scripts.aws_db_validate --no-require-tls` | Exit 0. 33 checks passed, including every `probe:*` denial (MySQL error 1142) and `benchmark:no-leftover-indexes` |
| API and worker | started with the exact credentials their ECS task definitions pass (`queryscale_app` for the API, `queryscale_benchmark` for the worker) | `/health/ready` 200; worker started |
| Smoke test | `python scripts/aws/smoke_test.py --base-url http://127.0.0.1:18000 --iterations 20 --expect-local-artifacts --tolerate-missing-adapters` | Exit 0. 9 passed, 0 failed, 1 pending (`cleanup_verified`). Real benchmark: 6 query groups, 20 executions, artifact written to the shared volume |
| Index cleanup after a real job | validation rerun | `benchmark:no-leftover-indexes: clean` |
| Worker interruption, attempt 1 | 100k-row dataset, 1000 iterations, `docker kill` while `running` during warm-up | After restart: `succeeded`, `attempts=2`, no leftover index |
| Worker interruption, attempt 2 | `docker kill` while the candidate index existed in the test database | After restart: `succeeded`, `attempts=2`, recovery dropped the partial index, validation clean |
| Unit tests inside the image | `python -m unittest discover -s tests` | 74 tests passed |

The interruption rehearsal works locally because both containers share one SQLite
file. In AWS the same sequence depends on the shared job store and SQS adapters; until
they are deployed `interruption_test.ps1` reports `PENDING-APP` at the claim step.
