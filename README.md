# PilotDeploy

Pilot's deploy repository: its Helm chart and per-stage configuration. The app is
`pvginkel/PilotApp`.

**Two stages**, each an Argo CD Application on the prd cluster that renders `chart/` with
`config/<stage>/`: `pilot-dev` at `https://pilot-dev.home`, internal, with its own Keycloak client,
database (`pilot_dev_db`) and secrets, on `kubecoder-dev`'s fleet, follows `main`; and `pilot-prd`,
production at `https://pilot.ginbov.nl`, public, on the operator's KubeCoder, follows the `prd`
branch. Every green PilotApp build pins `:dev-<n>` into `config/dev/` and `:prd-<n>` into
`config/prd/` in one commit on `main`, so dev rolls to it and production waits for a promotion: the
job `PilotApp/Promote-PRD` (`Jenkinsfile.promote`), started by hand, retags `pilot`, `pilot-ui` and
`pilot-sync` from `dev-<n>` to `prd-<n>`, fast-forwards `prd` to a commit on `main`, its tip unless
the run names another, moves `pilot-sync:prd-latest`, which production's store environment runs, to
that build's sidecar, and tags that commit `release-<m>`. A rollback is a revert on `main`,
promoted. prd's Terraform state owns the repository's webhook (`manage_webhook`), dev's none.

**The pod** runs the backend, Pilot's MCP server and the UI's nginx. The MCP server is the backend
image's second entry point (`python -m pilot_mcp`), the container `mcp`, calling the backend over
the pod's loopback, behind a Service of its own, `pilot-mcp`, at the stage's `mcp.host` in
`config/<stage>/values.yaml`: `pilot-dev-mcp.home` or `pilot-mcp.home`, intranet only on both
stages, since the session's bearer is its whole authentication. The backend hands a work item's
session `https://<mcp.host>/mcp` (`MCP_SERVER_URL`), and a brain session the brain's tools under
it, `…/mcp/store`.

**Each stage's store** is a KubeCoder environment on its fleet whose `pilot-sync` sidecar the
backend admits: `store.environmentId` in `config/<stage>/values.yaml` sets `STORE_ENVIRONMENT_ID`,
and the sidecar's bearer, `SYNC_TOKEN`, comes from the leaf `eso/prd/pilot/<stage>/sync`, property
`token`. The bearer's Secret is optional: a backend started while it is absent refuses every sync
call until its next start. The brain's sessions there run on `brain` in the same values: `model`,
`reasoningEffort` and `chatCloseTimeout` set the backend's `BRAIN_MODEL`, `BRAIN_REASONING_EFFORT`
and `CHAT_CLOSE_TIMEOUT`, each required, so that a stage without them fails its render, and
`tests/render-chart.py` pins them on both stages' backend and their absence from `migrate`.

**The notification bridge** is `notificationBridge.url` in the same values, the backend's
`NOTIFICATION_BRIDGE_URL`: `http://notification-bridge.notifications-prd.svc:8090` on both stages,
the bridge's in-cluster Service on the prd cluster, through which the backend notifies each person
under the token they paste on their profile. It is required, since a production backend refuses to
start without it, so that a stage without it fails its render, and `migrate` gets none;
`tests/render-chart.py` pins it on both stages' backend, its absence from `migrate`, and the render's
failure without it. The people's registrations are made for production, and the dev stage has none
of its own (`config/dev/values.yaml` says what a token saved there does).

**Rotation keepers** are the `rotation_epoch` map in each stage's `config/<stage>/rotation.tfvars`,
which `terraform/main.tf` passes to the credentials it mints: `db` re-mints the Postgres role's
password. SecretRotator's `terraform` kind writes it, one commit `rotate <keeper> (secret-rotator)`
per rotation, and a changed value re-mints its credential at the next sync. A keeper once set stays
in the file: removing it re-mints too.
