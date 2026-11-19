# PilotDeploy

Pilot's deploy repository: its Helm chart and per-stage configuration. The app is
`pvginkel/PilotApp`.

**Two stages**, each an Argo CD Application on the prd cluster that renders `chart/` with
`config/<stage>/`: `pilot-dev` at `https://pilot-dev.home`, internal, with its own Keycloak client,
database (`pilot_dev_db`) and secrets, on `kubecoder-dev`'s fleet, follows `main`; and `pilot-prd`,
production at `https://pilot.ginbov.nl`, public, on the operator's KubeCoder, follows the `prd`
branch. Every green PilotApp build pins `:dev-<n>` into `config/dev/` and `:prd-<n>` into
`config/prd/` in one commit on `main`, so dev rolls to it and production waits for a promotion: the
job `PilotApp/Promote-PRD` (`Jenkinsfile.promote`), started by hand, retags `pilot`, `pilot-ui`,
`pilot-collector` and `pilot-sync` from `dev-<n>` to `prd-<n>`, fast-forwards `prd` to a commit on
`main`, its tip unless the run names another, moves `pilot-sync:prd-latest`, which production's
store environment runs, to that build's sidecar, and tags that commit `release-<m>`. A commit whose
`images.collector` is empty or absent has no collector retagged. A rollback is a revert on `main`,
promoted. prd's Terraform state owns the repository's webhook (`manage_webhook`), dev's none.

**The pod** runs the backend, Pilot's MCP server and the UI's nginx. The MCP server is the backend
image's second entry point (`python -m pilot_mcp`), the container `mcp`, calling the backend over
the pod's loopback, behind a Service of its own, `pilot-mcp`, at the stage's `mcp.host` in
`config/<stage>/values.yaml`: `pilot-dev-mcp.home` or `pilot-mcp.home`, intranet only on both
stages, since a bearer is a caller's whole authentication. The backend hands a work item's session
`https://<mcp.host>/mcp` (`MCP_SERVER_URL`), and a brain session the brain's tools under it,
`…/mcp/store`; under it too is Pilot's projects surface, `…/mcp/projects`, which any session of
KubeCoder's production fleet calls with the surface's shared token.

**The projects surface's settings** are the `mcp` container's alone. Its shared token,
`PILOT_MCP_PROJECTS_TOKEN`, comes from the Secret `pilot-projects-token`, key `token`, which an
ExternalSecret of the same name fills from the one leaf both stages read,
`eso/prd/pilot/projects-token`, property `token`, under neither stage's tree, since a session
pointed at dev's surface sends what it sends production's. The stage's service user, the Keycloak
client whose client-credentials token the server calls the backend's `/api/` routes under, comes
from the Secret `pilot-service-user`, keys `client_id` and `client_secret`, filled from the stage's
own leaf, `eso/prd/pilot/<stage>/service-user`, as `PILOT_MCP_SERVICE_CLIENT_ID` and
`PILOT_MCP_SERVICE_CLIENT_SECRET`. `PILOT_MCP_TOKEN_URL` is a plain value, built in the chart from
the stage's `oidc.issuerUrl` as `<issuer>/protocol/openid-connect/token`, so the endpoint follows
the issuer whose tokens the backend admits. Every reference to the two Secrets is optional and each
leaf has an ExternalSecret of its own: a missing leaf leaves the pod starting and refuses the
surface alone, and an `mcp` started before its Secrets exist refuses every call to the surface
until its next start. `tests/render-chart.py` pins the four on both stages' `mcp` and on no other
container, both Secrets' references optional, each stage's service-user leaf its own, and the
shared leaf as no stage's.

**The collector** is a second pod on each stage, the Deployment `pilot-collector`
(`chart/templates/collector.yaml`), running the image `registry:5000/pilot-collector` under its own
command: one process that walks the transcripts on the stage's fleet's home, publishes them to the
backend, and serves a conversation's reading. It runs on the node that holds both fleets' homes,
the one labelled `homelab.local/storage: zpool5`, whose taint `homelab.local/performance=high` it
tolerates, one pod at a time: the Deployment's strategy is `Recreate`, so that a roll never runs
two walks. `collector.home` in `config/<stage>/values.yaml` names the stage's fleet's home on that
node, `/zpool5/kubecoder/system/home` for production and `/zpool5/kubecoder-dev/system/home` for
dev, and the pod mounts two folders of it read-only, `.claude/projects` and `.claude/plugins`, at
`/home/ubuntu/.claude/projects` and `/home/ubuntu/.claude/plugins`, where the plugins' install paths
point; never `.claude` whole, no other volume, and no service account token. Each is a host
path of type `Directory`, so a folder the node lacks keeps the pod from starting where the kubelet
would otherwise make it. The collector's three settings are `COLLECTOR_HOME`,
`/home/ubuntu/.claude`; `COLLECTOR_PILOT_URL`, `http://pilot`, the Service in front of the UI's
nginx, which proxies `/api/` to the backend; and `COLLECTOR_TOKEN`, its bearer.

**The collector's bearer** is generated in the cluster and placed by nobody: the Secret
`pilot-collector-token`, key `password`, a third beside `pilot-secret-key` and `pilot-drain-auth`
in `chart/templates/generated-secrets.yaml`. The collector and the backend both read it as
`COLLECTOR_TOKEN`, and neither read is optional. The backend reaches the collector's readings
through the Service `pilot-collector`, port 3503, which carries none of the front door's
annotations: nothing outside the cluster reaches it. The collector's probes are TCP probes of that
port, since every path there asks for the bearer. Its pod's label, `app.kubernetes.io/name:
pilot-collector`, keeps it out of the Services `pilot` and `pilot-mcp`.

**The collector's pin**, `images.collector`, decides whether a stage has a collector. While it is
the empty string the chart renders neither the Deployment nor the Service and hands the backend no
`COLLECTOR_TOKEN`, so the backend refuses every call of a collector's and its readiness reads the
collector as disabled. The PilotApp build pins it with the two others, and production's pin takes
effect with the promotion that retags the image. `tests/render-chart.py` renders each stage with
the pin set and with it empty, whatever the stage's values hold, and pins all of the above: the
node, the strategy, the two mounts off the stage's own fleet's home, the settings, the bearer on
the collector and the backend alone, the probes, the Service, and no stage's values naming another
stage's home. `tests/architecture-test.py` expects the collector's workload in a stage's artifact
exactly where that stage's values pin it.

**Each stage's store** is a KubeCoder environment on its fleet whose `pilot-sync` sidecar the
backend admits: `store.environmentId` in `config/<stage>/values.yaml` sets `STORE_ENVIRONMENT_ID`,
and the sidecar's bearer, `SYNC_TOKEN`, comes from the leaf `eso/prd/pilot/<stage>/sync`, property
`token`. The bearer's Secret is optional: a backend started while it is absent refuses every sync
call until its next start. The brain's sessions there run on `brain` in the same values: `model`,
`reasoningEffort` and `chatCloseTimeout` set the backend's `BRAIN_MODEL`, `BRAIN_REASONING_EFFORT`
and `CHAT_CLOSE_TIMEOUT`, each required, so that a stage without them fails its render, and
`tests/render-chart.py` pins them on both stages' backend and their absence from `migrate`.

**The gate's slot**, how many work items run at once, each in an environment of its own, is set in
`work` in the same values: `parallelismMax` across every project and `projectParallelismMax` in one
set the backend's `WORK_PARALLELISM_MAX` and `PROJECT_PARALLELISM_MAX`. Each is optional: one left
out is not set at all, and the backend's default stands, 3 across every project and in one project
the cross-project maximum. Production runs 5 and 3; the dev stage sets neither.
`tests/render-chart.py` pins each stage's backend on what its values set and `migrate` on neither.

**The stage's VAPID key**, which signs every web push the backend sends and whose public key the
browsers subscribe with, comes from the leaf `eso/prd/pilot/<stage>/vapid`, property
`private_key`, a P-256 key in PKCS#8 PEM: the ExternalSecret `pilot-vapid` fills the Secret
`pilot-vapid`, key `private_key`, with the leaf's PEM as it stands, through no template, and the
backend reads it as `VAPID_PRIVATE_KEY`. It is kept in OpenBao and never generated in the cluster,
since a new key ends every browser's subscription. The read is required, since a production backend
refuses to start without the key, and no other container, `migrate` among them, gets it;
`tests/render-chart.py` pins it on both stages: the backend alone holding it, off its stage's leaf,
and its Secret untemplated.

**YouTrack's webhook token** is production's alone, since the dev stage gets no tracker events: an
ExternalSecret fills the Secret `pilot-youtrack-webhook-token`, key `token`, from the leaf
`eso/prd/pilot/prd/youtrack-webhook-token`, property `token`, a copy of the instance's shared
webhook token that SecretRotator keeps. The backend reads it as `YOUTRACK_WEBHOOK_TOKEN` and compares
it with each delivery's `X-YouTrack-Token` on its tracker-events endpoint. The chart sets the variable
only where a stage's values carry the Secret, so the dev stage's backend refuses every delivery. The
Secret is optional: a backend started while it is absent refuses every delivery until its next start.
`tests/render-chart.py` pins it on production's backend and its absence from `migrate` and from the
dev stage.

**The read-only role** is production's alone: `readonly_role` in `config/prd/terraform.tfvars` has
`terraform/main.tf` make the login role `pilot_prd_readonly` on `pilot_prd_db`
(`terraform/modules/postgres-readonly-role`), with CONNECT on the database, USAGE on its schema
`public`, SELECT on every table and sequence in it, and default privileges that give it SELECT on
those `pilot_prd_db`, the role the migrations run as, creates later. It writes and creates nothing.
Its credentials go into the Secret `pilot-db-readonly` (keys `database`, `username`, `password`),
which the chart does not read: the operator's ad-hoc tools reach the database with it, through a
copy of its password in KubeCoder's prd catalog leaf `eso/prd/kubecoder/prd/catalog`, key
`pilot-prd-db-readonly-password`, which PilotApp's environment projects as
`PILOTAPP_PRD_DB_PASSWORD`. The hook holds no OpenBao credential, so the copy is made from the
Secret outside Terraform, from an environment that can write OpenBao, and no rotation renews it.
`tests/terraform-test.py` pins the role, its grants and its Secret on prd, and their absence from
dev.

**Rotation keepers** are the `rotation_epoch` map in each stage's `config/<stage>/rotation.tfvars`,
which `terraform/main.tf` passes to the credentials it mints: `db` re-mints the Postgres role's
password, and `readonly`, on prd, the read-only role's, after which the catalog's copy is written
again. SecretRotator's `terraform` kind writes it, one commit `rotate <keeper> (secret-rotator)`
per rotation, and a changed value re-mints its credential at the next sync. A keeper once set stays
in the file: removing it re-mints too.
