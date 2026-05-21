from __future__ import annotations

"""Build agent-aware resume commands. Maps agent type to its resume CLI syntax."""

import shlex

AGENT_RESUME_TEMPLATES: dict[str, str] = {
    "claude": "claude --resume {session_id}",
    "claude_code": "claude --resume {session_id}",
    "codex": "codex resume {session_id}",
    "opencode": "opencode --resume {session_id}",
    "amp": "amp --resume {session_id}",
    "hermes": "hermes --resume {session_id}",
    "kimi-code": "kimi-code --resume {session_id}",
    "aider": "aider --resume {session_id}",
    "cursor": "cursor --resume {session_id}",
    "cline": "cline --resume {session_id}",
    "roo-code": "roo-code --resume {session_id}",
    "gemini-cli": "gemini --resume {session_id}",
    "copilot": "copilot --resume {session_id}",
}

DEFAULT_TEMPLATE = "{agent} --resume {session_id}"


def resume_command(agent: str, session_id: str) -> str:
    """Build the resume CLI string for a given agent and session."""
    template = AGENT_RESUME_TEMPLATES.get(agent, DEFAULT_TEMPLATE)
    return template.format(agent=agent, session_id=shlex.quote(str(session_id)))


def full_resume_command(path: str, agent: str, session_id: str) -> str:
    """Build 'cd DIR && <agent-specific resume command>' for shell use."""
    return f"cd {shlex.quote(path)} && {resume_command(agent, session_id)}"
