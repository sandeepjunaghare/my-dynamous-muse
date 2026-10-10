"""The routing shapes: a geocoded point, a route, what a geocode found, and what a run produced.

**D13: coordinates come from the US Census Geocoder, never from Google Places.** Places lat/lng may
be cached for only 30 days and is barred as input to point-in-polygon analysis. The Census Geocoder
is free and public domain, so its points may be stored, and every :class:`GeoPoint` here is one.
"""

from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.shared.provenance import ProvenancedValue
from app.sourcing.schemas import StageCount, StageName


class GeoPoint(BaseModel):
    """A latitude and longitude from the Census Geocoder (D13: never from Places).

    Frozen because ``ProvenancedValue`` refuses to cite a value that could be edited afterwards.
    """

    model_config = ConfigDict(frozen=True)

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class RoutePoint(BaseModel):
    """One door the clustering places: the candidate, where it is, and its ZIP for the label."""

    model_config = ConfigDict(frozen=True)

    candidate_id: UUID
    registry_id: str
    point: GeoPoint
    postal_code: str


class RouteCluster(BaseModel):
    """One drive route: its place in label order, its label, and its doors in registry-id order."""

    model_config = ConfigDict(frozen=True)

    index: int = Field(ge=0)
    label: str
    """``"A · 75238"``: a letter in geographic order and the route's most common ZIP, the way the
    hand-built list names its routes ("Route Cluster B: 75238")."""

    anchor_postal_code: str
    members: tuple[RoutePoint, ...]


class GeocodeMatchStatus(StrEnum):
    """What the Census Geocoder said about one address."""

    matched = "matched"
    """Exactly one match. The only status that yields a point."""

    unmatched = "unmatched"
    """No match. The candidate is left out of routing, not placed from a guess."""

    ambiguous = "ambiguous"
    """More than one match. Picking one would be a guess, so the candidate is left out."""


class GeocodeOutcome(BaseModel):
    """The result of geocoding one address: a cited point exactly when it matched."""

    model_config = ConfigDict(frozen=True)

    status: GeocodeMatchStatus
    point: ProvenancedValue[GeoPoint] | None = None

    @model_validator(mode="after")
    def _point_only_when_matched(self) -> Self:
        if (self.status is GeocodeMatchStatus.matched) != (self.point is not None):
            raise ValueError("a geocode carries a point if and only if it matched")
        return self


class RoutingResult(BaseModel):
    """What clustering one run produced: its stage counts and its routes."""

    counts: dict[StageName, StageCount]
    clusters: tuple[RouteCluster, ...]


class RouteAssignmentResponse(BaseModel):
    """One ``route_assignment`` row, its point validated back into a citation."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_id: UUID
    candidate_id: UUID
    cluster_index: int
    cluster_label: str
    point: ProvenancedValue[GeoPoint]
    created_at: datetime


# The Census Geocoder's response, parsed only as far as we read it. Extra keys (the echoed input,
# the TIGER line, the address components) are ignored rather than modelled.


class CensusCoordinates(BaseModel):
    """A match's coordinates. **``x`` is longitude and ``y`` is latitude.**"""

    x: float
    y: float


class CensusMatch(BaseModel):
    """One entry of ``addressMatches``."""

    model_config = ConfigDict(populate_by_name=True)

    coordinates: CensusCoordinates
    matched_address: str = Field(alias="matchedAddress")


class CensusResult(BaseModel):
    """The ``result`` object. A miss is an empty ``addressMatches`` with HTTP 200, not a 404."""

    model_config = ConfigDict(populate_by_name=True)

    address_matches: list[CensusMatch] = Field(alias="addressMatches")


class CensusResponse(BaseModel):
    """The whole ``/geocoder/locations/address`` JSON body."""

    result: CensusResult
