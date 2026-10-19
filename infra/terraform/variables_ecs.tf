# Variables for the ECS services, load balancer and alarms stage.

# ---------------------------------------------------------------------------
# Task sizing and counts
# ---------------------------------------------------------------------------

variable "api_cpu" {
  description = "Fargate CPU units for the API task (256 = 0.25 vCPU)."
  type        = number
  default     = 256
}

variable "api_memory" {
  description = "Fargate memory in MiB for the API task."
  type        = number
  default     = 512
}

variable "api_desired_count" {
  description = "Number of API tasks. 0 pauses the service without destroying it."
  type        = number
  default     = 1

  validation {
    condition     = var.api_desired_count >= 0 && var.api_desired_count <= 4
    error_message = "api_desired_count must be between 0 and 4."
  }
}

variable "worker_cpu" {
  description = "Fargate CPU units for the worker task."
  type        = number
  default     = 256
}

variable "worker_memory" {
  description = "Fargate memory in MiB for the worker task."
  type        = number
  default     = 512
}

variable "worker_desired_count" {
  description = <<-EOT
    Number of worker tasks. Keep this at 1: there is a single benchmark database and the
    worker serialises jobs with a lock. Set 0 to pause. Until the shared job-store adapter
    lands, a worker can only see jobs in its own task's local SQLite file, so API-submitted
    jobs are not processed yet; the service still proves the image, secrets and database
    access work.
  EOT
  type        = number
  default     = 1

  validation {
    condition     = var.worker_desired_count >= 0 && var.worker_desired_count <= 1
    error_message = "worker_desired_count must be 0 or 1 while there is a single benchmark database."
  }
}

variable "worker_stop_timeout_seconds" {
  description = "Seconds ECS waits after SIGTERM before killing the worker container. Fargate maximum is 120."
  type        = number
  default     = 120

  validation {
    condition     = var.worker_stop_timeout_seconds >= 2 && var.worker_stop_timeout_seconds <= 120
    error_message = "worker_stop_timeout_seconds must be between 2 and 120."
  }
}

# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------

variable "ecs_tasks_subnet_tier" {
  description = <<-EOT
    Where ECS tasks run. "auto" picks private subnets when a NAT gateway exists and public
    subnets (with a public IP, inbound still blocked except from the ALB) otherwise.
    "private" requires enable_nat_gateway = true so tasks can pull images and reach AWS APIs.
  EOT
  type        = string
  default     = "auto"

  validation {
    condition     = contains(["auto", "public", "private"], var.ecs_tasks_subnet_tier)
    error_message = "ecs_tasks_subnet_tier must be auto, public or private."
  }

  validation {
    condition     = var.ecs_tasks_subnet_tier != "private" || var.enable_nat_gateway
    error_message = "ecs_tasks_subnet_tier = \"private\" requires enable_nat_gateway = true."
  }
}

# ---------------------------------------------------------------------------
# Load balancer
# ---------------------------------------------------------------------------

variable "acm_certificate_arn" {
  description = "ACM certificate ARN for HTTPS. Empty keeps the listener on HTTP only (no domain yet)."
  type        = string
  default     = ""
}

variable "alb_deletion_protection" {
  description = "Protect the load balancer from deletion. Off for development; it holds no data."
  type        = bool
  default     = false
}

variable "alb_idle_timeout_seconds" {
  description = "ALB idle timeout. The API answers quickly; benchmark work is asynchronous."
  type        = number
  default     = 60
}

variable "health_check_grace_period_seconds" {
  description = "Seconds after an API task starts before ALB health checks can mark it unhealthy."
  type        = number
  default     = 60
}

# ---------------------------------------------------------------------------
# Application configuration extension points
# ---------------------------------------------------------------------------

variable "api_extra_environment" {
  description = <<-EOT
    Additional non-secret environment variables for the API container. Use this for the
    settings the cloud adapters introduce once they are implemented; see the
    container_environment output for the proposed values. Never put passwords here.
  EOT
  type        = map(string)
  default     = {}
}

variable "worker_extra_environment" {
  description = "Additional non-secret environment variables for the worker container. Never put passwords here."
  type        = map(string)
  default     = {}
}

variable "jobs_db_consumers" {
  description = <<-EOT
    Which services receive the job-metadata database credential (DB_JOBS_USER plus the
    DB_JOBS_PASSWORD secret reference). Empty until the MySQL job store adapter reads them.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for item in var.jobs_db_consumers : contains(["api", "worker"], item)])
    error_message = "jobs_db_consumers entries must be api or worker."
  }
}

# ---------------------------------------------------------------------------
# Alarms
# ---------------------------------------------------------------------------

variable "alarm_emails" {
  description = "Email addresses subscribed to the alarm topic. Each must confirm the subscription email."
  type        = list(string)
  default     = []
}

variable "alarm_rds_free_storage_bytes" {
  description = "Alarm when RDS free storage drops below this many bytes (default 2 GiB)."
  type        = number
  default     = 2147483648
}

variable "alarm_queue_age_seconds" {
  description = "Alarm when the oldest job message is older than this. Should exceed one full benchmark."
  type        = number
  default     = 1800
}
