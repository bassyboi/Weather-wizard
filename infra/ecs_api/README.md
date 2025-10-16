# ECS Fargate API (Terraform)

## Inputs (set via -var or *.tfvars)
- aws_region: e.g., ap-southeast-2
- project: e.g., weather-wizard
- env: e.g., dev
- vpc_id: your VPC id
- public_subnet_ids: list of public subnet IDs for ALB
- private_subnet_ids: list of private subnet IDs for tasks
- api_image: ECR URI, e.g. 123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/weather-wizard/api:latest
- desired_count: 1+
- enable_https: true/false
- acm_certificate_arn: required if enable_https=true

## Usage
```bash
export AWS_REGION=ap-southeast-2
cd infra/ecs_api
terraform init
terraform apply \
  -var="aws_region=$AWS_REGION" \
  -var="project=weather-wizard" \
  -var="env=dev" \
  -var='vpc_id=vpc-xxxxxxxx' \
  -var='public_subnet_ids=["subnet-aaaa","subnet-bbbb"]' \
  -var='private_subnet_ids=["subnet-cccc","subnet-dddd"]' \
  -var='api_image=123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/weather-wizard/api:latest'
```

Outputs:
•alb_dns_name — hit http://alb_dns_name/health to verify

To enable HTTPS:
•Request/validate an ACM cert in the same region.
•Set enable_https=true and provide the acm_certificate_arn.
•Point your domain (Route53) at the ALB DNS with an Alias record.
