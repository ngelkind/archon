"""Web search + fetch.

web_search uses the ACTIVE LLM provider's native server-side search (Anthropic
or Gemini) so no extra party sees anything beyond the LLM provider itself.
The DuckDuckGo fallback (`ddgs`) is used only if installed AND explicitly
enabled (settings key web.ddg_enabled) — off by default per SECURITY.md.

web_fetch downloads a URL directly (plain HTTPS GET from the VM) and returns
extracted text wrapped as untrusted content.
"""

from __future__ import annotations

import html.parser
import json
import re

import httpx

from ..db import repo
from ..llm.base import ChatMessage, ProviderError, wrap_untrusted
from .registry import Registry, ToolContext

_NATIVE_PROVIDERS = {"anthropic", "gemini"}


class _TextExtractor(html.parser.HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.chunks.append(data.strip())


def register(registry: Registry) -> None:
    @registry.tool(
        "web_search",
        "Search the web. Uses the active LLM provider's built-in search "
        "(Anthropic/Gemini). Returns an answer with sources.",
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        scopes=("owner", "inbound"),
    )
    async def web_search(ctx: ToolContext, query: str) -> str:
        from ..llm.router import Router

        router: Router = ctx.rt.router  # type: ignore[assignment]
        provider = router.active_provider_name()
        if provider in _NATIVE_PROVIDERS:
            try:
                result = await router.complete(
                    purpose="agent",
                    system="You are a web research assistant. Search the web and "
                           "answer concisely with source URLs.",
                    messages=[ChatMessage(role="user", text=query)],
                    max_tokens=1500,
                    native_web_search=True,
                    chat_pk=ctx.origin_chat_pk,
                )
                return result.text or json.dumps({"error": "empty search result"})
            except ProviderError as exc:
                return json.dumps({"error": str(exc)})

        if repo.setting_get(ctx.store, "web.ddg_enabled", False):
            try:
                from ddgs import DDGS

                hits = list(DDGS().text(query, max_results=6))
                return json.dumps([
                    {"title": h.get("title"), "url": h.get("href"),
                     "snippet": h.get("body")}
                    for h in hits
                ], ensure_ascii=False)
            except ImportError:
                return json.dumps({"error": "ddgs not installed (uv sync --extra ddg)"})
        return json.dumps({
            "error": f"provider {provider} has no native web search; switch to "
                     "anthropic/gemini or enable web.ddg_enabled"
        })

    @registry.tool(
        "web_fetch",
        "Fetch a URL and return its readable text (untrusted content).",
        {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        scopes=("owner", "inbound"),
    )
    async def web_fetch(ctx: ToolContext, url: str) -> str:
        if not re.match(r"^https?://", url):
            return json.dumps({"error": "only http(s) URLs"})
        try:
            from ..net.client import new_async_client
            async with new_async_client(ctx.rt, subsystem="fetch", timeout=30,
                                        follow_redirects=True,
                                        headers={"User-Agent": "Archon/0.1"}) as client:
                resp = await client.get(url)
        except httpx.HTTPError as exc:
            return json.dumps({"error": f"fetch failed: {type(exc).__name__}"})
        if resp.status_code != 200:
            return json.dumps({"error": f"HTTP {resp.status_code}"})
        ctype = resp.headers.get("content-type", "")
        if "html" in ctype:
            extractor = _TextExtractor()
            try:
                extractor.feed(resp.text)
            except Exception:  # noqa: BLE001
                pass
            text = "\n".join(extractor.chunks)
        else:
            text = resp.text
        return wrap_untrusted(text[:15000])
