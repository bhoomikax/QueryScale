# Log groups for each ECS task kind. Alarms arrive with the ECS, ALB and RDS stages
# because they reference those resources.

locals {
  log_groups = {
    api       = "${local.log_group_base}/api"
    worker    = "${local.log_group_base}/worker"
    bootstrap = "${local.log_group_base}/bootstrap"
  }
}

resource "aws_cloudwatch_log_group" "app" {
  for_each = local.log_groups

  name              = each.value
  retention_in_days = var.log_retention_days

  tags = { Name = each.value }
}
