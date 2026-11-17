import json

import pytest

from app.clips.llm import MAX_RESPONSE_ITEMS, parse_clip_response
from app.clips.llm_clients import LLMOutputError
from tests.llm_helpers import item

# --- Response parsing ---------------------------------------------------------


def test_parse_accepts_valid_items_and_code_fence():
    items, discarded = parse_clip_response(
        "```json\n" + json.dumps([item(10, 30), item(40, 60.5)]) + "\n```"
    )
    assert discarded == 0
    assert [(i.start_seconds, i.end_seconds) for i in items] == [(10, 30), (40, 60.5)]
    assert parse_clip_response("  []  ") == ([], 0)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("not json", id="not_json"),
        pytest.param("", id="empty"),
        pytest.param('{"start_seconds": 1}', id="object_not_array"),
        pytest.param('[{"start_seconds": NaN}]', id="nan_literal"),
        # Explicit short IDs: pytest exports the node ID through an environment
        # variable, which Windows limits to 32767 characters.
        pytest.param("[" * 100_000 + "]" * 100_000, id="deeply_nested"),
        pytest.param(
            json.dumps([item(10, 30)] * (MAX_RESPONSE_ITEMS + 1)), id="too_many_items"
        ),
        pytest.param(json.dumps([{"start_seconds": 1}, "x", 3]), id="no_valid_items"),
        pytest.param("Sure! Here are the clips: []", id="prose_prefix"),
    ],
)
def test_parse_rejects_unusable_answers(text):
    with pytest.raises(LLMOutputError):
        parse_clip_response(text)


@pytest.mark.parametrize(
    "bad",
    [
        {k: v for k, v in item(10, 30).items() if k != "title"},
        {**item(10, 30), "extra": 1},
        item("10", 30),
        item(True, 30),
        item(-1, 30),
        item(30, 30),
        item(30, 10),
        item(10, 30, score=1.5),
        item(10, 30, score=-0.1),
        item(10, 30, title=""),
        item(10, 30, title="   "),
        item(10, 30, title="x" * 201),
        item(10, 30, reason="r" * 501),
        item(10, 30, title="bad\x07title"),
        item(10, 30, title=5),
        None,
        [10, 30],
    ],
)
def test_parse_discards_malformed_items_whole(bad):
    items, discarded = parse_clip_response(json.dumps([item(40, 60), bad]))
    assert discarded == 1
    assert len(items) == 1 and items[0].start_seconds == 40


def test_parse_discards_non_finite_numbers():
    infinite = json.dumps(item(1, 30)).replace(
        '"start_seconds": 1,', '"start_seconds": 1e999,'
    )
    assert "1e999" in infinite
    items, discarded = parse_clip_response(f"[{infinite}, {json.dumps(item(40, 60))}]")
    assert discarded == 1 and len(items) == 1
