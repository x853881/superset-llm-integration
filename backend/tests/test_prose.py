"""How the model is told to write its prose: brief when the request asks, with hard terms marked up
only where the panel can show them, and the markup removed from anything that comes back as input."""

from flask import Flask

from waxwing.nltosqlandback import nlsql_backend, performance, prose, request_context, sql_breakdown
from waxwing.nltosqlandback.request_context import CallContext


def _bound(verbosity: str = "full"):
    return request_context.bound(CallContext(endpoint="test", verbosity=verbosity))


def test_verbosity_is_full_unless_brief_is_asked_for():
    assert prose.verbosity("brief") == prose.BRIEF
    assert prose.verbosity(" Brief ") == prose.BRIEF
    assert prose.verbosity(None) == prose.FULL
    assert prose.verbosity("terse") == prose.FULL


def test_brief_instructions_only_for_a_brief_request():
    with _bound("full"):
        assert "brief" not in prose.instructions("the summary")
    with _bound("brief"):
        text = prose.instructions("the summary", "one sentence")
        assert "brief" in text and "one sentence" in text
    # Outside a request (a test, a startup task) nothing asked for brief.
    assert not prose.is_brief()


def test_terms_are_marked_unless_turned_off():
    assert "[[term|meaning]]" in prose.instructions("the summary")
    app = Flask(__name__)
    app.config["NLSQL_MARK_TERMS"] = False
    with app.app_context():
        assert prose.instructions("the summary") == ""


def test_strip_terms_keeps_the_term():
    text = "Sorting makes this [[O(n log n)|Grows a little faster than the rows.]] overall, via a [[hash join|A join that builds a lookup table.]]."
    assert prose.strip_terms(text) == "Sorting makes this O(n log n) overall, via a hash join."
    # Brackets that aren't markup are left alone.
    assert prose.strip_terms("ARRAY[[1,2]] and [[no pipe]]") == "ARRAY[[1,2]] and [[no pipe]]"


def test_descriptions_never_get_term_markup():
    """A description goes into the question box, so it's told to be brief but never to mark terms."""
    with _bound("brief"):
        cold = nlsql_backend._cold_start_nl_prompt("SELECT 1", "")
        grounded = nlsql_backend._grounded_nl_update_prompt("Counts orders", "+ x", "SELECT 1", "")
    for prompt in (cold, grounded):
        assert "[[term|meaning]]" not in prompt
        assert "one short" in prompt


def test_prompts_carry_the_instructions():
    with _bound("brief"):
        assert "[[term|meaning]]" in nlsql_backend._refine_prompt("add a limit", "SELECT 1", "")
        assert "brief" in performance._accelerate_prompt("SELECT 1", None)
        breakdown = sql_breakdown._prompt("SELECT 1", "")
        assert 'leave it empty ("")' in breakdown
    with _bound("full"):
        assert "two to four sentences" in sql_breakdown._prompt("SELECT 1", "")
