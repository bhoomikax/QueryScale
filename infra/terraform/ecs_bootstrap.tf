# ECS cluster and the one-off database bootstrap task definition.
#
# The bootstrap task runs scripts/aws_bootstrap.py from the same image as the API
# and worker. It is the ONLY task that receives the RDS master credential, injected
# as DB_USER / DB_PASSWORD so that the repository's existing seed and test-database
# helpers work unchanged. It creates the three databases and the three application
# users, loads the schema and synthetic data, and clones the benchmark database.
#
# The API and worker services arrive in the next milestone and reuse this cluster.

resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = var.ecs_container_insights ? "enabled" : "disabled"
  }

  tags = { Name = local.name }
}

locals {
  image_available = var.image_tag != ""

  # Non-secret settings shared by every container, published for the ECS milestone too.
  container_common_environment = {
    AWS_REGION                    = data.aws_region.current.region
    DB_HOST                       = aws_db_instance.main.address
    DB_PORT                       = tostring(var.db_port)
    DB_NAME                       = var.db_name
    DB_TEST_NAME                  = var.db_test_name
    DB_JOBS_NAME                  = var.db_jobs_name
    DB_TLS_VERIFY                 = "true"
    DB_TLS_CA                     = var.db_tls_ca_path
    DB_CHARSET                    = "utf8mb4"
    BENCHMARK_JOB_TIMEOUT_SECONDS = tostring(var.benchmark_job_timeout_seconds)
    BENCHMARK_JOB_MAX_ATTEMPTS    = tostring(var.benchmark_job_max_attempts)
  }

  bootstrap_environment = merge(local.container_common_environment, {
    # The bootstrap script derives the application user names from these.
    DB_APP_USER           = var.db_app_username
    DB_BENCHMARK_USER     = var.db_benchmark_username
    DB_JOBS_USER          = var.db_jobs_username
    BOOTSTRAP_REQUIRE_TLS = "true"
  })

  bootstrap_secrets = {
    # Master credential (RDS-managed secret): keys are "username" and "password".
    DB_USER     = "${aws_db_instance.main.master_user_secret[0].secret_arn}:username::"
    DB_PASSWORD = "${aws_db_instance.main.master_user_secret[0].secret_arn}:password::"
    # Application user passwords generated in secrets.tf.
    DB_APP_PASSWORD       = "${local.secret_db_app.arn}:password::"
    DB_BENCHMARK_PASSWORD = "${local.secret_db_benchmark.arn}:password::"
    DB_JOBS_PASSWORD      = "${local.secret_db_jobs.arn}:password::"
  }
}

resource "aws_ecs_task_definition" "bootstrap" {
  count = local.image_available ? 1 : 0

  family                   = "${local.name}-bootstrap"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.bootstrap_task_cpu)
  memory                   = tostring(var.bootstrap_task_memory)
  execution_role_arn       = aws_iam_role.bootstrap_execution.arn
  task_role_arn            = aws_iam_role.bootstrap_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.image_cpu_architecture
  }

  container_definitions = jsonencode([
    {
      name      = "bootstrap"
      image     = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
      essential = true
      command   = ["python", "-m", "scripts.aws_bootstrap"]
      user      = "10001:10001" # Matches USER in the Dockerfile.

      environment = [
        for key, value in local.bootstrap_environment : { name = key, value = value }
      ]

      secrets = [
        for key, value in local.bootstrap_secrets : { name = key, valueFrom = value }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.app["bootstrap"].name
          "awslogs-region"        = data.aws_region.current.region
          "awslogs-stream-prefix" = "bootstrap"
        }
      }

      readonlyRootFilesystem = false # scripts write nothing, but Python needs a writable /tmp.
    }
  ])

  tags = { Name = "${local.name}-bootstrap" }
}
