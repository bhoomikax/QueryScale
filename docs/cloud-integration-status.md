# Cloud integration status

Last reviewed: 2026-10-18, on branch `repair/ml-e2e` (based on `ml`).

## What exists where

| Capability | `ml` branch (this code) | `cloud` branch |
| --- | --- | --- |
| ML ranking (`backend/services/ml/`), rule scorer, schema metadata, queue-mode worker (`backend/worker.py`), Docker Compose stack | Yes | No (the files are absent) |
| Terraform for VPC, RDS MySQL, ECS Fargate, ALB, SQS, S3, CloudWatch alarms, budget (`infra/terraform/`, `infra/bootstrap/`) | No | Yes |
| AWS operations scripts (`scripts/aws/*.ps1`, `scripts/aws/smoke_test.py`, `scripts/aws_bootstrap.py`, `scripts/aws_db_validate.py`) | No | Yes |
| Alternative job system (`backend/jobs/*`: SQLite store, local queue, local artifact store, worker with attempts/recovery) | No | Yes |
| S3 model-artifact mirroring (`backend/services/artifact_store.py`, `ARTIFACT_STORE=s3`) | Yes, never exercised against a bucket | No |
| JSON logging for CloudWatch (`LOG_FORMAT=json`) | Yes | No |

## Deployment evidence

There is **no evidence of a real AWS deployment** on either branch. `docs/aws-validation.md`
on `cloud` records a local Docker rehearsal on 2026-10-14 and states: "Nothing was run
against AWS; rows 5 to 8 of the procedure table remain unexecuted there." No Terraform
state, no `dev.tfvars`, no ECR image tag, and no CloudWatch output is committed anywhere.
Any report must describe AWS as *designed and locally rehearsed*, not deployed.

## What is verified locally (this branch)

- Three-container Compose stack (`mysql:8.4`, API, single worker) healthy; readiness
  endpoint reports database reachability and worker heartbeat.
- Jobs submitted through the API are executed by the worker container from the shared
  SQLite job store in the `queryscale-runtime` volume.
- The same image runs `uvicorn backend.app:app` and `python -m backend.worker`, which is
  the split an ECS service pair needs.

## Integration required to ship the ML application on the cloud infrastructure

A dry-run merge (`git merge-tree --write-tree ml cloud`) conflicts in these files:

```
.dockerignore  .env.example  .gitignore  Dockerfile  README.md
backend/app.py  backend/routes/benchmark.py  backend/services/benchmark.py
backend/services/index_manager.py  dashboard/dashboard.js  dashboard/style.css
scripts/run_local_pipeline.py  scripts/workload.py
```

Recommended direction: keep `ml` as the application base and bring the `cloud`
infrastructure over, because `ml` carries the ML pipeline, the metadata layer, the
protocol-v2 benchmark, and the tests. Concretely:

1. Copy `infra/`, `docs/aws-validation.md`, `scripts/aws/`, `scripts/aws_bootstrap.py`,
   and `scripts/aws_db_validate.py` from `cloud` (no conflicts; they do not exist on `ml`).
2. Do **not** merge `backend/jobs/*` from `cloud`. `ml` already has an equivalent job
   store and worker (`backend/services/jobs.py`, `backend/worker.py`). The `cloud`
   Terraform task definition runs `python -m backend.jobs.worker`; change that command to
   `python -m backend.worker` in `infra/terraform/ecs_services.tf`.
3. The `cloud` health paths are `/health/live` and `/health/ready`; `ml` serves `/health`
   and `/health/ready`. Either add a `/health/live` alias in `backend/app.py` (one line) or
   change the ALB and container health checks in Terraform.
4. The `cloud` RDS bootstrap enforces TLS (`DB_TLS_VERIFY`, `DB_TLS_CA`). `ml`'s
   `backend/config.py` has no TLS settings; add `ssl_ca` / `ssl_verify_cert` to
   `Settings.mysql_config()` before pointing it at RDS.
5. `cloud` uses two database users (`queryscale_app` read-only on the primary,
   `queryscale_benchmark` on the test database). `ml` uses one `DB_USER` for both. Either
   add `DB_TEST_USER`/`DB_TEST_PASSWORD` to `index_manager.py` and `test_database.py`, or
   grant one user the privileges listed in the README.
6. SQS is provisioned on `cloud` but not consumed by either branch's worker. The `ml`
   worker polls SQLite, which only works while API and worker share one filesystem (one
   host or one task with a shared volume). For multi-task ECS, a queue adapter is still
   application work.
7. Model artifacts: set `ARTIFACT_STORE=s3` and install `boto3` in the image so trained
   models are mirrored; the worker task role needs `s3:PutObject` on the artifact prefix.

None of the above has been executed. No AWS resources were created or changed during this work.
