# CloudWatch alarms and the SNS topic they notify. Email subscriptions must be confirmed
# by the recipient; unconfirmed ones are left behind by terraform destroy and expire on
# their own after three days.
#
# Alarm inventory (names are "<prefix>-<env>-<suffix>"):
#   dlq-has-messages              SQS DLQ ApproximateNumberOfMessagesVisible > 0
#   job-queue-stalled             SQS ApproximateAgeOfOldestMessage > alarm_queue_age_seconds
#   api-5xx                       ALB HTTPCode_Target_5XX_Count + HTTPCode_ELB_5XX_Count
#   api-no-healthy-targets        ALB HealthyHostCount < 1
#   api-unhealthy-targets         ALB UnHealthyHostCount > 0
#   rds-cpu-high                  RDS CPUUtilization > 80 for 15 minutes
#   rds-storage-low               RDS FreeStorageSpace < alarm_rds_free_storage_bytes
#   api-tasks-below-desired       ECS RunningTaskCount < api_desired_count   (Container Insights)
#   worker-tasks-below-desired    ECS RunningTaskCount < worker_desired_count (Container Insights)
#   worker-not-running            AWS/ECS CPUUtilization missing for 10 minutes (no Insights)
#   candidate-index-cleanup-failed  worker log line from backend/jobs/worker.py

# Not encrypted on purpose (trivy AWS-0095): CloudWatch alarms cannot publish to a topic
# encrypted with the AWS-managed SNS key, and a customer-managed key costs about USD 1
# per month. The topic carries alarm names and descriptions only, never data or secrets.
resource "aws_sns_topic" "alarms" {
  name = "${local.name}-alarms"

  tags = { Name = "${local.name}-alarms" }
}

resource "aws_sns_topic_subscription" "alarm_email" {
  for_each = toset(var.alarm_emails)

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = each.value
}

locals {
  alarm_actions = [aws_sns_topic.alarms.arn]
}

# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "dlq_messages" {
  alarm_name          = "${local.name}-dlq-has-messages"
  alarm_description   = "A benchmark job message reached the dead-letter queue ${aws_sqs_queue.jobs_dlq.name}."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.jobs_dlq.name }
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "queue_oldest_message" {
  alarm_name          = "${local.name}-job-queue-stalled"
  alarm_description   = "The oldest message in ${aws_sqs_queue.jobs.name} is older than ${var.alarm_queue_age_seconds}s; the worker may be down."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.alarm_queue_age_seconds
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.jobs.name }
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# ---------------------------------------------------------------------------
# Load balancer and API
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  alarm_name          = "${local.name}-api-5xx"
  alarm_description   = "More than ${var.alarm_alb_5xx_threshold} server errors in 5 minutes from the API tasks or the load balancer itself."
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.alarm_alb_5xx_threshold
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions

  metric_query {
    id          = "total_5xx"
    expression  = "target_5xx + elb_5xx"
    label       = "All 5xx responses"
    return_data = true
  }

  metric_query {
    id = "target_5xx"
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_Target_5XX_Count"
      period      = 300
      stat        = "Sum"
      dimensions  = { LoadBalancer = aws_lb.api.arn_suffix }
    }
  }

  metric_query {
    id = "elb_5xx"
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_ELB_5XX_Count"
      period      = 300
      stat        = "Sum"
      dimensions  = { LoadBalancer = aws_lb.api.arn_suffix }
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "alb_no_healthy_hosts" {
  alarm_name          = "${local.name}-api-no-healthy-targets"
  alarm_description   = "No API task passes /health/ready. The dashboard is down or RDS is unreachable."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  dimensions = {
    LoadBalancer = aws_lb.api.arn_suffix
    TargetGroup  = aws_lb_target_group.api.arn_suffix
  }
  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "alb_unhealthy_hosts" {
  alarm_name          = "${local.name}-api-unhealthy-targets"
  alarm_description   = "At least one API task is failing /health/ready for 5 minutes."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  dimensions = {
    LoadBalancer = aws_lb.api.arn_suffix
    TargetGroup  = aws_lb_target_group.api.arn_suffix
  }
  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

# ---------------------------------------------------------------------------
# ECS running task counts
#
# RunningTaskCount lives in the ECS/ContainerInsights namespace and only exists when
# ecs_container_insights = true (metered by CloudWatch). Without Insights, the worker
# gets a proxy alarm: AWS/ECS publishes CPUUtilization per service only while a task
# runs, so ten minutes of missing data means no worker is running. The API needs no
# proxy because api-no-healthy-targets covers it.
# ---------------------------------------------------------------------------

locals {
  ecs_count_alarms = local.image_available && var.ecs_container_insights ? {
    api    = { service = aws_ecs_service.api[0].name, desired = var.api_desired_count }
    worker = { service = aws_ecs_service.worker[0].name, desired = var.worker_desired_count }
  } : {}
}

resource "aws_cloudwatch_metric_alarm" "ecs_tasks_below_desired" {
  for_each = { for key, value in local.ecs_count_alarms : key => value if value.desired > 0 }

  alarm_name          = "${local.name}-${each.key}-tasks-below-desired"
  alarm_description   = "ECS service ${each.value.service} has fewer running tasks than its desired count of ${each.value.desired}."
  namespace           = "ECS/ContainerInsights"
  metric_name         = "RunningTaskCount"
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 5
  threshold           = each.value.desired
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = each.value.service
  }
  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "worker_not_running" {
  count = local.image_available && !var.ecs_container_insights && var.worker_desired_count > 0 ? 1 : 0

  alarm_name          = "${local.name}-worker-not-running"
  alarm_description   = "No worker task has reported CPU for 10 minutes; the worker service is probably at zero running tasks. Enable ecs_container_insights for an exact RunningTaskCount alarm."
  namespace           = "AWS/ECS"
  metric_name         = "CPUUtilization"
  statistic           = "SampleCount"
  period              = 300
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.worker[0].name
  }
  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  alarm_name          = "${local.name}-rds-cpu-high"
  alarm_description   = "RDS ${aws_db_instance.main.identifier} CPU above 80 percent for 15 minutes."
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.main.identifier }
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "rds_free_storage" {
  alarm_name          = "${local.name}-rds-storage-low"
  alarm_description   = "RDS ${aws_db_instance.main.identifier} free storage is below ${var.alarm_rds_free_storage_bytes} bytes."
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.alarm_rds_free_storage_bytes
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.main.identifier }
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# ---------------------------------------------------------------------------
# Benchmark isolation: the one log line that must page someone
#
# backend/jobs/worker.py line 135 logs, with the worker's logging format
# "%(asctime)s %(levelname)s %(name)s %(message)s":
#     ... ERROR queryscale.worker Candidate-index cleanup failed for job <job id>
# The filter term below matches that phrase exactly. If the message text in
# backend/jobs/worker.py changes, this pattern must change with it.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_metric_filter" "cleanup_failed" {
  name           = "${local.name}-candidate-index-cleanup-failed"
  log_group_name = aws_cloudwatch_log_group.app["worker"].name
  pattern        = "\"Candidate-index cleanup failed\""

  metric_transformation {
    name          = "CandidateIndexCleanupFailed"
    namespace     = "QueryScale/${var.environment}"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "cleanup_failed" {
  alarm_name          = "${local.name}-candidate-index-cleanup-failed"
  alarm_description   = "The worker could not remove a candidate index from ${var.db_test_name}. Run the bootstrap task with --rebuild-test-db before the next benchmark."
  namespace           = "QueryScale/${var.environment}"
  metric_name         = "CandidateIndexCleanupFailed"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}
