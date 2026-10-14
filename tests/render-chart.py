#!/usr/bin/env python3
"""The gate: the pilot chart renders prd the way Argo CD renders it.

The stage is templated the way ArgoCDDeploy's registry has Argo produce it
(releases/templates/applications.yaml): `chart` under the Application's own name `pilot-prd`,
into that namespace, with `../config/prd/values.yaml` and the four `hook.*` helm parameters of
`releases.hookParameters` as `--set`. The library dependency is built from chart/Chart.lock, as
the repo-server builds it.

Run `cexec aac-tools chart-deps` first; the render needs chart/charts/ populated.
"""

import pathlib
import re
import subprocess
import sys
import tarfile
import time

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CHART = REPO / "chart"
STAGE = "prd"
# The Application's name and its destination namespace, one string (argo-cd D24).
NAMESPACE = f"pilot-{STAGE}"
# The architecture producer's id is the chart's name plus `-deploy`.
CHART_NAME = "pilot"
# Exact (D17), from the chart repository Argo's repo-server resolves it from.
LIBRARY = {"name": "homelab-shared", "version": "0.6.0", "repository": "https://charts.home"}
DEFINE = re.compile(r"\{\{-?\s*define\b")
# The registry entry's `repo:` form, and a SHA of the shape `$ARGOCD_APP_REVISION` becomes.
HOOK_REPO = "https://github.com/pvginkel/PilotDeploy.git"
HOOK_REVISION = "0123456789abcdef0123456789abcdef01234567"
# Where the library's Job template puts the hook (homelab-shared templates/_tf-presync-hook.tpl).
HOOK_NAMESPACE = "argocd-hooks"
HOOK_GUARD = "is required by the Terraform PreSync hook"
# The image slots of config/prd/values.yaml. PilotApp's build writes the first two
# (cicd.writeVersionPins, its Jenkinsfile's "Write image pins"); the gateway's is set by hand.
PINS = ("images.pilot", "images.pilotUI", "images.sseGateway")
# A registry:5000 image's per-build tag, as the pin step writes it.
TAG_PIN = re.compile(r":[0-9]+")
# A key line as cicd.writeVersionPins matches it (JenkinsPipelineUtils vars/cicd.groovy, applyPins).
KEY_LINE = re.compile(r" *([A-Za-z0-9_.\-]+): *(.*)")
# The one cluster-scoped kind the chart renders, which the releases AppProject's
# clusterResourceWhitelist carries (ArgoCDDeploy chart/templates/appproject.yaml).
CLUSTER_SCOPED = {("", "Namespace")}
# Namespaced kinds, as (group, kind). A render cannot tell a kind's scope, so a kind in neither
# set fails the gate until it is classified.
NAMESPACED = {
    ("", "ConfigMap"),
    ("batch", "Job"),
    ("rbac.authorization.k8s.io", "RoleBinding"),
}

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def hook_parameters():
    # In the library's argument order: repo, revision, stage, namespace.
    return {
        "hook.repo": HOOK_REPO,
        "hook.revision": HOOK_REVISION,
        "hook.stage": STAGE,
        "hook.namespace": NAMESPACE,
    }


def template(parameters):
    args = [
        "helm", "template", NAMESPACE, "chart",
        "--namespace", NAMESPACE,
        "--values", f"config/{STAGE}/values.yaml",
    ]
    for name, value in parameters.items():
        args += ["--set", f"{name}={value}"]
    return subprocess.run(args, cwd=REPO, capture_output=True, text=True)


def render():
    result = template(hook_parameters())
    if result.returncode != 0:
        sys.exit(f"helm template failed:\n{result.stderr}")
    return result.stdout


def objects(stdout):
    return [doc for doc in yaml.safe_load_all(stdout) if doc]


def find(docs, kind, name):
    for doc in docs:
        if doc["kind"] == kind and doc["metadata"].get("name") == name:
            return doc
    return None


def label(doc):
    return f"{doc['kind']}/{doc['metadata'].get('name')}"


def values_text():
    return (REPO / "config" / STAGE / "values.yaml").read_text()


def library_values():
    """The built library's own values.yaml, as chart-deps unpacked it from chart/Chart.lock."""
    archive = CHART / "charts" / f"{LIBRARY['name']}-{LIBRARY['version']}.tgz"
    with tarfile.open(archive) as tar:
        return yaml.safe_load(tar.extractfile(f"{LIBRARY['name']}/values.yaml"))


def pin_paths(text):
    """{dotted path: [line numbers]} of every key line cicd.writeVersionPins can rewrite: a
    block-style `key: value` line, its path the keys of the block lines above it. Comments,
    blank lines, block-scalar bodies and anything that is not a key line carry no path."""
    found = {}
    keys = []
    indents = []
    # Indent of an open block scalar (`key: |`); -1 when none is open.
    literal = -1
    for number, line in enumerate(text.split("\n"), start=1):
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if literal >= 0:
            if indent > literal:
                continue
            literal = -1
        match = KEY_LINE.fullmatch(line)
        if match is None:
            continue
        key, rest = match.groups()
        while indents and indents[-1] >= indent:
            indents.pop()
            keys.pop()
        if not rest or rest.startswith("#"):
            keys.append(key)
            indents.append(indent)
            continue
        if rest.startswith(("|", ">")):
            literal = indent
            continue
        found.setdefault(".".join([*keys, key]), []).append(number)
    return found


def check_library():
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    check(
        chart.get("name") == CHART_NAME,
        f"chart/Chart.yaml names the chart {chart.get('name')!r}, not {CHART_NAME!r}, from which "
        "the producer id pilot-deploy derives",
    )
    check(
        chart.get("dependencies") == [LIBRARY],
        f"chart/Chart.yaml does not pin exactly {LIBRARY}: {chart.get('dependencies')}",
    )
    links = sorted(str(p.relative_to(REPO)) for p in CHART.rglob("*") if p.is_symlink())
    check(not links, f"symlinks under chart/, which Argo's repo-server refuses: {links}")
    defines = sorted(
        p.name for p in (CHART / "templates").iterdir() if DEFINE.search(p.read_text())
    )
    check(not defines, f"templates define helpers the library chart provides: {defines}")
    tracked = subprocess.run(
        ["git", "ls-files", "chart/charts"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    check(not tracked, f"the built dependency is committed: {tracked}")


def check_chart_values():
    # A chart's own values.yaml is optional to Helm.
    path = CHART / "values.yaml"
    values = (yaml.safe_load(path.read_text()) if path.exists() else None) or {}
    check(
        "images" not in values,
        f"chart/values.yaml names images {values.get('images')}; the pins live in "
        f"config/{STAGE}/values.yaml alone, with no chart default behind them",
    )


def check_pin_slots():
    paths = pin_paths(values_text())
    images = yaml.safe_load(values_text()).get("images") or {}
    for pin in PINS:
        lines = paths.get(pin, [])
        check(
            len(lines) == 1,
            f"config/{STAGE}/values.yaml holds {pin} on lines {lines}, not on exactly one "
            "block-style key line, which cicd.writeVersionPins edits in place and never creates",
        )
        value = images.get(pin.removeprefix("images."))
        check(
            isinstance(value, str) and TAG_PIN.fullmatch(value) is not None,
            f"config/{STAGE}/values.yaml pins {pin} to {value!r}, not a tag suffix ':<build>'",
        )


def check_namespace_manifest(docs):
    names = [doc["metadata"]["name"] for doc in docs if doc["kind"] == "Namespace"]
    check(names == [NAMESPACE], f"renders Namespaces {names}, not the one {NAMESPACE} (D25)")
    manifest = find(docs, "Namespace", NAMESPACE)
    if manifest is None:
        return
    annotations = manifest["metadata"].get("annotations") or {}
    check(
        annotations.get("argocd.argoproj.io/sync-wave") == "-1",
        f"Namespace/{NAMESPACE} is missing sync-wave -1 (D25)",
    )
    check(
        annotations.get("argocd.argoproj.io/sync-options") == "Prune=false",
        f"Namespace/{NAMESPACE} is missing sync-options Prune=false (D26)",
    )


def check_hook(docs):
    jobs = [doc for doc in docs if doc["kind"] == "Job"]
    check(len(jobs) == 1, f"renders {len(jobs)} Jobs, not the one PreSync hook")
    if len(jobs) != 1:
        return
    metadata = jobs[0]["metadata"]
    check(
        metadata.get("name") == f"tf-presync-{NAMESPACE}"
        and metadata.get("namespace") == HOOK_NAMESPACE,
        f"the hook Job is {label(jobs[0])} in {metadata.get('namespace')}, "
        f"not tf-presync-{NAMESPACE} in {HOOK_NAMESPACE}",
    )
    check(
        (metadata.get("annotations") or {}).get("argocd.argoproj.io/hook") == "PreSync",
        "the hook Job is not a PreSync hook",
    )
    containers = jobs[0]["spec"]["template"]["spec"]["containers"]
    # hook.path is unset: the repo's root is the app, so the Job passes exactly four arguments.
    args = [c.get("args") for c in containers]
    expected = list(hook_parameters().values())
    check(args == [expected], f"the hook Job's args are {args}, not {expected}")
    image = f"registry:5000/argocd-hook:{library_values()['hook']['imageTag']}"
    check(
        [c.get("image") for c in containers] == [image],
        f"the hook Job runs {[c.get('image') for c in containers]}, not the library's estate-wide "
        f"pin {image}",
    )

    binding = find(docs, "RoleBinding", "tf-presync")
    check(
        binding is not None and binding["metadata"].get("namespace") == NAMESPACE,
        f"no RoleBinding/tf-presync in {NAMESPACE}, the hook's grant there",
    )
    revision = find(docs, "ConfigMap", "tf-presync-revision")
    check(
        revision is not None
        and revision["metadata"].get("namespace") == NAMESPACE
        and revision["data"] == {"revision": HOOK_REVISION},
        f"no ConfigMap/tf-presync-revision in {NAMESPACE} carrying the synced revision (D67)",
    )

    for omitted in hook_parameters():
        parameters = {k: v for k, v in hook_parameters().items() if k != omitted}
        result = template(parameters)
        check(
            result.returncode != 0 and f"{omitted} {HOOK_GUARD}" in result.stderr,
            f"rendering without {omitted} does not fail on the library's guard",
        )


def check_scope(docs):
    for doc in docs:
        api_version = doc["apiVersion"]
        group_kind = (api_version.split("/")[0] if "/" in api_version else "", doc["kind"])
        if group_kind in CLUSTER_SCOPED:
            continue
        check(
            group_kind in NAMESPACED,
            f"{label(doc)} is a kind this gate has not classified; a cluster-scoped kind "
            "needs the releases AppProject to whitelist it first",
        )
        explicit = doc["metadata"].get("namespace")
        check(
            explicit in (None, HOOK_NAMESPACE, NAMESPACE),
            f"{label(doc)} renders into {explicit}, outside the Application's destination",
        )


def check_render_stable(first):
    # `now` renders to the second, so a render more than a second later differs if any value
    # reads the clock; one reading randomness differs on any second render.
    time.sleep(1.1)
    check(render() == first, "two renders of the same commit differ")


def main():
    check_library()
    check_chart_values()
    check_pin_slots()
    first = render()
    docs = objects(first)
    check_namespace_manifest(docs)
    check_hook(docs)
    check_scope(docs)
    check_render_stable(first)

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        sys.exit(1)
    print(f"ok: {STAGE} renders as Argo renders it ({len(docs)} objects)")


main()
