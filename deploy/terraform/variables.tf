variable "project_id" {
  description = "Google Cloud project ID"
  type        = string
}

variable "region" {
  description = "Region for Cloud Run / Artifact Registry"
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  type    = string
  default = "overnight-eng"
}

variable "image" {
  description = "Container image (Artifact Registry path) to deploy"
  type        = string
}

variable "time_zone" {
  description = "IANA tz for the nightly digest schedule"
  type        = string
  default     = "Etc/UTC"
}

variable "secrets" {
  description = "Secret name -> value map (ANTHROPIC_API_KEY, GITHUB_TOKEN, SENTRY_AUTH_TOKEN, ...)"
  type        = map(string)
  default     = {}
  sensitive   = true
}
