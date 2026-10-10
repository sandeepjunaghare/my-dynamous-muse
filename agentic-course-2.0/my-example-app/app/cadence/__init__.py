"""The cadence slice — the three-touch state machine Sales Hub Starter cannot hold (D4).

We own the schedule; HubSpot owns the outcomes. The public surface:

* :class:`~app.cadence.service.CadenceService` — ``enrol`` (the seam T9 and T13 call), ``sync``
  (with ``dry_run``), ``park`` and ``overdue``
* :class:`~app.cadence.machine.CadencePosition` and :func:`~app.cadence.machine.next_position`
* :class:`~app.cadence.sync.OutcomeReader` and :func:`~app.cadence.sync.find_signal` — the D5
  done-policy, which T13 reuses to reconstruct an adopted prospect's position
* :class:`~app.cadence.adoption.Adopter` and :func:`~app.cadence.adoption.reconstruct` — T13's
  adoption of the hand-worked prospects, from a roster a person writes
"""

from app.cadence.adoption import Adopter, reconstruct
from app.cadence.exceptions import (
    AlreadyEnrolledError,
    AlreadyParkedError,
    CadenceError,
    NotEnrolledError,
    RosterError,
    SyncRunningError,
)
from app.cadence.machine import CadencePosition, CadenceStatus, Touch, next_position
from app.cadence.service import CadenceService
from app.cadence.sync import OutcomeReader, find_signal, plan_advance

__all__ = [
    "Adopter",
    "AlreadyEnrolledError",
    "AlreadyParkedError",
    "CadenceError",
    "CadencePosition",
    "CadenceService",
    "CadenceStatus",
    "NotEnrolledError",
    "OutcomeReader",
    "RosterError",
    "SyncRunningError",
    "Touch",
    "find_signal",
    "next_position",
    "plan_advance",
    "reconstruct",
]
