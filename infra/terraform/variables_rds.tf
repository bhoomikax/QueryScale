# Variables for the RDS and database-bootstrap stage.

# ---------------------------------------------------------------------------
# Instance sizing and engine
# ---------------------------------------------------------------------------

variable "db_engine_version" {
  description = "RDS for MySQL engine version. The repository targets MySQL 8.4."
  type        = string
  default     = "8.4"
}

variable "db_parameter_group_family" {
  description = "Parameter group family matching db_engine_version."
  type        = string
  default     = "mysql8.4"
}

variable "db_instance_class" {
  description = "RDS instance class. db.t4g.micro is the cheapest Graviton class and free-tier eligible in many accounts."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = "Initial gp3 storage in GiB. 20 is the RDS minimum."
  type        = number
  default     = 20

  validation {
    condition     = var.db_allocated_storage >= 20
    error_message = "db_allocated_storage must be at least 20 GiB."
  }
}

variable "db_max_allocated_storage" {
  description = "Upper bound for storage autoscaling in GiB. 0 disables autoscaling."
  type        = number
  default     = 0

  validation {
    condition     = var.db_max_allocated_storage == 0 || var.db_max_allocated_storage > var.db_allocated_storage
    error_message = "db_max_allocated_storage must be 0 or greater than db_allocated_storage."
  }
}

variable "db_multi_az" {
  description = "Multi-AZ standby. Doubles instance cost; off for development."
  type        = bool
  default     = false
}

variable "db_master_username" {
  description = "Master (administrative) user name. Its password is generated and stored by RDS."
  type        = string
  default     = "queryscale_admin"

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,15}$", var.db_master_username))
    error_message = "db_master_username must be 1-16 characters, alphanumeric or underscore, starting with a letter."
  }
}

variable "db_jobs_username" {
  description = "Job-metadata database user name (proposed DB_JOBS_USER). Interface pending with Person 1."
  type        = string
  default     = "queryscale_jobs"
}

variable "db_ca_cert_identifier" {
  description = "RDS server certificate authority. The bundled infra/certs/rds-global-bundle.pem trusts all current RDS CAs."
  type        = string
  default     = "rds-ca-rsa2048-g1"
}

variable "db_tls_ca_path" {
  description = "Path inside the container where the Dockerfile copies the RDS CA bundle (DB_TLS_CA)."
  type        = string
  default     = "/app/certs/rds-global-bundle.pem"
}

# ---------------------------------------------------------------------------
# Backups, maintenance, logs
# ---------------------------------------------------------------------------

variable "db_backup_retention_days" {
  description = "Automated backup retention in days. 0 disables backups (not recommended even in dev)."
  type        = number
  default     = 7

  validation {
    condition     = var.db_backup_retention_days >= 0 && var.db_backup_retention_days <= 35
    error_message = "db_backup_retention_days must be between 0 and 35."
  }
}

variable "db_backup_window" {
  description = "Daily backup window in UTC (hh24:mi-hh24:mi)."
  type        = string
  default     = "02:00-03:00"
}

variable "db_maintenance_window" {
  description = "Weekly maintenance window in UTC (ddd:hh24:mi-ddd:hh24:mi). Must not overlap the backup window."
  type        = string
  default     = "sun:03:30-sun:04:30"
}

variable "db_cloudwatch_log_exports" {
  description = "RDS log types exported to CloudWatch Logs. Empty list disables exports."
  type        = list(string)
  default     = ["error"]

  validation {
    condition     = alltrue([for log in var.db_cloudwatch_log_exports : contains(["audit", "error", "general", "slowquery"], log)])
    error_message = "db_cloudwatch_log_exports entries must be audit, error, general or slowquery."
  }
}

# ---------------------------------------------------------------------------
# Teardown safety
# ---------------------------------------------------------------------------

variable "db_deletion_protection" {
  description = "Refuse to delete the instance. Must be set to false and applied before terraform destroy."
  type        = bool
  default     = true
}

variable "db_skip_final_snapshot" {
  description = "Skip the final snapshot on deletion. Keep false unless the data is disposable."
  type        = bool
  default     = false
}

variable "db_apply_immediately" {
  description = "Apply modifications immediately instead of in the maintenance window. May cause downtime."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# ECS cluster and bootstrap task
# ---------------------------------------------------------------------------

variable "ecs_container_insights" {
  description = "Enable Container Insights on the cluster. Adds CloudWatch metric cost; off for development."
  type        = bool
  default     = false
}

variable "image_tag" {
  description = <<-EOT
    Image tag in the ECR repository, normally the short git SHA pushed by the README steps.
    Leave empty until an image exists; the bootstrap task definition is created only when set.
  EOT
  type        = string
  default     = ""
}

variable "image_cpu_architecture" {
  description = "CPU architecture the image was built for. X86_64 for a default docker build on Windows or Intel macOS; ARM64 for Apple silicon without --platform."
  type        = string
  default     = "X86_64"

  validation {
    condition     = contains(["X86_64", "ARM64"], var.image_cpu_architecture)
    error_message = "image_cpu_architecture must be X86_64 or ARM64."
  }
}

variable "bootstrap_task_cpu" {
  description = "Fargate CPU units for the bootstrap task."
  type        = number
  default     = 256
}

variable "bootstrap_task_memory" {
  description = "Fargate memory in MiB for the bootstrap task. Seeding 100k transactions fits in 512."
  type        = number
  default     = 512
}
