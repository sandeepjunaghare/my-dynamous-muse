"""The ``vertical_manifest`` table — identity and lifecycle in columns, cited content in JSONB.

The split is deliberate. ``vertical``, ``version`` and ``status`` are what the database indexes and
constrains, so they are real columns; the researched content is always read whole and nothing
queries inside it, so it is one JSONB ``body`` whose shape
:class:`~app.manifests.schemas.ManifestBody` owns.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class VerticalManifest(Base):
    """One version of one vertical's manifest.

    The **"at most one ACTIVE per vertical" invariant is a partial unique index**
    (``unique (vertical) where status = 'active'``). It is written by hand in
    ``alembic/versions/0002_vertical_manifest.py`` — autogenerate omits ``postgresql_where`` when
    *creating* one — and it is also declared below so autogenerate can *see* it. Leaving it out of
    the model was tried first and is worse: ``alembic check`` then reports the index as removed,
    which means the next ticket to run ``--autogenerate`` silently emits a ``drop_index`` for the
    one constraint this slice exists to guarantee. Declared both places, ``alembic check`` is clean.
    """

    __tablename__ = "vertical_manifest"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    vertical: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    body: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    """The cited content. Serialised with ``ManifestBody.model_dump(mode="json")``."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    """Who accepted the terms and flipped it active. Null for as long as it is a DRAFT."""

    __table_args__ = (
        UniqueConstraint("vertical", "version", name="uq_vertical_manifest_vertical_version"),
        Index("ix_vertical_manifest_vertical", "vertical"),
        Index(
            "uq_vertical_manifest_one_active_per_vertical",
            "vertical",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )
