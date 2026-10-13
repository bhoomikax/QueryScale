locals {
  name = "${var.name_prefix}-${var.environment}"

  # Mirrors the application's own environment variable names (see .env.example and
  # backend/config.py). Infrastructure only publishes values; Person 1 owns the adapters.
  queue_name     = "${local.name}-benchmark-jobs.fifo"
  dlq_name       = "${local.name}-benchmark-jobs-dlq.fifo"
  log_group_base = "/${var.name_prefix}/${var.environment}"

  # A worker that exceeds BENCHMARK_JOB_TIMEOUT_SECONDS exits with os._exit(124)
  # (backend/jobs/worker.py). The queue must keep the message invisible at least
  # that long, otherwise a second worker could start the same benchmark.
  sqs_visibility_timeout_seconds = var.sqs_visibility_timeout_seconds

  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)
}
