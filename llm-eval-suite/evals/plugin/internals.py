"""What happened inside the plugin while it answered a golden, from the
progress stream every answer arrives in. No LLM involved, and nothing to
configure in the plugin or Superset.

Each step the plugin reports carries how long it took, and a step it redoes
because the first draft wasn't valid is restarted as a "Second attempt".
"""

from typing import Any, Dict, List

# How the plugin labels a step it is redoing (see nlsql_backend,
# sql_error_fix and performance in the plugin).
SECOND_ATTEMPT = "Second attempt"


def plugin_steps(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Each step the plugin reported, in order, as it last described it:
    {"key", "label", "status", "seconds", "detail"}."""
    steps: Dict[str, Dict[str, Any]] = {}
    for event in events:
        if event.get("type") != "stage" or not event.get("key"):
            continue
        steps[event["key"]] = {
            name: event.get(name)
            for name in ("key", "label", "status", "seconds", "detail")
        }
    return list(steps.values())


def retries_in_stream(events: List[Dict[str, Any]]) -> int:
    """How many times the plugin restarted a step as a second attempt."""
    retries, attempting = 0, set()
    for event in events:
        if event.get("type") != "stage":
            continue
        key = event.get("key")
        if str(event.get("detail") or "").startswith(SECOND_ATTEMPT):
            if key not in attempting:
                retries += 1
                attempting.add(key)
        elif event.get("status") != "active":
            attempting.discard(key)
    return retries
