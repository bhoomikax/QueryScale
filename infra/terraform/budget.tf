# Account-wide monthly cost budget with email notifications.
#
# A budget only NOTIFIES. It does not stop, pause or cap anything; charges continue
# until resources are paused or destroyed (see the README's "Pause versus destroy").
#
# The budget is deliberately not filtered by tag: a tag filter only works after the tag
# is activated as a cost allocation tag in Billing, and until then a filtered budget
# silently reports zero spend.

resource "aws_budgets_budget" "monthly" {
  # Off by default: the account already has a console budget (QueryScale-Budget, USD 10/month)
  # and a second budget would only duplicate its emails. Budgets never cap spending.
  count = var.create_budget ? 1 : 0

  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_monthly_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Percent-of-limit thresholds on actual spend.
  dynamic "notification" {
    for_each = toset(var.budget_alert_thresholds_percent)

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = var.budget_alert_emails
    }
  }

  # Absolute dollar thresholds on actual spend.
  dynamic "notification" {
    for_each = toset(var.budget_alert_absolute_usd)

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "ABSOLUTE_VALUE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = var.budget_alert_emails
    }
  }

  # Forecast: AWS projects the month will exceed this percent of the limit.
  dynamic "notification" {
    for_each = var.budget_forecast_threshold_percent > 0 ? [var.budget_forecast_threshold_percent] : []

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "FORECASTED"
      subscriber_email_addresses = var.budget_alert_emails
    }
  }
}
