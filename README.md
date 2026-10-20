# PilotDeploy

Pilot's deploy repository: its Helm chart and per-stage configuration. The app is
`pvginkel/PilotApp`.

**Two stages**, each an Argo CD Application on the prd cluster that renders `chart/` with
`config/<stage>/`: `pilot-prd`, production at `https://pilot.ginbov.nl`, public, on the operator's
KubeCoder; and `pilot-dev` at `https://pilot-dev.home`, internal, with its own Keycloak client,
database (`pilot_dev_db`) and secrets, on `kubecoder-dev`'s fleet. prd's Terraform state owns the
repository's webhook (`manage_webhook`), dev's none.

**Rotation keepers** are the `rotation_epoch` map in each stage's `config/<stage>/rotation.tfvars`,
which `terraform/main.tf` passes to the credentials it mints: `db` re-mints the Postgres role's
password. SecretRotator's `terraform` kind writes it, one commit `rotate <keeper> (secret-rotator)`
per rotation, and a changed value re-mints its credential at the next sync. A keeper once set stays
in the file: removing it re-mints too.
