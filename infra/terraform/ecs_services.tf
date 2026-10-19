# API and worker task definitions and services on the ECS cluster from ecs_bootstrap.tf.
#
# Facts taken from the repository, not invented:
#   - container port 8000 (Dockerfile EXPOSE, API_PORT default in backend/config.py)
#   - API command is the Dockerfile CMD: uvicorn backend.app:app on API_HOST:API_PORT
#   - worker command: python -m backend.worker (README, backend/worker.py)
#   - liveness /health/live, readiness /health/ready (backend/app.py)
#   - the image runs as uid 10001 and has a writable /app/runtime for the local job store
#
# Only the currently implemented settings are passed. The cloud job adapters are not in
# the backend yet; their variables go through api_extra_environment and
# worker_extra_environment once they exist, so no second contract is invented here.
#
# Credentials: the API gets the app user's password, the worker gets the benchmark
# user's password, both as Secrets Manager references resolved by the execution role.
# Neither task definition references the RDS master secret.

locals {
  image_uri = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"

  ecs_task_subnet_ids = (
    var.ecs_tasks_subnet_tier == "private" || (var.ecs_tasks_subnet_tier == "auto" && var.enable_nat_gateway)
  ) ? aws_subnet.private[*].id : aws_subnet.public[*].id

  ecs_task_assign_public_ip = !(
    var.ecs_tasks_subnet_tier == "private" || (var.ecs_tasks_subnet_tier == "auto" && var.enable_nat_gateway)
  )

  jobs_secret_ref = "${local.secret_db_jobs.arn}:password::"

  api_environment = merge(
    local.container_common_environment,
    {
      API_HOST = "0.0.0.0"
      API_PORT = tostring(var.container_port)
      DB_USER  = var.db_app_username
      # The API task runs analysis, benchmark, and training jobs itself (thread mode).
      # Its SQLite job store is task-local, so a separate worker task cannot see the
      # jobs; keep worker_desired_count at 0 unless a shared job store exists.
      JOB_EXECUTION_MODE = "thread"
      # Benchmarks open the test database as the least-privilege benchmark user.
      DB_TEST_USER = var.db_benchmark_username
    },
    contains(var.jobs_db_consumers, "api") ? { DB_JOBS_USER = var.db_jobs_username } : {},
    var.api_extra_environment,
  )

  api_secrets = merge(
    {
      DB_PASSWORD      = "${local.secret_db_app.arn}:password::"
      DB_TEST_PASSWORD = "${local.secret_db_benchmark.arn}:password::"
    },
    contains(var.jobs_db_consumers, "api") ? { DB_JOBS_PASSWORD = local.jobs_secret_ref } : {},
  )

  worker_environment = merge(
    local.container_common_environment,
    {
      # The worker only opens benchmark-database connections (backend/services/benchmark.py,
      # index_manager.py use test_mysql_config). DB_USER is set to the same account so the
      # DB_TEST_USER fallback in backend/config.py never resolves to "root".
      # NOTE: this worker polls a SQLite job store in its own task; it does not share the
      # API task's store, so API-submitted jobs never reach it. It is kept for a future
      # shared job store; set worker_desired_count = 0 for now (see dev.tfvars.example).
      DB_USER            = var.db_benchmark_username
      DB_TEST_USER       = var.db_benchmark_username
      JOB_EXECUTION_MODE = "queue"
    },
    contains(var.jobs_db_consumers, "worker") ? { DB_JOBS_USER = var.db_jobs_username } : {},
    var.worker_extra_environment,
  )

  worker_secrets = merge(
    {
      DB_PASSWORD      = "${local.secret_db_benchmark.arn}:password::"
      DB_TEST_PASSWORD = "${local.secret_db_benchmark.arn}:password::"
    },
    contains(var.jobs_db_consumers, "worker") ? { DB_JOBS_PASSWORD = local.jobs_secret_ref } : {},
  )

  # Same probe as the Dockerfile HEALTHCHECK; ECS ignores the image's own HEALTHCHECK.
  api_health_command = [
    "CMD-SHELL",
    "python -c \"import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('API_PORT', '8000') + '/health/live', timeout=3)\" || exit 1",
  ]
}

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

resource "aws_ecs_task_definition" "api" {
  count = local.image_available ? 1 : 0

  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.api_cpu)
  memory                   = tostring(var.api_memory)
  execution_role_arn       = local.api_execution_role_arn
  task_role_arn            = aws_iam_role.api_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.image_cpu_architecture
  }

  container_definitions = jsonencode([
    {
      name      = "api"
      image     = local.image_uri
      essential = true
      user      = "10001:10001"
      # No command: the Dockerfile CMD starts uvicorn with API_HOST/API_PORT.

      portMappings = [
        { containerPort = var.container_port, protocol = "tcp" }
      ]

      environment = [for key, value in local.api_environment : { name = key, value = value }]
      secrets     = [for key, value in local.api_secrets : { name = key, valueFrom = value }]

      healthCheck = {
        command     = local.api_health_command
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }

      linuxParameters = { initProcessEnabled = true }

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.app["api"].name
          "awslogs-region"        = data.aws_region.current.region
          "awslogs-stream-prefix" = "api"
        }
      }
    }
  ])

  tags = { Name = "${local.name}-api" }
}

resource "aws_ecs_service" "api" {
  count = local.image_available ? 1 : 0

  name            = "${local.name}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api[0].arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"
  propagate_tags  = "SERVICE"

  enable_execute_command            = var.enable_ecs_exec
  health_check_grace_period_seconds = var.health_check_grace_period_seconds

  network_configuration {
    subnets          = local.ecs_task_subnet_ids
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = local.ecs_task_assign_public_ip
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = var.container_port
  }

  # Rolling deploy that keeps the old task serving until the new one is healthy, and
  # rolls back automatically when the new task definition cannot become healthy.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_lb_listener.http]

  tags = { Name = "${local.name}-api" }
}

# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

resource "aws_ecs_task_definition" "worker" {
  count = local.image_available ? 1 : 0

  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.worker_cpu)
  memory                   = tostring(var.worker_memory)
  execution_role_arn       = local.worker_execution_role_arn
  task_role_arn            = aws_iam_role.worker_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.image_cpu_architecture
  }

  container_definitions = jsonencode([
    {
      name      = "worker"
      image     = local.image_uri
      essential = true
      user      = "10001:10001"
      command   = ["python", "-m", "backend.worker"]

      # Give an in-flight benchmark the maximum Fargate allows to wind down. A job that
      # outlives this is recovered by the worker's own recovery path on the next start.
      stopTimeout = var.worker_stop_timeout_seconds

      environment = [for key, value in local.worker_environment : { name = key, value = value }]
      secrets     = [for key, value in local.worker_secrets : { name = key, valueFrom = value }]

      # No healthCheck: the worker exposes no port. ECS restarts it if the process exits,
      # which is exactly what the job timeout does (os._exit(124) in backend/jobs/worker.py).

      linuxParameters = { initProcessEnabled = true }

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.app["worker"].name
          "awslogs-region"        = data.aws_region.current.region
          "awslogs-stream-prefix" = "worker"
        }
      }
    }
  ])

  tags = { Name = "${local.name}-worker" }
}

resource "aws_ecs_service" "worker" {
  count = local.image_available ? 1 : 0

  name            = "${local.name}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker[0].arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"
  propagate_tags  = "SERVICE"

  enable_execute_command = var.enable_ecs_exec

  network_configuration {
    subnets          = local.ecs_task_subnet_ids
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = local.ecs_task_assign_public_ip
  }

  # Never run two workers at once: there is one benchmark database and the worker's lock
  # and recovery logic assume a single active process. ECS stops the old task before it
  # starts the new one.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  tags = { Name = "${local.name}-worker" }
}
