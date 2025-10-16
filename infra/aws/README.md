# Terraform (minimal)
Usage:
  export AWS_REGION=ap-southeast-2
  cd infra/aws
  terraform init
  terraform apply -var="aws_region=$AWS_REGION" -var="project=weather-wizard" -var="env=dev"
