"""Swappable LLM judge for DeepEval metrics.

The judge is chosen entirely from environment variables, so the same suite can
grade with a hosted model in CI and a local model on an air-gapped box:

    JUDGE_PROVIDER          openai | anthropic | azure | ollama | openrouter | vllm
                            | none  (default: openai)
                            none runs only the deterministic SQL metrics
    JUDGE_MODEL_NAME        model id, e.g. gpt-4.1, llama3.1:70b  (required)
    JUDGE_API_KEY           optional; falls back to the provider's own env var
                            (OPENAI_API_KEY, ANTHROPIC_API_KEY, OPENROUTER_API_KEY, ...).
                            openai with JUDGE_BASE_URL and no key sends no
                            Authorization header, for a server that knows its
                            clients by their certificates
    JUDGE_BASE_URL          OpenAI-compatible base URL, Azure endpoint, Ollama host
                            or vLLM server (e.g. http://vllm:8000/v1). A
                            self-hosted OpenAI-compatible server: openai with
                            this set to its /v1 address
    JUDGE_CLIENT_CERT, JUDGE_CLIENT_KEY, JUDGE_CLIENT_KEY_PASSWORD, JUDGE_CA_BUNDLE
                            openai only: the client certificate and CA file for
                            a server reached over mutual TLS (judge/tls.py)
    JUDGE_HEADERS           openai only: extra headers on every judge request,
                            Name=value pairs separated by commas (judge/tls.py)
    JUDGE_TEMPERATURE       default 0 for reproducible grading
    JUDGE_AZURE_DEPLOYMENT  Azure deployment name (falls back to AZURE_DEPLOYMENT_NAME)
    JUDGE_AZURE_API_VERSION Azure API version (falls back to OPENAI_API_VERSION)
    JUDGE_THRESHOLD         pass mark for the judged metrics (default 0.8)
    JUDGE_EFFORT            only for a judge that takes an effort instead of
                            a temperature: low | medium | high | xhigh | max
                            (default medium)
    JUDGE_SECOND_OPINION_MODEL  optional: a model of the same provider that
                            re-judges borderline scores, and whose verdict
                            counts (judge/second_opinion.py)
    JUDGE_SECOND_OPINION_EFFORT the second model's effort, for such a judge
                            (default medium, whatever JUDGE_EFFORT is)
    JUDGE_SECOND_OPINION_MARGIN how far below the pass mark a score is still
                            borderline (default 0.1: 0.7 and 0.8 are re-asked)

A judge that exists only on a developer's machine lives in the suite's dev/
folder, which isn't published: when the folder is there, its suite_dev
package adds its providers with register_provider.
"""


import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Tuple

import httpx
from deepeval.config.settings import get_settings

from deepeval.models import (
    AnthropicModel,
    AzureOpenAIModel,
    DeepEvalBaseLLM,
    LocalModel,
    OllamaModel,
    OpenAIModel,
    OpenRouterModel,
)

from judge.second_opinion import SecondOpinionModel
from judge.tls import TlsSettings, parse_headers, ssl_context, warn_if_unused


PROVIDER_ALIASES = {
    "openai": "openai",
    "anthropic": "anthropic",
    "azure": "azure",
    "azure_openai": "azure",
    "ollama": "ollama",
    "openrouter": "openrouter",
    "vllm": "vllm",
    "local": "vllm",
    "openai_compatible": "vllm",
}

EFFORTS = ("low", "medium", "high", "xhigh", "max")


class JudgeConfigError(ValueError):
    """Raised when the JUDGE_* environment does not describe a usable judge."""


@dataclass(frozen=True)
class JudgeSettings:
    provider: str
    model_name: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: float = 0.0
    azure_deployment: Optional[str] = None
    azure_api_version: Optional[str] = None
    effort: Optional[str] = None
    #: The client certificate and CAs for an HTTPS server (judge/tls.py).
    tls: TlsSettings = TlsSettings()
    #: Extra headers for every judge request, as (name, value) pairs.
    headers: Tuple[Tuple[str, str], ...] = ()
    #: The settings these were read from, for a provider added with
    #: register_provider that has settings of its own.
    env: Mapping[str, str] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "JudgeSettings":
        raw_provider = env.get("JUDGE_PROVIDER", "openai").strip().lower()
        provider = PROVIDER_ALIASES.get(raw_provider)
        if provider is None:
            raise JudgeConfigError(
                f"Unsupported JUDGE_PROVIDER={raw_provider!r}. "
                f"Expected one of: {', '.join(sorted(set(PROVIDER_ALIASES)))}"
            )

        model_name = env.get("JUDGE_MODEL_NAME", "").strip()
        if not model_name:
            raise JudgeConfigError("JUDGE_MODEL_NAME must be set.")

        try:
            temperature = float(env.get("JUDGE_TEMPERATURE", "0"))
        except ValueError as e:
            raise JudgeConfigError("JUDGE_TEMPERATURE must be a number.") from e

        effort = env.get("JUDGE_EFFORT", "").strip().lower() or None
        if effort and effort not in EFFORTS:
            raise JudgeConfigError(
                f"JUDGE_EFFORT={effort!r}: expected one of {', '.join(EFFORTS)}"
            )

        try:
            headers = parse_headers(env.get("JUDGE_HEADERS", ""))
        except ValueError as e:
            raise JudgeConfigError(str(e)) from e

        return cls(
            provider=provider,
            model_name=model_name,
            api_key=env.get("JUDGE_API_KEY") or None,
            base_url=env.get("JUDGE_BASE_URL") or None,
            temperature=temperature,
            azure_deployment=env.get("JUDGE_AZURE_DEPLOYMENT") or None,
            azure_api_version=env.get("JUDGE_AZURE_API_VERSION") or None,
            effort=effort,
            tls=TlsSettings.from_env(env),
            headers=headers,
            env=env,
        )


# What DeepEval's OpenAIModel and the openai SDK are given when there is no
# key: neither builds a client without one. It is never sent (_openai).
NO_API_KEY = "EMPTY"


def _drop_authorization(request: httpx.Request) -> None:
    request.headers.pop("Authorization", None)


async def _drop_authorization_async(request: httpx.Request) -> None:
    request.headers.pop("Authorization", None)


def _openai(s: JudgeSettings) -> DeepEvalBaseLLM:
    """OpenAI, or with JUDGE_BASE_URL any server that speaks its API.

    Such a server may want a client certificate, extra headers and no API key,
    as the plugin's own model calls allow. DeepEval takes one HTTP client for
    its plain calls and one for its asynchronous ones, which the judged
    metrics use, so both carry the certificate and the headers."""
    try:
        context = ssl_context(s.tls)
    except ValueError as e:
        raise JudgeConfigError(str(e)) from e
    warn_if_unused(s.tls, s.base_url)

    api_key = s.api_key
    hooks, async_hooks = [], []
    if s.base_url and not api_key and not get_settings().OPENAI_API_KEY:
        # The SDK sends "Authorization: Bearer <key>" whatever the key, and
        # some gateways treat a made-up one as a bad credential. Take the
        # header off as each request leaves.
        api_key = NO_API_KEY
        hooks, async_hooks = [_drop_authorization], [_drop_authorization_async]

    clients = {}
    if context is not None or s.headers or hooks:
        options = dict(
            verify=context if context is not None else True,
            headers=dict(s.headers),
        )
        clients = dict(
            http_client=httpx.Client(event_hooks={"request": hooks}, **options),
            async_http_client=httpx.AsyncClient(
                event_hooks={"request": async_hooks}, **options
            ),
        )
    return OpenAIModel(
        model=s.model_name,
        api_key=api_key,
        base_url=s.base_url,
        temperature=s.temperature,
        **clients,
    )


def _anthropic(s: JudgeSettings) -> DeepEvalBaseLLM:
    return AnthropicModel(
        model=s.model_name, api_key=s.api_key, temperature=s.temperature
    )


def _azure(s: JudgeSettings) -> DeepEvalBaseLLM:
    # None values let DeepEval fall back to AZURE_OPENAI_ENDPOINT,
    # AZURE_DEPLOYMENT_NAME and OPENAI_API_VERSION.
    return AzureOpenAIModel(
        model=s.model_name,
        api_key=s.api_key,
        base_url=s.base_url,
        deployment_name=s.azure_deployment,
        api_version=s.azure_api_version,
        temperature=s.temperature,
    )


def _ollama(s: JudgeSettings) -> DeepEvalBaseLLM:
    return OllamaModel(
        model=s.model_name,
        base_url=s.base_url or "http://ollama:11434",
        temperature=s.temperature,
    )


def _openrouter(s: JudgeSettings) -> DeepEvalBaseLLM:
    # api_key None falls back to OPENROUTER_API_KEY.
    return OpenRouterModel(
        model=s.model_name,
        api_key=s.api_key,
        base_url=s.base_url,
        temperature=s.temperature,
    )


def _vllm(s: JudgeSettings) -> DeepEvalBaseLLM:
    if not s.base_url:
        raise JudgeConfigError(
            "JUDGE_BASE_URL is required for vllm, e.g. http://vllm:8000/v1"
        )
    # vLLM's OpenAI-compatible server ignores the key unless started with
    # --api-key, but the client still needs a non-empty value.
    return LocalModel(
        model=s.model_name,
        base_url=s.base_url,
        api_key=s.api_key or "EMPTY",
        temperature=s.temperature,
    )


_BUILDERS: Dict[str, Callable[[JudgeSettings], DeepEvalBaseLLM]] = {
    "openai": _openai,
    "anthropic": _anthropic,
    "azure": _azure,
    "ollama": _ollama,
    "openrouter": _openrouter,
    "vllm": _vllm,
}


def register_provider(
    name: str,
    builder: Callable[[JudgeSettings], DeepEvalBaseLLM],
    aliases: Tuple[str, ...] = (),
) -> None:
    """Adds a judge that JUDGE_PROVIDER=<name>, or any of `aliases`, chooses."""
    _BUILDERS[name] = builder
    for alias in (name, *aliases):
        PROVIDER_ALIASES[alias] = name


def build_judge(settings: JudgeSettings) -> DeepEvalBaseLLM:
    return _BUILDERS[settings.provider](settings)


def judge_enabled(env: Mapping[str, str] = os.environ) -> bool:
    return env.get("JUDGE_PROVIDER", "openai").strip().lower() != "none"


def get_judge_model(env: Mapping[str, str] = os.environ) -> DeepEvalBaseLLM:
    settings = JudgeSettings.from_env(env)
    judge = build_judge(settings)
    second = env.get("JUDGE_SECOND_OPINION_MODEL", "").strip()
    if not second or second == settings.model_name:
        return judge
    try:
        margin = float(env.get("JUDGE_SECOND_OPINION_MARGIN", "0.1"))
    except ValueError as e:
        raise JudgeConfigError("JUDGE_SECOND_OPINION_MARGIN must be a number.") from e
    effort = env.get("JUDGE_SECOND_OPINION_EFFORT", "").strip().lower() or "medium"
    if effort not in EFFORTS:
        raise JudgeConfigError(
            f"JUDGE_SECOND_OPINION_EFFORT={effort!r}: expected one of {', '.join(EFFORTS)}"
        )
    return SecondOpinionModel(
        judge,
        build_judge(replace(settings, model_name=second, effort=effort)),
        judge_threshold(env),
        margin,
    )


def judge_threshold(env: Mapping[str, str] = os.environ) -> float:
    return float(env.get("JUDGE_THRESHOLD", "0.8"))


# Judges that exist only on a developer's machine: the suite's dev/ folder,
# when it is there, registers them. Last, as it imports this module.
_DEV = Path(__file__).resolve().parents[2] / "dev"
if (_DEV / "suite_dev").is_dir():
    if str(_DEV) not in sys.path:
        sys.path.append(str(_DEV))
    import suite_dev  # noqa: E402,F401
