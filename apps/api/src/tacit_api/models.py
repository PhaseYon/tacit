"""ORM mappings. Better Auth tables keep its camelCase column names in the database."""

from datetime import datetime

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "user"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str]
    email: Mapped[str]
    image: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column("createdAt")


class Workspace(Base):
    """A Better Auth organization."""

    __tablename__ = "organization"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str]
    slug: Mapped[str]
    logo: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column("createdAt")


class Member(Base):
    __tablename__ = "member"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column("organizationId", ForeignKey("organization.id"))
    user_id: Mapped[str] = mapped_column("userId", ForeignKey("user.id"))
    role: Mapped[str]
    created_at: Mapped[datetime] = mapped_column("createdAt")
