# Variables for operational hardening: alarms and budget thresholds.

variable "create_budget" {
  description = "Create the Terraform-managed monthly budget. Keep false when a budget already exists in the console; a budget only sends notifications and never stops spending."
  type        = bool
  default     = false
}

variable "budget_alert_absolute_usd" {
  description = "Absolute dollar amounts of actual monthly spend that trigger an email, independent of the percent thresholds."
  type        = list(number)
  default     = []

  validation {
    condition     = alltrue([for amount in var.budget_alert_absolute_usd : amount > 0])
    error_message = "Each entry in budget_alert_absolute_usd must be greater than zero."
  }
}

variable "alarm_alb_5xx_threshold" {
  description = "Number of 5xx responses (target plus load balancer) in five minutes that triggers the api-5xx alarm."
  type        = number
  default     = 5
}
