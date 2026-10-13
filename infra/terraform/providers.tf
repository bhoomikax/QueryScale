provider "aws" {
  region = var.region

  default_tags {
    tags = merge(
      {
        Project     = var.name_prefix
        Environment = var.environment
        ManagedBy   = "terraform"
        Stack       = "main"
      },
      var.extra_tags,
    )
  }
}

provider "random" {}
