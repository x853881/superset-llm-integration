"""Columns that hold another column's values in a converted form, and what the model is told of them.

Some sites keep a converted copy of a column beside it, named after it with a suffix:
``PHONE_CLEANSED`` holds ``PHONE`` standardised into one format, ``NAME_TRANSLITERATED`` holds
``NAME`` in Latin script, ``NAME_TRANSLATED`` holds it translated. Which suffixes, and what each
means, is ``NLSQL_DERIVED_COLUMN_SUFFIXES`` - the ones below are what we've been told so far, still
to be confirmed::

    NLSQL_DERIVED_COLUMN_SUFFIXES = {
        "_CLEANSED": "cleansed",
        "_TRANSLITERATED": "transliterated",
        "_TRANSLATED": "translated",
    }
    NLSQL_HIDE_CLEANSED_ORIGINALS = True

**Cleansed** columns are the ones to use, so by default (``NLSQL_HIDE_CLEANSED_ORIGINALS``) the
original isn't shown to the model at all - it can't pick the wrong one - and the cleansed column
takes on its description. An original that is a key stays: joins need it. With the setting off
both are shown, the original marked as the one not to use.

**Transliterated and translated** columns are both shown with their original: which is right
depends on the request (a name typed in Latin script, or in the original one), so each is marked
as a copy of its original, and the prompt says how to choose (`guidance`).

A suffix only counts when the column without it is in the same table; a column that just happens
to end in one is left as it is. Names are matched ignoring case.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Optional

from flask import current_app

from .semantic_schema import ColumnDescription, TableDescription

CLEANSED = "cleansed"
TRANSLITERATED = "transliterated"
TRANSLATED = "translated"
DEFAULT_SUFFIXES = {"_CLEANSED": CLEANSED, "_TRANSLITERATED": TRANSLITERATED, "_TRANSLATED": TRANSLATED}

_NOTES = {
    TRANSLITERATED: "{original} transliterated into Latin script",
    TRANSLATED: "{original} translated",
}


@dataclass(frozen=True)
class Policy:
    #: Suffix (any case) -> what it means: "cleansed", "transliterated" or "translated".
    suffixes: dict[str, str]
    hide_cleansed_originals: bool = True

    @classmethod
    def from_config(cls) -> "Policy":
        try:
            config = current_app.config
        except RuntimeError:
            return cls(DEFAULT_SUFFIXES)
        return cls(
            dict(config.get("NLSQL_DERIVED_COLUMN_SUFFIXES", DEFAULT_SUFFIXES) or {}),
            bool(config.get("NLSQL_HIDE_CLEANSED_ORIGINALS", True)),
        )


def apply(table: TableDescription, policy: Policy) -> TableDescription:
    """`table` as the model should see it: cleansed originals left out (or marked), and each
    converted column's description saying what it is a copy of."""
    by_name = {column.name.lower(): column for column in table.columns}
    derived = _derived(table, policy)
    if not derived:
        return table
    cleansed_originals = {
        original.name.lower(): column_name for column_name, (original, kind) in derived.items() if kind == CLEANSED
    }
    columns: list[ColumnDescription] = []
    for column in table.columns:
        name = column.name.lower()
        if name in cleansed_originals:
            if policy.hide_cleansed_originals and not _is_key(column):
                continue
            cleansed = by_name[cleansed_originals[name]].name
            note = f"the raw values: use {cleansed} instead unless the raw value is asked for"
            columns.append(dataclasses.replace(column, description=f"{column.description} ({note})" if column.description else note))
            continue
        if name not in derived:
            columns.append(column)
            continue
        original, kind = derived[name]
        hidden = kind == CLEANSED and policy.hide_cleansed_originals and not _is_key(original)
        columns.append(_described_as_copy(column, original, kind, hidden))
    return dataclasses.replace(table, columns=columns)


def guidance(tables: list[TableDescription], policy: Policy) -> Optional[str]:
    """A line for the prompt's header saying how to choose between an original and its converted
    copies, when any of `tables` has one; None otherwise."""
    kinds = {kind for table in tables for _original, kind in _derived(table, policy).values()}
    lines = []
    if CLEANSED in kinds:
        lines.append("Columns ending in a cleansed suffix hold their original's values standardised into one format: use them.")
    if kinds & {TRANSLITERATED, TRANSLATED}:
        lines.append(
            "Columns marked as transliterated or translated copies hold their original's values converted: filter on the one "
            "matching how the value is written in the request, and show the original alongside unless asked otherwise."
        )
    return " ".join(lines) or None


def _derived(table: TableDescription, policy: Policy) -> dict[str, tuple[ColumnDescription, str]]:
    """Each converted column of `table`, by lower-cased name: its original, and what kind of copy it is."""
    by_name = {column.name.lower(): column for column in table.columns}
    derived = {}
    for column in table.columns:
        found = _original_of(column.name, by_name, policy)
        if found is not None:
            derived[column.name.lower()] = found
    return derived


def _original_of(name: str, by_name: dict[str, ColumnDescription], policy: Policy) -> Optional[tuple[ColumnDescription, str]]:
    upper = name.upper()
    # Longest first, so "_TRANSLITERATED" isn't read as some shorter suffix it happens to end with.
    for suffix, kind in sorted(policy.suffixes.items(), key=lambda pair: -len(pair[0])):
        if upper.endswith(suffix.upper()) and len(upper) > len(suffix):
            original = by_name.get(name[: -len(suffix)].lower())
            if original is not None:
                return original, kind
    return None


def _described_as_copy(column: ColumnDescription, original: ColumnDescription, kind: str, original_hidden: bool) -> ColumnDescription:
    if kind == CLEANSED:
        note = f"{original.name} standardised into one format" + ("" if original_hidden else "; use this rather than " + original.name)
    else:
        note = _NOTES.get(kind, f"{original.name} ({kind})").format(original=original.name)
    # The copy means what its original means, and the original is usually the one described.
    meaning = column.description or original.description
    return dataclasses.replace(column, description=f"{meaning} ({note})" if meaning else note)


def _is_key(column: ColumnDescription) -> bool:
    return column.is_primary_key or column.is_foreign_key
