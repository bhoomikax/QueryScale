# Benchmark job queue. FIFO with a single message group gives at most one job in
# flight, which matches the one-test-database constraint the local file lock enforces
# today (backend/jobs/lock.py). The message body is expected to carry only the job ID;
# the job store stays the source of truth. Message shape is agreed with Person 1.

resource "aws_sqs_queue" "jobs_dlq" {
  name                        = local.dlq_name
  fifo_queue                  = true
  content_based_deduplication = true
  message_retention_seconds   = var.dlq_message_retention_seconds
  sqs_managed_sse_enabled     = true

  tags = { Name = local.dlq_name }
}

resource "aws_sqs_queue" "jobs" {
  name                        = local.queue_name
  fifo_queue                  = true
  content_based_deduplication = true
  deduplication_scope         = "queue"
  fifo_throughput_limit       = "perQueue"
  visibility_timeout_seconds  = local.sqs_visibility_timeout_seconds
  message_retention_seconds   = var.sqs_message_retention_seconds
  receive_wait_time_seconds   = 20 # Long polling keeps the idle worker cheap.
  sqs_managed_sse_enabled     = true

  # The job store counts attempts itself. One extra receive lets the worker mark the
  # job failed before SQS gives up on the message entirely.
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.jobs_dlq.arn
    maxReceiveCount     = var.benchmark_job_max_attempts + 1
  })

  tags = { Name = local.queue_name }
}

resource "aws_sqs_queue_redrive_allow_policy" "jobs_dlq" {
  queue_url = aws_sqs_queue.jobs_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.jobs.arn]
  })
}

# Only the task roles defined in iam.tf may use the queue; deny everything else over plain HTTP.
data "aws_iam_policy_document" "jobs_queue" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["sqs:*"]
    resources = [aws_sqs_queue.jobs.arn]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sqs_queue_policy" "jobs" {
  queue_url = aws_sqs_queue.jobs.id
  policy    = data.aws_iam_policy_document.jobs_queue.json
}
