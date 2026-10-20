# AWS deployment evidence, 2026-10-20

Captured from the short-lived QueryScale deployment in `ap-southeast-2` (ECS Fargate API
task behind an ALB, RDS MySQL 8.4 `db.t4g.micro`) built from commit `ac6f263` on branch
`integration/ml-cloud`. The stack is torn down after the course review; these files are the
durable record. Account IDs, the ALB hostname, and VPC-internal IPs are replaced with
placeholders. No secret values were ever written to logs.

| File | What it shows |
| --- | --- |
| `smoke_test_output.txt` | `scripts/aws/smoke_test.py` against the ALB: 11 passed, 0 failed, 1 pending (per-job S3 artifacts, a known unimplemented feature). Includes the JSON report. |
| `benchmark_run_BR-20261020T084739Z-dd99e141.json` | Full protocol-v2 run record from `/api/benchmark/history/{run_id}`: 50 iterations on RDS, per-query medians, EXPLAIN plans before and after, workload benefit 0.697, cleanup `dropped`. |
| `job_benchmark-ba992d994633.json` | The job record (`PENDING -> RUNNING -> COMPLETED`) with the result summary. |
| `benchmark_results.json` | `/api/benchmark/results` rows for that run. |
| `api_ml_model.json` | `/api/ml/model`: the deployed task ranks with the measured-data model `v20261018T172058Z-e813dcc956e5`. |
| `health_ready.json` | `/health/ready?check_db=true` after bootstrap: `ok`, database reachable, ranking method `ml`. |
| `validation_after_smoke_test.txt` | `scripts/aws_db_validate.py` run as an ECS task after the benchmark: 32 checks passed, `no-leftover-indexes: clean`, row counts unchanged. |
| `api_log_tail.txt` | CloudWatch log tail of the API task around the smoke test. |

Known gaps visible here: measured index size and rows examined are empty because the
least-privilege benchmark user has no access to `mysql.innodb_index_stats` or
`performance_schema`; write-probe overhead percentages are noisy on a `t4g.micro` and should
not be quoted as costs; per-job artifacts are not uploaded to S3.
