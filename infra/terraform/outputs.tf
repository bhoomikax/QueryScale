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

output "ecs_task_subnet_ids" {
  description = "Subnets ECS tasks must use: private when NAT is enabled, otherwise public with a public IP."
  value       = var.enable_nat_gateway ? aws_subnet.private[*].id : aws_subnet.public[*].id
}

output "ecs_task_assign_public_ip" {
  description = "Whether run-task and services must assign a public IP (true when there is no NAT gateway)."
  value       = !var.enable_nat_gateway
}

# ---------------------------------------------------------------------------
# Registry, storage, queue, logs
# ---------------------------------------------------------------------------

output "ecr_repository_url" {
  description = "Push target for docker push."
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

output "log_group_names" {
  description = "CloudWatch log group names by task kind."
  value       = { for key, group in aws_cloudwatch_log_group.app : key => group.name }
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

output "rds_endpoint" {
  description = "RDS endpoint as host:port."
  value       = aws_db_instance.main.endpoint
}

output "rds_address" {
  description = "RDS host name (DB_HOST)."
  value       = aws_db_instance.main.address
}

output "rds_port" {
  description = "RDS port (DB_PORT)."
  value       = aws_db_instance.main.port
}

output "rds_instance_identifier" {
  description = "RDS instance identifier for aws rds commands."
  value       = aws_db_instance.main.identifier
}

output "rds_master_secret_arn" {
  description = "ARN of the RDS-managed master secret. Readable only by the bootstrap execution role."
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
}

output "secret_arns" {
  description = "Secrets Manager ARNs of the application database users. Values are never output."
  value       = { for key, secret in aws_secretsmanager_secret.db_user : "db_${key}" => secret.arn }
}

output "database_names" {
  description = "The three databases the bootstrap task creates."
  value = {
    source    = var.db_name
    benchmark = var.db_test_name
    jobs      = var.db_jobs_name
  }
}

# ---------------------------------------------------------------------------
# IAM, ECS
# ---------------------------------------------------------------------------

output "iam_role_arns" {
  description = "Role ARNs consumed by the ECS task definitions."
  value = {
    execution           = aws_iam_role.execution.arn
    bootstrap_execution = aws_iam_role.bootstrap_execution.arn
    api_task            = aws_iam_role.api_task.arn
    worker_task         = aws_iam_role.worker_task.arn
    bootstrap_task      = aws_iam_role.bootstrap_task.arn
  }
}

output "ecs_cluster_name" {
  description = "ECS cluster shared by the bootstrap task and the upcoming services."
  value       = aws_ecs_cluster.main.name
}

output "bootstrap_task_definition_arn" {
  description = "Task definition for scripts/aws/run_bootstrap_task.ps1. Null until image_tag is set."
  value       = local.bootstrap_enabled ? aws_ecs_task_definition.bootstrap[0].arn : null
}

# ---------------------------------------------------------------------------
# Application contract
# ---------------------------------------------------------------------------

output "container_environment" {
  description = <<-EOT
    Non-secret environment values the ECS stage will pass to the API and worker
    containers. Names follow the proposed additions to backend/config.py; the adapters
    that read JOB_* and DB_JOBS_* are owned by Person 1. Passwords are injected from
    Secrets Manager (see secret_arns), never from here.
  EOT
  value = merge(local.container_common_environment, {
    JOB_BACKEND         = "aws"
    JOB_QUEUE_URL       = aws_sqs_queue.jobs.url
    JOB_ARTIFACT_BUCKET = aws_s3_bucket.artifacts.bucket
    JOB_ARTIFACT_PREFIX = var.artifact_prefix
    DB_USER             = var.db_app_username
    DB_TEST_USER        = var.db_benchmark_username
    DB_JOBS_USER        = var.db_jobs_username
    API_PORT            = tostring(var.container_port)
  })
}
