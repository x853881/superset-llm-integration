# LLM-quality suite for the nltosqlandback Superset plugin. See README.md.
#
# One image for production and for a developer's machine. Production runs it
# as a Kubernetes Job (production/job.yaml). On a developer's machine,
# ./run_evals.sh and run/unit.sh build it and run it for you, so a change is
# tested the way production runs it.
#
# Build, from this folder:
#   docker build -t llm-eval-suite .
#   docker build -t llm-eval-suite \
#     --build-arg BASE_IMAGE=registry.access.redhat.com/ubi9/python-311 .
#
#   BASE_IMAGE  the image to build on: any with Python 3.11 or newer as
#               `python`, with pip, and sh. Default python:3.11-slim.
#   PIP_INDEX   a mirror of PyPI to install the Python packages from, when
#               the build can't reach pypi.org (pip's --index-url).
#
# Run: the image runs ./run_evals.sh --container, which takes its settings
# from the environment (production/settings.env lists them). With no
# arguments it runs the smoke goldens; any arguments replace that and go to
# run_evals.sh:
#   docker run --rm --env-file .env.eval llm-eval-suite
#   docker run --rm --env-file .env.eval llm-eval-suite sql --runs 3
#
# The report prints at the end, and the exit status is 0 only when every
# golden passed. Result files go to /suite/results.
#
# Offline unit tests:
#   docker run --rm --entrypoint pytest llm-eval-suite -m unit evals/unit

ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ARG PIP_INDEX=

# Some base images (Red Hat's UBI ones) build as a user who can't make /suite.
USER 0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    DEEPEVAL_TELEMETRY_OPT_OUT=1 \
    HOME=/suite

WORKDIR /suite

# The packages requirements.txt lists, at the versions in requirements.lock.
COPY requirements.txt requirements.lock ./
RUN python -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
      || { echo "BASE_IMAGE needs Python 3.11 or newer as python" >&2; exit 1; } \
    && pip install ${PIP_INDEX:+--index-url "$PIP_INDEX"} \
         -r requirements.txt -c requirements.lock

COPY pytest.ini run_evals.sh ./
COPY reports/report.py ./reports/
COPY run ./run
COPY evals ./evals

# The suite writes to /suite (results/, and DeepEval's .deepeval/). Owned by
# the root group and writable by it, as on OpenShift, where a pod runs as an
# arbitrary user in that group; writable by anyone too, as run_evals.sh runs
# the container as you on Linux.
RUN mkdir -p results \
    && chgrp -R 0 /suite && chmod -R g=u /suite \
    && chmod a+rwx /suite /suite/results
USER 1001:0

ENTRYPOINT ["sh", "./run_evals.sh", "--container"]
CMD ["smoke"]
