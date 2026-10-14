# Applied only by the Argo CD PreSync hook (ArgoCDTools argocd-hook/presync), from its own clone.
# It fills the empty http backend at init, exports its environment (TF_VAR_*, GITHUB_TOKEN,
# KUBE_CONFIG_PATH) and passes config/<stage>/*.tfvars at apply.

terraform {
  required_providers {
    github = {
      source = "integrations/github"
    }
    kubernetes = {
      source = "hashicorp/kubernetes"
    }
    postgresql = {
      source = "cyrilgdn/postgresql"
    }
    random = {
      source = "hashicorp/random"
    }
  }

  backend "http" {}
}

# Follows KUBE_CONFIG_PATH, a kubeconfig the hook mints from its own ServiceAccount.
provider "kubernetes" {}

# The Postgres substrate's terraform_admin role (CREATEDB CREATEROLE, not superuser), against the
# CNPG primary: CREATE DATABASE cannot run through the PgBouncer pooler.
provider "postgresql" {
  host      = var.postgres_admin_host
  port      = 5432
  username  = "terraform_admin"
  password  = var.postgres_admin_password
  sslmode   = "require"
  superuser = false
}

# Authenticates with GITHUB_TOKEN.
provider "github" {
  owner = "pvginkel"
}
