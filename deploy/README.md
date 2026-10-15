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

## What has not been verified here

Nothing in this directory has been **executed**. There is no container runtime and no cluster in the
environment this repository is built and tested in, so:

* `docker build`, `docker compose up`, `kubectl apply -k` and `helm install` have never run;
* `helm lint` and `helm template` have never run — the chart is rendered by
  `tools/deploy_check.py` instead, which implements a declared subset of Helm's language and
  **fails by name** on any construct outside it rather than skipping it;
* cluster admission, a missing StorageClass, a cluster without a NetworkPolicy plugin, and your
  storage's durability are all unknown to this repository.

If you are the person who runs these for the first time on a real cluster, the interesting feedback
is welcome as an issue: the checks above are the ones this project believes matter, and a deployment
that shows otherwise is evidence worth having.
