"""Shared types for session parsers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class NormalizedMessage:
    idx: int
    role: str  # "user", "assistant", "tool", "system"
    content: str
    author: str | None = None  # model name
    created_at: int | None = None  # ms since epoch


@dataclass
class NormalizedSession:
    session_id: str
    agent: str  # "claude_code", "codex", "hermes", etc.
    source_path: str  # absolute path to session file
    workspace: str | None = None
    title: str | None = None
    started_at: int | None = None  # ms since epoch
    ended_at: int | None = None  # ms since epoch
    model: str | None = None
    messages: list[NormalizedMessage] = field(default_factory=list)


class AgentParser(Protocol):
    """Interface for per-agent session parsers."""

    agent_slug: str

    def detect_roots(self) -> list[str]:
        """Return root directories where this agent stores sessions."""
        ...

    def list_sessions(self, root: str) -> list[str]:
        """Return absolute paths to session files/dirs under root."""
        ...

    def parse_session(self, path: str) -> NormalizedSession | None:
        """Parse a session file into a NormalizedSession, or None if unparseable."""
        ...

    def parse_metadata(self, path: str) -> NormalizedSession | None:
        """Parse only session metadata (no messages) for fast listing.
        Falls back to parse_session if not implemented separately."""
        ...
