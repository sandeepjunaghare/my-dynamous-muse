"""What the owner node may say, and what of it is kept.

The answer is model output: lax and untrusted, validated once, then held to the pages the node
verifiably read by ``admit_owner``. Its own :class:`OwnerCitation` rather than qualification's
``Citation``, so ``sourcing`` imports nothing from ``qualification`` for its own node.
"""

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from app.shared.provenance import ProvenancedValue
from app.sourcing.schemas import Principal


class OwnerCitation(BaseModel):
    """The page naming the owner, and the words on it that name them."""

    url: str
    quote: str


class OwnerAnswer(BaseModel):
    """One call's answer: a person and where they are named, or ``name: null`` and why."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    title: str | None = None
    reason: str = ""
    citation: OwnerCitation | None = None


@dataclass(frozen=True)
class AdmittedOwner:
    """The owner the gate kept, or why it kept nobody — the reason, never the name."""

    owner: ProvenancedValue[Principal] | None
    omitted: str | None
