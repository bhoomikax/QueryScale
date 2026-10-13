# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

output "vpc_id" {
  description = "VPC ID."
  value       = aws_vpc.main.id
}

output "public_subnet_ids" {
  description = "Public subnets for the ALB, and for ECS tasks when NAT is disabled."
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "Private subnets for RDS, and for ECS tasks when NAT is enabled."
  value       = aws_subnet.private[*].id
}

output "security_group_ids" {
  description = "Security group IDs by role."
  value = {
    alb       = aws_security_group.alb.id
    ecs_tasks = aws_security_group.ecs_tasks.id
    rds       = aws_security_group.rds.id
  }
}

# ---------------------------------------------------------------------------
# Registry, storage, queue, secrets, logs
# ---------------------------------------------------------------------------

output "ecr_repository_url" {
  description = "Push target for `docker push`."
  value       = aws_ecr_repository.app.repository_url
}

output "artifact_bucket_name" {
  description = "S3 bucket for benchmark artifacts (JOB_ARTIFACT_BUCKET)."
  value       = aws_s3_bucket.artifacts.bucket
}

output "job_queue_url" {
  description = "FIFO job queue URL (JOB_QUEUE_URL)."
  value       = aws_sqs_queue.jobs.url
}

output "job_queue_arn" {
  description = "FIFO job queue ARN."
  value       = aws_sqs_queue.jobs.arn
}

output "job_dlq_url" {
  description = "Dead-letter queue URL for inspection and redrive."
  value       = aws_sqs_queue.jobs_dlq.url
}

output "secret_arns" {
  description = "Secrets Manager ARNs for ECS secret injection. Values are never output."
  value = {
    db_app       = aws_secretsmanager_secret.db_app.arn
    db_benchmark = aws_secretsmanager_secret.db_benchmark.arn
  }
}

output "log_group_names" {
  description = "CloudWatch log group names by task kind."
  value       = { for key, group in aws_cloudwatch_log_group.app : key => group.name }
}

# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------

output "iam_role_arns" {
  description = "Role ARNs consumed by the ECS stage task definitions."
  value = {
    execution           = aws_iam_role.execution.arn
    bootstrap_execution = aws_iam_role.bootstrap_execution.arn
    api_task            = aws_iam_role.api_task.arn
    worker_task         = aws_iam_role.worker_task.arn
    bootstrap_task      = aws_iam_role.bootstrap_task.arn
  }
}

# ---------------------------------------------------------------------------
# Application contract
# ---------------------------------------------------------------------------

output "container_environment" {
  description = <<-EOT
    Non-secret environment values the ECS stage will pass to the containers. Names follow
    the proposed additions to backend/config.py; the adapters that read them are owned by
    Person 1. Secret values (passwords) are injected from Secrets Manager, never from here.
  EOT
  value = {
    AWS_REGION                    = data.aws_region.current.region
    JOB_BACKEND                   = "aws"
    JOB_QUEUE_URL                 = aws_sqs_queue.jobs.url
    JOB_ARTIFACT_BUCKET           = aws_s3_bucket.artifacts.bucket
    JOB_ARTIFACT_PREFIX           = var.artifact_prefix
    DB_NAME                       = var.db_name
    DB_TEST_NAME                  = var.db_test_name
    DB_JOBS_NAME                  = var.db_jobs_name
    DB_USER                       = var.db_app_username
    DB_TEST_USER                  = var.db_benchmark_username
    DB_PORT                       = tostring(var.db_port)
    DB_TLS_VERIFY                 = "true"
    API_PORT                      = tostring(var.container_port)
    BENCHMARK_JOB_TIMEOUT_SECONDS = tostring(var.benchmark_job_timeout_seconds)
    BENCHMARK_JOB_MAX_ATTEMPTS    = tostring(var.benchmark_job_max_attempts)
  }
}
