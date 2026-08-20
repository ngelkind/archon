"""Video downloader (yt-dlp) — YouTube, TikTok, Instagram, X, and ~1800 sites.

Downloads to the media dir, capped in size so a Telegram send stays feasible.
yt-dlp is blocking, so callers use asyncio.to_thread. ffmpeg on the host is
used for format merges where the site doesn't serve a pre-merged mp4.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

# Telegram Bot API caps uploads at 50MB; the userbot (MTProto) at ~2GB.
BOT_API_LIMIT = 49 * 1024 * 1024
_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)


class DownloadError(Exception):
    pass


@dataclass(slots=True)
class DownloadedVideo:
    path: str
    title: str
    duration_s: int | None
    width: int | None
    height: int | None
    size_bytes: int
    extractor: str


def extract_url(text: str) -> str | None:
    m = _URL_RE.search(text or "")
    return m.group(0) if m else None


def _download_sync(url: str, out_dir: Path, max_bytes: int) -> DownloadedVideo:
    import yt_dlp

    out_dir.mkdir(parents=True, exist_ok=True)
    # Prefer a single pre-merged mp4 <= max_bytes; fall back to best mp4-ish.
    fmt = (f"best[ext=mp4][filesize<{max_bytes}]/"
           f"best[filesize<{max_bytes}]/"
           f"bv*[ext=mp4]+ba[ext=m4a]/best")
    opts = {
        "format": fmt,
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "max_filesize": max_bytes,
        "socket_timeout": 60,
        # YouTube blocks datacenter IPs on the default 'web' client with
        # "Sign in to confirm you're not a bot". These player clients avoid
        # that check without cookies. If a cookies file is present, use it too.
        "extractor_args": {
            "youtube": {"player_client": ["tv", "ios", "mweb", "android_vr", "web"]}
        },
    }
    cookies = out_dir.parent / "youtube_cookies.txt"
    if cookies.exists():
        opts["cookiefile"] = str(cookies)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
    except Exception as exc:  # yt-dlp raises many internal error types
        raise DownloadError(f"download failed: {type(exc).__name__}: {str(exc)[:200]}") from exc

    p = Path(path)
    if not p.exists():
        # merge_output_format may have changed the extension
        candidates = list(out_dir.glob(f"{info.get('id', '')}*"))
        if candidates:
            p = max(candidates, key=lambda c: c.stat().st_size)
    if not p.exists():
        raise DownloadError("download produced no file (video may exceed the size cap)")

    size = p.stat().st_size
    if size > max_bytes:
        p.unlink(missing_ok=True)
        raise DownloadError(f"video is {size // 1024 // 1024}MB, over the "
                            f"{max_bytes // 1024 // 1024}MB limit for this send")
    return DownloadedVideo(
        path=str(p),
        title=info.get("title") or p.stem,
        duration_s=info.get("duration"),
        width=info.get("width"),
        height=info.get("height"),
        size_bytes=size,
        extractor=info.get("extractor_key") or info.get("extractor") or "unknown",
    )


async def download(url: str, out_dir: Path, max_bytes: int = BOT_API_LIMIT) -> DownloadedVideo:
    return await asyncio.to_thread(_download_sync, url, out_dir, max_bytes)
