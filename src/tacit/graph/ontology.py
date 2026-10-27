"""Kept small on purpose (design §5). Bump ONTOLOGY_VERSION on any change: it's part of
every episode's input hash, so changes re-extract."""

from typing import Literal

from pydantic import BaseModel, Field

ONTOLOGY_VERSION = "1"


class Person(BaseModel):
    """A human participant or someone mentioned by name."""

    github_login: str | None = Field(None, description="GitHub login without @, if known")


class Component(BaseModel):
    """A part of our own system: a repo, service, module, package or API."""

    kind: Literal["repo", "service", "module", "package", "api", "other"] | None = Field(
        None, description="What sort of component it is"
    )


class Technology(BaseModel):
    """Something external we adopt, use or drop: a language, library, database, vendor or tool."""

    category: Literal["language", "library", "database", "vendor", "tool", "other"] | None = Field(
        None, description="What sort of technology it is"
    )


class Decision(BaseModel):
    """A choice the team made or considered, with its reasoning."""

    status: Literal["proposed", "accepted", "rejected", "superseded"] | None = Field(
        None, description="Where the decision stands"
    )
    rationale: str | None = Field(None, description="One-sentence reason given for it")


class Decided(BaseModel):
    """A person proposed, approved or made a decision."""


class Affects(BaseModel):
    """A decision changes or concerns a component or technology."""


class Supersedes(BaseModel):
    """A decision explicitly replaces an earlier one."""


class Uses(BaseModel):
    """A component uses a technology."""


ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Person": Person,
    "Component": Component,
    "Technology": Technology,
    "Decision": Decision,
}
EDGE_TYPES: dict[str, type[BaseModel]] = {
    "DECIDED": Decided,
    "AFFECTS": Affects,
    "SUPERSEDES": Supersedes,
    "USES": Uses,
}
EDGE_TYPE_MAP: dict[tuple[str, str], list[str]] = {
    ("Person", "Decision"): ["DECIDED"],
    ("Decision", "Component"): ["AFFECTS"],
    ("Decision", "Technology"): ["AFFECTS"],
    ("Decision", "Decision"): ["SUPERSEDES"],
    ("Component", "Technology"): ["USES"],
}

EXTRACTION_INSTRUCTIONS = """\
This is engineering discussion from a software team (pull requests, reviews, issues,
design docs). Name people exactly by their @login (without the @). Extract a Decision
only when there is an explicit choice or a stated rationale; ignore code-level detail
such as variable names. An approval of a pull request means that person DECIDED the
decision it contains."""
