"""A page an agent verifiably read, and the key that says two URLs name the same page.

Shared by every agent whose output must cite what it read: the manifest-authoring agent (T12) and
the ``classify_rollup`` judgment node (T7), with ``resolve_owner`` (T6) next. A citation is admitted
only when its URL matches a :class:`PageRead` under :func:`normalize_url` — a URL the model merely
remembers is not a read.

Pure: no SDK import, no I/O. What makes a fetch count as a read lives in ``agent_reads.py``.
"""

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class PageRead:
    """One page the agent fetched successfully, and when this process saw the result."""

    url: str
    read_at: datetime


def normalize_url(url: str) -> str:
    """The comparison key for "is this the page that was read".

    Scheme and host are case-insensitive and a fragment never names a different page; a trailing
    slash is how the same page is written two ways. Query strings are kept — they often do select
    a different record. Raises ValueError for a URL that does not parse.
    """
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))
