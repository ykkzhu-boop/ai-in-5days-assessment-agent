variable "project_id" {
  type        = string
  description = "The Google Cloud Project ID where resources will be provisioned."
  default     = "ai-in-5days-assessment"
}

variable "region" {
  type        = string
  description = "The Google Cloud region to provision resources."
  default     = "us-central1"
}

variable "service_name" {
  type        = string
  description = "The name of the Cloud Run service to deploy."
  default     = "ambient-expense-agent"
}

variable "artifact_repository_name" {
  type        = string
  description = "The name of the Artifact Registry repository."
  default     = "agent-docker-repo"
}
