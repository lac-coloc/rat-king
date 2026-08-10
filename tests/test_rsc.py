import json

import pytest

from bk_crowns.rsc import RSCParseError, decode_next_chunks, iter_balanced_json


def test_decode_next_chunks_decodes_json_strings_from_multiple_scripts() -> None:
    chunks = ['0:{"message":"une \\"citation\\""}\n', '1:{"value":2}']
    html = "".join(
        f"<script>self.__next_f.push({json.dumps([1, chunk])})</script>" for chunk in chunks
    )

    assert decode_next_chunks(html) == chunks


def test_balanced_json_ignores_braces_inside_escaped_strings() -> None:
    text = 'prefix 12:{"name":"brace } and \\"quote\\"", "nested":{"value":3}} suffix'

    assert list(iter_balanced_json(text)) == [
        {"name": 'brace } and "quote"', "nested": {"value": 3}}
    ]


def test_decode_next_chunks_rejects_unbalanced_push_calls() -> None:
    html = '<script>self.__next_f.push([1,"unfinished"</script>'

    with pytest.raises(RSCParseError, match="non équilibré"):
        decode_next_chunks(html)


def test_decode_next_chunks_ignores_unrelated_scripts() -> None:
    assert decode_next_chunks("<script>console.log('hello')</script>") == []
