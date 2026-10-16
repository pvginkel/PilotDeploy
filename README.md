# PilotDeploy

Pilot's deploy repository: its Helm chart and per-stage configuration. The app is
`pvginkel/PilotApp`.

**Rotation keepers** are the `rotation_epoch` map in `config/prd/rotation.tfvars`, which
`terraform/main.tf` passes to the credentials it mints: `db` re-mints the Postgres role's password.
SecretRotator's `terraform` kind writes it, one commit `rotate <keeper> (secret-rotator)` per
rotation, and a changed value re-mints its credential at the next sync. A keeper once set stays in
the file: removing it re-mints too.
