"""Cleansed, transliterated and translated copies of columns, and what the model is told of them."""

from flask import Flask

from waxwing.nltosqlandback import derived_columns
from waxwing.nltosqlandback.derived_columns import DEFAULT_SUFFIXES, Policy
from waxwing.nltosqlandback.semantic_schema import ColumnDescription, TableDescription


def _table(*columns):
    return TableDescription(schema="crm", name="people", columns=list(columns))


def _column(name, description=None, key=False):
    return ColumnDescription(name=name, data_type="VARCHAR", description=description, is_foreign_key=key)


def _described(table):
    return {column.name: column.description for column in table.columns}


HIDE = Policy(DEFAULT_SUFFIXES)
SHOW = Policy(DEFAULT_SUFFIXES, hide_cleansed_originals=False)


def test_a_cleansed_column_s_original_is_hidden_and_its_meaning_carried_over():
    table = _table(_column("ID"), _column("PHONE", "Contact number"), _column("PHONE_CLEANSED"))
    assert _described(derived_columns.apply(table, HIDE)) == {
        "ID": None,
        "PHONE_CLEANSED": "Contact number (PHONE standardised into one format)",
    }


def test_both_are_shown_when_hiding_is_off_with_the_cleansed_one_preferred():
    table = _table(_column("phone"), _column("phone_cleansed", "E.164 phone number"))
    assert _described(derived_columns.apply(table, SHOW)) == {
        "phone": "the raw values: use phone_cleansed instead unless the raw value is asked for",
        "phone_cleansed": "E.164 phone number (phone standardised into one format; use this rather than phone)",
    }


def test_an_original_that_is_a_key_is_kept_for_joins():
    table = _table(_column("CUST_REF", key=True), _column("CUST_REF_CLEANSED"))
    assert [column.name for column in derived_columns.apply(table, HIDE).columns] == ["CUST_REF", "CUST_REF_CLEANSED"]


def test_transliterated_and_translated_copies_are_shown_beside_their_original():
    table = _table(_column("NAME", "Full name"), _column("NAME_TRANSLITERATED"), _column("NAME_TRANSLATED"))
    assert _described(derived_columns.apply(table, HIDE)) == {
        "NAME": "Full name",
        "NAME_TRANSLITERATED": "Full name (NAME transliterated into Latin script)",
        "NAME_TRANSLATED": "Full name (NAME translated)",
    }


def test_a_suffix_without_its_original_is_just_a_name():
    table = _table(_column("DATA_CLEANSED", "When the batch was cleaned"), _column("ADDRESS_TRANSLATED"))
    assert derived_columns.apply(table, HIDE) is table
    assert derived_columns.guidance([table], HIDE) is None


def test_the_prompt_is_told_how_to_choose_only_when_there_is_a_choice():
    cleansed = _table(_column("PHONE"), _column("PHONE_CLEANSED"))
    translated = _table(_column("CITY"), _column("CITY_TRANSLATED"))
    assert "use them" in derived_columns.guidance([cleansed], HIDE)
    assert "transliterated or translated" not in derived_columns.guidance([cleansed], HIDE)
    assert "matching how the value is written in the request" in derived_columns.guidance([translated], HIDE)


def test_the_suffixes_and_hiding_come_from_config():
    app = Flask(__name__)
    app.config["NLSQL_DERIVED_COLUMN_SUFFIXES"] = {"_STD": "cleansed"}
    app.config["NLSQL_HIDE_CLEANSED_ORIGINALS"] = False
    with app.app_context():
        policy = Policy.from_config()
    assert policy == Policy({"_STD": "cleansed"}, hide_cleansed_originals=False)
    assert Policy.from_config() == HIDE  # outside an app: the defaults
