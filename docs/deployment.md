# Deployment: containers, pods, and what is actually verified

Docket is not a service. It is a program you wrap around an agent, and everything it produces is a
file. That makes deployment unusually simple — and unusually easy to get wrong in the one way that
matters, which is *scope*: a receipt says what the boundary saw, and a deployment that quietly moves
the agent outside the boundary produces evidence of a run that did not happen.

This page covers the recipes in [`deploy/`](../deploy), what they are checked against, and where the
honesty stops. The short version: **the images and manifests have been read by a checker and by
tests; none of them has been run here**, because this repository is built and tested in an
environment with no container runtime and no cluster (`which docker podman buildah` finds nothing;
there is no `kubectl`).

## The three shapes

| shape | what runs where | use it when |
| --- | --- | --- |
| **one process, one host** | `docket run -- <agent>` on your machine or in CI | the default. Nothing to deploy. |
| **one container** | the agent and Docket in the same container ([`deploy/Dockerfile`](../deploy/Dockerfile)) | you already build images and want the run's workspace, snapshots and evidence to be the image's business |
| **one pod, two containers** | your agent under Docket, plus *something for it to call* ([`deploy/kubernetes/`](../deploy/kubernetes), [`deploy/helm/docket/`](../deploy/helm/docket)) | you are running agents on Kubernetes and want caps and a receipt per run |

The rule that makes the third shape work is that the agent and the boundary must share a filesystem
view (Docket snapshots the tree the agent edits, and writes evidence beside it) and must reach each
other over loopback. Pods get both for free: containers in a pod share a network namespace, so
`127.0.0.1` in one container is `127.0.0.1` in the other.

## What the boundary does *not* expose

The boundary binds **loopback only** (`src/docket/proxy.py`), and that is a design decision, not an
oversight:

* **no Service, and no published ports.** Nothing outside the pod can address the boundary, so
  nothing outside the pod can post events into a live run or read its ledger through the
  instrumentation endpoints;
* **no scraping from outside.** `/docket/metrics` is Prometheus text and it is real, but it is on a
  loopback socket. Read it from inside the pod:

  ```console
  $ kubectl exec deploy/docket-agent -c docket -- python -c \
      "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8787/docket/metrics').read().decode())"
  ```

  If you want the numbers in your monitoring system, add a sidecar that curls loopback and exposes
  them on the pod network — and think about what else that sidecar exposes, because the boundary's
  write paths live on the same socket.

* **no `--port` beyond loopback.** `docket run --port 8787` fixes the port *inside* the network
  namespace, which is what a probe needs; it does not change the interface it binds.

## What the deploy check verifies

`make deploy-check` runs [`tools/deploy_check.py`](../tools/deploy_check.py), which reads the
artefacts and asserts the invariants that hold regardless of which cluster they land on:

| check | what it establishes |
| --- | --- |
| `chart.metadata`, `chart.renders` | the chart is well-formed and renders under its own values |
| `chart.values-are-an-interface` | every `.Values` reference is declared and every declared value is used |
| `documents.are-declarations` | every Kubernetes document has an apiVersion and a kind |
| `images.are-pinned` | no `:latest`, no untagged images |
| `pod.is-hardened` | non-root, no privilege escalation, all capabilities dropped, read-only root filesystem, seccomp, no `hostPath`, no host namespaces |
| `containers.have-resources` | requests and limits on every container |
| `probes.use-a-declared-port` | every probed port is a port the container declares |
| `deployment.one-writer` | `Recreate` strategy and an evidence mount at `/work/.docket` |
| `configmap.policy-parses` | the embedded policy is read by **Docket's own policy loader** |
| `networkpolicy.default-deny` | empty ingress, declared policyTypes, DNS and 443 egress |
| `compose.waits-and-is-read-only` | the verifier waits for the run, mounts evidence read-only, and no ports are published |
| `kustomization.paths-exist`, `references.resolve` | every referenced path exists |
| `dockerfile.is-buildable-by-inspection` | the Dockerfile's sources exist, it installs as a non-root user, it sets a stop signal, and it bakes in no credential |

The checker is itself tested, which is the part that makes it more than decoration:
`tests/test_deploy_check.py` breaks each invariant on purpose — a privileged pod, a missing probe, an
unpinned image, an undeclared value, a `range` construct the renderer does not implement — and
asserts that the checker fails with a message naming the problem.

## What the deploy check does not verify

Stated as plainly as the list above, because the difference between "checked" and "ran" is the whole
argument of this project:

* **Nothing is executed.** No image is built, no container starts, no manifest is applied, no cluster
  sees these objects. `docker build` and `kubectl apply -k deploy/kubernetes` have never run here.
* **No admission control.** Whether *your* cluster accepts these objects (Pod Security Admission
  level, a policy engine, a mutating webhook, a missing StorageClass, a NetworkPolicy plugin that is
  not installed) is unknown to this repository. A cluster without a NetworkPolicy implementation
  ignores the file silently.
* **No Helm client.** `helm lint` and `helm template` have not been run. To keep that honest rather
  than merely unstated, the chart uses a *declared subset* of the template language (values
  substitution, single conditionals, `range` over declared lists — see the annotation in
  `deploy/helm/docket/Chart.yaml`) so that the checker can render it deterministically and refuse
  anything outside that subset **by name** instead of skipping it. If you use a Helm construct the
  checker does not know, it fails; that is deliberate.
* **No cluster-scale behaviour.** One replica is the only topology that is correct (a run is one
  writer to one evidence directory) and the manifests say so; whether your StorageClass gives you
  the durability the ledger's `fsync` deserves is a question about your storage, not about Docket.
* **No provider is contacted.** The example pod answers itself with `docket.fakeupstream`, a
  scripted provider, so that the manifest produces a real receipt offline. Point it at a real
  provider and the receipt will show that provider's real numbers — and no CI here will ever have
  called it.

## Operating notes

**Evidence is the artefact.** Put the evidence directory on a volume you keep, back up, and can read
after the pod is gone. `latest.receipt.json` is written next to the per-run receipts and is the file
to collect; a receipt is self-contained (it embeds the price table and the policy digest), so it
verifies years later without this repository's state.

**Readiness is the signal that matters.** `/docket/readyz` turns red when the ledger can no longer be
written or the volume is below `min_free_bytes`. A run that keeps going with unusable evidence is
precisely the failure this tool exists to prevent, so wire that probe to something that pages a
human, not just to a load balancer.

**Stop with SIGTERM, never SIGKILL.** `docket run` forwards the signal to the agent's process group,
reaps the child, and *then* writes the receipt. `SIGKILL` leaves a readable prefix and no receipt,
which is recoverable (`docket doctor`) but is not the same thing as a clean end.

**One writer per evidence directory.** Two runs pointed at one directory is an evidence defect you
can see: the ledger takes an exclusive lock and the second run is refused by name. Do not defeat that
by sharing a volume between replicas.

**Costs.** `max_usd` is the cap people actually defend against; for subscription-backed agents set
`--budget-mode quota` and use token, call and wall-clock caps, then read `cost_basis` in the receipt
to see which basis was in force.

## Try the shapes without a cluster

The compose file is a smoke test of the image and the two-container wiring, not a production
topology:

```console
$ docker compose -f deploy/compose.yaml up --build --abort-on-container-exit
```

It starts a scripted provider, runs the reference agent under the real boundary with real caps, and
then verifies the receipt from a *separate* container that mounts the evidence volume read-only. If
that sequence completes, the image runs, the wiring works, and the receipt is valid — which is
exactly as much as a container can prove, and no more.
