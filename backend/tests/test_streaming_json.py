from waxwing.nltosqlandback.streaming_json import StreamingJsonString, partial_string_field


def _feed(field: str, *chunks: str) -> list[str]:
    """What a consumer of `field` would be handed, chunk by chunk, as the text arrives."""
    partial = StreamingJsonString(field)
    buffer = ""
    emitted = []
    for chunk in chunks:
        buffer += chunk
        emitted.append(partial.feed(buffer))
    return emitted


def test_nothing_is_reported_before_the_field_arrives():
    assert _feed("sqlText", '{"status": "Suc', 'cess", "sql') == ["", ""]


def test_the_value_is_reported_in_the_pieces_it_arrives_in():
    assert _feed("sqlText", '{"sqlText": "SELECT', ' 1 FROM t', '", "tablesUsed": []}') == ["SELECT", " 1 FROM t", ""]


def test_escapes_are_decoded():
    assert partial_string_field(r'{"sqlText": "SELECT\n  \"a\"\tFROM t"}', "sqlText") == 'SELECT\n  "a"\tFROM t'


def test_a_unicode_escape_is_decoded():
    assert partial_string_field(r'{"sqlText": "café"}', "sqlText") == "café"


def test_a_half_arrived_escape_is_held_back_until_it_is_complete():
    # Reporting the backslash on its own would put a stray one in front of the user.
    # The chunk boundary falls between the backslash and the "n" of a \n escape.
    assert _feed("sqlText", '{"sqlText": "SELECT 1' + "\\", "n  FROM t") == ["SELECT 1", "\n  FROM t"]


def test_a_half_arrived_unicode_escape_is_held_back_until_it_is_complete():
    assert _feed("sqlText", r'{"sqlText": "caf\u00', 'e9 x"') == ["caf", "é x"]


def test_a_missing_field_reads_as_nothing_rather_than_an_error():
    assert partial_string_field('{"status": "Success"}', "sqlText") is None


def test_a_field_whose_value_is_not_a_string_reads_as_nothing():
    assert partial_string_field('{"sqlText": null, "x": 1}', "sqlText") is None
    assert partial_string_field('{"sqlText": ', "sqlText") is None


def test_the_value_stops_at_its_closing_quote():
    buffer = '{"sqlText": "SELECT 1", "sqlDetailedDescription": "counts the rows"}'
    assert partial_string_field(buffer, "sqlText") == "SELECT 1"
    assert partial_string_field(buffer, "sqlDetailedDescription") == "counts the rows"


def test_several_fields_are_tracked_independently():
    buffer = ""
    summary = StreamingJsonString("problemSummary")
    sql = StreamingJsonString("fixedSql")
    buffer += '{"problemSummary": "The column does not exist'
    assert (summary.feed(buffer), sql.feed(buffer)) == ("The column does not exist", "")
    buffer += '", "fixedSql": "SELECT 1"}'
    assert (summary.feed(buffer), sql.feed(buffer)) == ("", "SELECT 1")
