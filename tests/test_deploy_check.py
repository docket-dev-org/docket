"""The deploy checker has to be able to *fail*, or it is decoration.

There is no container runtime and no cluster here, so `deploy/` is checked statically by
`tools/deploy_check.py`. That makes the checker part of the evidence, and it puts it under the same
standard as everything else: a tool that cannot fail proves nothing. Two kinds of test below:

* **the checks pass on the real tree**, and the run is not vacuous (there are checks, and they read
  real documents);
* **each check fails on a broken copy of the tree**, one fixture per check — a privileged pod, an
  unpinned image, a probe on a port nothing declares, a policy Docket would refuse to load, a
  template construct the renderer does not implement. Each fixture asserts its own edit landed, so a
  fixture cannot silently stop testing anything when a file is refactored.

The renderer is tested directly as well: it implements a *subset* of Helm's language, and the
property that makes that acceptable is that it refuses anything outside the subset by name rather
than rendering around it.

The checker reads YAML with PyYAML, which is a dependency of the *tool* and not of `docket`. That
makes it optional, and optional in this repository has a defined meaning: the tests that need it
**skip with the reason**, the way the Ed25519 tests skip without `cryptography`, rather than failing
and making a bare clone look broken. The one test that must run either way is the last one in the
file: with the import shadowed, the checker has to exit 2 with a reason, because a check that
cannot run must never be readable as a check that passed.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "tools" / "deploy_check.py"

#: Why the checker cannot run here, or `None` if it can. Kept separate from the fixture so the
#: reason is stated once and a reader of the skip line learns what to install.
YAML_MISSING = (
    None
    if importlib.util.find_spec("yaml") is not None
    else (
        "needs PyYAML, which tools/deploy_check.py reads the manifests with. The checker exits 2 "
        "with that reason rather than passing, so there is nothing here to assert: "
        "`pip install pyyaml` (or `pip install -e '.[deploy]'`) and re-run"
    )
)


def _load():
    """Load the checker as a module.

    It is registered in `sys.modules` before execution because `@dataclass` looks its own module up
    there — loading a file without registering it is the documented way to break dataclasses, and
    the failure message ("'NoneType' object has no attribute '__dict__'") gives no hint of that.
    """
    name = "deploy_check_under_test"
    spec = importlib.util.spec_from_file_location(name, CHECKER)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _require_yaml() -> None:
    """Skip *only* the tests that need the checker to read a file.

    The renderer tests below exercise the template subset directly and never open a manifest, so
    they keep running in an environment with no PyYAML: skipping them would be a skip that hides
    coverage rather than a skip that reports a missing tool.
    """
    if YAML_MISSING is not None:
        pytest.skip(YAML_MISSING)


@pytest.fixture()
def checker():
    module = _load()
    module.set_root(ROOT)
    yield module
    module.set_root(ROOT)


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A copy of the deployment artefacts, for breaking on purpose."""
    root = tmp_path / "repo"
    (root / "deploy").mkdir(parents=True)
    shutil.copytree(ROOT / "deploy", root / "deploy", dirs_exist_ok=True)
    for extra in (".dockerignore", "pyproject.toml", "README.md", "LICENSE", "CHANGELOG.md"):
        source = ROOT / extra
        if source.exists():
            shutil.copy2(source, root / extra)
    return root


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"fixture drift: {old[:60]!r} is not in {path}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


# --------------------------------------------------------------------------- #
# The renderer, which is the part that could quietly lie
# --------------------------------------------------------------------------- #


def test_the_renderer_substitutes_conditionals_and_ranges(checker):
    values = {"a": {"b": "x"}, "flag": True, "off": False, "list": ["one", "two"], "items": [{"n": "p"}]}
    template = (
        "{{ .Values.a.b }}|{{ if .Values.flag }}yes{{ else }}no{{ end }}|"
        "{{ if not .Values.off }}on{{ end }}|{{ range .Values.list }}{{ . }},{{ end }}|"
        "{{ range .Values.items }}{{ .n }}{{ end }}|{{ .Values.a.b | quote }}"
    )
    assert checker.render(template, values, "t") == 'x|yes|on|one,two,|p|"x"'


@pytest.mark.parametrize(
    "template,why",
    [
        ('{{ .Values.a | default "z" }}', "function call"),
        ('{{ include "name" . }}', "include"),
        ("{{ with .Values.a }}{{ end }}", "with"),
        ("{{ range .Values.missing }}{{ end }}", "undeclared value"),
        ("{{ if .Values.a }}no end", "unterminated if"),
        ("{{ .Values.a.b.c }}", "path through a non-mapping"),
        ('{{ .Values.list | join "," }}', "unsupported pipe"),
    ],
)
def test_the_renderer_refuses_what_it_does_not_implement(checker, template: str, why: str):
    """The declared subset is the point: an unimplemented construct must be a *failure with a
    reason*, never a skipped template that reports a pass for a file nobody read."""
    with pytest.raises(checker.DeployCheckError):
        checker.render(template, {"a": {"b": "x"}, "list": ["one"]}, f"template ({why})")


def test_a_range_over_a_scalar_says_so(checker):
    with pytest.raises(checker.DeployCheckError) as caught:
        checker.render("{{ range .Values.n }}x{{ end }}", {"n": 3}, "t")
    assert "needs a list" in str(caught.value)


def test_a_quoted_number_is_refused_rather_than_coerced(checker):
    """`| quote` on a number is almost always a values file that lost its type; refusing it keeps
    the chart's interface honest instead of rendering `5.0` where `"5.00"` was meant."""
    with pytest.raises(checker.DeployCheckError) as caught:
        checker.render("{{ .Values.n | quote }}", {"n": 5}, "t")
    assert "non-string" in str(caught.value)


# --------------------------------------------------------------------------- #
# The real tree
# --------------------------------------------------------------------------- #


def test_every_check_passes_on_this_repository(checker):
    _require_yaml()
    results, rendered = checker.run(None)
    failures = {result.name: result.problems for result in results if not result.ok}
    assert not failures, failures
    assert len(results) >= 15, f"only {len(results)} checks ran"
    documents = checker.documents(rendered[0], "rendered chart")
    assert len(documents) >= 5, "the rendered chart produced no Kubernetes documents"


def test_the_check_names_are_stable(checker):
    """The names are the interface: a test fixture, and a reader of the output, both depend on them."""
    names = [name for name, _ in checker.CHECKS]
    assert len(names) == len(set(names)), "duplicate check name"
    for required in (
        "pod.is-hardened",
        "probes.use-a-declared-port",
        "images.are-pinned",
        "configmap.policy-parses",
        "compose.waits-and-is-read-only",
        "dockerfile.is-buildable-by-inspection",
    ):
        assert required in names


def test_the_rendered_chart_is_a_deployment_a_pod_and_a_policy(checker):
    _require_yaml()
    rendered = checker.render_chart(ROOT / "deploy" / "helm" / "docket")
    text = "\n---\n".join(text for name, text in rendered.items() if name.endswith(".yaml"))
    kinds = {document.get("kind") for document in checker.documents(text, "chart")}
    assert {"Deployment", "ConfigMap", "PersistentVolumeClaim", "ServiceAccount", "NetworkPolicy"} <= kinds
    body = rendered["deployment.yaml"]
    assert "--port" in body and "/docket/readyz" in body


# --------------------------------------------------------------------------- #
# One break per check
# --------------------------------------------------------------------------- #


def _fail(checker, root: Path, name: str) -> list[str]:
    _require_yaml()
    checker.set_root(root)
    try:
        results, _ = checker.run([name])
        assert results, f"no check matched {name}"
        return results[0].problems
    finally:
        checker.set_root(ROOT)


def test_an_unpinned_image_is_caught(checker, tree: Path):
    _edit(tree / "deploy" / "kubernetes" / "deployment.yaml", "image: docket:0.1.0", "image: docket:latest")
    problems = _fail(checker, tree, "images.are-pinned")
    assert any("no pinned tag" in problem for problem in problems), problems


def test_a_privileged_container_is_caught(checker, tree: Path):
    _edit(
        tree / "deploy" / "kubernetes" / "deployment.yaml",
        "allowPrivilegeEscalation: false",
        "allowPrivilegeEscalation: false\n            privileged: true",
    )
    problems = _fail(checker, tree, "pod.is-hardened")
    assert any("privileged" in problem for problem in problems), problems


def test_a_root_pod_is_caught(checker, tree: Path):
    _edit(tree / "deploy" / "kubernetes" / "deployment.yaml", "runAsNonRoot: true", "runAsNonRoot: false")
    problems = _fail(checker, tree, "pod.is-hardened")
    assert any("runAsNonRoot" in problem for problem in problems), problems


def test_a_hostpath_volume_is_caught(checker, tree: Path):
    _edit(
        tree / "deploy" / "kubernetes" / "deployment.yaml",
        "emptyDir: {}",
        "hostPath:\n            path: /var/run/docker.sock",
    )
    problems = _fail(checker, tree, "pod.is-hardened")
    assert any("hostPath" in problem for problem in problems), problems


def test_a_container_without_limits_is_caught(checker, tree: Path):
    _edit(
        tree / "deploy" / "kubernetes" / "deployment.yaml",
        "            limits:\n              cpu: 500m\n              memory: 512Mi",
        "            limits:\n              cpu: 500m",
    )
    problems = _fail(checker, tree, "containers.have-resources")
    assert any("limits.memory" in problem for problem in problems), problems


def test_a_probe_on_an_undeclared_port_is_caught(checker, tree: Path):
    """The defect this check exists for: rename the boundary's port in `--port`, leave the probe
    behind, and the pod never becomes Ready on any cluster."""
    _edit(
        tree / "deploy" / "kubernetes" / "deployment.yaml",
        "127.0.0.1:8787/docket/readyz",
        "127.0.0.1:9999/docket/readyz",
    )
    problems = _fail(checker, tree, "probes.use-a-declared-port")
    assert any("9999" in problem for problem in problems), problems


def test_a_rolling_update_strategy_is_caught(checker, tree: Path):
    _edit(tree / "deploy" / "kubernetes" / "deployment.yaml", "type: Recreate", "type: RollingUpdate")
    problems = _fail(checker, tree, "deployment.one-writer")
    assert any("RollingUpdate" in problem and "evidence volume" in problem for problem in problems), problems


def test_an_unmounted_evidence_volume_is_caught(checker, tree: Path):
    _edit(tree / "deploy" / "kubernetes" / "deployment.yaml", "mountPath: /work/.docket", "mountPath: /work/evidence")
    problems = _fail(checker, tree, "deployment.one-writer")
    assert any("/work/.docket" in problem for problem in problems), problems


def test_a_policy_docket_would_refuse_is_caught(checker, tree: Path):
    """The ConfigMap's policy is parsed by Docket's own loader, so a manifest cannot ship a policy
    the tool would reject at startup — for example a cap that is not a number."""
    _edit(tree / "deploy" / "kubernetes" / "deployment.yaml", "max_usd = 5.00", "max_usd = 'lots'")
    problems = _fail(checker, tree, "configmap.policy-parses")
    assert problems, "an unparsable policy must fail the check"


def test_an_open_ingress_network_policy_is_caught(checker, tree: Path):
    _edit(tree / "deploy" / "kubernetes" / "deployment.yaml", "  ingress: []", "  ingress:\n    - {}")
    problems = _fail(checker, tree, "networkpolicy.default-deny")
    assert any("ingress is not empty" in problem for problem in problems), problems


def test_a_published_port_in_compose_is_caught(checker, tree: Path):
    _edit(
        tree / "deploy" / "compose.yaml",
        "    read_only: true\n    tmpfs:\n      - /tmp\n    security_opt:\n      - no-new-privileges:true\n    cap_drop:\n      - ALL\n    healthcheck:\n      # The boundary",
        '    ports:\n      - "8787:8787"\n    read_only: true\n    tmpfs:\n      - /tmp\n    security_opt:\n      - no-new-privileges:true\n    cap_drop:\n      - ALL\n    healthcheck:\n      # The boundary',
    )
    problems = _fail(checker, tree, "compose.waits-and-is-read-only")
    assert any("publishes ports" in problem for problem in problems), problems


def test_a_verifier_that_does_not_wait_is_caught(checker, tree: Path):
    _edit(tree / "deploy" / "compose.yaml", "condition: service_completed_successfully", "condition: service_started")
    problems = _fail(checker, tree, "compose.waits-and-is-read-only")
    assert any("does not wait" in problem for problem in problems), problems


def test_a_writable_evidence_mount_in_compose_is_caught(checker, tree: Path):
    _edit(tree / "deploy" / "compose.yaml", "- evidence:/work/.docket:ro", "- evidence:/work/.docket")
    problems = _fail(checker, tree, "compose.waits-and-is-read-only")
    assert any("writable" in problem for problem in problems), problems


def test_a_kustomization_pointing_at_nothing_is_caught(checker, tree: Path):
    _edit(
        tree / "deploy" / "kubernetes" / "kustomization.yaml",
        "  - deployment.yaml",
        "  - deployment.yaml\n  - service.yaml",
    )
    problems = _fail(checker, tree, "kustomization.paths-exist")
    assert any("service.yaml" in problem for problem in problems), problems


def test_a_dockerfile_that_runs_as_root_is_caught(checker, tree: Path):
    _edit(tree / "deploy" / "Dockerfile", "USER docket", "USER root")
    problems = _fail(checker, tree, "dockerfile.is-buildable-by-inspection")
    assert any("root" in problem for problem in problems), problems


def test_a_dockerfile_copying_a_missing_file_is_caught(checker, tree: Path):
    _edit(tree / "deploy" / "Dockerfile", "COPY src ./src", "COPY srcc ./src")
    problems = _fail(checker, tree, "dockerfile.is-buildable-by-inspection")
    assert any("srcc" in problem for problem in problems), problems


def test_a_missing_dockerignore_is_caught(checker, tree: Path):
    (tree / ".dockerignore").unlink()
    problems = _fail(checker, tree, "dockerfile.is-buildable-by-inspection")
    assert any("dockerignore" in problem for problem in problems), problems


def test_a_dangling_reference_is_caught(checker, tree: Path):
    readme = tree / "deploy" / "README.md"
    assert readme.read_text(encoding="utf-8").strip(), "fixture drift: no README to extend"
    readme.write_text(readme.read_text(encoding="utf-8") + "\nSee `docs/deploy-guide.md` for the long form.\n")
    # Written in backticks on purpose: that is how documentation references are usually spelled,
    # and the checker has to notice those too.
    problems = _fail(checker, tree, "references.resolve")
    assert any("deploy-guide.md" in problem for problem in problems), problems


def test_an_undeclared_value_is_caught(checker, tree: Path):
    _edit(
        tree / "deploy" / "helm" / "docket" / "templates" / "deployment.yaml",
        "name: docket-agent",
        "name: docket-agent-{{ .Values.missing.value }}",
    )
    problems = _fail(checker, tree, "chart.values-are-an-interface")
    assert any("undeclared .Values.missing.value" in problem for problem in problems), problems


def test_a_template_that_cannot_render_does_not_hide_the_other_checks(checker, tree: Path):
    """A render failure is one check's problem, not a traceback that skips the rest: the run has to
    finish, name the construct, and still report on the artefacts it could read."""
    _require_yaml()
    _edit(
        tree / "deploy" / "helm" / "docket" / "templates" / "pvc.yaml",
        "{{ if .Values.persistence.enabled }}",
        "{{ if .Values.persistence.enabled | toYaml }}",
    )
    checker.set_root(tree)
    try:
        results, _ = checker.run(None)
    finally:
        checker.set_root(ROOT)
    failed = {result.name: result.problems for result in results if not result.ok}
    assert "chart.renders" in failed, failed
    assert any("unsupported" in problem for problem in failed["chart.renders"]), failed["chart.renders"]
    assert len(results) >= 15, "the whole run still produced every check"


def test_a_declared_but_unused_value_is_caught(checker, tree: Path):
    """The zero-tolerance rule in miniature: a knob nobody reads is a knob that lies about existing."""
    _edit(tree / "deploy" / "helm" / "docket" / "values.yaml", "resources:\n", "featureX: true\n\nresources:\n")
    problems = _fail(checker, tree, "chart.values-are-an-interface")
    assert any("no template uses" in problem and "featureX" in problem for problem in problems), problems


def test_an_unimplemented_construct_in_a_real_template_is_caught(checker, tree: Path):
    _edit(
        tree / "deploy" / "helm" / "docket" / "templates" / "deployment.yaml",
        "  replicas: 1",
        '  replicas: 1\n  annotation: {{ include "docket.name" . }}',
    )
    problems = _fail(checker, tree, "chart.renders")
    assert any("unsupported template construct" in problem for problem in problems), problems


def test_a_chart_without_metadata_is_caught(checker, tree: Path):
    _edit(tree / "deploy" / "helm" / "docket" / "Chart.yaml", "apiVersion: v2", "apiVersion: v1")
    problems = _fail(checker, tree, "chart.metadata")
    assert any("apiVersion" in problem for problem in problems), problems


def test_a_workflow_that_does_not_parse_is_caught(checker, tree: Path):
    """A broken workflow runs nothing and looks green. This is the check that would have caught the
    unquoted colon in this repository's own CI, so it is tested against the same defect."""
    workflows = tree / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "broken.yml").write_text(
        "name: broken\non: push\njobs:\n  x:\n    runs-on: ubuntu-latest\n    steps:\n      - name: bad (colon: here)\n        run: true\n",
        encoding="utf-8",
    )
    problems = _fail(checker, tree, "workflows.parse")
    assert any("does not parse" in problem for problem in problems), problems


def test_a_workflow_step_that_does_nothing_is_caught(checker, tree: Path):
    workflows = tree / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - name: nothing\n",
        encoding="utf-8",
    )
    problems = _fail(checker, tree, "workflows.parse")
    assert any("neither uses an action nor runs a command" in problem for problem in problems), problems


def test_a_document_without_a_kind_is_caught(checker, tree: Path):
    _edit(tree / "deploy" / "kubernetes" / "deployment.yaml", "kind: ServiceAccount", "kind0: ServiceAccount")
    problems = _fail(checker, tree, "documents.are-declarations")
    assert any("apiVersion/kind" in problem for problem in problems), problems


# --------------------------------------------------------------------------- #
# The dependency itself, which has to fail loudly rather than quietly
# --------------------------------------------------------------------------- #


def test_a_missing_pyyaml_is_exit_2_with_a_reason_never_a_pass(tmp_path: Path):
    """The checker's dependency is optional, so the dangerous failure is the quiet one.

    Uninstalling PyYAML to test this would be a test that mutates the machine it runs on, so the
    import is *shadowed* instead: a `yaml.py` that raises `ImportError` earlier on `sys.path`. The
    checker must then exit 2 — "could not run", distinct from 1 — and say so on stderr, because a
    deployment check that silently passes when it read nothing is worse than no check at all.
    """
    blocker = tmp_path / "blocker"
    blocker.mkdir()
    (blocker / "yaml.py").write_text('raise ImportError("blocked by the test")\n', encoding="utf-8")

    env = os.environ.copy()
    # The checker bootstraps its own `sys.path` from its location, so replacing PYTHONPATH here
    # affects only the `import yaml` under test.
    env["PYTHONPATH"] = str(blocker)
    done = subprocess.run(
        [sys.executable, str(CHECKER)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert done.returncode == 2, f"exit {done.returncode}\n{done.stdout}{done.stderr}"
    message = done.stderr.lower()
    assert "pyyaml is not installed" in message, done.stderr
    assert "not a pass" in message, done.stderr
    assert "pip install pyyaml" in message, done.stderr
    assert "ok" not in done.stdout.lower(), f"a check reported a pass with no YAML reader:\n{done.stdout}"
