"""Splitting a stated owner name into ``firstname`` / ``lastname`` (OQ4-A).

Every case is a real shape from the 2026-10-11 sample of 3,000 census ``company_officer_1`` values.
The invariant under all of them: nothing is dropped, nothing is invented, and the casing is the
source's.
"""

import pytest

from app.promotion.names import NameSplit, split_owner_name

CASES: list[tuple[str, NameSplit]] = [
    # Two tokens: confident.
    ("DAVE MUSGRAVES", NameSplit(first="DAVE", last="MUSGRAVES", ambiguous=False)),
    ("CHANCE  BRITT", NameSplit(first="CHANCE", last="BRITT", ambiguous=False)),
    # Given + single-letter initial + surname: confident, the initial stays with the first name.
    ("MICHAEL J RICKETTS", NameSplit(first="MICHAEL J", last="RICKETTS", ambiguous=False)),
    ("JEFFREY J. MCCAIG", NameSplit(first="JEFFREY J.", last="MCCAIG", ambiguous=False)),
    # Three or more full words: middle name, compound surname or a glued title. First token, rest.
    ("RYAN BRENT SHANKLE", NameSplit(first="RYAN", last="BRENT SHANKLE", ambiguous=True)),
    ("ALONDRA GALLEGOS LARES", NameSplit(first="ALONDRA", last="GALLEGOS LARES", ambiguous=True)),
    ("JUAN JOSE DE LEON", NameSplit(first="JUAN", last="JOSE DE LEON", ambiguous=True)),
    ("CARL P MCLENDON CEO", NameSplit(first="CARL", last="P MCLENDON CEO", ambiguous=True)),
    # A trailing generational suffix stays attached to the last name and does not count as a word.
    ("ROBERT J MURPHY JR", NameSplit(first="ROBERT J", last="MURPHY JR", ambiguous=False)),
    ("DAVID WICKER III", NameSplit(first="DAVID", last="WICKER III", ambiguous=False)),
    # A comma appears only before a suffix in the sample; it becomes a space.
    (
        "TERRANCE TERRELL WILLIS, SR.",
        NameSplit(first="TERRANCE", last="TERRELL WILLIS SR.", ambiguous=True),
    ),
    ("SAM HOUSTON,JR.", NameSplit(first="SAM", last="HOUSTON JR.", ambiguous=False)),
    # A suffix token in first position is a name, not a suffix.
    ("JR WICKER", NameSplit(first="JR", last="WICKER", ambiguous=False)),
    # A single word: last name only, flagged.
    ("MADONNA", NameSplit(first=None, last="MADONNA", ambiguous=True)),
    ("WICKER JR", NameSplit(first=None, last="WICKER JR", ambiguous=True)),
    # Case is never changed: 22 of 3,000 are mixed case, and title-casing mangles MCDONALD.
    ("Ryan Roach", NameSplit(first="Ryan", last="Roach", ambiguous=False)),
    ("Jo Ann mcdonald", NameSplit(first="Jo", last="Ann mcdonald", ambiguous=True)),
]


@pytest.mark.parametrize(("name", "expected"), CASES, ids=[name for name, _ in CASES])
def test_each_sampled_shape_splits_as_decided(name: str, expected: NameSplit) -> None:
    assert split_owner_name(name) == expected


@pytest.mark.parametrize("name", [name for name, _ in CASES])
def test_nothing_is_dropped_and_nothing_invented(name: str) -> None:
    """Re-joined, the split is the stated name with whitespace collapsed and the comma a space."""
    split = split_owner_name(name)
    stated = " ".join(name.replace(",", " ").split())

    rejoined = split.last if split.first is None else f"{split.first} {split.last}"

    assert rejoined == stated


@pytest.mark.parametrize("name", ["", "   ", ","])
def test_a_blank_name_is_a_programming_error(name: str) -> None:
    """`Principal` refuses a blank name upstream, so one arriving here is a bug, not data."""
    with pytest.raises(ValueError, match="blank"):
        split_owner_name(name)


def test_no_title_is_stripped() -> None:
    """OQ4: a hard-coded title list is the sort of guess E18 warns about. It is flagged instead."""
    split = split_owner_name("CARL P MCLENDON CEO")

    assert split.last.endswith("CEO")
    assert split.ambiguous is True
