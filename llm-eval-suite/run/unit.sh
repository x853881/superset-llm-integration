#!/usr/bin/env sh
# Unit tests for the suite's own code: offline, no Superset or LLM, a few
# seconds. Extra arguments go to pytest, e.g. -k timing. Where the suite has
# its dev folder, that folder's tests run too.
#
#   run/unit.sh           in the suite's image (Dockerfile), as production has it
#   run/unit.sh --local   on this machine, in .venv (no Docker needed)
#
# SUITE_BASE_IMAGE builds the image on another base image: see run/image.sh.
cd "$(dirname "$0")/.." || exit 1
dev_tests=""
dev_mount=""
if [ -d dev/tests ]; then
  dev_tests=dev/tests
  dev_mount="$PWD/dev:/suite/dev"
fi
if [ "${1:-}" = --local ]; then
  shift
  . run/venv.sh
  setup_venv --local || exit $?
  exec .venv/bin/pytest -m unit evals/unit $dev_tests -q "$@"
fi
. run/image.sh
build_image || exit 1
# The image runs the suite unless told otherwise: here, pytest. Some tests
# read files of this folder that the image doesn't hold.
exec docker run --rm --entrypoint pytest -v "$PWD/evals:/suite/evals" \
  -v "$PWD/Dockerfile:/suite/Dockerfile:ro" \
  -v "$PWD/production:/suite/production:ro" \
  ${dev_mount:+-v "$dev_mount"} "$SUITE_IMAGE" \
  -m unit evals/unit $dev_tests -q "$@"
