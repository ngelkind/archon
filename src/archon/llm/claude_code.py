"""Claude Code CLI backend — `claude --print` as a locked-down subprocess.

Uses the Claude subscription, so calls cost $0 (tracked but not billed).
Ported from your-other-project/providers/claude_code.py; every flag
is load-bearing for security (see that file's docstring). No custom tool
calling — the router only routes tool-free purposes (triage fallback, heavy
text generation, persona drafting) here.

Text goes in on stdin, never argv. The subprocess env is an allowlist so the
rest of Archon's secrets are not inherited.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

from .base import ChatMessage, LLMResult, ProviderError, ToolSpec, Usage, validate_model

_ENV_ALLOWLIST = (
    "PATH", "SystemRoot", "windir", "COMSPEC",
    "TEMP", "TMP", "TMPDIR",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA",
    "LANG", "LC_ALL",
    "CLAUDE_CONFIG_DIR",
    "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    # Deliberately NOT ANTHROPIC_API_KEY: subscription calls must never
    # silently fall onto metered billing (pattern from jobpipe).
)


def _find_executable() -> list[str]:
    override = os.environ.get("ARCHON_CLAUDE_BIN")
    if override:
        if not Path(override).is_file():
            raise ProviderError(f"ARCHON_CLAUDE_BIN does not point at a file: {override}")
        return [override]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            native = (
                Path(appdata) / "npm" / "node_modules" / "@anthropic-ai"
                / "claude-code" / "bin" / "claude.exe"
            )
            if native.is_file():
                return [str(native)]
    found = shutil.which("claude")
    if found:
        return [found]
    raise ProviderError("the `claude` CLI was not found; install Claude Code or set ARCHON_CLAUDE_BIN")


def _child_env() -> dict[str, str]:
    return {name: os.environ[name] for name in _ENV_ALLOWLIST if os.environ.get(name)}


def _flatten(messages: list[ChatMessage]) -> str:
    parts: list[str] = []
    for m in messages:
        speaker = "Assistant" if m.role == "assistant" else "User"
        if m.text:
            parts.append(f"{speaker}: {m.text}")
    return "\n\n".join(parts)


class ClaudeCodeProvider:
    name = "claude_code"
    supports_tools = False
    supports_vision = False

    def __init__(self, timeout_s: float = 180.0) -> None:
        self._argv0 = _find_executable()
        self._timeout = timeout_s
        # Subscription concurrency is limited; serialize to at most 2 in flight.
        self._sem = asyncio.Semaphore(2)

    def _command(self, model: str, system: str) -> list[str]:
        return [
            *self._argv0,
            "--print",
            "--output-format", "json",
            "--model", validate_model(model),
            "--tools", "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--safe-mode",
            "--system-prompt", system,
        ]

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        json_only: bool = False,
    ) -> LLMResult:
        if tools:
            raise ProviderError("claude_code backend does not support tool calling")
        prompt = _flatten(messages)
        if json_only:
            prompt += "\n\nRespond with ONLY a valid JSON object, no prose, no code fences."

        async with self._sem:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self._command(model, system),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(Path.home()),
                    env=_child_env(),
                )
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(prompt.encode("utf-8")), timeout=self._timeout
                )
            except TimeoutError:
                raise ProviderError(f"claude CLI timed out after {self._timeout:.0f}s") from None
            except OSError as exc:
                raise ProviderError(f"could not run the claude CLI: {exc.strerror}") from None

        if proc.returncode != 0:
            # stderr may echo the prompt; report only the exit code.
            raise ProviderError(f"claude CLI exited with code {proc.returncode}")
        try:
            payload = json.loads(stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            raise ProviderError("claude CLI returned output that was not JSON") from None
        if not isinstance(payload, dict) or payload.get("is_error"):
            raise ProviderError("claude CLI reported an error")
        if payload.get("permission_denials"):
            # Should be impossible with --tools ""; stop if the sandbox
            # assumption is ever wrong.
            raise ProviderError("claude CLI reported permission denials")
        text = payload.get("result")
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("claude CLI returned an empty result")

        usage = payload.get("usage") or {}
        return LLMResult(
            text=text,
            tool_calls=[],
            usage=Usage(
                in_tokens=int(usage.get("input_tokens") or 0),
                out_tokens=int(usage.get("output_tokens") or 0),
                cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
                cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
            ),
            model=model,
            provider=self.name,
            stop_reason="end",
        )
