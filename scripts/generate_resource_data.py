#!/usr/bin/env python3
"""Regenerates the bundled resources: the table embeddings, then the *_data.py modules.

1. Embeds every table in semantic_schema.json - the exact text the backend embeds at runtime,
   `TableDescription.to_prompt_text()` with the document prefix - via the OpenAI-compatible
   embeddings endpoint, and writes the vectors to embeddings_cache.jsonl. Tables already in that
   file under the same text and model are reused rather than re-embedded; entries for anything
   else (tables since renamed or edited, cached queries) are dropped.

2. Mirrors each data file under resources/ into a .py module holding its exact text as a string
   literal. Superset loads extension backend code through its in-memory module loader
   (superset.extensions.utils.InMemoryLoader), which can only load .py modules - there is no real
   file on disk to read a resource from.

The model and document prefix must match the deployment's NLSQL_EMBEDDING_* config, or every
cached vector misses at runtime. The defaults below match dev/superset_config.py; override with
the same-named environment variables or the flags. The API key comes from NLSQL_EMBEDDING_API_KEY
or OPENROUTER_API_KEY, in the environment or the repo's .env.

Run after editing semantic_schema.json:

    python scripts/generate_resource_data.py           # embed what's missing, rewrite the modules
    python scripts/generate_resource_data.py --check   # exit 1 if anything is out of date (no API calls)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESOURCES = REPO / "backend/src/waxwing/nltosqlandback/resources"

# Rendered with the backend's own code, so the embedded text can't drift from what it looks up.
sys.path.insert(0, str(REPO / "backend/src"))
from waxwing.nltosqlandback.semantic_schema import _table_from_json  # noqa: E402

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "qwen/qwen3-embedding-8b"
DEFAULT_DOCUMENT_PREFIX = ""
TIMEOUT_SECONDS = 60

WHY_EMBEDDED = """\
Embedded as a Python literal (rather than left as a plain data file loaded via
importlib.resources) because Superset loads extension backend code through its own
in-memory module loader (see superset.extensions.utils.InMemoryLoader), which has no
real file on disk to read a resource from - only .py modules are loadable there."""


@dataclass(frozen=True)
class Resource:
    source: str
    module: str
    variable: str
    verb: str  # first word of the module docstring


SCHEMA = Resource("semantic_schema.json", "semantic_schema_data.py", "SEMANTIC_SCHEMA_JSON", "Auto-generated")
EMBEDDINGS = Resource("embeddings_cache.jsonl", "embeddings_cache_data.py", "EMBEDDINGS_CACHE_JSONL", "Generated")


def render(resource: Resource, text: str) -> str:
    header = (f"{resource.verb} from {resource.source} by scripts/generate_resource_data.py"
              " - do not edit by hand.")
    return f'"""{header}\n\n{WHY_EMBEDDED}\n"""\n\n{resource.variable}: str = {text!r}\n'


def dotenv(name: str) -> str:
    path = REPO / ".env"
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == name:
            return value.strip().strip("'\"")
    return ""


def api_key() -> str:
    for name in ("NLSQL_EMBEDDING_API_KEY", "OPENROUTER_API_KEY"):
        value = os.environ.get(name) or dotenv(name)
        if value:
            return value
    return ""


def embed(base_url: str, model: str, key: str, text: str) -> list[float]:
    # Same request and error handling as embedding_client._request, on the stdlib so the script
    # runs without the backend's dependencies installed.
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/embeddings",
        data=json.dumps({"model": model, "input": text}).encode("utf-8"),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = json.load(response)
    except urllib.error.HTTPError as ex:
        raise RuntimeError(f"HTTP {ex.code}: {ex.read().decode('utf-8', 'replace')}") from ex
    error = body.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise RuntimeError(message or "The embedding backend returned an error.")
    vector = (body.get("data") or [{}])[0].get("embedding") or []
    if not vector:
        raise RuntimeError("The embedding backend returned no vector.")
    return vector


def load_cache(path: Path) -> dict[tuple[str, str], list[float]]:
    if not path.is_file():
        return {}
    entries = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            entries[(entry["text"], entry["model"])] = entry["embedding"]
    return entries


def embeddings_jsonl(args: argparse.Namespace, cached: dict[tuple[str, str], list[float]]) -> str | None:
    """The jsonl text for every table's embedding, embedding the ones not already in `cached` -
    or None under --check when any are missing, since that needs API calls."""
    raw = json.loads((RESOURCES / SCHEMA.source).read_text(encoding="utf-8"))
    texts = [args.document_prefix + _table_from_json(t).to_prompt_text() for t in raw.get("tables", [])]
    missing = [text for text in texts if (text, args.model) not in cached]
    if missing and args.check:
        print(f"STALE: {EMBEDDINGS.source} is missing {len(missing)} of {len(texts)} tables for {args.model}")
        return None
    if missing:
        key = api_key()
        if not key:
            sys.exit("No API key: set NLSQL_EMBEDDING_API_KEY or OPENROUTER_API_KEY (environment or .env).")
        for i, text in enumerate(missing, 1):
            print(f"embedding {i}/{len(missing)}: {text.splitlines()[0][:80]}")
            cached[(text, args.model)] = embed(args.base_url, args.model, key, text)
    return "".join(
        json.dumps({"text": text, "model": args.model, "embedding": cached[(text, args.model)]}) + "\n"
        for text in texts
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="report stale resources instead of rewriting them, without calling the API")
    parser.add_argument("--base-url", default=os.environ.get("NLSQL_EMBEDDING_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.environ.get("NLSQL_EMBEDDING_MODEL", DEFAULT_MODEL))
    parser.add_argument("--document-prefix",
                        default=os.environ.get("NLSQL_EMBEDDING_DOCUMENT_PREFIX", DEFAULT_DOCUMENT_PREFIX))
    args = parser.parse_args()

    stale = []
    cache_path = RESOURCES / EMBEDDINGS.source
    jsonl = embeddings_jsonl(args, load_cache(cache_path))
    if jsonl is None:
        stale.append(EMBEDDINGS.source)
    else:
        current = cache_path.read_text(encoding="utf-8") if cache_path.is_file() else None
        if current == jsonl:
            print(f"up to date: {EMBEDDINGS.source}")
        elif args.check:
            stale.append(EMBEDDINGS.source)
            print(f"STALE: {EMBEDDINGS.source} holds entries for tables no longer in {SCHEMA.source}")
        else:
            cache_path.write_text(jsonl, encoding="utf-8")
            print(f"regenerated: {EMBEDDINGS.source}")

    for resource in (SCHEMA, EMBEDDINGS):
        source = RESOURCES / resource.source
        if not source.is_file():
            # Only reachable under --check, before the embeddings have ever been generated.
            continue
        target = RESOURCES / resource.module
        wanted = render(resource, source.read_text(encoding="utf-8"))
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current == wanted:
            print(f"up to date: {resource.module}")
            continue
        stale.append(resource.module)
        if args.check:
            print(f"STALE: {resource.module} does not match {resource.source}")
        else:
            target.write_text(wanted, encoding="utf-8")
            print(f"regenerated: {resource.module}")
    return 1 if args.check and stale else 0


if __name__ == "__main__":
    sys.exit(main())
