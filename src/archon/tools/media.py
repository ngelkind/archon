"""Media tools — download a video so the agent can attach/schedule/send it."""

from __future__ import annotations

import json

from ..platforms import downloader
from .registry import Registry, ToolContext


# TODO(TOS-REVIEW): YouTube/other — downloads remote video for the agent to attach/send (platform ToS on downloading) — review before launch
def register(registry: Registry) -> None:
    @registry.tool(
        "download_video",
        "Download a video from a URL (YouTube, TikTok, Instagram, X, and most "
        "sites) to local storage. Returns a local path usable with "
        "wa_send_image-style sends, tg sends, or schedule_message.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "large": {"type": "boolean",
                          "description": "allow up to ~2GB (only for userbot/MTProto sends)"},
            },
            "required": ["url"],
        },
        scopes=("owner",),
        sensitive=True,
    )
    async def download_video(ctx: ToolContext, url: str, large: bool = False) -> str:
        cap = (1900 * 1024 * 1024) if large else downloader.BOT_API_LIMIT
        try:
            v = await downloader.download(url, ctx.rt.settings.media_dir, max_bytes=cap)
        except downloader.DownloadError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps({
            "ok": True, "path": v.path, "title": v.title,
            "size_mb": round(v.size_bytes / 1024 / 1024, 1),
            "duration_s": v.duration_s, "source": v.extractor,
        }, ensure_ascii=False)
