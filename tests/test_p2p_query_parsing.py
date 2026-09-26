"""The master query parser and the Android mirror share one contract.

``keep_blank_values=True`` (``_parse_request_query``) keeps the Python master
and the Android ``parseQueryParamLists`` / ``parseQueryParams`` in agreement:
``k=`` yields an empty value on both, and a repeated key resolves to its first
value on both.
"""

from hsync_client.p2p_server import _parse_request_query


def test_blank_values_are_preserved():
    query = _parse_request_query("/x?filename=&path=a/b")
    assert query["filename"] == [""]
    assert query["path"] == ["a/b"]


def test_repeated_key_keeps_every_value_and_first_wins():
    query = _parse_request_query("/x?k=1&k=2&k=3")
    assert query["k"] == ["1", "2", "3"]
    assert query["k"][0] == "1"


def test_missing_key_is_absent():
    query = _parse_request_query("/x?a=1")
    assert "b" not in query
