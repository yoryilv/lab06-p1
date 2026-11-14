"""Modelos ORM de SQLAlchemy para la aplicación."""

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.sql import func

from app.database import Base


class User(Base):
    """Modelo de usuario almacenado en PostgreSQL."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    full_name = Column(String(150), nullable=False, index=True)
    email = Column(String(150), unique=True, nullable=False, index=True)
    role = Column(String(50), nullable=False, default="Usuario")
    is_active = Column(Boolean, default=True, nullable=False)
    avatar_key = Column(String(512), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

