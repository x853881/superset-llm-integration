# Sets up .venv, for running the suite on this machine instead of in Docker.
# Sourced by run_evals.sh and run/unit.sh from the suite's folder:
#   setup_venv <the option that needs it, for messages> || exit $?
#
# The first time, it makes .venv with Python 3.11 (python3.11 if that's on
# the PATH, else python3 if it is 3.11 or newer). It installs what
# requirements.txt lists, at the exact versions in requirements.lock, and
# again only when either file changes.
setup_venv() {
  if [ ! -x .venv/bin/python ]; then
    venv_python=$(command -v python3.11 || command -v python3)
    if [ -z "$venv_python" ] ||
      ! "$venv_python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
      echo "$1 runs the suite on this machine: it needs Python 3.11, as python3.11 or python3." >&2
      return 2
    fi
    echo "Setting up .venv to run the suite on this machine (first time only)..."
    "$venv_python" -m venv .venv || return 1
  fi
  venv_reqs=$(cat requirements.txt requirements.lock 2>/dev/null | cksum)
  if [ "$(cat .venv/.requirements 2>/dev/null)" != "$venv_reqs" ]; then
    if [ -f requirements.lock ]; then
      PIP_DISABLE_PIP_VERSION_CHECK=1 .venv/bin/pip install -q -r requirements.txt -c requirements.lock || return 1
    else
      PIP_DISABLE_PIP_VERSION_CHECK=1 .venv/bin/pip install -q -r requirements.txt || return 1
    fi
    printf '%s\n' "$venv_reqs" > .venv/.requirements
  fi
}
