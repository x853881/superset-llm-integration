"""Unit tests for what production runs: the smoke goldens, the suite's
image (Dockerfile, run_evals.sh --container), production's folder
(production/settings.env and production/job.yaml), and
./run_evals.sh --local, which runs the suite without Docker.

Offline: run_evals.sh runs in a copy of its folder whose .venv holds
stand-ins for python and deepeval, with another deepeval on the PATH, and a
stand-in docker records its arguments.

Run: pytest -m unit
"""

import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest
import sqlglot
from sqlglot import exp

from plugin.endpoints import ENDPOINTS, SQL, endpoint_for
from suite.config import unresolved_judge_settings
from suite.dataset import load_dataset, local_dataset, select_part

pytestmark = pytest.mark.unit

SUITE = Path(__file__).resolve().parents[2]


def _suite_file(*parts):
    """A file of the suite's folder that its image doesn't hold: run/unit.sh
    mounts these into the container, and a run of the image's own tests
    without them skips the tests that read them."""
    path = SUITE.joinpath(*parts)
    if not path.exists():
        pytest.skip(f"{path.relative_to(SUITE)} isn't in the suite's image")
    return path


###############################################
# Smoke goldens (goldens/smoke/)
###############################################


def _smoke():
    return list(load_dataset("smoke").goldens)


def test_smoke_part_is_only_the_smoke_goldens(monkeypatch):
    monkeypatch.delenv("GOLDENS_FILE", raising=False)
    smoke = _smoke()
    assert select_part(smoke, "smoke") == smoke
    assert 3 <= len(smoke) <= 6
    assert all(g.name.startswith("smoke_") for g in smoke)
    # No other part runs them: they aren't in the suite's own goldens at all.
    others = {g.name for g in local_dataset().goldens}
    assert not others & {g.name for g in smoke}


def test_smoke_goldens_are_complete_and_need_no_tables():
    for golden in _smoke():
        meta = golden.additional_metadata
        endpoint = endpoint_for(meta)
        assert endpoint.name in ENDPOINTS
        assert all(meta.get(field) for field in endpoint.required_metadata), golden.name
        queries = [meta.get("sql"), golden.expected_output]
        if endpoint.name in ("generate-nl", "refine-sql") or not golden.expected_output:
            queries.append(golden.input if endpoint.name == "generate-nl" else None)
        for sql in filter(None, queries):
            tables = list(sqlglot.parse_one(sql, dialect="postgres").find_all(exp.Table))
            assert not tables, f"{golden.name} reads a table: {sql}"
        if endpoint.output == SQL:
            assert golden.expected_output, f"{golden.name} needs a reference"


###############################################
# ./run_evals.sh --local and --container
###############################################


def _script(args, cwd=SUITE, env=None):
    return subprocess.run(
        ["sh", str(Path(cwd) / "run_evals.sh"), *args],
        capture_output=True,
        text=True,
        # Only these: the tests' own environment holds the developer's
        # settings. Without EVAL_CAFFEINATED, macOS would start caffeinate
        # around the script.
        env={
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", ""),
            "EVAL_CAFFEINATED": "1",
            **(env or {}),
        },
    )


def test_container_and_local_dont_go_together(tmp_path):
    folder = _copy_of_the_suite(tmp_path)
    done = _script(["--container", "--local"], cwd=folder)
    assert done.returncode == 2
    assert "on its own" in done.stderr
    assert not (folder / "docker-was-called").exists()


def _copy_of_the_suite(tmp_path, env_eval=""):
    """The run scripts in a folder of their own, with a .venv that looks set
    up, a deepeval that saves its environment and a docker that leaves a
    mark if anything calls it."""
    folder = tmp_path / "suite"
    (folder / "run").mkdir(parents=True)
    for name in (
        "run_evals.sh",
        "run/venv.sh",
        "run/image.sh",
        "requirements.txt",
        "requirements.lock",
    ):
        shutil.copy(SUITE / name, folder / name)
    bin = folder / ".venv" / "bin"
    bin.mkdir(parents=True)
    programs = {
        bin / "python": "exit 0",
        bin / "deepeval": "env > deepeval-env",
        # The image's own, as --container finds it.
        tmp_path / "bin" / "deepeval": "env > deepeval-env",
        tmp_path / "bin" / "docker": (
            f'touch "{folder}/docker-was-called"\n'
            f'{{ echo ---; printf "%s\\n" "$@"; }} >> "{folder}/docker-calls"'
        ),
    }
    (tmp_path / "bin").mkdir()
    for path, body in programs.items():
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)
    stamp = subprocess.run(
        "cat requirements.txt requirements.lock | cksum",
        shell=True, cwd=folder, capture_output=True, text=True,
    ).stdout
    (folder / ".venv" / ".requirements").write_text(stamp)
    if env_eval is not None:
        (folder / ".env.eval").write_text(env_eval)
    return folder


def _stand_ins(tmp_path):
    return f"{tmp_path / 'bin'}:/usr/bin:/bin"


def _seen_by_deepeval(folder):
    return dict(
        line.split("=", 1)
        for line in (folder / "deepeval-env").read_text().splitlines()
        if "=" in line
    )


def _local_run(tmp_path, env_eval, args=("smoke",), env=None):
    folder = _copy_of_the_suite(tmp_path, env_eval)
    done = _script(
        ["--local", *args], cwd=folder, env={"PATH": _stand_ins(tmp_path), **(env or {})}
    )
    return folder, done, _seen_by_deepeval(folder)


def test_local_runs_without_docker_and_sets_what_deepeval_needs(tmp_path):
    folder, done, seen = _local_run(
        tmp_path,
        "SUPERSET_URL=http://host.docker.internal:8088\n"
        "JUDGE_PROVIDER=openai\nJUDGE_MODEL_NAME=judge-model\n",
    )

    assert "on this machine, judged by judge-model" in done.stdout
    assert not (folder / "docker-was-called").exists()
    assert seen["EVAL_PART"] == "smoke"
    assert seen["TIMING_VIA"] == "sqllab"
    assert seen["DEEPEVAL_TELEMETRY_OPT_OUT"] == "1"
    assert seen["DEEPEVAL_RESULTS_FOLDER"] == str(folder.resolve() / "results")
    assert seen["DEEPEVAL_PER_TASK_TIMEOUT_SECONDS_OVERRIDE"] == "600"
    assert seen["DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS_OVERRIDE"] == "300"
    assert seen["SUPERSET_URL"] == "http://localhost:8088"
    assert seen["PATH"].startswith(str(folder.resolve() / ".venv" / "bin"))
    # The judge is .env.eval's, not one the script chose.
    assert "JUDGE_PROVIDER" not in seen and "JUDGE_MODEL_NAME" not in seen


def test_local_time_limits_come_from_the_settings_and_the_environment_wins(tmp_path):
    _, _, seen = _local_run(
        tmp_path,
        "SUPERSET_URL=https://superset.example.org\n"
        "DEEPEVAL_PER_TASK_TIMEOUT_SECONDS_OVERRIDE=900\n"
        "DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS_OVERRIDE=450\n",
        env={"DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS_OVERRIDE": "60"},
    )

    assert seen["DEEPEVAL_PER_TASK_TIMEOUT_SECONDS_OVERRIDE"] == "900"
    assert seen["DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS_OVERRIDE"] == "60"
    assert seen["SUPERSET_URL"] == "https://superset.example.org"


def test_local_needs_a_settings_file(tmp_path):
    folder = _copy_of_the_suite(tmp_path, env_eval=None)
    done = _script(["--local", "smoke"], cwd=folder)
    assert done.returncode == 2
    assert "production/settings.env" in done.stderr


def test_container_takes_its_settings_from_the_environment(tmp_path):
    """The image's run: no .env.eval and no .venv."""
    folder = _copy_of_the_suite(tmp_path, env_eval=None)
    done = _script(
        ["--container", "smoke"],
        cwd=folder,
        env={
            "PATH": _stand_ins(tmp_path),
            "JUDGE_MODEL_NAME": "judge-model",
            "SUPERSET_URL": "http://superset:8088",
            "DEEPEVAL_PER_TASK_TIMEOUT_SECONDS_OVERRIDE": "900",
        },
    )
    seen = _seen_by_deepeval(folder)

    assert "in its container, judged by judge-model" in done.stdout
    assert not (folder / "docker-was-called").exists()
    assert seen["PATH"] == _stand_ins(tmp_path)
    assert seen["SUPERSET_URL"] == "http://superset:8088"
    assert seen["EVAL_PART"] == "smoke"
    assert seen["TIMING_VIA"] == "sqllab"
    assert seen["DEEPEVAL_TELEMETRY_OPT_OUT"] == "1"
    assert seen["DEEPEVAL_RESULTS_FOLDER"] == str(folder.resolve() / "results")
    assert seen["DEEPEVAL_PER_TASK_TIMEOUT_SECONDS_OVERRIDE"] == "900"
    assert seen["DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS_OVERRIDE"] == "300"


def _docker_calls(folder):
    """Each call's arguments, in order."""
    text = (folder / "docker-calls").read_text()
    return [call.splitlines() for call in text.split("---\n")[1:]]


def test_without_local_the_suite_runs_in_its_image(tmp_path):
    """./run_evals.sh builds the image and runs it with .env.eval, the
    certificate files it names, and the run's options for the script inside."""
    cert = tmp_path / "client.pem"
    cert.write_text("certificate")
    folder = _copy_of_the_suite(
        tmp_path,
        f"JUDGE_CLIENT_CERT={cert}\n"
        "JUDGE_CA_BUNDLE=certs/ca.pem\n"
        "JUDGE_CLIENT_KEY=/nowhere/client.key\n",
    )
    (folder / "certs").mkdir()
    (folder / "certs" / "ca.pem").write_text("CA")
    done = _script(
        ["smoke", "--runs", "3", "--all-runs", "-k", "kenya"],
        cwd=folder,
        env={"PATH": _stand_ins(tmp_path), "SUITE_BASE_IMAGE": "base:1"},
    )
    assert done.returncode == 0, done.stderr
    build, run = _docker_calls(folder)

    assert build[0] == "build" and "BASE_IMAGE=base:1" in build
    assert run[0] == "run"
    assert run[run.index("--env-file") + 1] == ".env.eval"
    assert run[run.index("TIMING_VIA=sqllab") - 1] == "-e"
    mounts = [run[i + 1] for i, arg in enumerate(run) if arg == "-v"]
    here = folder.resolve()
    assert f"{cert}:{cert}:ro" in mounts
    assert f"{here}/certs/ca.pem:/suite/certs/ca.pem:ro" in mounts
    assert f"{here}/results:/suite/results" in mounts
    assert not any("client.key" in mount for mount in mounts)
    image = run.index("llm-eval-suite:dev")
    assert run[image + 1 :] == [
        "--runs", "3", "--parallel", "1", "--all-runs", "smoke", "-k", "kenya",
    ]
    assert not (folder / "deepeval-env").exists()


def test_without_local_the_suite_needs_a_settings_file(tmp_path):
    folder = _copy_of_the_suite(tmp_path, env_eval=None)
    done = _script(["smoke"], cwd=folder, env={"PATH": _stand_ins(tmp_path)})
    assert done.returncode == 2
    assert ".env.eval.example" in done.stderr
    assert not (folder / "docker-was-called").exists()


def test_the_image_builds_on_any_base_and_runs_the_smoke_goldens():
    lines = [
        line
        for line in _suite_file("Dockerfile").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines[0].startswith("ARG BASE_IMAGE=")
    assert lines[1] == "FROM ${BASE_IMAGE}"
    assert any("-c requirements.lock" in line for line in lines)
    assert 'ENTRYPOINT ["sh", "./run_evals.sh", "--container"]' in lines
    assert 'CMD ["smoke"]' in lines
    # Not root, and in the root group, which can write to /suite.
    assert [line for line in lines if line.startswith("USER")][-1] == "USER 1001:0"
    # The dev folder isn't published, so the image never has it.
    copied = [line.split()[1:-1] for line in lines if line.startswith("COPY")]
    assert not any("dev" in sources for sources in copied)


def _production_settings():
    text = _suite_file("production", "settings.env").read_text()
    return dict(
        line.split("=", 1)
        for line in text.splitlines()
        if "=" in line and not line.startswith("#")
    )


def _config(settings):
    """The suite's settings as a run given only `settings` would read them."""
    from suite.config import SuiteConfig

    keep = {k: v for k, v in os.environ.items() if not k.startswith(("SUPERSET_", "SQL_"))}
    with mock.patch.dict(os.environ, {**keep, **settings}, clear=True):
        return SuiteConfig.from_env()


def test_production_settings_have_what_a_run_needs():
    settings = _production_settings()
    assert settings["JUDGE_PROVIDER"] == "openai"
    assert settings["STRICT_LIVE"] == "1" and settings["TIMING_VIA"] == "sqllab"
    assert settings["JUDGE_API_KEY"] == ""
    for name in (
        "SUPERSET_URL",
        "SUPERSET_HEADERS",
        "JUDGE_BASE_URL",
        "JUDGE_MODEL_NAME",
        "JUDGE_CLIENT_CERT",
        "JUDGE_CLIENT_KEY",
    ):
        assert settings[name], name
    # Production's Superset takes its users from headers. Which headers is
    # for production to fill in, and the suite won't run until it has.
    assert settings["SUPERSET_AUTH"] == "headers"
    assert "SUPERSET_USERNAME" not in settings
    assert _config(settings).unresolved_placeholders() == [
        "SUPERSET_URL", "SUPERSET_DATABASE_ID", "SUPERSET_SCHEMA", "SUPERSET_HEADERS",
    ]
    # The judge's two are named in the same message.
    assert unresolved_judge_settings(settings) == ["JUDGE_BASE_URL", "JUDGE_MODEL_NAME"]
    # It becomes a ConfigMap: the password goes in a Secret.
    assert "SUPERSET_PASSWORD" not in settings


def test_the_job_has_the_settings_and_certificates_where_the_settings_say():
    job = _suite_file("production", "job.yaml").read_text()
    settings = _production_settings()
    assert "mountPath: /certs" in job
    for name in ("JUDGE_CLIENT_CERT", "JUDGE_CLIENT_KEY", "JUDGE_CA_BUNDLE"):
        path = Path(settings[name])
        assert path.parent == Path("/certs"), name
        # The Secret is made with --from-file=<that name>=<file>.
        assert f"--from-file={path.name}=" in job, name
    for name in ("llm-eval-suite-settings", "llm-eval-suite-login", "llm-eval-suite-certs"):
        assert f"name: {name}" in job or f"secretName: {name}" in job, name
    assert 'args: ["smoke"]' in job


def test_the_report_keeps_the_smoke_goldens(monkeypatch):
    """The report leaves out goldens no longer in the suite: the smoke
    goldens, in their own folder, are still in it."""
    monkeypatch.delenv("GOLDENS_FILE", raising=False)
    from unit.test_without_llm import _report_module

    names = _report_module()._suite_goldens()
    assert {g.name for g in _smoke()} <= names
    assert {g.name for g in local_dataset().goldens} <= names
