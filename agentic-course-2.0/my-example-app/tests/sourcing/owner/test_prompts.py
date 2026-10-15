"""The owner node's prompt: generic across verticals, carrying the candidate's cited identity."""

import pytest

from app.sourcing.owner.prompts import SYSTEM_PROMPT, build_user_prompt
from tests.sourcing.builders import (
    CENSUS_URL,
    QCMOBILE_URL,
    SEARCH_RESULT_URL,
    a_candidate,
    looked_up,
    sourced,
)

VERTICAL_WORDS = ("freight", "broker", "carrier", "fire", "sprinkler")
"""The seeded verticals' slugs and vocabulary: none may bias a node every vertical shares."""


@pytest.mark.parametrize("word", VERTICAL_WORDS)
def test_the_system_prompt_names_no_vertical(word: str) -> None:
    assert word not in SYSTEM_PROMPT.lower()


def test_every_cited_field_is_rendered_with_its_url() -> None:
    fields = a_candidate("555").model_copy(
        update={
            "email": sourced("bryan@acme.test"),
            "website": looked_up("https://acme.test"),
        }
    )
    prompt = build_user_prompt(fields)

    assert "registry id: 555" in prompt
    assert "Acme Logistics 555 LLC" in prompt
    assert "Acme Freight" in prompt
    assert "2100 Olympic Dr, Arlington, TX 76011" in prompt
    assert "+1-817-555-0100" in prompt
    assert QCMOBILE_URL in prompt
    assert "bryan@acme.test" in prompt
    assert f"https://acme.test (source: {SEARCH_RESULT_URL})" in prompt
    assert CENSUS_URL in prompt


def test_an_absent_field_prints_no_line() -> None:
    prompt = build_user_prompt(a_candidate("555", with_phone=False))
    assert "phone" not in prompt
    assert "email" not in prompt
    assert "website" not in prompt
