# Terraform (minimal)

This creates the API image repository and a versioned data bucket with all public
access blocked, bucket-owner-enforced object ownership, and default SSE-S3 encryption.
Retention/object-lock and lifecycle policy are operational choices and are not
configured by this module.

Usage:

```bash
  export AWS_REGION=ap-southeast-2
  cd infra/aws
  terraform init
  terraform apply -var="aws_region=$AWS_REGION" -var="project=weather-wizard" -var="env=dev"
```

The live recorder additionally needs a GitHub OIDC role scoped to `PutObject` and
`GetObject` on its exact archive prefix. Create that role deliberately for the real
repository/default branch; this module does not infer an account trust policy.
