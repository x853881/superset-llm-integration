"""Judge model names, as DeepEval saves them with each judged metric."""

import re

# A judge may say where it ran after its model's name: "some-model (Somewhere)".
_WHERE = re.compile(r" \([^()]*\)")


def plain_model_name(name: str) -> str:
    """`name` without any note in brackets after the model."""
    return _WHERE.sub("", name)
