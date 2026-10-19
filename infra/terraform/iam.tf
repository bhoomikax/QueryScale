# IAM roles for ECS. One execution role per task kind (what the ECS agent needs to
# start that task) and one task role per task kind (what the code may call at runtime).
#
#   api_execution        pulls the image, writes the api log group, reads the app secret
#   worker_execution     pulls the image, writes the worker log group, reads the benchmark secret
#   bootstrap_execution  pulls the image, writes the bootstrap log group, reads every app
#                        secret and the RDS-managed master secret
#   api_task             sends to the job queue, reads artifacts for presigned downloads
#   worker_task          receives/deletes queue messages, writes artifacts
#   bootstrap_task       no AWS API access; it only talks to MySQL over the network
#
# Each execution role can read exactly the secrets its task definition references, and
# only the bootstrap execution role can read the master secret. The jobs-database secret
# is added to the api/worker execution roles only when jobs_db_consumers names them.

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    # Confused-deputy protection: only tasks from this account may assume the role.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${data.aws_partition.current.partition}:ecs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"]
    }
  }
}

# ---------------------------------------------------------------------------
# Execution roles
# ---------------------------------------------------------------------------

locals {
  execution_roles = {
    api = {
      log_group = aws_cloudwatch_log_group.app["api"].arn
      secrets = concat(
        [local.secret_db_app.arn],
        contains(var.jobs_db_consumers, "api") ? [local.secret_db_jobs.arn] : [],
      )
    }
    worker = {
      log_group = aws_cloudwatch_log_group.app["worker"].arn
      secrets = concat(
        [local.secret_db_benchmark.arn],
        contains(var.jobs_db_consumers, "worker") ? [local.secret_db_jobs.arn] : [],
      )
    }
    bootstrap = {
      log_group = aws_cloudwatch_log_group.app["bootstrap"].arn
      secrets = concat(
        [for secret in aws_secretsmanager_secret.db_user : secret.arn],
        [aws_db_instance.main.master_user_secret[0].secret_arn],
      )
    }
  }
}

data "aws_iam_policy_document" "execution" {
  for_each = local.execution_roles

  statement {
    sid       = "EcrAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # This action does not support resource-level permissions.
  }

  statement {
    sid    = "EcrPull"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
    ]
    resources = [aws_ecr_repository.app.arn]
  }

  statement {
    sid    = "WriteOwnLogGroup"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${each.value.log_group}:*"]
  }

  statement {
    sid       = "ReadReferencedSecrets"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = each.value.secrets
  }
}

resource "aws_iam_role" "execution" {
  for_each = local.execution_roles

  name               = "${local.name}-ecs-${each.key}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = { Name = "${local.name}-ecs-${each.key}-execution" }
}

resource "aws_iam_role_policy" "execution" {
  for_each = local.execution_roles

  name   = "execution"
  role   = aws_iam_role.execution[each.key].id
  policy = data.aws_iam_policy_document.execution[each.key].json
}

# Stable aliases used by the task definitions and outputs.
locals {
  api_execution_role_arn       = aws_iam_role.execution["api"].arn
  worker_execution_role_arn    = aws_iam_role.execution["worker"].arn
  bootstrap_execution_role_arn = aws_iam_role.execution["bootstrap"].arn
}

# ---------------------------------------------------------------------------
# Task roles
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "api_task" {
  statement {
    sid    = "SubmitBenchmarkJobs"
    effect = "Allow"
    actions = [
      "sqs:SendMessage",
      "sqs:GetQueueUrl",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.jobs.arn]
  }

  statement {
    sid       = "ReadArtifactsForPresignedDownloads"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/${var.artifact_prefix}/*"]
  }

  statement {
    sid       = "ListArtifactPrefix"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${var.artifact_prefix}/*"]
    }
  }
}

resource "aws_iam_role" "api_task" {
  name               = "${local.name}-api-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = { Name = "${local.name}-api-task" }
}

resource "aws_iam_role_policy" "api_task" {
  name   = "api"
  role   = aws_iam_role.api_task.id
  policy = data.aws_iam_policy_document.api_task.json
}

data "aws_iam_policy_document" "worker_task" {
  statement {
    sid    = "ConsumeBenchmarkJobs"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueUrl",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.jobs.arn]
  }

  statement {
    sid    = "WriteArtifacts"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:AbortMultipartUpload",
    ]
    resources = ["${aws_s3_bucket.artifacts.arn}/${var.artifact_prefix}/*"]
  }
}

resource "aws_iam_role" "worker_task" {
  name               = "${local.name}-worker-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = { Name = "${local.name}-worker-task" }
}

resource "aws_iam_role_policy" "worker_task" {
  name   = "worker"
  role   = aws_iam_role.worker_task.id
  policy = data.aws_iam_policy_document.worker_task.json
}

# The bootstrap task (schema init, seed, test DB clone) needs no AWS API calls at runtime.
# Its database credentials arrive through the execution role's secret injection.
resource "aws_iam_role" "bootstrap_task" {
  name               = "${local.name}-bootstrap-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = { Name = "${local.name}-bootstrap-task" }
}

# ---------------------------------------------------------------------------
# Optional: ECS Exec for interactive debugging
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_exec" {
  statement {
    sid    = "EcsExecSsmChannel"
    effect = "Allow"
    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"] # SSM Messages actions do not support resource-level permissions.
  }
}

resource "aws_iam_role_policy" "ecs_exec" {
  for_each = var.enable_ecs_exec ? {
    api       = aws_iam_role.api_task.id
    worker    = aws_iam_role.worker_task.id
    bootstrap = aws_iam_role.bootstrap_task.id
  } : {}

  name   = "ecs-exec"
  role   = each.value
  policy = data.aws_iam_policy_document.ecs_exec.json
}
