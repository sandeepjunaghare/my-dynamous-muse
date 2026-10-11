"""T5's stored evidence as the evaluator's :data:`RecordSet`: the one bridge between the shapes.

T5 stores each source's raw record on the candidate (``CandidateFields.source_records``): cited,
zero or more rows of ``str | int`` pairs. The evaluator reads ``{source: {field: text}}``. This
turns the first into the second, deciding only what the shapes force:

* **One row** is the source's record: every value as text (``power_units`` 0 → ``"0"``; casting
  back is a rule's business, see ``rules.py``).
* **No rows** — QCMobile does not know the USDOT, the revocations list has no entry — is left
  out: there is no record for a predicate to read, so its rules are ``not_evaluable``
  (``source_missing``), never a drop on a field that was never there to be absent.
* **Several rows** (the revocations, which repeat) are left out too: no single row is "the record",
  and reading those rows is the ``authority_revoked`` judgment rule's job, not a predicate's.

Each kept source also yields the URL it was read from, so a disqualification is cited to the
record (the QCMobile carrier URL), not merely to the source's base URL.
"""

from dataclasses import dataclass

from app.qualification.schemas import RecordSet
from app.sourcing.schemas import CandidateFields


@dataclass(frozen=True, kw_only=True)
class CandidateRecords:
    """One candidate's predicate-readable records and where each was read."""

    records: RecordSet
    source_urls: dict[str, str]


def records_from(fields: CandidateFields) -> CandidateRecords:
    """The single-row source records of ``fields``, as text, with each one's citation URL."""
    records: dict[str, dict[str, str | None]] = {}
    urls: dict[str, str] = {}
    for cited in fields.source_records:
        record = cited.value
        if len(record.rows) != 1:
            continue
        (row,) = record.rows
        text: dict[str, str | None] = {key: str(value) for key, value in row.values}
        records[record.source] = text
        urls[record.source] = cited.source_url
    return CandidateRecords(records=records, source_urls=urls)
