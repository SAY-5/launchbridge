resource "aws_lb" "this" {
  name               = "${var.name}-${var.environment}"
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id
}

resource "aws_lb_target_group" "api" {
  name        = "${var.name}-api"
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.this.id

  health_check {
    path                = "/readyz"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_target_group" "receiver" {
  count       = var.deploy_receiver_fake ? 1 : 0
  name        = "${var.name}-receiver"
  port        = 8081
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.this.id

  health_check {
    path    = "/healthz"
    matcher = "200"
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# The receiver fake is exposed under /receiver/* so the smoke suite can drive failure
# injection from outside the VPC. Remove this rule (or set deploy_receiver_fake=false)
# for anything beyond a trial.
resource "aws_lb_listener_rule" "receiver" {
  count        = var.deploy_receiver_fake ? 1 : 0
  listener_arn = aws_lb_listener.http.arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.receiver[0].arn
  }

  condition {
    http_header {
      http_header_name = "X-Target"
      values           = ["receiver"]
    }
  }
}
