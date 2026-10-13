# Three security groups form a strict chain: internet (allowlisted) -> ALB -> ECS tasks -> RDS.
# No group accepts inbound traffic from anywhere it does not need to.

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Load balancer: inbound only from allowlisted CIDRs"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-alb" }
}

resource "aws_vpc_security_group_ingress_rule" "alb_in" {
  for_each = {
    for pair in setproduct(local.alb_ingress_ports, var.alb_allowed_cidrs) :
    "${pair[0]}-${pair[1]}" => { port = pair[0], cidr = pair[1] }
  }

  security_group_id = aws_security_group.alb.id
  description       = "Allowlisted client access"
  ip_protocol       = "tcp"
  from_port         = each.value.port
  to_port           = each.value.port
  cidr_ipv4         = each.value.cidr
}

resource "aws_vpc_security_group_egress_rule" "alb_to_tasks" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Forward only to ECS tasks on the container port"
  ip_protocol                  = "tcp"
  from_port                    = var.container_port
  to_port                      = var.container_port
  referenced_security_group_id = aws_security_group.ecs_tasks.id
}

resource "aws_security_group" "ecs_tasks" {
  name        = "${local.name}-ecs-tasks"
  description = "API and worker tasks: inbound only from the ALB"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-ecs-tasks" }
}

resource "aws_vpc_security_group_ingress_rule" "tasks_from_alb" {
  security_group_id            = aws_security_group.ecs_tasks.id
  description                  = "API traffic from the load balancer"
  ip_protocol                  = "tcp"
  from_port                    = var.container_port
  to_port                      = var.container_port
  referenced_security_group_id = aws_security_group.alb.id
}

# Tasks need outbound HTTPS for ECR, SQS, Secrets Manager and CloudWatch, plus MySQL to RDS.
# The 0.0.0.0/0 destination (trivy AWS-0104) is what makes the no-NAT layout work; replacing
# it with interface endpoints adds about USD 7 per endpoint per month.
resource "aws_vpc_security_group_egress_rule" "tasks_https_out" {
  security_group_id = aws_security_group.ecs_tasks.id
  description       = "AWS service APIs and image pulls"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "tasks_to_rds" {
  security_group_id            = aws_security_group.ecs_tasks.id
  description                  = "MySQL to RDS"
  ip_protocol                  = "tcp"
  from_port                    = var.db_port
  to_port                      = var.db_port
  referenced_security_group_id = aws_security_group.rds.id
}

resource "aws_security_group" "rds" {
  name        = "${local.name}-rds"
  description = "RDS MySQL: inbound only from ECS tasks"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-rds" }
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_tasks" {
  security_group_id            = aws_security_group.rds.id
  description                  = "MySQL from API, worker and bootstrap tasks"
  ip_protocol                  = "tcp"
  from_port                    = var.db_port
  to_port                      = var.db_port
  referenced_security_group_id = aws_security_group.ecs_tasks.id
}
# No egress rule on the RDS group: the database never initiates connections.
