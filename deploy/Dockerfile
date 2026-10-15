# The Docket image.
#
# Build it from the repository root, because it installs the package from source:
#
#     docker build -f deploy/Dockerfile -t docket:0.1.0 .
#
# Two stages, because the install needs a build backend and the run does not: the runtime stage
# carries the installed package and nothing that was needed to produce it. The image has no
# credentials, no policy and no agent baked in — all three belong to the operator, and the run is
# what records them.
#
# `docket` is not published to PyPI (see PUBLISH.md), so the wheel is built from this checkout
# rather than downloaded. The build therefore needs network access for the base image and the
# build backend; nothing at *run* time does, and the image itself contains no network clients
# beyond what the standard library provides.

FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src

# Only what the build backend reads: the packaging metadata, the source, and the two files the
# metadata refers to. `.dockerignore` keeps the rest of the checkout out of the context.
COPY pyproject.toml README.md LICENSE CHANGELOG.md ./
COPY src ./src

RUN pip install --quiet hatchling \
 && pip install --quiet --no-build-isolation --no-deps --prefix=/install .


FROM python:3.12-slim AS runtime

# A fixed, unprivileged uid: an orchestrator can pin it (`runAsUser`) and the evidence directory
# can be owned by it without anyone guessing a number. 10001 is high enough not to collide with a
# host user and consistent with the manifests in deploy/.
ARG DOCKET_UID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DOCKET_DIR=/work/.docket

RUN groupadd --gid ${DOCKET_UID} docket \
 && useradd --uid ${DOCKET_UID} --gid ${DOCKET_UID} --create-home --shell /usr/sbin/nologin docket

COPY --from=build /install /usr/local

WORKDIR /work
RUN mkdir -p /work/.docket && chown -R docket:docket /work

USER docket
VOLUME ["/work/.docket"]

# The boundary listens on loopback inside this container. It is not published by default: Docket
# is not a service to route traffic to, and `docker run -p` on it would expose an evidence
# recorder to a network it was deliberately kept off. A pod that wants to scrape the metrics
# endpoint asks for a fixed port (`--port`) and declares it in its own manifest.
#
# No HEALTHCHECK here, deliberately: the honest liveness signal belongs to the *run* (the
# boundary's `/docket/healthz`, on the port the operator fixed), and a check that this image can
# answer on its own would be a check that passes with no run in progress. deploy/compose.yaml and
# deploy/kubernetes/ show the real probes.

# `docket run` forwards SIGTERM to the agent and closes the receipt, which is the reason this is
# SIGTERM and not SIGKILL: a container being replaced should stop recording, not stop existing.
STOPSIGNAL SIGTERM

ENTRYPOINT ["docket"]
CMD ["--help"]
