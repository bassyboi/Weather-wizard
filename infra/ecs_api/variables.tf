variable "aws_region"      { type = string }
variable "project"         { type = string }
variable "env"             { type = string }
variable "vpc_id"          { type = string }
variable "public_subnet_ids" {
  type = list(string)
  description = "2+ public subnets for ALB"
}
variable "private_subnet_ids" {
  type = list(string)
  description = "2+ private subnets for Fargate tasks"
}
variable "api_image" {
  type = string
  description = "ECR image URI for API, e.g. 123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/weather-wizard/api:latest"
}
variable "desired_count"   { type = number  default = 1 }
variable "cpu"             { type = number  default = 256 } # 0.25 vCPU
variable "memory"          { type = number  default = 512 } # 0.5 GB
variable "container_port"  { type = number  default = 8000 }
variable "health_path"     { type = string  default = "/health" }
variable "enable_https"    { type = bool    default = false }
variable "acm_certificate_arn" { type = string  default = "" } # used when enable_https=true
variable "allow_ingress_cidrs" {
  type = list(string)
  default = ["0.0.0.0/0"]  # lock down later
}
variable "data_dir"        { type = string  default = "/data" } # example env
