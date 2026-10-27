"""Which sources exist and how to build them from the user's own tokens.

Each source module exposes a class with `__init__(self, token: str, config: Config)`
implementing tacit.sources.base.Source.
"""

import importlib

from .. import secrets
from ..config import Config, get_config
from .base import Source, SourceNotConfigured

# name -> (module, class, secret name, how to get a token)
SOURCES: dict[str, tuple[str, str, secrets.Name, str]] = {
    "github": (
        "tacit.sources.github.source",
        "GitHubSource",
        "github_token",
        "run `tacit auth github` (reuses `gh auth token` if you have the GitHub CLI)",
    ),
    "slack": (
        "tacit.sources.slack.source",
        "SlackSource",
        "slack_token",
        "run `tacit auth slack` with a user token (xoxp-) from your team's Tacit Slack app",
    ),
    "notion": (
        "tacit.sources.notion.source",
        "NotionSource",
        "notion_token",
        "run `tacit auth notion` with your team's Notion integration token",
    ),
}


def source_for(name: str, config: Config | None = None) -> Source:
    if name not in SOURCES:
        raise SourceNotConfigured(f"Unknown source {name!r}; use one of {', '.join(SOURCES)}")
    module, cls, secret, hint = SOURCES[name]
    token = secrets.get(secret)
    if not token:
        raise SourceNotConfigured(f"No {name} token: {hint}")
    return getattr(importlib.import_module(module), cls)(token, config or get_config())


def available_sources(config: Config | None = None) -> dict[str, Source]:
    out: dict[str, Source] = {}
    for name in SOURCES:
        try:
            out[name] = source_for(name, config)
        except (SourceNotConfigured, ImportError):
            continue
    return out
