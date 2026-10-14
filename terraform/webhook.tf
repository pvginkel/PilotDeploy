# This repository's push deliveries, to the webhook relay, which verifies the signature and
# forwards each delivery to argocd-server (argo-cd D39, D49).
resource "github_repository_webhook" "argocd" {
  count = var.manage_webhook ? 1 : 0

  repository = "PilotDeploy"
  active     = true
  events     = ["push"]

  configuration {
    url          = "https://deploy-hooks.webathome.org/api/webhook"
    content_type = "json"
    insecure_ssl = false
    secret       = var.github_webhook_secret
  }
}
