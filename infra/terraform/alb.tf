# Application Load Balancer in front of the API service.
#
# Internet-facing (trivy AWS-0053) but reachable only from alb_allowed_cidrs, and HTTP
# only (trivy AWS-0054) until acm_certificate_arn is set, because no domain exists yet.
#
# Ingress is limited by the alb security group to alb_allowed_cidrs. The listener is
# HTTP until acm_certificate_arn is set, after which port 80 redirects to HTTPS.
# Health checks hit /health/ready (backend/app.py), which runs SELECT 1 against RDS,
# so a task with bad database configuration never receives traffic.

locals {
  alb_https_enabled = var.acm_certificate_arn != ""
  alb_ingress_ports = local.alb_https_enabled ? [80, 443] : [80]
}

resource "aws_lb" "api" {
  name               = substr("${local.name}-api", 0, 32)
  load_balancer_type = "application"
  internal           = false
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.alb.id]

  idle_timeout               = var.alb_idle_timeout_seconds
  drop_invalid_header_fields = true
  enable_deletion_protection = var.alb_deletion_protection

  tags = { Name = "${local.name}-api" }
}

resource "aws_lb_target_group" "api" {
  name_prefix = "qsapi-"
  vpc_id      = aws_vpc.main.id
  port        = var.container_port
  protocol    = "HTTP"
  target_type = "ip" # Required for Fargate awsvpc tasks.

  deregistration_delay = 15

  health_check {
    enabled             = true
    path                = "/health/ready"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  lifecycle {
    create_before_destroy = true
  }

  tags = { Name = "${local.name}-api" }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  # Without a certificate, forward. With one, redirect to HTTPS.
  dynamic "default_action" {
    for_each = local.alb_https_enabled ? [] : [1]
    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.api.arn
    }
  }

  dynamic "default_action" {
    for_each = local.alb_https_enabled ? [1] : []
    content {
      type = "redirect"
      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }

  tags = { Name = "${local.name}-http" }
}

resource "aws_lb_listener" "https" {
  count = local.alb_https_enabled ? 1 : 0

  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }

  tags = { Name = "${local.name}-https" }
}
