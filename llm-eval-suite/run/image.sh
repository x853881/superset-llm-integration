# Builds the suite's image (Dockerfile) on this machine, for run_evals.sh and
# run/unit.sh. Sourced from the suite's folder:
#   build_image || exit 1
#
# SUITE_BASE_IMAGE builds it on another base image, such as production's,
# to test a change on that:
#   SUITE_BASE_IMAGE=registry.access.redhat.com/ubi9/python-311 ./run_evals.sh smoke
SUITE_IMAGE=llm-eval-suite:dev

build_image() {
  docker build -q ${SUITE_BASE_IMAGE:+--build-arg BASE_IMAGE="$SUITE_BASE_IMAGE"} \
    -t "$SUITE_IMAGE" . >/dev/null
}
