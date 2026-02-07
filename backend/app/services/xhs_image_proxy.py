from __future__ import annotations

import os
from urllib.parse import urlparse

import requests


def _safe_allowed(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in {"http", "https"}:
        return False
    host = (p.netloc or "").lower()
    # Only allow known XHS CDN hosts (strict match, avoid substring SSRF).
    # Examples:
    # - sns-webpic-*.xhscdn.com: note images
    # - *.xhscdn.com: legacy/static
    # - *.xhsimg.com: legacy/static
    # - picasso-static.xiaohongshu.com: web assets occasionally used as og:image/placeholder
    if host == "picasso-static.xiaohongshu.com":
        return True

    allowed_suffixes = (
        ".xhscdn.com",
        ".xhsimg.com",
        ".xiaohongshu.com",
    )
    if any(host.endswith(suf) for suf in allowed_suffixes):
        # Additional hardening: must look like one of the known image hosts.
        # This avoids accepting arbitrary subdomains under xiaohongshu.com that might
        # serve non-image content.
        if "xhscdn" in host or "xhsimg" in host or host.startswith("picasso-static."):
            return True
        return False

    return False


def fetch_xhs_image(url: str, timeout: int = 20) -> tuple[bytes, str]:
    if not _safe_allowed(url):
        raise ValueError("image url not allowed")

    cookie = (os.getenv("XHS_COOKIE") or "").strip()
    headers = {
        "User-Agent": os.getenv(
            "XHS_USER_AGENT",
            (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        ),
        "Referer": "https://www.xiaohongshu.com/",
    }
    if cookie:
        headers["Cookie"] = cookie

    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()

    ctype = (r.headers.get("content-type") or "application/octet-stream").split(";", 1)[0].strip()
    data = r.content or b""
    return data, ctype
