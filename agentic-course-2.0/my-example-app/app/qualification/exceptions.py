"""Deliberate failures in the qualification slice.

Every one derives from :class:`~app.core.exceptions.LocalProspectEngineError`, so the CLI prints
it as one line and ``app.main`` renders it as structured JSON. Callers must not catch these to
reshape them.
"""

from typing import ClassVar

from app.core.exceptions import LocalProspectEngineError


class QualificationError(LocalProspectEngineError):
    """Base for every deliberate failure in the qualification slice."""

    default_code: ClassVar[str] = "qualification_error"


class RuleNotEvaluableError(QualificationError):
    """A rule's operator and value do not fit each other, so no record could ever be judged by it.

    Distinct from a record the rule cannot judge (a blank field): that is an ordinary outcome. This
    is the rule itself being malformed — ``in_set`` with a scalar, a numeric compare against text.
    """

    default_code: ClassVar[str] = "rule_not_evaluable"
    status_code: ClassVar[int] = 422

    def __init__(self, message: str, *, rule_id: str) -> None:
        super().__init__(message)
        self.rule_id = rule_id


class BatchWideDisqualificationError(QualificationError):
    """One predicate rule fired on (nearly) every batch candidate it could judge.

    That is the signature of a rule that is wrong, not of a batch that is: a misspelled field, or a
    field the source never stores, is absent on every record and fires a negated operator on every
    one. Recording it would suppress the whole batch for the vertical for good, so nothing is
    recorded and the run fails, handing its batch back.
    """

    default_code: ClassVar[str] = "batch_wide_disqualification"
    status_code: ClassVar[int] = 422

    def __init__(self, message: str, *, rule_id: str, fired: int, judged: int) -> None:
        super().__init__(message)
        self.rule_id = rule_id
        self.fired = fired
        self.judged = judged


class DryRunSourceError(QualificationError):
    """A dry-run was handed a file for a source it cannot read offline."""

    default_code: ClassVar[str] = "dry_run_source_invalid"
    status_code: ClassVar[int] = 422

    def __init__(self, message: str, *, source_name: str) -> None:
        super().__init__(message)
        self.source_name = source_name


class JudgmentNodeError(QualificationError):
    """The ``classify_rollup`` node's run did not produce a usable answer.

    The SDK failed to start, the run ended on a cap or an error, or the result carried no valid
    structured answer. Nothing is recorded for the candidate. 502: an upstream dependency failed.
    """

    default_code: ClassVar[str] = "judgment_node_failed"
    status_code: ClassVar[int] = 502

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason
