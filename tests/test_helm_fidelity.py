"""Faithfulness of the checker's Helm render, and the tool that tests it against real Helm.

`tools/deploy_check.py` renders the chart through a *declared subset* of the template language so
that it can run anywhere — no Helm, no cluster, no network — and refuses anything outside that subset
by name. That design buys portability and it costs a claim: the render has to be the render a cluster
would get. `tools/helm_fidelity.py` is how that claim is checked rather than asserted: it runs the
real `helm template`, keys both renders by `(kind, name)` and compares them field for field.

Two things are tested here. The comparison itself — pairing, difference reporting, and the exit code
when Helm is absent, which must be "could not run" and never a pass — is tested directly, because it
is the part that decides whether a divergence is found. The end-to-end comparison runs only where
Helm exists, and **skips with that reason** where it does not, the same convention the optional
`cryptography` and PyYAML tests follow: CI has no Helm client, and a skip that says so is honest
where a green "verified against Helm" would not be.
"""

from __future__ import annotations

import importlib.util
import itertools
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "helm_fidelity.py"

#: Why the end-to-end comparison cannot run here, or `None` if it can. The comparison has *two*
#: prerequisites and it needed two gates: the client must exist, and the tool must be able to read
#: YAML. The second one was missing until the dependency matrix found it — with a Helm client on
#: `PATH` and PyYAML absent the test ran, and the tool exited 2 with "PyYAML is not installed", so
#: the suite went red for a setup the README describes. That is the same defect the deploy-check
#: file had, in the same direction: a missing optional extra must *skip with its reason*, because a
#: failure says the code is wrong when the environment is.
HELM_MISSING = (
    None
    if shutil.which("helm") is not None
    else (
        "needs the Helm client, which is not installed here. The tool exits 2 with that reason "
        "rather than reporting success, so there is nothing to assert but the reason: install Helm "
        "(https://helm.sh/docs/intro/install/) and re-run to compare the renders for real"
    )
)

#: The other half of the same prerequisite. Only the end-to-end test needs it spelled out: every
#: other test in this file asks for `yaml_module` as a fixture, and the end-to-end one drives the
#: tool as a subprocess, where a fixture cannot reach.
YAML_MISSING = (
    None
    if importlib.util.find_spec("yaml") is not None
    else (
        "the comparison renders the chart with Helm and then reads values.yaml with PyYAML, so it "
        "cannot run without it: `pip install pyyaml` and re-run"
    )
)


def _load():
    """Load the tool as a module, registering it in `sys.modules` before execution."""
    name = "helm_fidelity_under_test"
    spec = importlib.util.spec_from_file_location(name, TOOL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tool():
    return _load()


@pytest.fixture()
def yaml_module():
    return pytest.importorskip("yaml", reason="the comparison reads YAML; `pip install pyyaml`")


def test_documents_are_paired_by_kind_and_name_not_by_position(tool, yaml_module):
    """Two renders that order their documents differently still describe the same objects.

    Pairing by position would report the reordering as a difference and would hide the case that
    matters — the same documents in the same order with different content — behind whichever
    document happened to come first. Pairing by `(kind, name)` also makes an ambiguous render an
    error rather than a coin toss, which is asserted here too.
    """
    first = "kind: Service\nmetadata:\n  name: one\nspec:\n  ports: []\n"
    second = "kind: Deployment\nmetadata:\n  name: two\nspec:\n  replicas: 1\n"
    stream = f"{first}---\n{second}"
    reordered = f"{second}---\n{first}"

    parsed = tool.documents_from_stream(stream, "fixture", yaml_module)
    assert set(parsed) == {("Service", "one"), ("Deployment", "two")}
    assert parsed == tool.documents_from_stream(reordered, "fixture", yaml_module)

    duplicate = f"{first}---\n{first}"
    with pytest.raises(SystemExit) as raised:
        tool.documents_from_stream(duplicate, "fixture", yaml_module)
    assert "two documents named" in str(raised.value)


def test_a_block_scalar_keeps_the_newline_that_ends_it(tool, yaml_module):
    """The first difference this tool ever reported was its own off-by-one-character bug.

    A separator line is `---` at the start of a line; a *character* `---` inside a value is not one.
    Splitting the stream on `"\\n---"` ate the newline ending the previous document's last line,
    which for a `policy.toml: |` block scalar is part of the value, so the tool accused the checker
    of a one-character difference that Helm did not see. The separator is matched as a line here, and
    this test is the reason it stays that way.
    """
    document = "kind: ConfigMap\nmetadata:\n  name: policy\ndata:\n  policy.toml: |\n    [caps]\n    max_usd = 5.00\n"
    stream = f"{document}---\nkind: Service\nmetadata:\n  name: none\n"
    parsed = tool.documents_from_stream(stream, "fixture", yaml_module)
    assert parsed[("ConfigMap", "policy")]["data"]["policy.toml"] == "[caps]\nmax_usd = 5.00\n"


def test_a_difference_is_reported_with_the_path_of_the_field(tool):
    """A report that says "the ConfigMap differs" is a report nobody can act on."""
    helm = {"kind": "Deployment", "spec": {"template": {"spec": {"containers": [{"name": "a", "image": "x:1"}]}}}}
    checker = {"kind": "Deployment", "spec": {"template": {"spec": {"containers": [{"name": "a", "image": "x:2"}]}}}}
    difference = tool.first_difference(helm, checker)
    assert difference is not None
    assert ".spec.template.spec.containers[0].image" in difference
    assert "x:1" in difference and "x:2" in difference
    assert tool.first_difference(helm, helm) is None

    shorter = {"kind": "Deployment", "spec": {"template": {"spec": {"containers": []}}}}
    assert "length 1 versus 0" in str(tool.first_difference(helm, shorter, ""))

    # A field one render has and the other does not is still a difference, and the report says which
    # render has it. `first_difference(as_helm, as_checker)`: the first argument is what Helm printed.
    template = helm["spec"]["template"]
    as_checker_with_extra = {"kind": "Deployment", "spec": {"template": template, "extra": 1}}
    assert "only in the checker's render" in str(
        tool.first_difference({"kind": "Deployment", "spec": {"template": template}}, as_checker_with_extra)
    )
    assert "only in the Helm render" in str(
        tool.first_difference(as_checker_with_extra, {"kind": "Deployment", "spec": {"template": template}})
    )


def test_a_missing_helm_is_exit_2_with_a_reason_never_a_pass(tmp_path: Path):
    """The whole convention, in one test that runs with or without Helm installed.

    `make check` deliberately does not run this tool, because CI has no Helm. That is only honest if
    the tool refuses to report success when it cannot do its job: an empty `PATH` makes the client
    unfindable, and the run has to end in "could not run" with the reason on stderr — the same code
    and the same manner as `tools/deploy_check.py` without PyYAML.
    """
    empty = tmp_path / "empty-path"
    empty.mkdir()
    completed = subprocess.run(
        [sys.executable, str(TOOL)],
        cwd=str(ROOT),
        env={**os.environ, "PATH": str(empty)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 2, f"exit {completed.returncode}: {completed.stdout}{completed.stderr}"
    assert "is not installed" in completed.stderr
    assert "This is not a pass" in completed.stderr
    assert completed.stdout == "", "a tool that could not run prints no verdict on stdout"


@pytest.mark.skipif(HELM_MISSING is not None, reason=HELM_MISSING or "")
@pytest.mark.skipif(YAML_MISSING is not None, reason=YAML_MISSING or "")
def test_the_checkers_render_is_the_render_helm_produces():
    """The end-to-end claim: same documents, same fields, from a real Helm client.

    This is the test that would have caught the integer defect on its own — and did, in the form of
    the tool's first run reporting a difference in the policy's block scalar. It prints the Helm
    version it compared against so that a reader of the log knows which release made the claim.
    """
    completed = subprocess.run(
        [sys.executable, str(TOOL)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, f"{completed.stdout}{completed.stderr}"
    assert "documents identical field for field" in completed.stdout
    total, _, rest = completed.stdout.partition("/")
    assert total.isdigit() and int(total) >= 5, f"only {total} documents were compared"
    assert "v3." in rest, "the Helm version is not in the output, so the claim is not dateable"


# --------------------------------------------------------------------------- #
# `main()`'s contract, driven by a stand-in Helm
# --------------------------------------------------------------------------- #
#
# The comparison above needs the real client and skips without it — CI has no Helm, and a check
# that cannot run must say so rather than pass. What does *not* need the real client is the tool's
# own contract: the two renders agree → 0, they differ → 1, Helm cannot run → 2, each with a
# message that names what happened. Every one of those paths is reachable with a stand-in on
# `--helm`, and the stand-in prints whatever payload the test hands it. The payload for the
# agreeing case is produced by *the checker itself*, which makes these assertions about the
# reporter rather than about Helm's rendering — the rendering is the other test's business.

STAND_IN_HELM = '''#!/usr/bin/env python3
"""A stand-in Helm: `version --short` prints a version, `template` prints the payload it is given."""
import pathlib
import sys

here = pathlib.Path(__file__).resolve().parent
if len(sys.argv) > 1 and sys.argv[1] == "version":
    if (here / "version-fails").exists():
        print("Error: no version", file=sys.stderr)
        raise SystemExit(3)
    print("v9.9.9")
    raise SystemExit(0)
sys.stderr.write((here / "stderr.txt").read_text(encoding="utf-8"))
sys.stdout.write((here / "payload.yaml").read_text(encoding="utf-8"))
raise SystemExit(int((here / "exit.txt").read_text(encoding="utf-8").strip()))
'''


@pytest.fixture()
def stand_in(tmp_path: Path, tool, yaml_module):
    """A chart the checker can render, and a factory for stand-in Helm clients."""
    chart = tmp_path / "chart"
    shutil.copytree(ROOT / "deploy" / "helm" / "docket", chart)
    rendered = tool._load_checker().render_chart(chart)
    payload = "\n---\n".join(text for name, text in sorted(rendered.items()) if name.endswith((".yaml", ".yml")))
    counter = itertools.count()

    def write(payload_text: str, code: int = 0, stderr: str = "") -> Path:
        directory = tmp_path / f"helm{counter.__next__()}"
        directory.mkdir()
        (directory / "payload.yaml").write_text(payload_text, encoding="utf-8")
        (directory / "stderr.txt").write_text(stderr, encoding="utf-8")
        (directory / "exit.txt").write_text(str(code), encoding="utf-8")
        script = directory / "helm"
        script.write_text(STAND_IN_HELM, encoding="utf-8")
        script.chmod(0o755)
        return script

    return SimpleNamespace(chart=chart, rendered=rendered, payload=payload, write=write)


def test_an_agreeing_render_is_exit_zero_and_says_what_was_skipped(tool, stand_in, capsys):
    """Kills `main`'s `return 0 -> return None` and the inverted `.yaml` skip test.

    A tool whose success path returns `None` still exits 0 through `raise SystemExit(None)`, so the
    contract is the *message* as much as the code: the two renders agreed, the version that
    rendered them is named, and the file that was deliberately not compared (`NOTES.txt`, which
    `helm template` does not emit) is named too — a skip that is not reported is a skip nobody can
    audit.
    """
    helm = stand_in.write(stand_in.payload)
    assert tool.main(["--chart", str(stand_in.chart), "--helm", str(helm)]) == 0
    out = capsys.readouterr().out
    assert "documents identical field for field (v9.9.9)" in out, out
    assert "NOTES.txt is not one" in out, out


def test_a_field_that_differs_is_exit_one_and_the_report_names_the_field(tool, stand_in, capsys, yaml_module):
    """Kills `main`'s `return 1` mutants, the `first_difference` append, and the version capture.

    The point of reporting exit 1 is the sentence after it. A reader has to be able to go to the
    document and the field, so the message is asserted with both, and the version label is asserted
    because a comparison that cannot name the client it ran is a comparison nobody can reproduce.
    """
    documents = list(yaml_module.safe_load_all(stand_in.payload))
    documents[0].setdefault("metadata", {}).setdefault("labels", {})["docket-probe"] = "differs"
    helm = stand_in.write(yaml_module.safe_dump_all(documents))
    assert tool.main(["--chart", str(stand_in.chart), "--helm", str(helm)]) == 1
    err = capsys.readouterr().err
    assert "1 difference(s)" in err and "(v9.9.9)" in err, err
    label = f"{documents[0]['kind']}/{documents[0]['metadata']['name']}"
    assert f"{label}: .metadata.labels.docket-probe" in err, err


def test_a_document_only_one_render_produces_is_named_with_its_kind_and_name(tool, stand_in, capsys, yaml_module):
    """Kills both one-sided branches of the document set comparison, and the `key[0]`/`key[1]` reads.

    "rendered by Helm, not by the checker" and its mirror are the two findings a fidelity tool
    exists to produce, and each has to carry the object's kind *and* name: a message that prints the
    name twice, or the kind where the name belongs, sends the reader to the wrong manifest.
    """
    documents = list(yaml_module.safe_load_all(stand_in.payload))
    extra = dict(documents[0])
    extra["metadata"] = dict(extra["metadata"], name="only-in-helm")
    helm = stand_in.write(yaml_module.safe_dump_all([*documents, extra]))
    assert tool.main(["--chart", str(stand_in.chart), "--helm", str(helm)]) == 1
    err = capsys.readouterr().err
    assert f"{extra['kind']}/only-in-helm: rendered by Helm, not by the checker" in err, err

    missing = documents[0]
    helm = stand_in.write(yaml_module.safe_dump_all(documents[1:]))
    assert tool.main(["--chart", str(stand_in.chart), "--helm", str(helm)]) == 1
    err = capsys.readouterr().err
    label = f"{missing['kind']}/{missing['metadata']['name']}"
    assert f"{label}: rendered by the checker, not by Helm" in err, err


def test_a_helm_that_fails_is_exit_two_and_its_own_error_is_shown(tool, stand_in, capsys):
    """Kills the `returncode != 0` comparison and its `return 2`.

    A Helm that ran and failed is not a difference between two renders — it is a run that produced
    no second render at all, so it is "could not run" (2), and the client's stderr is quoted rather
    than summarised, because that text is what the reader needs to fix it.
    """
    helm = stand_in.write("", code=3, stderr="Error: chart requires kubeVersion\n")
    assert tool.main(["--chart", str(stand_in.chart), "--helm", str(helm)]) == 2
    err = capsys.readouterr().err
    assert "helm template failed (3)" in err, err
    assert "chart requires kubeVersion" in err, err


def test_a_helm_that_cannot_be_executed_is_exit_two_and_never_a_crash(tool, stand_in, tmp_path, capsys):
    """Kills the `return 2` on the `OSError` path.

    `shutil.which` finds a file with the executable bit; whether the kernel can *run* it is a
    second question, and the answer here is `OSError`. That must reach the reader as the "could not
    run" code with the reason, exactly as a missing binary does, rather than as a traceback out of
    a check that was only trying to compare two renders.
    """
    helm = stand_in.write(stand_in.payload)
    broken = helm.read_text(encoding="utf-8").replace("#!/usr/bin/env python3", "#! /nonexistent/interpreter")
    helm.write_text(broken, encoding="utf-8")
    assert tool.main(["--chart", str(stand_in.chart), "--helm", str(helm)]) == 2
    assert "could not run" in capsys.readouterr().err


def test_a_duplicate_document_in_the_checkers_own_render_is_refused(tool, stand_in, tmp_path, yaml_module):
    """Kills the duplicate guard's `raise` and its message.

    Two templates rendering one object is a chart bug the checker's own renderer would otherwise
    hide: the second write would silently overwrite the first and the comparison would look clean.
    The name is asserted so the reader knows *which* object is rendered twice.
    """
    chart = tmp_path / "duplicate"
    shutil.copytree(ROOT / "deploy" / "helm" / "docket", chart)
    (chart / "templates" / "configmap-again.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: docket-policy\ndata:\n  again: 'true'\n",
        encoding="utf-8",
    )
    helm = stand_in.write(stand_in.payload)
    with pytest.raises(SystemExit) as refused:
        tool.main(["--chart", str(chart), "--helm", str(helm)])
    assert "renders ConfigMap/docket-policy twice" in str(refused.value), str(refused.value)


def test_a_stream_with_two_documents_of_one_name_is_refused(tool, yaml_module):
    """Kills the `key[0]`/`key[1]` reads in the stream-level duplicate message.

    Keying by kind-and-name is what makes the comparison order-independent, so a stream that names
    one object twice is refused rather than silently keeping the last one. The message has to name
    the object the way a `kubectl` reader does — kind first, then name: reversed, it sends them
    looking for a resource kind called "docket-policy".
    """
    twice = "kind: ConfigMap\nmetadata:\n  name: docket-policy\ndata: {}\n---\nkind: ConfigMap\nmetadata:\n  name: docket-policy\ndata: {}\n"
    with pytest.raises(SystemExit) as refused:
        tool.documents_from_stream(twice, "probe", yaml_module)
    assert "two documents named ConfigMap/docket-policy" in str(refused.value), str(refused.value)


def test_a_helm_that_cannot_report_its_version_is_still_reported(tool, stand_in, capsys):
    """Kills the `check=` mutant on the `version --short` probe.

    The version is a *label* on the comparison, not a precondition for it: a client that renders
    correctly and cannot answer `version` has produced a usable second render, and the honest report
    says the label is unknown. Letting that call raise turns a working comparison into a traceback
    about metadata.
    """
    helm = stand_in.write(stand_in.payload)
    (helm.parent / "version-fails").write_text("", encoding="utf-8")
    assert tool.main(["--chart", str(stand_in.chart), "--helm", str(helm)]) == 0
    assert "unknown version" in capsys.readouterr().out


def test_the_tool_looks_for_the_chart_and_the_checker_where_they_are(tool):
    """Kills `ROOT = ...parents[1] -> parents[0]`.

    The tool finds both the chart and the checker it reuses relative to its own file. A `ROOT` one
    level too high points at the repository instead of the tree — the default chart becomes
    "deploy/helm/docket" under an unrelated directory, and the failure shows up as a missing file
    rather than as the wrong path.
    """
    assert tool.ROOT.resolve() == ROOT.resolve()
    assert tool.DEFAULT_CHART.is_dir()
    assert (tool.DEFAULT_CHART / "Chart.yaml").is_file()
    assert (tool.ROOT / "tools" / "deploy_check.py").is_file()


def test_the_checker_loader_returns_the_module_it_registered(tool):
    """Kills `_load_checker`'s `return module -> return None`.

    The loader exists so this tool can reuse the checker's renderer instead of growing a second
    one; `None` here becomes an `AttributeError` on the next line, which reads as "the checker is
    broken" rather than "the loader returned nothing". Registration in `sys.modules` is asserted
    too: it is what lets the loaded module's own dataclasses resolve by name.
    """
    module = tool._load_checker()
    assert module is sys.modules["docket_deploy_check"]
    assert callable(module.render_chart)
    assert callable(module._yaml)


def test_a_mapping_or_a_list_against_a_scalar_is_a_value_difference_not_a_crash(tool):
    """Kills the type guards in `first_difference` (`and -> or`).

    The two branches walk into a mapping or a list only when *both* sides are one. Read as `or`,
    the mapping branch would index a string with a string, and the list branch would compare the
    lengths of a list and a word and report "length 2 versus 1" — a difference, but not the
    difference, and one that sends the reader looking for a missing element instead of a wrong type.
    """
    assert tool.first_difference({"a": 1}, "a") == " ({'a': 1} versus 'a')"
    assert tool.first_difference([1, 2], "xy") == " ([1, 2] versus 'xy')"


def test_a_document_without_a_kind_is_skipped_and_a_broken_one_is_numbered(tool, yaml_module):
    """Kills the skip condition's `or -> and` and the `index + 1` in the parse-failure message.

    A rendered stream can hold a document that is not a Kubernetes object — a note, a comment-only
    return — and the reader is told which split failed by *position*, counting from one the way a
    person reading the stream would.
    """
    found = tool.documents_from_stream(
        "metadata:\n  name: nameless\n---\nkind: ConfigMap\nmetadata:\n  name: real\n", "probe", yaml_module
    )
    assert list(found) == [("ConfigMap", "real")]

    with pytest.raises(SystemExit) as broken:
        tool.documents_from_stream("kind: ConfigMap\n---\n{unclosed\n", "probe", yaml_module)
    assert "document 2 is not YAML" in str(broken.value), str(broken.value)
