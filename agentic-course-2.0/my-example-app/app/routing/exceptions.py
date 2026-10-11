"""Deliberate failures in the routing slice.

Every one derives from :class:`~app.core.exceptions.LocalProspectEngineError`, so ``app.main``'s
centralized handler can render it as structured JSON should a route ever surface one.
"""

from typing import ClassVar

from app.core.exceptions import LocalProspectEngineError


class RoutingError(LocalProspectEngineError):
    """Base for every deliberate failure in the routing slice."""

    default_code: ClassVar[str] = "routing_error"


class GeocoderUnavailableError(RoutingError):
    """The Census Geocoder could not be reached, or kept failing, after the retries ran out.

    ``status`` is the last HTTP status seen, or ``None`` when no response came back at all.
    """

    default_code: ClassVar[str] = "geocoder_unavailable"
    status_code: ClassVar[int] = 502

    def __init__(self, message: str, *, status: int | None) -> None:
        super().__init__(message)
        self.status = status


class GeocoderResponseShapeError(RoutingError):
    """The Census Geocoder answered 200 with a body that is not the shape it documents."""

    default_code: ClassVar[str] = "geocoder_response_shape"
    status_code: ClassVar[int] = 502
