# LLM-quality suite for the nltosqlandback Superset plugin

This suite sends the plugin a fixed set of test questions (**goldens**)
through its API and grades every answer. Rule-based checks grade what can be
checked exactly: whether the SQL is safe, returns the right rows and is fast
enough. An LLM **judge** grades the rest: whether the SQL fits the question
and whether the explanations are accurate.

**To run it in production**, you need only the [production/](production/)
folder: the two files you fill in are there, and its
[README](production/README.md) has the steps.

## What's where

```
production/         what production fills in, and the steps to run it there
  README.md           the steps
  settings.env        the settings, which become a ConfigMap
  job.yaml            the Kubernetes Job
evals/              the suite itself, which its image holds
  goldens/            the test questions, one file per endpoint
    smoke/            a few that need no tables
  metrics/            rule-based metrics
  judge/              the LLM judge: prompts, metrics, context, model
  plugin/             calling Superset and the plugin
  suite/              settings, loading goldens, which metrics grade what, parallel locks, psql
  llm_quality/        the live suite
  unit/               offline tests of the suite's own code
  *.py                scripts: record results, check or compare judges, refresh table notes,
                      read table rankings from the plugin's log (table_ranking.py),
                      recall at k of the plugin's table ranking (shortlist_recall.py)
reports/            the report printed after each run, and a page comparing two runs
run/                helpers: unit tests, the image build, the local .venv
test_data/          loads the dev test tables into a local Superset
run_evals.sh        runs the suite and prints the report
Dockerfile          the suite's image, for production and this machine
.env.eval.example   the settings for a developer's machine: copy it to .env.eval
```

Git leaves out `.env.eval` (your settings and keys), `results/` (each run's
report and data) and `.venv/`.

## Setting it up

You need Docker Desktop, and Superset running locally with the plugin
installed. The suite runs in its own image, the one production runs
([Dockerfile](Dockerfile)), which `./run_evals.sh` builds for you.

1. **Get Superset with the plugin.** Use Superset 6.1.0, the version
   production runs: `git clone https://github.com/apache/superset.git`, then
   `git checkout 6.1.0` in it. Build the plugin at the root of this repo
   (`pip install apache-superset-extensions-cli`, then
   `superset-extensions bundle`) and copy the `nltosqlandback-*.supx` it makes
   into the Superset checkout's `docker/extensions-installed/`.
2. **Point the plugin at its model, and start Superset.** Copy
   [deploy/superset_config_docker.py](../deploy/superset_config_docker.py) to
   the checkout's `docker/pythonpath_dev/superset_config_docker.py`. It
   already points the plugin at OpenRouter; give it your OpenRouter API key,
   in the file or as `OPENROUTER_API_KEY=...` in the checkout's
   `docker/.env-local`. Then run `docker compose up` in the checkout and check
   that http://localhost:8088 loads.

   These are the steps in the repo's [README](../README.md). Don't use the
   repo's own `docker-compose.yaml` for the suite: it's for working on the
   plugin with hot reload, and has no Postgres examples database to load the
   test tables into.
3. **Load the test data.** Download the ACLED files listed in `SOURCES` in
   [test_data/load_acled.py](test_data/load_acled.py) from
   https://acleddata.com (an account is needed), then run
   `./test_data/load_acled.sh` (it looks in `~/Downloads`; set `ACLED_DIR` to
   use another folder). For the other datasets, see
   [test_data/README.md](test_data/README.md).
4. **Configure.** Run `cp .env.eval.example .env.eval`, then add your
   `OPENROUTER_API_KEY` (the judge's key; the plugin's is the one in step 2)
   and, if your login isn't admin/admin, your Superset login. Don't put
   quotes around values. The example file explains every setting.
5. **Check it works.** Run `run/unit.sh` (offline, takes seconds), then
   `./run_evals.sh -k kenya` (one golden against your Superset).

`--local` runs the suite on your machine instead of in Docker, in a local
`.venv` (Python 3.11, created on the first run), with the judge set in
`.env.eval`.

Where the suite has its `dev/` folder, [dev/README.md](dev/README.md) lists
more options for a developer's machine. The folder isn't published, so a
published copy of the suite has none of them.

## Running in production

Everything production needs is in [production/](production/): the settings
and the Kubernetes Job it fills in, and a [README](production/README.md)
with the steps, what to do if one fails and what to do after the first run.
When a step changes, change it in that README and in the comments at the top
of [production/job.yaml](production/job.yaml), which number the steps the
same way.

**Testing a change before production has it.** On a developer's machine,
`./run_evals.sh` and `run/unit.sh` build the same image and run it, as the
Job does, against your local Superset with the settings in `.env.eval`.
`SUITE_BASE_IMAGE` builds it on production's base image:

```bash
SUITE_BASE_IMAGE=registry.access.redhat.com/ubi9/python-311 run/unit.sh
SUITE_BASE_IMAGE=registry.access.redhat.com/ubi9/python-311 ./run_evals.sh smoke
```

`--local` still runs the suite without Docker, in `.venv`.

## Running it

```bash
./run_evals.sh                       # every golden, once
./run_evals.sh --local               # ...on this machine, without Docker
./run_evals.sh --local smoke         # a few goldens that need no tables
./run_evals.sh sql                   # one part: sql, fix, accelerate, describe, explain-plan, prose, clarify, ...
./run_evals.sh -k "refine or kenya"  # goldens by name
./run_evals.sh --runs 3              # every golden 3 times, for pass rates
./run_evals.sh --parallel 3          # 3 goldens at a time
```

The comment at the top of [run_evals.sh](run_evals.sh) lists every option and
part. The report prints at the end of a run and is saved to
`results/latest_report.txt`, next to the run's raw JSON (`results/` is not
committed).

## Where to make changes

Paths are under `evals/` unless they start with another of the suite's
folders. Settings go in `.env.eval`, and their defaults are in
`suite/config.py`.

| To change… | Edit |
|---|---|
| A golden: add, fix or remove one | `goldens/<endpoint>.json`, one file per endpoint. Rerun it with `-k <name>` |
| A slow accelerate original's recorded result | `record_expected_results.py <name>`. Re-record everything after reloading data |
| How strict a judged metric is | Its steps and rubric in `judge/prompts.py`, then run `judge_canaries.py` |
| Pass marks, time limits, row limits, speed-up required | `.env.eval` (`JUDGE_THRESHOLD`, `PLUGIN_LATENCY_BUDGET_S`, `ACCELERATE_MIN_SAVING`, ...) |
| Which model judges | `JUDGE_PROVIDER` / `JUDGE_MODEL_NAME` in `.env.eval`. Code: `judge/model.py` |
| How the judge connects (certificates, headers, no API key) | `JUDGE_CLIENT_CERT` and the settings beside it in `.env.eval`. Code: `judge/tls.py`, `judge/model.py` |
| The smoke goldens | `goldens/smoke/smoke.json`. They must need no tables |
| What the judge sees besides the answer | `judge/context.py` |
| A rule-based check | `metrics/`: safety in `sql_safety.py`, performance in `sql_performance.py`, row comparison in `query_results.py`, timing in `timing.py` |
| Which metrics grade which golden | The metric lists and `BUILDERS` in `suite/endpoint_metrics.py` |
| A new metric | `metrics/` (subclass `PassFailMetric`) or `judge/metrics.py`; register it in `suite/endpoint_metrics.py`; describe it in `METRICS` in [reports/report.py](reports/report.py); add a unit test |
| A new plugin endpoint | `ENDPOINTS` in `plugin/endpoints.py`, then goldens, metrics, and a part in `suite/dataset.py` and `run_evals.sh` |
| The parts of the suite | `SUITE_PARTS` / `GOLDEN_PARTS` in `suite/dataset.py` |
| Table notes (types, NOT NULL, indexes) the judge uses | Regenerate `schema_metadata.json` with `generate_schema_metadata.py`. Hand-written fields are kept |
| The report's layout or grade | [reports/report.py](reports/report.py) |
| A page comparing two runs (`python reports/dashboard.py`, offline HTML) | [reports/dashboard.py](reports/dashboard.py) and [reports/dashboard_template.html](reports/dashboard_template.html) |
| What production fills in, or its Job | [production/settings.env](production/settings.env) and [production/job.yaml](production/job.yaml); the steps are in [production/README.md](production/README.md). A new setting production must set goes in `settings.env` too |

After any change, run `run/unit.sh` (or `run/unit.sh --local`). It catches
most slips, such as a golden in the wrong file or a metric with no builder.
Changing the judge, the goldens or a pass mark makes earlier runs
incomparable.
