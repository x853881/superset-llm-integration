#!/usr/bin/env sh
# Run the LLM-quality suite and print a readable report. It runs in the
# suite's image (Dockerfile), as production runs it, unless --local says to
# run it on this machine without Docker.
#
#   ./run_evals.sh                                  # every golden, once
#   ./run_evals.sh --local                          # ...on this machine, without Docker
#   ./run_evals.sh --local smoke                    # a few goldens that need no tables
#   ./run_evals.sh accelerate                       # one part of the suite
#   ./run_evals.sh fix -k month                     # extra args go to deepeval test run
#   ./run_evals.sh -k "refine or kenya"             # or filter every golden by name
#   ./run_evals.sh sql --runs 3                     # each golden 3 times, for pass rates
#   ./run_evals.sh --runs 3 --all-runs              # ...steady goldens too
#   ./run_evals.sh --runs 3 --parallel 3            # ...3 goldens at a time
#
# Where the suite has its dev folder (dev/, which isn't published),
# dev/options.sh adds options for a developer's machine: see dev/README.md.
#
# Parts (see SUITE_PARTS in evals/suite/dataset.py):
#   all           every golden (the default)
#   sql           generate-sql, update-sql-from-nl, refine-sql
#   fix           explain-sql-error
#   accelerate    accelerate
#   describe      generate-nl, update-nl-from-sql-diff, describe-breakdown
#   explain-plan  explain-plan
#   prose         describe and explain-plan together: every plain-English answer
#   clarify       requests too ambiguous to answer, where the plugin should ask
#                 a question back (goldens with "expect_clarification": true)
#   regression    easy goldens kept to catch the plugin getting worse at the
#                 basics ("regression": true)
#   waiting       goldens expected to fail until the plugin changes, e.g. it
#                 needs metadata it isn't given yet ("waiting_on_plugin": true)
#   injection     inputs carrying an instruction to the plugin, in a comment,
#                 a string or an error message, which it should ignore
#                 ("injection": true)
#   smoke        a few goldens that need no tables (evals/goldens/smoke/),
#                 to show that Superset, SQL Lab, the plugin, its model and
#                 the judge all connect. Never part of any other part.
# or any single endpoint name, e.g. generate-sql.
#
# --runs N asks every golden N times, and the report gives each golden's pass
# rate. The suite goes through all its goldens once, then again, so a slow
# spell at the LLM provider doesn't land on every run of one golden. It takes
# N times as long. Without --runs, each golden runs once. A golden that
# passed every one of its recent runs with high judged scores is steady and
# asked once (evals/suite/steady.py); --all-runs asks every golden N
# times.
#
# --parallel N runs N goldens at a time, in N worker processes (pytest-xdist),
# so a run finishes sooner. Grading stays as fair as a serial run's: no query
# runs while another is being timed, and no request waits in Superset's queue,
# as Superset gets only SUPERSET_MAX_CONCURRENT requests at once (default 1:
# the main stack's development server answers one at a time), each waiting
# for its turn before its clock starts (evals/suite/locks.py). Judging
# and timing (through psql where TIMING_VIA=psql, so Superset stays free) then
# overlap with other workers' plugin calls (see README.md). A judge that runs
# as a program on this machine makes at most JUDGE_MAX_CONCURRENT (default 4)
# judgements at once. After the run, the report says whether the locks held.
#
# Without --local, this script builds the suite's image (Dockerfile) and runs
# it, with the settings in .env.eval and the certificate files they name, so a
# change is tested the way production's Kubernetes Job runs it.
# SUITE_BASE_IMAGE builds the image on another base image, such as
# production's: see run/image.sh.
#
# --container is how the image runs the suite (its ENTRYPOINT): with the
# Python packages it holds, and its settings from the environment (a
# .env.eval is read only if there is one). Otherwise it runs as --local does.
#
# --local runs the suite on this machine, in .venv (set up the first time,
# with Python 3.11), for a machine without Docker. The judge is the one in
# .env.eval, and queries are timed through SQL Lab unless TIMING_VIA says
# otherwise. Nothing is sent to DeepEval. A slow judge gets more time than
# DeepEval's defaults: 600 seconds for all of a golden's metrics
# (DEEPEVAL_PER_TASK_TIMEOUT_SECONDS_OVERRIDE) and 300 for one judge request
# (DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS_OVERRIDE); set either in .env.eval
# or the environment to change it.
#
# On macOS the Mac is kept awake until the run finishes (caffeinate), so you
# can lock the screen and leave it.
#
# The report is also saved to results/latest_report.txt (and, for a part,
# results/latest_report_<part>.txt), DeepEval's full console output to
# results/latest_console.log and each run's data to
# results/test_run_<timestamp>.json.
set -u

# Keep the Mac awake until the run finishes (macOS only): sleep freezes
# Docker and breaks the run's connections. Locking the screen is fine, and
# the display can still turn off; closing a laptop's lid still sleeps it.
if [ -z "${EVAL_CAFFEINATED:-}" ] && command -v caffeinate >/dev/null 2>&1; then
  # By full path, through sh: caffeinate looks a bare name up on the PATH,
  # and `sh run_evals.sh` gives $0 without a slash.
  EVAL_CAFFEINATED=1 exec caffeinate -i sh "$(cd "$(dirname "$0")" && pwd)/$(basename "$0")" "$@"
fi

cd "$(dirname "$0")"
mkdir -p results

# Options for a developer's machine: dev/options.sh, where the suite has its
# dev folder, takes the ones it knows (dev_option), sets dev_mode to the one
# it took, and then runs the suite its own way (dev_run).
dev_mode=""
dev_option() { return 1; }
dev_run() { :; }
if [ -f dev/options.sh ]; then
  . dev/options.sh
fi

# --runs N, --parallel N, --local and the dev options can go anywhere among
# the arguments; the others keep their order.
runs=1
runs_next=""
parallel=1
parallel_next=""
local=""
container=""
all_runs=""
for arg do
  shift
  if [ -n "$runs_next" ]; then
    runs=$arg
    runs_next=""
    continue
  fi
  if [ -n "$parallel_next" ]; then
    parallel=$arg
    parallel_next=""
    continue
  fi
  case "$arg" in
    --runs) runs=""; runs_next=1 ;;
    --runs=*) runs=${arg#--runs=} ;;
    --parallel) parallel=""; parallel_next=1 ;;
    --parallel=*) parallel=${arg#--parallel=} ;;
    --local) local=1 ;;
    --container) container=1 ;;
    --all-runs) all_runs=1 ;;
    *) dev_option "$arg" || set -- "$@" "$arg" ;;
  esac
done
case "$runs" in
  "" | *[!0-9]*) runs=0 ;;
esac
if [ "$runs" -lt 1 ]; then
  echo "--runs takes a whole number, 1 or more" >&2
  exit 2
fi
case "$parallel" in
  "" | *[!0-9]*) parallel=0 ;;
esac
if [ "$parallel" -lt 1 ]; then
  echo "--parallel takes a whole number, 1 or more" >&2
  exit 2
fi

part=""
case "${1:-}" in
  "" | -*) ;;
  *) part=$1; shift ;;
esac
if [ -n "$local" ] && [ -n "$dev_mode" ]; then
  echo "--local takes its judge from .env.eval, and $dev_mode chooses its own: use one or the other" >&2
  exit 2
fi
if [ -n "$container" ] && [ -n "$local$dev_mode" ]; then
  echo "--container is how the suite's image runs the suite, with its settings from the environment: use it on its own" >&2
  exit 2
fi

# A value set in .env.eval (the last one), or nothing.
setting() {
  sed -n "s/^$1=//p" .env.eval 2>/dev/null | tail -n 1
}

# Without --local or the dev options, the suite runs in its image, which
# runs this script again inside, with --container.
if [ -z "$local$container$dev_mode" ]; then
  if [ ! -f .env.eval ]; then
    echo "The suite reads its settings from .env.eval: copy .env.eval.example to .env.eval and fill it in." >&2
    exit 2
  fi
  . run/image.sh
  build_image || exit 1
  # docker run's arguments are built back to front: the run's options for the
  # script inside, then the image before them, then docker's own options.
  set -- --runs "$runs" --parallel "$parallel" ${all_runs:+--all-runs} ${part:+"$part"} "$@"
  set -- "$SUITE_IMAGE" "$@"
  # The files the settings name, where the container finds them by the same
  # setting: an absolute path at that path, a relative one under /suite.
  for name in JUDGE_CLIENT_CERT JUDGE_CLIENT_KEY JUDGE_CA_BUNDLE REQUESTS_CA_BUNDLE; do
    file=$(setting "$name")
    if [ -z "$file" ] || [ ! -f "$file" ]; then
      continue
    fi
    case "$file" in
      /*) set -- -v "$file:$file:ro" "$@" ;;
      *) set -- -v "$PWD/$file:/suite/$file:ro" "$@" ;;
    esac
  done
  # On Linux, run the container as you: its own user can't write to results/,
  # which belongs to you. Docker Desktop (macOS, Windows) maps ownership itself.
  if [ "$(uname -s)" = Linux ]; then
    set -- --user "$(id -u):$(id -g)" "$@"
  fi
  # The suite's code from this folder, so a change to it needs no rebuild;
  # the report and result files to this folder's results/.
  # The container has no docker command, so queries are timed through SQL
  # Lab, whatever .env.eval says, rather than psql in the database's
  # container (TIMING_VIA).
  docker run --rm --env-file .env.eval \
    -e TIMING_VIA=sqllab \
    -v "$PWD/evals:/suite/evals" \
    -v "$PWD/results:/suite/results" \
    "$@"
  exit $?
fi

# DeepEval's --repeat asks each golden again (using pytest-repeat); session
# scope makes each repeat a full pass over the goldens.
times=""
if [ "$runs" -gt 1 ]; then
  times=", each golden $runs times"
  set -- --repeat "$runs" --repeat-scope=session "$@"
fi
# DeepEval's -n shares the goldens out among that many pytest-xdist workers.
if [ "$parallel" -gt 1 ]; then
  times="$times, $parallel at a time"
  set -- -n "$parallel" "$@"
fi

# Runs the suite, then writes and prints the report. Paths are from the suite's
# folder: /suite in Docker, or this folder when it runs on this machine. A parallel run logs
# its locks and checks them after (evals/suite/locks.py).
run_suite='before=$(ls results/test_run_*.json 2>/dev/null | tail -n 1)
  rm -f results/latest_junit.xml
  rm -rf results/latest_locks
  if [ "${EVAL_PARALLEL:-1}" -gt 1 ]; then
    export EVAL_LOCK_LOG="$PWD/results/latest_locks"
  fi
  deepeval test run evals/llm_quality -m llm_quality \
    --junitxml=results/latest_junit.xml "$@" \
    > results/latest_console.log 2>&1
  after=$(ls results/test_run_*.json 2>/dev/null | tail -n 1)
  if [ -n "$after" ] && [ "$after" != "$before" ]; then
    python reports/report.py --run "$after" > results/latest_report.txt
  elif [ -f results/latest_junit.xml ]; then
    python reports/report.py --no-run > results/latest_report.txt
  else
    echo "No results were produced. Last lines of results/latest_console.log:"
    tail -n 25 results/latest_console.log
    exit 1
  fi
  status=$?
  if [ -n "${EVAL_LOCK_LOG:-}" ]; then
    python evals/suite/locks.py "$EVAL_LOCK_LOG" >> results/latest_report.txt
  fi
  cat results/latest_report.txt
  if [ -n "$EVAL_PART" ]; then
    cp results/latest_report.txt "results/latest_report_$EVAL_PART.txt"
  fi
  if [ -n "${JUDGE_COMPARE_MODELS:-}" ] && [ -n "$after" ] && [ "$after" != "$before" ]; then
    echo
    python evals/judge_compare.py --run "$after" $JUDGE_COMPARE_MODELS \
      > results/latest_judge_comparison.txt
    cat results/latest_judge_comparison.txt
  fi
  exit $status'

# On this machine, the suite's Python packages are in .venv.
if [ -n "$dev_mode" ] || [ -n "$local" ]; then
  . run/venv.sh
  setup_venv "${local:+--local}$dev_mode" || exit $?
  # .env.eval may give Superset's address as Docker sees it; from here that
  # is localhost. Values set here win over .env.eval's.
  url=$(setting SUPERSET_URL | sed 's#//host[.]docker[.]internal#//localhost#')
fi

# --local and --container run the suite here: --local in .venv, with its
# settings in .env.eval; --container in the suite's image, with the
# packages installed there and the settings in the environment.
if [ -n "$local$container" ]; then
  if [ -n "$local" ] && [ ! -f .env.eval ]; then
    echo "--local reads its settings from .env.eval: copy production/settings.env (or .env.eval.example) to .env.eval and fill it in." >&2
    exit 2
  fi
  if [ -n "$local" ]; then
    where="on this machine"
    path="$PWD/.venv/bin:$PATH"
  else
    where="in its container"
    path=$PATH
  fi
  # Set before DeepEval starts: .env.eval is only read once the suite's own
  # code is imported. A value in the environment wins over .env.eval's.
  provider=${JUDGE_PROVIDER:-$(setting JUDGE_PROVIDER)}
  model=${JUDGE_MODEL_NAME:-$(setting JUDGE_MODEL_NAME)}
  case "$provider" in
    none) judged="with no judge: only the rule-based metrics" ;;
    *) judged="judged by ${model:-the model in JUDGE_MODEL_NAME}" ;;
  esac
  # Timing through psql (the suite's default) runs docker. DeepEval allows 3
  # minutes for all of a golden's metrics, and one judge request may get as
  # little as 30 seconds: a slow self-hosted model needs more of both.
  timing=${TIMING_VIA:-$(setting TIMING_VIA)}
  task_limit=${DEEPEVAL_PER_TASK_TIMEOUT_SECONDS_OVERRIDE:-$(setting DEEPEVAL_PER_TASK_TIMEOUT_SECONDS_OVERRIDE)}
  attempt_limit=${DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS_OVERRIDE:-$(setting DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS_OVERRIDE)}
  echo "Running the LLM-quality suite${part:+ ($part)}$times $where, $judged..."
  env PATH="$path" \
    ${url:+SUPERSET_URL="$url"} \
    TIMING_VIA="${timing:-sqllab}" \
    DEEPEVAL_RESULTS_FOLDER="$PWD/results" \
    DEEPEVAL_PER_TASK_TIMEOUT_SECONDS_OVERRIDE="${task_limit:-600}" \
    DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS_OVERRIDE="${attempt_limit:-300}" \
    DEEPEVAL_TELEMETRY_OPT_OUT=1 \
    COLUMNS=120 \
    EVAL_PART="$part" \
    EVAL_PARALLEL="$parallel" \
    EVAL_ALL_RUNS="$all_runs" \
    sh -c "$run_suite" sh "$@"
  exit $?
fi

if [ -n "$dev_mode" ]; then
  dev_run "$@"
  exit $?
fi
