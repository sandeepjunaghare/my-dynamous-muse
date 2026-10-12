"""Splitting a stated owner name into HubSpot's ``firstname`` / ``lastname`` (OQ4-A). Pure.

The owner arrives as one cited string (census ``company_officer_1``), and HubSpot wants two fields.
Splitting is a write-time transformation of a cited value, and a careless split is exactly the E18
failure the write-gate exists to prevent, so the rule is the one that guesses least:

* **First token → ``firstname``; everything after it → ``lastname``.** Nothing is dropped. The
  first token was the given name in every sampled case, so the name a caller says aloud is right;
  compound surnames (``GALLEGOS LARES``, ``DE LEON``) survive intact.
* **A single-letter initial after the given name goes with the first name** (``MICHAEL J`` /
  ``RICKETTS``), which keeps the surname clean without dropping the initial.
* **A trailing generational suffix** (JR, SR, II, III, IV) stays on the last name and does not count
  as a word; in first position it is a name (``JR WICKER``). A comma, which only ever precedes a
  suffix in the sample, becomes a space.
* **Three or more full words are ambiguous** — a middle name, a compound surname or a glued title
  (``CARL P MCLENDON CEO``) cannot be told apart from the string — and are **flagged**, not guessed.
  No title list strips anything: that list would be the guess.
* **Case is never changed.** Title-casing mangles ``MCDONALD`` and ``DE LA CRUZ``, and the citation
  is to the stated string.

Measured on 3,000 census ``company_officer_1`` values (TX brokers, 2026-10-11): every name is
given-name first; 73.5% split confidently, 26.5% are ambiguous, 1 is a single word; 22 are mixed
case and the rest ALL CAPS.
"""

import re
from dataclasses import dataclass

_SUFFIXES = frozenset({"JR", "JR.", "SR", "SR.", "II", "III", "IV"})
_INITIAL = re.compile(r"^[A-Za-z]\.?$")


@dataclass(frozen=True)
class NameSplit:
    """A stated name split for HubSpot. ``first`` is ``None`` for a single word."""

    first: str | None
    last: str
    ambiguous: bool


def split_owner_name(name: str) -> NameSplit:
    """Split a stated owner name per OQ4-A. Output strings are the input's tokens, re-joined.

    Raises ``ValueError`` on a blank name: ``Principal`` refuses one upstream, so it is a bug here.
    """
    tokens = name.replace(",", " ").split()
    if not tokens:
        raise ValueError("owner name is blank")

    has_suffix = len(tokens) > 1 and tokens[-1].upper() in _SUFFIXES
    core = tokens[:-1] if has_suffix else tokens
    suffix = tokens[-1:] if has_suffix else []

    if len(core) == 1:
        return NameSplit(first=None, last=" ".join(tokens), ambiguous=True)
    if len(core) == 2:
        return NameSplit(first=core[0], last=" ".join([core[1], *suffix]), ambiguous=False)
    if len(core) == 3 and _INITIAL.match(core[1]):
        return NameSplit(
            first=" ".join(core[:2]), last=" ".join([core[2], *suffix]), ambiguous=False
        )
    return NameSplit(first=core[0], last=" ".join(tokens[1:]), ambiguous=True)
