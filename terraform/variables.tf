# Every variable has a default, because a Destroy Stage run (ArgoCDTools presync.destroy) plans
# against these declarations with TF_VAR_stage alone. A sync supplies every one.

# From the hook's environment.

variable "namespace" {
  description = "The Application's destination namespace, pilot-<stage> (TF_VAR_namespace). The chart creates it; nothing here does."
  type        = string
  default     = ""
}

variable "postgres_admin_host" {
  description = "The Postgres substrate's read-write endpoint, which the postgresql provider administers."
  type        = string
  default     = ""
}

variable "postgres_admin_password" {
  description = "The terraform_admin role's password."
  type        = string
  sensitive   = true
  default     = ""
}

variable "github_webhook_secret" {
  description = "Argo CD's shared webhook secret, the one the relay verifies deliveries against."
  type        = string
  sensitive   = true
  default     = ""
}

# From config/<stage>/*.tfvars.

variable "manage_webhook" {
  description = "Whether this stage's state owns the repository's GitHub webhook. True in exactly one stage: the webhook is per repository and the state per stage, so a second owner's apply fails on GitHub's hook-already-exists."
  type        = bool
  default     = false
}
