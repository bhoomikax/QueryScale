# Private RDS for MySQL. One instance hosts three databases that the bootstrap task
# creates: the source database (DB_NAME), the disposable benchmark database
# (DB_TEST_NAME) and the job-metadata database (DB_JOBS_NAME). RDS manages the
# master password in Secrets Manager; only the bootstrap execution role may read it.

resource "aws_db_subnet_group" "main" {
  name        = "${local.name}-rds"
  description = "Private subnets for the QueryScale MySQL instance"
  subnet_ids  = aws_subnet.private[*].id

  tags = { Name = "${local.name}-rds" }
}

resource "aws_db_parameter_group" "main" {
  name_prefix = "${local.name}-mysql-"
  family      = var.db_parameter_group_family
  description = "QueryScale MySQL settings: TLS required on every connection"

  # Server-side enforcement of what backend/config.py already asks for client-side.
  parameter {
    name  = "require_secure_transport"
    value = "ON"
  }

  lifecycle {
    create_before_destroy = true
  }

  tags = { Name = "${local.name}-mysql" }
}

resource "aws_db_instance" "main" {
  identifier = "${local.name}-mysql"

  engine                      = "mysql"
  engine_version              = var.db_engine_version
  instance_class              = var.db_instance_class
  allocated_storage           = var.db_allocated_storage
  max_allocated_storage       = var.db_max_allocated_storage
  storage_type                = "gp3"
  storage_encrypted           = true
  ca_cert_identifier          = var.db_ca_cert_identifier
  auto_minor_version_upgrade  = true
  allow_major_version_upgrade = false

  # No db_name on purpose: the bootstrap task creates all three databases so that
  # names stay configurable and never diverge from the application contract.
  username                    = var.db_master_username
  manage_master_user_password = true
  port                        = var.db_port

  db_subnet_group_name   = aws_db_subnet_group.main.name
  parameter_group_name   = aws_db_parameter_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false
  multi_az               = var.db_multi_az

  backup_retention_period = var.db_backup_retention_days
  backup_window           = var.db_backup_window
  maintenance_window      = var.db_maintenance_window
  copy_tags_to_snapshot   = true

  enabled_cloudwatch_logs_exports = var.db_cloudwatch_log_exports

  # Teardown safety. deletion_protection must be switched off and applied before a
  # destroy can proceed, and a final snapshot is taken unless explicitly skipped.
  deletion_protection       = var.db_deletion_protection
  skip_final_snapshot       = var.db_skip_final_snapshot
  final_snapshot_identifier = var.db_skip_final_snapshot ? null : "${local.name}-mysql-final-${formatdate("YYYYMMDDhhmm", timestamp())}"
  apply_immediately         = var.db_apply_immediately

  performance_insights_enabled = false
  monitoring_interval          = 0

  lifecycle {
    # timestamp() changes every plan; only evaluate it when a destroy actually happens.
    ignore_changes = [final_snapshot_identifier]
  }

  tags = { Name = "${local.name}-mysql" }
}

# Explicit log groups so retention is bounded instead of "never expire".
resource "aws_cloudwatch_log_group" "rds" {
  for_each = toset(var.db_cloudwatch_log_exports)

  name              = "/aws/rds/instance/${local.name}-mysql/${each.value}"
  retention_in_days = var.log_retention_days

  tags = { Name = "${local.name}-mysql-${each.value}" }
}
