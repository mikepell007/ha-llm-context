"""Stage 3: send a timeline, context files, and a question to an
OpenAI-compatible /chat/completions endpoint (Ollama, llama.cpp, LM Studio)."""

from __future__ import annotations

from pathlib import Path

import httpx

from .config import LLMSettings

SYSTEM_PROMPT = """\
You help troubleshoot Home Assistant automations. You are given a condensed \
timeline of entity states, logbook entries, automation runs (traces), and \
error-log messages, plus optional files describing what the automations are \
meant to do.

- Base your answer on the timeline. Quote the times and entities you rely on.
- If the timeline doesn't contain enough to answer, say what is missing and \
which entity, attribute, or time range would settle it.
- The "Dropped" section lists what was left out; consider whether it matters.
- Don't invent entities, services, or events.
- Be brief: the likely cause first, then the evidence, then a suggested fix."""


def estimate_tokens(text: str) -> int:
    """Rough count (about 4 characters per token); good enough to warn on."""
    return len(text) // 4


def build_messages(timeline: str, question: str, context_files: list[Path]) -> list[dict[str, str]]:
    parts = []
    for f in context_files:
        parts.append(f"## Context file: {f.name}\n```\n{f.read_text().rstrip()}\n```")
    parts.append(timeline.rstrip())
    parts.append(f"## Question\n{question.strip()}")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def ask(settings: LLMSettings, messages: list[dict[str, str]],
        transport: httpx.BaseTransport | None = None, timeout: float = 600) -> str:
    headers = {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else {}
    with httpx.Client(timeout=timeout, transport=transport) as client:
        r = client.post(
            f"{settings.base_url}/chat/completions",
            headers=headers,
            json={"model": settings.model, "messages": messages, "temperature": 0.2},
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
