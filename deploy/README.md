# `deploy/` — the container and cluster recipes, and exactly what has been checked

Everything in this directory is a *recipe*: a Dockerfile, a compose file, Kubernetes manifests and a
Helm chart. The full guide is [`docs/deployment.md`](../docs/deployment.md); this file is the map and
the honesty note.

```
deploy/
  Dockerfile              the image: two-stage, non-root, no credentials, STOPSIGNAL SIGTERM
  compose.yaml            a three-service offline smoke test (provider, boundary, verifier)
  policy.example.toml     the policy compose mounts read-only into the boundary
  kubernetes/             one pod: the agent under Docket + a scripted provider on loopback
  helm/docket/            the same topology as a chart, in a declared template subset
tools/deploy_check.py     reads all of the above and checks it — `make deploy-check`
tests/test_deploy_check.py breaks each check on purpose, so the checker can fail
```

## What has been verified here

`make deploy-check` passes on this commit. It parses every document, renders the chart with its own
values, and asserts the invariants listed in [`docs/deployment.md`](../docs/deployment.md#what-the-deploy-check-verifies):
non-root pods, no privilege escalation, dropped capabilities, read-only root filesystems, resource
requests and limits, pinned image tags, probes whose ports the container declares, a `Recreate`
strategy with the evidence mounted where the policy points, a policy inside each ConfigMap that
Docket's *own* loader accepts, a default-deny NetworkPolicy, a compose file whose verifier waits for
the run and mounts evidence read-only, and every referenced path existing.

The checker's own tests break each invariant deliberately and assert the failure, one fixture per
check.

### The chart has now been rendered by Helm itself

`helm lint` and `helm template` **were** run against a released Helm — v3.16.3, 2026-10-15 — and
the result is in the repository as a fact rather than a hope:

```console
$ helm lint deploy/helm/docket
==> Linting deploy/helm/docket
[INFO] Chart.yaml: icon is recommended

1 chart(s) linted, 0 chart(s) failed
$ helm template docket deploy/helm/docket | grep -c '^kind:'
5
```

All five documents (NetworkPolicy, ServiceAccount, ConfigMap, PersistentVolumeClaim, Deployment)
render, and — this is the part worth recording — **they are identical, document for document, to
what `tools/deploy_check.py` renders**. That equality is what makes the static checker evidence
rather than a parallel universe, and it did not hold when it was first measured:

> **The chart used to render a policy Docket read as a float.** Helm hands every template a
> `float64` for each number in `values.yaml` (YAML → JSON → `map[string]any`), and Go prints a
> `float64` in scientific notation once the exponent reaches 6 — so the policy mounted into the
> pod said `max_ledger_bytes = 6.7108864e+07`, not `67108864`. TOML reads that as a float, and
> Docket's loader accepted it (a whole float is a whole number), so nothing failed: the run would
> have worked with a policy whose *type* is not the one the key documents, and any reader that
> wanted an integer would have been reading a different document from the one the project reviews.
> The static renderer hid it by rendering the number as Python wrote it.
>
> Two fixes, both in this tree: the chart's integer values are now written through `| int64`, so
> the rendered text says what the value means; and `tools/deploy_check.py` now renders values the
> way Helm does — `float64`, Go's `%v` — closing the fidelity gap that made the divergence
> invisible, and it refuses an integer key that arrives as a float, naming the line and the fix.
> `tests/test_deploy_check.py` pins the number formatting against a table produced by Helm, and
> reproduces the defect as a fixture.

**The comparison is a command, not a memory.** `tools/helm_fidelity.py` runs `helm template`, parses
both renders into documents keyed by `kind` and `metadata.name`, and compares them field for field,
naming the path of the first difference inside a document. It is deliberately **not** part of
`make check`: there is no Helm client in the CI environment, and a check that cannot run must say so
rather than pass — so it exits 2 with that reason, the same convention as `tools/deploy_check.py`
without PyYAML. Where Helm *is* installed:

```console
$ make helm-fidelity          # or: python3 tools/helm_fidelity.py
5/5 documents identical field for field (v3.16.3+gcfd0749) (checked every YAML template; NOTES.txt is not one)
```

Its own first working run is worth recording, because it is the failure mode of a comparison tool:
it reported one difference — the policy block scalar was missing its final newline — and the
difference was the tool's. It had split the document stream on the characters `"\n---"`, which eats
the newline ending the previous document's last line; for a `policy.toml: |` block scalar that
newline is part of the value. The separator is matched as a *line* now, and
`tests/test_helm_fidelity.py` pins that, along with the pairing rule, the difference reporting, and
the exit code when Helm is absent.

## What has not been verified here

Nothing in this directory has been **executed**. There is no container runtime and no cluster in the
environment this repository is built and tested in, so:

* `docker build`, `docker compose up`, `kubectl apply -k` and `helm install` have never run;
* `helm install`, `helm upgrade` and a cluster placing the pod have never run — the release-time
  behaviour (hooks, ordering, `--wait`) is not exercised. `helm lint` and `helm template` *have*, and
  the chart is still rendered by `tools/deploy_check.py` for the checks themselves, because that
  renderer is the one that can **fail by name** on any construct outside its subset; the two
  renderers are compared by `tools/helm_fidelity.py` rather than assumed equal;
* cluster admission, a missing StorageClass, a cluster without a NetworkPolicy plugin, and your
  storage's durability are all unknown to this repository.

If you are the person who runs these for the first time on a real cluster, the interesting feedback
is welcome as an issue: the checks above are the ones this project believes matter, and a deployment
that shows otherwise is evidence worth having.
