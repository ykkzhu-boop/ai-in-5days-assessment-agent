output "service_url" {
  value       = google_cloud_run_v2_service.agent_service.uri
  description = "The public URL of the deployed Ambient Expense Agent service."
}

output "repository_url" {
  value       = google_artifact_registry_repository.repo.url
  description = "The container repository URL in Artifact Registry."
}
