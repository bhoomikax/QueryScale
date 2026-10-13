# ---------------------------------------------------------------------------
# Identity and naming
# ---------------------------------------------------------------------------

variable "region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Short lowercase project prefix used in resource names and tags."
  type        = string
  default     = "queryscale"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.name_prefix))
    error_message = "name_prefix must be lowercase alphanumeric with hyphens, 2-21 characters, starting with a letter."
  }
}

variable "environment" {
  description = "Environment name, for example dev or staging."
  type        = string
  default     = "dev"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,11}$", var.environment))
    error_message = "environment must be lowercase alphanumeric, 2-12 characters."
  }
}

variable "extra_tags" {
  description = "Additional tags applied to every resource."
  type        = map(string)
  default     = {}
}

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.42.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid IPv4 CIDR block."
  }
}

variable "az_count" {
  description = "Number of availability zones. ALB and RDS subnet groups need at least 2."
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "az_count must be 2 or 3."
  }
}

variable "enable_nat_gateway" {
  description = <<-EOT
    Create one NAT gateway so tasks in private subnets can reach ECR, SQS, Secrets Manager
    and CloudWatch. Costs roughly USD 33 per month plus data. When false, ECS tasks must run
    in the public subnets with public IPs (their security group still allows no inbound
    traffic except from the ALB). RDS always stays in the private subnets.
  EOT
  type        = bool
  default     = false
}

variable "alb_allowed_cidrs" {
  description = <<-EOT
    IPv4 CIDR blocks allowed to reach the load balancer. The API has no authentication, so
    restrict this to the addresses of the people who need it. 0.0.0.0/0 is rejected unless
    alb_allow_public_ingress is true.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.alb_allowed_cidrs) > 0
    error_message = "alb_allowed_cidrs must contain at least one CIDR block."
  }

  validation {
    condition     = alltrue([for cidr in var.alb_allowed_cidrs : can(cidrhost(cidr, 0))])
    error_message = "Every entry in alb_allowed_cidrs must be a valid IPv4 CIDR block."
  }

  validation {
    condition     = var.alb_allow_public_ingress || !contains(var.alb_allowed_cidrs, "0.0.0.0/0")
    error_message = "alb_allowed_cidrs contains 0.0.0.0/0. Set alb_allow_public_ingress = true to confirm you want an unauthenticated API reachable from the internet."
  }
}

variable "alb_allow_public_ingress" {
  description = "Explicit opt-in required before 0.0.0.0/0 is accepted in alb_allowed_cidrs."
  type        = bool
  default     = false
}

variable "alb_ingress_ports" {
  description = "Listener ports opened on the ALB security group. Add 443 once an ACM certificate exists."
  type        = list(number)
  default     = [80]
}

# ---------------------------------------------------------------------------
# Application contract (values taken from the repository, not invented)
# ---------------------------------------------------------------------------

variable "container_port" {
  description = "Port the API container listens on. Dockerfile EXPOSE 8000 and API_PORT default to 8000."
  type        = number
  default     = 8000
}

variable "db_port" {
  description = "MySQL port the RDS security group opens to ECS tasks. Matches DB_PORT in .env.example."
  type        = number
  default     = 3306
}

variable "db_app_username" {
  description = "Application database user name. Matches DB_USER in .env.example."
  type        = string
  default     = "queryscale_app"
}

variable "db_benchmark_username" {
  description = "Benchmark database user name. Matches DB_TEST_USER in .env.example."
  type        = string
  default     = "queryscale_benchmark"
}

variable "db_name" {
  description = "Source database name. Matches DB_NAME in .env.example."
  type        = string
  default     = "queryscale"
}

variable "db_test_name" {
  description = "Isolated benchmark database name. Matches DB_TEST_NAME in .env.example."
  type        = string
  default     = "queryscale_test"
}

variable "db_jobs_name" {
  description = "Database that will hold shared job state once the MySQL job store adapter lands."
  type        = string
  default     = "queryscale_jobs"
}

variable "benchmark_job_timeout_seconds" {
  description = "Must match BENCHMARK_JOB_TIMEOUT_SECONDS given to the worker (default 900 in .env.example)."
  type        = number
  default     = 900
}

variable "benchmark_job_max_attempts" {
  description = "Must match BENCHMARK_JOB_MAX_ATTEMPTS given to the worker (default 3 in .env.example)."
  type        = number
  default     = 3

  validation {
    condition     = var.benchmark_job_max_attempts >= 1
    error_message = "benchmark_job_max_attempts must be at least 1."
  }
}

# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

variable "sqs_visibility_timeout_seconds" {
  description = "How long a received job message stays hidden. Must exceed benchmark_job_timeout_seconds."
  type        = number
  default     = 1200

  validation {
    condition     = var.sqs_visibility_timeout_seconds > var.benchmark_job_timeout_seconds
    error_message = "sqs_visibility_timeout_seconds must be greater than benchmark_job_timeout_seconds."
  }

  validation {
    condition     = var.sqs_visibility_timeout_seconds <= 43200
    error_message = "SQS visibility timeout cannot exceed 43200 seconds."
  }
}

variable "sqs_message_retention_seconds" {
  description = "Retention for the main job queue. Default is 4 days."
  type        = number
  default     = 345600
}

variable "dlq_message_retention_seconds" {
  description = "Retention for the dead-letter queue. Default is 14 days; keep it longer than the main queue."
  type        = number
  default     = 1209600
}

# ---------------------------------------------------------------------------
# Artifact storage
# ---------------------------------------------------------------------------

variable "artifact_prefix" {
  description = "Key prefix under which worker result artifacts are written (JOB_ARTIFACT_PREFIX)."
  type        = string
  default     = "jobs"

  validation {
    condition     = can(regex("^[a-zA-Z0-9_-]+$", var.artifact_prefix))
    error_message = "artifact_prefix must be a single path segment without slashes."
  }
}

variable "artifact_expiration_days" {
  description = "Days before benchmark artifacts expire. 0 disables expiration."
  type        = number
  default     = 30
}

variable "artifact_bucket_force_destroy" {
  description = "Allow terraform destroy to delete the artifact bucket even when it contains objects. Dev only."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# Registry, secrets, logs
# ---------------------------------------------------------------------------

variable "ecr_force_delete" {
  description = "Allow terraform destroy to delete the ECR repository even when it contains images. Dev only."
  type        = bool
  default     = false
}

variable "ecr_keep_last_images" {
  description = "Number of tagged images retained by the ECR lifecycle policy."
  type        = number
  default     = 10
}

variable "secret_recovery_window_days" {
  description = "Secrets Manager recovery window on deletion. 0 deletes immediately (dev only); 7-30 otherwise."
  type        = number
  default     = 7

  validation {
    condition     = var.secret_recovery_window_days == 0 || (var.secret_recovery_window_days >= 7 && var.secret_recovery_window_days <= 30)
    error_message = "secret_recovery_window_days must be 0 or between 7 and 30."
  }
}

variable "log_retention_days" {
  description = "CloudWatch log retention for API, worker and bootstrap log groups."
  type        = number
  default     = 14

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "log_retention_days must be one of the CloudWatch Logs supported retention values."
  }
}

variable "enable_ecs_exec" {
  description = "Grant task roles the SSM Messages permissions required for aws ecs execute-command debugging."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# Cost controls
# ---------------------------------------------------------------------------

variable "budget_monthly_limit_usd" {
  description = "Monthly cost budget for the whole account in USD."
  type        = number
  default     = 50
}

variable "budget_alert_thresholds_percent" {
  description = "Actual-spend thresholds (percent of the limit) that trigger an email."
  type        = list(number)
  default     = [50, 80, 100]

  validation {
    condition     = alltrue([for t in var.budget_alert_thresholds_percent : t > 0 && t <= 1000])
    error_message = "Each threshold must be between 1 and 1000 percent."
  }
}

variable "budget_forecast_threshold_percent" {
  description = "Forecasted-spend threshold (percent of the limit) that triggers an email. 0 disables it."
  type        = number
  default     = 100
}

variable "budget_alert_emails" {
  description = "Email addresses that receive budget alerts."
  type        = list(string)

  validation {
    condition     = length(var.budget_alert_emails) > 0
    error_message = "budget_alert_emails must contain at least one address."
  }
}
