# Variables for operational hardening: alarms and budget thresholds.

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
