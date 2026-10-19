variable "region" {
  description = "AWS region for the state bucket. Use the same region as the main stack."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Short lowercase project prefix used in resource names."
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
