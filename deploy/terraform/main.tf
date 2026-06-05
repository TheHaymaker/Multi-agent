# Plan B — Google Cloud deployment (the Dev Signal Part 4 mirror).
#
# The agents/, tools/, and PolicyGuard code is IDENTICAL to Plan A; only the service bindings
# change (see overnight_eng/runtime.py): Vertex Memory Bank instead of Mem0/pgvector, Vertex
# Session Service instead of Postgres, Cloud Trace instead of Langfuse, Secret Manager instead
# of SOPS. Because Cloud Run is request/event-driven, the overnight loop moves to
# Cloud Scheduler -> Pub/Sub -> this service (the one structural change vs. local).

terraform {
  required_providers {
    google = { source = "hashicorp/google", version = "~> 5.0" }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_project_service" "services" {
  for_each = toset([
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "aiplatform.googleapis.com",          # Vertex (Claude via Model Garden + Memory Bank)
    "secretmanager.googleapis.com",
    "cloudscheduler.googleapis.com",      # nightly trigger
    "pubsub.googleapis.com",              # event fan-in
    "logging.googleapis.com",
    "cloudtrace.googleapis.com",          # observability (replaces Langfuse)
  ])
  service            = each.key
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "repo" {
  location      = var.region
  repository_id = "overnight-eng-repo"
  format        = "DOCKER"
  depends_on    = [google_project_service.services]
}

# Least-privilege service account (principle of least privilege, like Dev Signal).
resource "google_service_account" "agent_sa" {
  account_id   = "${var.service_name}-sa"
  display_name = "Overnight Engineering agent"
}

locals {
  agent_roles = [
    "roles/aiplatform.user",
    "roles/secretmanager.secretAccessor",
    "roles/logging.logWriter",
    "roles/cloudtrace.agent",
    "roles/pubsub.subscriber",
  ]
}

resource "google_project_iam_member" "agent_roles" {
  for_each = toset(local.agent_roles)
  project  = var.project_id
  role     = each.key
  member   = "serviceAccount:${google_service_account.agent_sa.email}"
}

resource "google_secret_manager_secret" "secrets" {
  for_each  = toset(keys(var.secrets))
  secret_id = each.key
  replication { auto {} }
  depends_on = [google_project_service.services]
}

resource "google_secret_manager_secret_version" "secret_versions" {
  for_each    = toset(keys(var.secrets))
  secret      = google_secret_manager_secret.secrets[each.key].id
  secret_data = var.secrets[each.key]
}

resource "google_cloud_run_v2_service" "default" {
  name     = var.service_name
  location = var.region
  template {
    service_account = google_service_account.agent_sa.email
    containers {
      image = var.image
      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "True"
      }
      resources { limits = { cpu = "1", memory = "2Gi" } }
    }
  }
  depends_on = [google_project_iam_member.agent_roles]
}

# Overnight loop: Cloud Scheduler -> Pub/Sub -> Cloud Run (replaces the local daemon).
resource "google_pubsub_topic" "sweeps" {
  name = "overnight-sweeps"
}

resource "google_cloud_scheduler_job" "nightly" {
  name      = "overnight-nightly"
  schedule  = "0 6 * * *"
  time_zone = var.time_zone
  pubsub_target {
    topic_name = google_pubsub_topic.sweeps.id
    data       = base64encode("{\"job\":\"nightly_sweep\"}")
  }
}
