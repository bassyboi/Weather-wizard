terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" {
  region = var.aws_region
}

locals {
  name    = "${var.project}-${var.env}"
  svcname = "${var.project}-${var.env}-api"
}

# -------------------------------
# CloudWatch Logs
# -------------------------------
resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${local.svcname}"
  retention_in_days = 14
}

# -------------------------------
# ECS Cluster
# -------------------------------
resource "aws_ecs_cluster" "this" {
  name = "${local.name}-cluster"
}

# -------------------------------
# Security Groups
# -------------------------------
# ALB SG: allow inbound HTTP/HTTPS from internet
resource "aws_security_group" "alb" {
  name        = "${local.name}-alb-sg"
  description = "ALB ingress"
  vpc_id      = var.vpc_id

  dynamic "ingress" {
    for_each = var.enable_https ? toset(["80","443"]) : toset(["80"])
    content {
      from_port   = tonumber(ingress.value)
      to_port     = tonumber(ingress.value)
      protocol    = "tcp"
      cidr_blocks = var.allow_ingress_cidrs
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Task SG: allow traffic only from ALB to container_port
resource "aws_security_group" "task" {
  name        = "${local.name}-task-sg"
  description = "ECS task ingress from ALB"
  vpc_id      = var.vpc_id

  ingress {
    from_port       = var.container_port
    to_port         = var.container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# -------------------------------
# ALB + Target Group + Listener
# -------------------------------
resource "aws_lb" "api" {
  name               = "${local.name}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids
}

resource "aws_lb_target_group" "api" {
  name     = "${local.name}-tg"
  port     = var.container_port
  protocol = "HTTP"
  vpc_id   = var.vpc_id
  health_check {
    path                = var.health_path
    matcher             = "200"
    healthy_threshold   = 2
    unhealthy_threshold = 5
    interval            = 30
    timeout             = 5
  }
  target_type = "ip"
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = var.enable_https ? "redirect" : "forward"
    target_group_arn = var.enable_https ? null : aws_lb_target_group.api.arn
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  count             = var.enable_https ? 1 : 0
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-2016-08"
  certificate_arn   = var.acm_certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# -------------------------------
# IAM for ECS Tasks
# -------------------------------
data "aws_iam_policy_document" "task_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals { type = "Service", identifiers = ["ecs-tasks.amazonaws.com"] }
  }
}

resource "aws_iam_role" "task_role" {
  name               = "${local.name}-task-role"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
}

resource "aws_iam_role" "exec_role" {
  name               = "${local.name}-exec-role"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
}

# Allow pulling from ECR and writing logs
resource "aws_iam_role_policy_attachment" "exec_ecr" {
  role       = aws_iam_role.exec_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# (Optional) narrow S3 permissions for app data later
# resource "aws_iam_policy" "task_s3" { ... }
# resource "aws_iam_role_policy_attachment" "task_s3_attach" { ... }

# -------------------------------
# Task Definition
# -------------------------------
resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = tostring(var.cpu)
  memory                   = tostring(var.memory)
  execution_role_arn       = aws_iam_role.exec_role.arn
  task_role_arn            = aws_iam_role.task_role.arn

  container_definitions = jsonencode([
    {
      name      = "api"
      image     = var.api_image
      essential = true
      portMappings = [{
        containerPort = var.container_port
        hostPort      = var.container_port
        protocol      = "tcp"
      }]
      environment = [
        { name = "PORT", value = tostring(var.container_port) },
        { name = "DATA_DIR", value = var.data_dir }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.api.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "api"
        }
      }
      healthCheck = {
        command     = ["CMD-SHELL", "curl -fsS http://localhost:${var.container_port}${var.health_path} || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 15
      }
    }
  ])
}

# -------------------------------
# ECS Service
# -------------------------------
resource "aws_ecs_service" "api" {
  name            = "${local.svcname}"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"
  enable_execute_command = true

  network_configuration {
    security_groups  = [aws_security_group.task.id]
    subnets          = var.private_subnet_ids
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = var.container_port
  }

  lifecycle {
    ignore_changes = [desired_count] # allow manual scaling without drift
  }

  depends_on = [
    aws_lb_listener.http
  ]
}

output "alb_dns_name"   { value = aws_lb.api.dns_name }
output "service_name"   { value = aws_ecs_service.api.name }
output "cluster_name"   { value = aws_ecs_cluster.this.name }
