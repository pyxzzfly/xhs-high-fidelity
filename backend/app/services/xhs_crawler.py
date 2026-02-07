"""Lightweight XHS note extractor (ported from xhs-copy).

Goal here: user only provides a Xiaohongshu link/share text, we extract:
- title
- content
- canonical_url, note_id
- image_urls (NO base64 download to avoid huge payload)

If extraction fails due to anti-bot, user can optionally set XHS_COOKIE.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

import requests
import asyncio

from app.core.logger import TaskLogger

class XHSCrawlError(Exception):
    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code


logger = logging.getLogger("xhs-high-fidelity")


# --- Persistent Playwright context (for one-click crawling after one-time login) ---
_PW_SINGLETON = None
_PW_CTX_SINGLETON = None
_PW_CTX_META: dict[str, Any] | None = None
_PW_LOCK = asyncio.Lock()
_PW_CTX_USE_LOCK = asyncio.Lock()

# --- Cookie persistence ---
_COOKIE_PERSIST_LOCK = asyncio.Lock()
_COOKIE_PERSIST_LAST_TS = 0.0
_COOKIE_PERSIST_LAST_SHA256 = ""


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off"}:
        return False
    return default


def _redact_url(url: str) -> str:
    """Redact sensitive query params (best-effort)."""
    u = (url or "").strip()
    if not u:
        return ""
    # Avoid leaking xsec_token etc. in logs.
    u = re.sub(r"(xsec_token=)[^&#]+", r"\\1<redacted>", u, flags=re.IGNORECASE)
    u = re.sub(r"(token=)[^&#]+", r"\\1<redacted>", u, flags=re.IGNORECASE)
    return u


def _stage_log(task: TaskLogger | None, stage: str, **props: Any) -> None:
    if task is None:
        return
    if not _env_bool("XHS_LOG_STAGE", True):
        return
    try:
        task.info("xhs.stage", stage=stage, **props)
    except Exception:
        # Never let logging break extraction.
        pass


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except Exception:
        return default


async def _get_persistent_context(*, user_data_dir: str, headless: bool, user_agent: str):
    """Get or create a singleton persistent Chromium context.

    This avoids `SingletonLock` errors and makes crawling much faster/stabler.
    If user closes the login window (context), we will recreate it on demand.
    """
    global _PW_SINGLETON, _PW_CTX_SINGLETON, _PW_CTX_META

    async with _PW_LOCK:
        requested = {"user_data_dir": user_data_dir, "headless": headless, "user_agent": user_agent}

        if _PW_CTX_SINGLETON is not None:
            try:
                if hasattr(_PW_CTX_SINGLETON, "is_closed") and _PW_CTX_SINGLETON.is_closed():
                    _PW_CTX_SINGLETON = None
                    _PW_CTX_META = None
                else:
                    # Touch pages to ensure context is still usable
                    _ = _PW_CTX_SINGLETON.pages
                    if _PW_CTX_META == requested:
                        return _PW_CTX_SINGLETON

                    # Params changed (e.g. headless -> headful for login). Recreate safely.
                    try:
                        await _PW_CTX_SINGLETON.close()
                    except Exception:
                        pass
                    _PW_CTX_SINGLETON = None
                    _PW_CTX_META = None
            except Exception:
                _PW_CTX_SINGLETON = None
                _PW_CTX_META = None

        try:
            from playwright.async_api import async_playwright
        except ModuleNotFoundError as exc:
            raise XHSCrawlError(
                "缺少 playwright 依赖（需要安装 playwright 并执行 playwright install chromium）",
                status_code=502,
            ) from exc

        if _PW_SINGLETON is None:
            _PW_SINGLETON = await async_playwright().start()
        launch_args = ["--disable-blink-features=AutomationControlled"]
        Path(user_data_dir).expanduser().mkdir(parents=True, exist_ok=True)
        _PW_CTX_SINGLETON = await _PW_SINGLETON.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=headless,
            args=launch_args,
            user_agent=user_agent,
        )
        _PW_CTX_META = requested
        return _PW_CTX_SINGLETON


async def _close_persistent_context(*, reason: str = "") -> None:
    """Close and clear the singleton persistent context.

    Closing the persistent context will close the Chromium window/process. The
    login state is kept on disk inside `user_data_dir`.
    """
    global _PW_CTX_SINGLETON, _PW_CTX_META

    ctx = None
    meta = None
    async with _PW_LOCK:
        ctx = _PW_CTX_SINGLETON
        meta = _PW_CTX_META
        _PW_CTX_SINGLETON = None
        _PW_CTX_META = None

    if ctx is None:
        return

    try:
        await ctx.close()
    except Exception:
        pass
    finally:
        # Best-effort log: do not crash extraction.
        try:
            logger.info(
                "XHS persistent context closed (reason=%s headless=%s user_data_dir_set=%s)",
                reason,
                bool((meta or {}).get("headless", True)),
                bool((meta or {}).get("user_data_dir", "")),
            )
        except Exception:
            pass


def _relay_hint() -> str:

    return (
        "\n\n无法自动获取正文时的替代方案（推荐）：\n"
        "1) 用 Chrome 打开该笔记页面（确保你能看到正文）\n"
        "2) 安装并启用 OpenClaw Browser Relay 扩展\n"
        "3) 在该 tab 上点击扩展图标并 Attach/Connect\n"
        "4) 由前端/人工从页面复制正文粘贴到‘原稿全文’继续洗稿\n"
        "（原因：小红书对无头/自动化环境风控较严，服务端抓取可能被拦截）"
    )


def _not_found_hint() -> str:
    return (
        "小红书提示：你访问的页面不存在（可能笔记已删除/不可见/链接无效）。"
        "如果你在浏览器里能正常打开该笔记，但采集端提示不存在，可能是反爬/风控伪装；"
        "建议使用 Browser Relay 从你可见的页面提取正文。"
        + _relay_hint()
    )


def _gate_reason(text: str) -> str | None:
    """Best-effort classification for common XHS gate pages.

    Returned values:
    - "consent": user agreement / privacy page
    - "login": login modal/page
    - "risk": 300012 / IP risk-control page
    - "placeholder": generic homepage/slogan placeholder (often means blocked/redirected)
    """
    t = (text or "").strip()
    if not t:
        return None

    risk_markers = (
        "安全限制",
        "IP存在风险",
        "请切换可靠网络环境后重试",
        "300012",
    )
    if any(m in t for m in risk_markers):
        return "risk"

    consent_markers = ("用户协议", "隐私政策", "同意并继续")
    if any(m in t for m in consent_markers):
        return "consent"

    # Avoid using just "登录": normal pages can contain it (e.g. comment UI hints),
    # which would cause false positives and endless "need login" loops.
    login_markers = (
        "扫码登录",
        "手机号登录",
        "验证码登录",
        "短信登录",
        "密码登录",
        "账号登录",
        "请先登录",
        "登录可见",
        "登录后可见",
        "登录后查看",
        "登录查看全部评论",
        "登录后评论",
        "马上登录",
        "立即登录",
    )
    if any(m in t for m in login_markers):
        return "login"

    placeholder_markers = (
        "小红书 - 你的生活兴趣社区",
        "3 亿人的生活经验",
    )
    if any(m in t for m in placeholder_markers):
        return "placeholder"

    return None


def _looks_like_login_or_consent_gate(text: str) -> bool:
    return _gate_reason(text) in {"login", "consent"}


def _looks_like_not_found_page(text: str) -> bool:
    """Detect XHS 404-ish pages.

    XHS sometimes returns HTTP 200 with a "page not found" body, so we need a
    content-based check.
    """
    t = (text or "").strip()
    if not t:
        return False
    if "你访问的页面不存在" in t:
        return True
    # Keep it strict to avoid false positives from normal note content.
    if re.search(r"你访问的页面.{0,12}不存在", t):
        return True
    return False


def _looks_like_interactive_login_or_consent_gate(text: str) -> bool:
    """Signals that user can likely fix by interacting with a visible browser window."""
    t = (text or "").strip()
    if not t:
        return False

    consent_markers = ("用户协议", "隐私政策", "同意并继续")
    if any(m in t for m in consent_markers):
        return True

    # Note-visibility login gate (content hidden unless logged in).
    # Exclude common comment-only prompts like "登录查看全部评论".
    if "评论" not in t:
        visibility_markers = (
            "登录后可见",
            "登录可见",
            "登录后查看",
            "请先登录",
            "登录后查看全部",
        )
        if any(m in t for m in visibility_markers):
            return True

    # Stronger-than-"登录": avoid waiting for manual verification when page only hints login for comments.
    strong_login_markers = (
        "扫码登录",
        "手机号登录",
        "验证码登录",
        "短信登录",
        "密码登录",
        "账号登录",
        "登录/注册",
        "登录注册",
        "注册登录",
    )
    return any(m in t for m in strong_login_markers)


async def _page_looks_like_interactive_gate_async(page, body_text: str) -> bool:
    """Fallback gate detection using DOM structure, in case body.innerText is empty."""
    if _looks_like_interactive_login_or_consent_gate(body_text):
        return True

    try:
        return bool(
            await page.evaluate(
                """
                () => {
                  const t = (document.body?.innerText || '').trim();
                  const hasConsent = t.includes('用户协议') || t.includes('隐私政策') || t.includes('同意并继续');
                  if (hasConsent) return true;

                  // Basic login markers in DOM (text may be hidden behind modal).
                  const btnText = (el) => ((el?.innerText || el?.textContent || '').trim());
                  const loginLike = (s) => /登录|注册|验证码|手机号|扫码/.test(s || '');
                  const clickable = Array.from(document.querySelectorAll('button,a,div[role="button"]'));
                  if (clickable.some(el => loginLike(btnText(el)))) return true;

                  // Inputs / QR elements
                  const phone = document.querySelector('input[type="tel"], input[placeholder*="手机"], input[placeholder*="手机号"], input[name*="phone"]');
                  if (phone) return true;

                  const qr = document.querySelector('img[alt*="二维码"], img[src*="qr"], canvas');
                  if (qr && t.includes('小红书')) return true;

                  // Visibility gate
                  if (!t.includes('评论') && (t.includes('登录后可见') || t.includes('登录可见') || t.includes('请先登录'))) return true;
                  return false;
                }
                """
            )
        )
    except Exception:
        return False


def _looks_like_risk_control_gate(text: str) -> bool:
    return _gate_reason(text) == "risk"


def _looks_like_placeholder_gate(text: str) -> bool:
    return _gate_reason(text) == "placeholder"


def _cookies_to_header(cookies: list[dict[str, Any]]) -> str:
    """Convert Playwright cookie dicts to a stable Cookie header string."""
    now = time.time()
    out: list[str] = []
    for c in sorted(cookies or [], key=lambda x: str(x.get("name", ""))):
        try:
            name = str(c.get("name", "") or "").strip()
            value = str(c.get("value", "") or "").strip()
        except Exception:
            continue
        if not name or value == "":
            continue

        expires = c.get("expires")
        if isinstance(expires, (int, float)) and expires not in (-1, 0):
            # Playwright uses -1 for session cookies.
            if expires > 0 and expires <= now:
                continue

        out.append(f"{name}={value}")
    return "; ".join(out).strip()


def _upsert_env_line(lines: list[str], key: str, value: str) -> list[str]:
    """Upsert KEY=value into an .env file (preserve other lines)."""
    needle = f"{key}="
    rendered = f"{key}={value}"

    out: list[str] = []
    found = False
    for line in lines:
        if line.startswith(needle):
            if found:
                # Remove duplicated lines to keep the file clean.
                continue
            out.append(rendered)
            found = True
            continue
        out.append(line)

    if not found:
        if out and out[-1].strip() != "":
            out.append("")
        out.append(rendered)
    return out


async def _persist_cookie_header(cookie_header: str) -> None:
    """Persist cookie header into process env and (optionally) backend/.env.

    SECURITY: Do not log the cookie value. Only log length/hash.
    """
    cookie_header = (cookie_header or "").strip()
    if not cookie_header:
        return

    # Always update current process env so image proxy can use it immediately.
    os.environ["XHS_COOKIE"] = cookie_header

    if not _env_bool("XHS_COOKIE_PERSIST_TO_ENV", True):
        return

    env_path_raw = (os.getenv("XHS_COOKIE_ENV_PATH") or "").strip()
    if env_path_raw:
        env_path = Path(env_path_raw).expanduser()
    else:
        backend_dir = Path(__file__).resolve().parents[2]
        env_path = backend_dir / ".env"

    min_interval_s = max(_env_int("XHS_COOKIE_PERSIST_MIN_INTERVAL_S", 60), 0)
    sha = hashlib.sha256(cookie_header.encode("utf-8")).hexdigest()
    now = time.time()

    async with _COOKIE_PERSIST_LOCK:
        global _COOKIE_PERSIST_LAST_TS, _COOKIE_PERSIST_LAST_SHA256

        # Skip redundant frequent writes.
        if sha == _COOKIE_PERSIST_LAST_SHA256 and (now - _COOKIE_PERSIST_LAST_TS) < min_interval_s:
            return

        _COOKIE_PERSIST_LAST_TS = now
        _COOKIE_PERSIST_LAST_SHA256 = sha

        try:
            raw = env_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raw = ""
        except Exception as exc:
            logger.warning("XHS_COOKIE persist: failed to read env file: %s", exc)
            raw = ""

        lines = raw.splitlines()
        new_lines = _upsert_env_line(lines, "XHS_COOKIE", cookie_header)
        new_text = "\n".join(new_lines).rstrip("\n") + "\n"

        try:
            env_path.parent.mkdir(parents=True, exist_ok=True)
            mode = (env_path.stat().st_mode & 0o777) if env_path.exists() else 0o600
            tmp_path = env_path.with_name(f"{env_path.name}.tmp.{os.getpid()}")

            fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_text)

            os.replace(str(tmp_path), str(env_path))
            logger.info(
                "XHS_COOKIE persisted to %s (len=%s sha256=%s)",
                str(env_path),
                len(cookie_header),
                sha[:10],
            )
        except Exception as exc:
            logger.warning("XHS_COOKIE persist: failed to write env file: %s", exc)


_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_TRAILING_URL_CHARS = "，。,.!！?？\"'）)]}>"


def _strip_url(candidate: str) -> str:
    url = (candidate or "").strip()
    while url and url[-1] in _TRAILING_URL_CHARS:
        url = url[:-1]
    return url


def _extract_xhs_url(source_text: str) -> str:
    urls: list[str] = []
    for match in _URL_PATTERN.findall(source_text or ""):
        candidate = _strip_url(match)
        host = (urlparse(candidate).netloc or "").lower()
        if "xiaohongshu.com" in host:
            urls.append(candidate)

    if not urls:
        raise XHSCrawlError("未识别到有效小红书链接", status_code=400)

    # Prefer note-like urls when the share text contains multiple links
    # (e.g. user profile + note link).
    for u in urls:
        decoded = unquote(u or "")
        if re.search(r"/explore/[0-9a-zA-Z]+", decoded):
            return u
        if re.search(r"/discovery/item/[0-9a-zA-Z]+", decoded):
            return u
        if re.search(r"[?&]noteId=[0-9a-zA-Z]+", decoded, flags=re.IGNORECASE):
            return u
        if re.search(r"[?&]note_id=[0-9a-zA-Z]+", decoded, flags=re.IGNORECASE):
            return u

    return urls[0]


def _extract_note_id(url: str) -> str:
    decoded = unquote(url or "")
    patterns = [
        r"/explore/([0-9a-zA-Z]+)",
        r"/discovery/item/([0-9a-zA-Z]+)",
        r"[?&]noteId=([0-9a-zA-Z]+)",
        r"[?&]note_id=([0-9a-zA-Z]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, decoded)
        if match:
            return match.group(1)

    path_lower = (urlparse(decoded).path or "").lower()
    if "/user/profile/" in path_lower:
        raise XHSCrawlError("链接不是小红书笔记页面（看起来是用户主页链接）", status_code=400)

    segments = [segment for segment in urlparse(decoded).path.split("/") if segment]
    if segments:
        tail = segments[-1]
        if re.fullmatch(r"[0-9a-zA-Z]{8,}", tail):
            return tail

    raise XHSCrawlError("未识别到小红书笔记ID", status_code=400)


def _build_canonical_url(note_id: str) -> str:
    return f"https://www.xiaohongshu.com/explore/{note_id}"


def _build_url_candidates(*, source_url: str, note_id: str) -> list[str]:
    """Build a small set of candidate URLs for the same note.

    XHS routes change over time. Some note IDs may work on /explore, others on
    /discovery/item, and sometimes only the full share URL (with query params)
    works.
    """
    out: list[str] = []

    def _add(u: str) -> None:
        u = (u or "").strip()
        if not u:
            return
        if u in out:
            return
        out.append(u)

    _add(source_url)
    _add(f"https://www.xiaohongshu.com/explore/{note_id}")
    _add(f"https://www.xiaohongshu.com/discovery/item/{note_id}")
    return out


def _http_headers(cookie: Optional[str] = None) -> dict[str, str]:
    user_agent = os.getenv(
        "XHS_USER_AGENT",
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.xiaohongshu.com/",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _collapse_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _normalize_content_text(value: str) -> str:
    text = html.unescape(str(value or ""))
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", " ")
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    # compact blank lines
    out: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if not blank:
                out.append("")
            blank = True
            continue
        blank = False
        out.append(line)
    return "\n".join(out).strip()


def _extract_meta(html_text: str, key: str) -> str:
    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']{re.escape(key)}["\']',
        rf'<meta[^>]+name=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']{re.escape(key)}["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text, flags=re.IGNORECASE)
        if match:
            return html.unescape(match.group(1)).strip()
    return ""


def _extract_title_tag(html_text: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html_text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return _collapse_text(html.unescape(match.group(1)))


def _clean_title(title: str) -> str:
    """Normalize XHS page titles (strip site suffix, collapse whitespace)."""
    t = _collapse_text(title or "")
    if not t:
        return ""
    # Common suffixes from <title> / og:title.
    t = re.sub(r"\s*[-|｜]\s*小红书\s*$", "", t).strip()
    return t


def _strip_leading_title_from_content(title: str, content: str) -> str:
    """Remove duplicated title prefix inside content when present.

    Some XHS pages/embedded JSON include the title again at the start of the
    description/content field, causing 'title' to appear twice in reference_text.
    """
    t = _clean_title(title)
    c = (content or "").lstrip()
    if not t or not c:
        return content or ""

    # Generate a few safe variants for matching.
    variants = {t}
    variants.add(t.replace("｜", "|"))
    variants.add(t.replace("|", "｜"))
    variants.add(_collapse_text(t))

    for v in sorted(variants, key=len, reverse=True):
        if not v:
            continue
        if not c.startswith(v):
            continue
        # Only strip when the next char looks like a separator/newline/punctuation.
        nxt = c[len(v) : len(v) + 1]
        if nxt and nxt not in {"\n", "\r", " ", "\t", "｜", "|", "-", "—", "·", ":", "：", "，", ",", "。", "!", "！"}:
            continue
        c2 = c[len(v) :].lstrip(" \t\r\n-—|｜·:：")
        return c2

    return content or ""


_DATE_LINE_RE = re.compile(
    r"^\s*(20\d{2})[-/.年]\s*(\d{1,2})[-/.月]\s*(\d{1,2})(?:\s*[日号])?(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\s*$"
)


def _strip_publish_date_lines(text: str) -> str:
    """Remove obvious publish-date lines from extracted content."""
    raw = (text or "").strip()
    if not raw:
        return ""

    lines = raw.split("\n")
    out: list[str] = []
    for line in lines:
        s = (line or "").strip()
        if not s:
            out.append("")
            continue
        if _DATE_LINE_RE.match(s):
            # Drop pure date/timestamp lines.
            continue
        if s.startswith("编辑于") and _DATE_LINE_RE.search(s):
            continue
        out.append(s)

    # collapse blank lines again
    compact: list[str] = []
    blank = False
    for l in out:
        if not l:
            if not blank:
                compact.append("")
            blank = True
            continue
        blank = False
        compact.append(l)
    return "\n".join(compact).strip()


def _dedupe_content_text(title: str, content: str) -> str:
    """Best-effort dedupe for common 'double extracted' patterns."""
    content = (content or "").strip()
    if not content:
        return ""

    # 1) Remove publish dates (user doesn't want it).
    content = _strip_publish_date_lines(content)
    if not content:
        return ""

    lines = [ln.strip() for ln in content.split("\n")]
    lines = [ln for ln in lines if ln != ""]
    if not lines:
        return ""

    # 2) If first line is just a concatenation of the next 2-4 lines, drop it.
    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").strip())

    first = norm(lines[0])
    for k in (2, 3, 4):
        if len(lines) >= 1 + k:
            joined = norm(" ".join(lines[1 : 1 + k]))
            if first == joined and joined:
                lines = lines[1:]
                break

    # 2b) If content starts with title, and the first line is a "title + rest" summary,
    # prefer the split lines (avoid losing the title line).
    t_norm = norm(_clean_title(title))
    if t_norm and len(lines) >= 2:
        l0 = norm(lines[0])
        l1 = norm(lines[1])
        if l1 == t_norm and l0.startswith(t_norm):
            lines = lines[1:]

    # 3) Remove consecutive duplicate lines (very common in DOM extraction).
    deduped: list[str] = []
    prev = None
    for ln in lines:
        n = norm(ln)
        if prev is not None and n == prev:
            continue
        deduped.append(ln)
        prev = n

    # 3b) Drop standalone title lines from content (title will be shown separately).
    t_norm = norm(_clean_title(title))
    if t_norm:
        deduped = [ln for ln in deduped if norm(ln) != t_norm]

    # 3c) If still small and repetitive, globally dedupe identical lines (keep order).
    if len(deduped) <= 12:
        seen: set[str] = set()
        out2: list[str] = []
        for ln in deduped:
            n = norm(ln)
            if n in seen:
                continue
            seen.add(n)
            out2.append(ln)
        deduped = out2

    # 4) If the whole content is duplicated (A + A), keep one copy.
    if len(deduped) % 2 == 0 and len(deduped) >= 4:
        mid = len(deduped) // 2
        if [norm(x) for x in deduped[:mid]] == [norm(x) for x in deduped[mid:]]:
            deduped = deduped[:mid]

    # 5) Strip duplicated title prefix again (after transforms).
    rebuilt = "\n".join(deduped).strip()
    rebuilt = _strip_leading_title_from_content(title, rebuilt).strip()
    rebuilt = _strip_publish_date_lines(rebuilt)
    return rebuilt.strip()

def _extract_braced_json(text: str, start_index: int) -> Optional[str]:
    depth = 0
    in_string = False
    escaped = False
    begin = -1
    for index in range(start_index, len(text)):
        char = text[index]
        if begin == -1:
            if char == "{":
                begin = index
                depth = 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[begin : index + 1]
    return None


def _extract_json_candidates(html_text: str) -> list[Any]:
    candidates: list[Any] = []

    marker_patterns = [
        r"window\\.__INITIAL_STATE__\\s*=",
        r"window\\.__INITIAL_SSR_STATE__\\s*=",
        r"__INITIAL_STATE__\\s*=",
    ]
    for marker_pattern in marker_patterns:
        for match in re.finditer(marker_pattern, html_text):
            snippet = _extract_braced_json(html_text, match.end())
            if not snippet:
                continue
            try:
                candidates.append(json.loads(snippet))
            except json.JSONDecodeError:
                continue

    json_script_patterns = [
        r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>',
    ]
    for script_pattern in json_script_patterns:
        for match in re.finditer(script_pattern, html_text, flags=re.IGNORECASE | re.DOTALL):
            raw = html.unescape((match.group(1) or "").strip())
            if not raw:
                continue
            try:
                candidates.append(json.loads(raw))
            except json.JSONDecodeError:
                continue

    return candidates


_COMMENT_PATH_MARKERS = (
    "comment",
    "comments",
    "commentlist",
    "comment_info",
    "sub_comment",
    "reply",
    "replies",
)


def _is_comment_path(path_tokens: tuple[str, ...]) -> bool:
    for token in path_tokens:
        lower = (token or "").lower()
        if any(marker in lower for marker in _COMMENT_PATH_MARKERS):
            return True
    return False


def _collect_fields(value: Any, output: dict[str, list[str]], path: tuple[str, ...] = ()) -> None:
    # Skip anything under comment/reply paths to avoid mixing comments into正文.
    if _is_comment_path(path):
        return

    if isinstance(value, dict):
        for k, v in value.items():
            key = str(k)
            lower = key.lower()
            next_path = path + (lower,)
            if lower.endswith("title") and isinstance(v, str):
                output["titles"].append(v)
            if ("content" in lower or "desc" in lower) and isinstance(v, str):
                output["contents"].append(v)
            if any(h in lower for h in ("image", "img", "cover", "url", "src")) and isinstance(v, str):
                output["images"].append(v)
            _collect_fields(v, output, next_path)
    elif isinstance(value, list):
        for item in value:
            _collect_fields(item, output, path)


def _looks_like_image_url(value: str) -> bool:
    text = html.unescape((value or "").strip()).lower()
    if not text.startswith("http") and not text.startswith("//"):
        return False
    if text.startswith("//"):
        text = "https:" + text
    parsed = urlparse(text)
    host = (parsed.netloc or "").lower()
    cdn_host_keywords = ("xhscdn.com", "xhsimg.com", "sns-webpic")
    if not any(k in host for k in cdn_host_keywords) and "xiaohongshu.com" not in host:
        return False
    return True


def _looks_like_note_image_url(value: str) -> bool:
    """Stricter filter: keep only note illustration images, exclude avatars/emojis/comments."""
    raw = html.unescape((value or "").strip())
    if not _looks_like_image_url(raw):
        return False
    u = _normalize_image_url(raw)
    parsed = urlparse(u)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()

    # Exclude comments/replies assets aggressively
    if any(tok in path for tok in ("comment", "comments", "reply", "replies", "commentlist")):
        return False

    # Exclude avatars / emoji / stickers
    if "avatar" in host or "sns-avatar" in host:
        return False
    if any(tok in path for tok in ("/avatar/", "emoji", "emoticon", "sticker")):
        return False

    # Keep common note image path markers
    keep_markers = (
        "/notes_pre_post/",
        "/notes/",
        "/note/",
        "/images/",
        "/image/",
        "/photo/",
        "/pics/",
        "/pic/",
        # sometimes covers are stored without these markers
        "/1040g",
    )
    return any(m in path for m in keep_markers)


def _normalize_image_url(raw_url: str) -> str:
    url = html.unescape((raw_url or "").strip())
    url = _strip_url(url)
    if url.startswith("//"):
        url = f"https:{url}"
    return url


def _image_asset_key(raw_url: str) -> str:
    """Best-effort key to dedupe the same image across different variants.

    XHS often returns the same image with different scheme (http/https), query params,
    or variant suffixes.
    """
    url = _normalize_image_url(raw_url)
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return url

    # Strip query
    path = (parsed.path or "").split("?", 1)[0]

    # Common pattern contains /notes_pre_post/<asset_id>!variant
    m = re.search(r"/(?:notes_pre_post|notes|note|images|image|photo)/([^/?#!]+)", path, flags=re.IGNORECASE)
    if m:
        return m.group(1).lower()

    # Fallback: host+path without variant after '!'
    path = path.split("!", 1)[0]
    return f"{(parsed.netloc or '').lower()}{path}".lower()


def _dedupe_keep_order(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        s = (item or "").strip()
        if not s:
            continue
        key = _image_asset_key(s) or s
        if key in seen:
            continue
        seen.add(key)
        out.append(_normalize_image_url(s))
    return out


def _choose_best_text(candidates: list[str]) -> str:
    # prefer longer non-empty
    cleaned = [c for c in (candidates or []) if isinstance(c, str) and c.strip()]
    cleaned.sort(key=lambda x: len(x), reverse=True)
    return cleaned[0].strip() if cleaned else ""


def _choose_best_title(*, meta_title: str, title_tag: str, candidates: list[str]) -> str:
    """Choose a plausible note title (avoid picking content/hashtags as title)."""

    def norm(s: str) -> str:
        return _collapse_text(_clean_title(s or ""))

    # Prefer meta/title_tag first.
    ordered = [meta_title, title_tag] + list(candidates or [])
    best = ""
    best_score = -10**9
    for raw in ordered:
        t = norm(raw)
        if not t:
            continue
        if "\n" in t or "\r" in t:
            continue
        if len(t) > 80:
            # likely title+content merged or garbage
            continue
        # Heuristics: title rarely contains hashtags; if it does, down-rank heavily.
        score = 0
        if "#" in t:
            score -= 50
        if "http://" in t or "https://" in t:
            score -= 50
        # Prefer typical title lengths (8-28 chars) but don't hard-fail.
        L = len(t)
        if 8 <= L <= 28:
            score += 20
        elif 4 <= L <= 40:
            score += 10
        else:
            score -= 5

        # Prefer meta sources slightly.
        if raw == meta_title:
            score += 8
        if raw == title_tag:
            score += 4

        if score > best_score:
            best_score = score
            best = t

    return best.strip()


def _prefer_split_title_content(title: str, content: str) -> tuple[str, str]:
    """Prefer the split form: single title line + remaining content lines.

    Some XHS pages expose both:
    - merged line: "title + content + hashtags" (one line)
    - split lines: title line + content line(s)

    User preference: keep split form.
    """

    def norm(s: str) -> str:
        return _collapse_text(s or "")

    cur_title = _clean_title(title or "")
    cur_norm = norm(cur_title)
    raw = _strip_publish_date_lines(content or "")
    lines = [ln.strip() for ln in (raw or "").split("\n") if ln.strip()]
    if not lines:
        return cur_title, ""

    # If content begins with a merged summary that equals the next 2-4 lines joined, drop the merged line.
    if len(lines) >= 3:
        first = norm(lines[0])
        for k in (2, 3, 4):
            if len(lines) >= 1 + k:
                joined = norm(" ".join(lines[1 : 1 + k]))
                if first == joined and joined:
                    lines = lines[1:]
                    break

    if len(lines) < 2:
        return cur_title, "\n".join(lines).strip()

    title_line = _clean_title(lines[0])
    if not title_line:
        return cur_title, "\n".join(lines).strip()
    if len(title_line) > 80:
        return cur_title, "\n".join(lines).strip()
    if "#" in title_line:
        # Title line should not be a hashtag soup.
        return cur_title, "\n".join(lines).strip()

    body = "\n".join(lines[1:]).strip()
    body = _strip_publish_date_lines(body)
    if not body:
        return cur_title, ""

    # Decide whether to override the current title.
    if not cur_norm:
        return title_line, body
    if "#" in cur_norm:
        return title_line, body

    tl_norm = norm(title_line)
    if cur_norm == tl_norm:
        return cur_title, body

    # If current title looks like "title + first content line", prefer the split title.
    merged2 = norm(f"{title_line} {lines[1]}")
    if cur_norm == merged2:
        return title_line, body
    if cur_norm.startswith(tl_norm) and (len(cur_norm) - len(tl_norm)) >= 6:
        return title_line, body

    return cur_title, body


def _cookie_header_to_playwright(cookie_header: str, domain: str = ".xiaohongshu.com") -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    for part in (cookie_header or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            }
        )
    return cookies


async def _collect_page_note_images_async(page) -> list[str]:
    # best-effort: collect visible img src
    urls = await page.evaluate(
        """
        () => {
          const out = [];
          const imgs = Array.from(document.querySelectorAll('img'));
          for (const img of imgs) {
            const src = img.getAttribute('src') || img.getAttribute('data-src') || '';
            if (src) out.push(src);
          }
          return out;
        }
        """
    )
    if not isinstance(urls, list):
        return []
    cleaned: list[str] = []
    for u in urls:
        if not isinstance(u, str):
            continue
        if not _looks_like_note_image_url(u):
            continue
        cleaned.append(_normalize_image_url(u))
    return _dedupe_keep_order(cleaned)


async def _crawl_with_cookie_playwright_async(
    *,
    url_candidates: list[str],
    timeout: int,
    cookie_header: str | None,
    trace_id: str | None,
) -> tuple[str, list[str], str]:
    """Return (reference_text, image_urls) using playwright async API."""
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ModuleNotFoundError as exc:
        raise XHSCrawlError(
            "缺少 playwright 依赖（需要安装 playwright 并执行 playwright install chromium）",
            status_code=502,
        ) from exc

    from playwright._impl._errors import TargetClosedError

    class _NeedHeadful(Exception):
        pass

    task = TaskLogger(trace_id) if trace_id else None

    timeout_ms = int(timeout * 1000)
    user_agent = _http_headers().get("User-Agent", "")

    playwright_headless = _env_bool("XHS_PLAYWRIGHT_HEADLESS", True)
    user_data_dir = (os.getenv("XHS_USER_DATA_DIR") or "").strip()

    auto_login = _env_bool("XHS_AUTO_LOGIN_ON_401", True)
    auto_wait_ms = max(_env_int("XHS_AUTO_LOGIN_WAIT_MS", 120000), 0)
    poll_ms = max(_env_int("XHS_AUTO_LOGIN_POLL_INTERVAL_MS", 1000), 250)

    candidates: list[str] = []
    for u in url_candidates or []:
        if not isinstance(u, str):
            continue
        u = u.strip()
        if not u:
            continue
        if u in candidates:
            continue
        candidates.append(u)
    if not candidates:
        raise XHSCrawlError("缺少可用的小红书链接", status_code=400)

    async def _wait_for_login_clear(page) -> None:
        _stage_log(
            task,
            "wait_login_start",
            wait_ms=auto_wait_ms,
            poll_ms=poll_ms,
            url=_redact_url(getattr(page, "url", "") or ""),
        )
        started = time.monotonic()
        if auto_wait_ms <= 0:
            raise XHSCrawlError(
                "采集被登录/协议页拦截：请在弹出的浏览器窗口中完成同意/登录后重试" + _relay_hint(),
                status_code=401,
            )

        deadline = time.time() + (auto_wait_ms / 1000.0)
        while True:
            if time.time() >= deadline:
                raise XHSCrawlError(
                    "采集被登录/协议页拦截：等待登录超时，请在弹出的浏览器窗口中完成同意/登录后重试" + _relay_hint(),
                    status_code=401,
                )
            try:
                body = (await page.inner_text("body"))[:4000]
            except TargetClosedError as exc:
                raise XHSCrawlError(
                    "采集窗口被关闭或浏览器上下文已失效：请重新采集以再次打开浏览器窗口。" + _relay_hint(),
                    status_code=409,
                ) from exc
            except Exception:
                body = ""

            if _looks_like_not_found_page(body):
                raise XHSCrawlError(
                    _not_found_hint(),
                    status_code=404,
                )

            if _looks_like_risk_control_gate(body):
                raise XHSCrawlError(
                    "采集被风控页拦截（300012/IP 风险）。建议切换网络/代理或使用 Browser Relay 从你可见页面提取正文。"
                    + _relay_hint(),
                    status_code=429,
                )
            still_gate = _looks_like_interactive_login_or_consent_gate(body)
            if not still_gate:
                try:
                    still_gate = await _page_looks_like_interactive_gate_async(page, body)
                except Exception:
                    still_gate = False

            if not still_gate:
                _stage_log(task, "wait_login_done", elapsed_ms=int((time.monotonic() - started) * 1000))
                return
            await page.wait_for_timeout(poll_ms)

    async def _persist_context_cookie(ctx) -> None:
        try:
            cookies = await ctx.cookies("https://www.xiaohongshu.com/")
        except Exception:
            cookies = []
        header = _cookies_to_header(cookies)
        if header:
            sha = hashlib.sha256(header.encode("utf-8")).hexdigest()
            _stage_log(
                task,
                "persist_cookie_done",
                cookie_len=len(header),
                cookie_sha_prefix=sha[:10],
                persisted_to_env=_env_bool("XHS_COOKIE_PERSIST_TO_ENV", True),
            )
            await _persist_cookie_header(header)

    async def _try_urls_in_context(ctx, *, headless: bool) -> tuple[str, list[str], str]:
        _stage_log(
            task,
            "pw_context_get",
            persistent=bool(user_data_dir),
            headless=headless,
            pages_count=len(getattr(ctx, "pages", []) or []),
        )
        last_404: XHSCrawlError | None = None
        last_timeout: XHSCrawlError | None = None
        for u in candidates:
            try:
                _stage_log(task, "pw_goto", url=_redact_url(u))
                ref_text, image_urls = await _crawl_in_context(ctx, headless=headless, target_url=u)
                return ref_text, image_urls, u
            except XHSCrawlError as exc:
                if exc.status_code == 404 and len(candidates) > 1:
                    last_404 = exc
                    continue
                if exc.status_code == 504 and len(candidates) > 1:
                    last_timeout = exc
                    continue
                raise

        if last_404 is not None:
            raise last_404
        if last_timeout is not None:
            raise last_timeout
        raise XHSCrawlError("playwright 抓取失败（未命中可用的候选链接）", status_code=422)

    async def _crawl_in_context(ctx, *, headless: bool, target_url: str) -> tuple[str, list[str]]:
        """Return (reference_text, image_urls) for one URL inside an existing context."""
        page_image_urls: list[str] = []
        page_note_title = ""
        page_note_content = ""
        gate_reason_after: str | None = None

        if cookie_header:
            cookies = _cookie_header_to_playwright(cookie_header)
            if cookies:
                try:
                    await ctx.add_cookies(cookies)
                except Exception:
                    pass

        page = await ctx.new_page()
        keep_open = False
        try:
            # IMPORTANT: do NOT block images/fonts when we might need manual login (QR/slider).
            if headless:
                try:
                    await page.route(
                        "**/*",
                        lambda route, request: route.abort()
                        if request.resource_type in {"image", "media", "font"}
                        else route.continue_(),
                    )
                except Exception:
                    pass

            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
                await page.wait_for_timeout(1200)  # SPA hydrate
            except PlaywrightTimeoutError as exc:
                _stage_log(task, "pw_goto_timeout", url=_redact_url(target_url), timeout_ms=timeout_ms)
                raise XHSCrawlError(
                    f"playwright 打开页面超时（{timeout_ms/1000:.0f}s）。建议稍后重试/切换网络，或使用 Browser Relay。" + _relay_hint(),
                    status_code=504,
                ) from exc
            except TargetClosedError as exc:
                raise XHSCrawlError(
                    "采集窗口被关闭或浏览器上下文已失效：请重新采集以再次打开浏览器窗口。" + _relay_hint(),
                    status_code=409,
                ) from exc

            # Gate detection (before heavy extraction).
            try:
                body = (await page.inner_text("body"))[:4000]
            except Exception:
                body = ""

            if _looks_like_not_found_page(body):
                _stage_log(task, "gate_detected", gate_type="not_found", phase="before", url=_redact_url(target_url))
                raise XHSCrawlError(_not_found_hint(), status_code=404)
            if _looks_like_risk_control_gate(body):
                _stage_log(task, "gate_detected", gate_type="risk", phase="before", url=_redact_url(target_url))
                raise XHSCrawlError(
                    "采集被风控页拦截（300012/IP 风险）。建议切换网络/代理或使用 Browser Relay 从你可见页面提取正文。"
                    + _relay_hint(),
                    status_code=429,
                )

            if await _page_looks_like_interactive_gate_async(page, body):
                _stage_log(task, "gate_detected", gate_type="login_or_consent", phase="before", url=_redact_url(target_url))
                if not auto_login:
                    raise XHSCrawlError(
                        "采集被登录/协议页拦截：请先完成登录/同意协议后重试。" + _relay_hint(),
                        status_code=401,
                    )
                if headless:
                    # Caller needs to recreate a headful context to allow manual login.
                    raise _NeedHeadful()

                try:
                    await page.bring_to_front()
                except Exception:
                    pass

                await _wait_for_login_clear(page)
                # Re-open the note URL after login to ensure we're on the correct page.
                _stage_log(task, "reload_after_login", url=_redact_url(target_url))
                await page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
                await page.wait_for_timeout(1200)

            # Try to wait for a meaningful note container; don't hang forever.
            try:
                await page.wait_for_selector("article, [class*='note']", timeout=min(8000, timeout_ms))
            except Exception:
                pass

            # Scroll a bit to trigger lazy-loading
            try:
                await page.evaluate(
                    """
                    () => {
                      window.scrollTo(0, document.body.scrollHeight);
                    }
                    """
                )
                await page.wait_for_timeout(1200)
            except Exception:
                pass

            try:
                page_image_urls = await _collect_page_note_images_async(page)
            except Exception:
                page_image_urls = []

            try:
                note_signals = await page.evaluate(
                    """
                    () => {
                      const pickText = (selectors) => {
                        for (const selector of selectors) {
                          const nodes = Array.from(document.querySelectorAll(selector));
                          for (const node of nodes) {
                            const text = (node.innerText || node.textContent || '').trim();
                            if (text && text.length >= 8) return text;
                          }
                        }
                        return '';
                      };
                      const title = pickText(['h1','[class*="title"]','[class*="note-title"]']);
                      const content = pickText(['[class*="note-content"]','[class*="content"]','[class*="desc"]','article']);
                      return {title, content};
                    }
                    """
                )
                if isinstance(note_signals, dict):
                    page_note_title = str(note_signals.get("title", "") or "")
                    page_note_content = str(note_signals.get("content", "") or "")
            except Exception:
                pass
            _stage_log(
                task,
                "extract_dom_done",
                page_title_len=len((page_note_title or "").strip()),
                page_content_len=len((page_note_content or "").strip()),
                page_images=len(page_image_urls or []),
            )

            try:
                body2 = (await page.inner_text("body"))[:4000]
            except Exception:
                body2 = ""
            gate_reason_after = _gate_reason(body2)

            if _looks_like_not_found_page(body2):
                _stage_log(task, "gate_detected", gate_type="not_found", phase="after", url=_redact_url(target_url))
                raise XHSCrawlError(_not_found_hint(), status_code=404)
            if _looks_like_risk_control_gate(body2):
                _stage_log(task, "gate_detected", gate_type="risk", phase="after", url=_redact_url(target_url))
                raise XHSCrawlError(
                    "采集被风控页拦截（300012/IP 风险）。建议切换网络/代理或使用 Browser Relay 从你可见页面提取正文。"
                    + _relay_hint(),
                    status_code=429,
                )
            if await _page_looks_like_interactive_gate_async(page, body2):
                if auto_login and not headless:
                    _stage_log(task, "gate_detected", gate_type="login_or_consent", phase="after", url=_redact_url(target_url))
                    # Gate can show up after hydration/scroll; allow one more interactive wait + reload.
                    try:
                        await page.bring_to_front()
                    except Exception:
                        pass
                    await _wait_for_login_clear(page)
                    _stage_log(task, "reload_after_login", url=_redact_url(target_url))
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
                    await page.wait_for_timeout(1200)

                    try:
                        body2 = (await page.inner_text("body"))[:4000]
                    except Exception:
                        body2 = ""
                    gate_reason_after = _gate_reason(body2)

                    if _looks_like_not_found_page(body2):
                        raise XHSCrawlError(_not_found_hint(), status_code=404)
                    if _looks_like_risk_control_gate(body2):
                        raise XHSCrawlError(
                            "采集被风控页拦截（300012/IP 风险）。建议切换网络/代理或使用 Browser Relay 从你可见页面提取正文。"
                            + _relay_hint(),
                            status_code=429,
                        )
                    if await _page_looks_like_interactive_gate_async(page, body2):
                        raise XHSCrawlError(
                            "采集被登录/协议页拦截：请在弹出的浏览器窗口中完成同意/登录后重试" + _relay_hint(),
                            status_code=401,
                        )
                else:
                    raise XHSCrawlError(
                        "采集被登录/协议页拦截：请在弹出的浏览器窗口中完成同意/登录后重试" + _relay_hint(),
                        status_code=401,
                    )

            html_text = await page.content()

            fields = {"titles": [], "contents": [], "images": []}
            for cand in _extract_json_candidates(html_text):
                _collect_fields(cand, fields)
            _stage_log(
                task,
                "extract_json_done",
                json_title_candidates=len(fields.get("titles") or []),
                json_content_candidates=len(fields.get("contents") or []),
                json_images_candidates=len(fields.get("images") or []),
            )

            meta_title = _extract_meta(html_text, "og:title")
            title_tag = _extract_title_tag(html_text)
            desc = _extract_meta(html_text, "og:description")
            og_image = _extract_meta(html_text, "og:image")

            titles = (
                fields["titles"]
                + ([meta_title] if meta_title else [])
                + ([title_tag] if title_tag else [])
                + ([page_note_title] if page_note_title else [])
            )
            contents = fields["contents"] + ([desc] if desc else []) + ([page_note_content] if page_note_content else [])

            title_best = _choose_best_title(meta_title=meta_title, title_tag=title_tag, candidates=titles)
            content_best = _normalize_content_text(_choose_best_text(contents))
            content_best = _normalize_content_text(_strip_leading_title_from_content(title_best, content_best))
            content_best = _normalize_content_text(_dedupe_content_text(title_best, content_best))
            title_best, content_best = _prefer_split_title_content(title_best, content_best)
            title_best = _clean_title(title_best)
            content_best = _normalize_content_text(content_best)
            ref_text = f"{title_best}\n{content_best}".strip()

            if _looks_like_not_found_page(ref_text) or _looks_like_not_found_page(title_best):
                raise XHSCrawlError(_not_found_hint(), status_code=404)

            image_urls: list[str] = []
            for raw in fields["images"]:
                if not isinstance(raw, str):
                    continue
                if not _looks_like_note_image_url(raw):
                    continue
                image_urls.append(_normalize_image_url(raw))

            # Regex fallback: sometimes URLs exist only inside embedded JSON or css.
            if not image_urls:
                url_hits = re.findall(r"https?://[^\s\"']+", html_text)
                for hit in url_hits:
                    if _looks_like_note_image_url(hit):
                        image_urls.append(_normalize_image_url(hit))

            # Ensure at least cover image when available
            if og_image and _looks_like_image_url(og_image) and "avatar" not in og_image.lower():
                image_urls.append(_normalize_image_url(og_image))

            image_urls = _dedupe_keep_order(page_image_urls + image_urls)[:12]

            _stage_log(
                task,
                "extract_done",
                title_len=len((title_best or "").strip()),
                content_len=len((content_best or "").strip()),
                image_count=len(image_urls or []),
            )

            # Final gate check: avoid false positives by combining extracted payload and page body signals.
            extracted_reason = _gate_reason(ref_text) or _gate_reason(content_best)
            final_reason = gate_reason_after or extracted_reason
            if final_reason == "risk":
                raise XHSCrawlError(
                    "采集被风控页拦截（300012/IP 风险）。建议切换网络/代理或使用 Browser Relay 从你可见页面提取正文。"
                    + _relay_hint(),
                    status_code=429,
                )
            if final_reason in {"login", "consent"}:
                if not content_best.strip():
                    raise XHSCrawlError(
                        "采集被登录/协议页拦截：请在弹出的浏览器窗口中完成同意/登录后重试" + _relay_hint(),
                        status_code=401,
                    )
            if final_reason == "placeholder":
                if _looks_like_placeholder_gate(ref_text) or (not content_best.strip()):
                    raise XHSCrawlError(
                        "采集结果疑似被拦截（只拿到口号/占位内容），请先登录或切换网络后重试。" + _relay_hint(),
                        status_code=401,
                    )

            await _persist_context_cookie(ctx)
            return ref_text, image_urls
        except XHSCrawlError as exc:
            # If we are headful and the page is a login/consent gate, keep it open so user can interact.
            if exc.status_code == 401 and not headless:
                keep_open = True
            raise
        finally:
            if not keep_open:
                try:
                    await page.close()
                except Exception:
                    pass

    async with async_playwright() as pw:
        launch_args = ["--disable-blink-features=AutomationControlled"]

        # Prefer persistent profile when configured.
        if user_data_dir:
            async with _PW_CTX_USE_LOCK:
                # 1st attempt (headless as configured)
                ctx = await _get_persistent_context(
                    user_data_dir=user_data_dir, headless=playwright_headless, user_agent=user_agent
                )
                try:
                    result = await _try_urls_in_context(ctx, headless=playwright_headless)
                except _NeedHeadful:
                    # Recreate headful context for manual login and retry.
                    ctx2 = await _get_persistent_context(user_data_dir=user_data_dir, headless=False, user_agent=user_agent)
                    result = await _try_urls_in_context(ctx2, headless=False)

                # Success: close the persistent browser window/process to avoid leaving UI open.
                await _close_persistent_context(reason="extract_done")
                return result

        # Fallback: ephemeral browser
        browser = await pw.chromium.launch(headless=playwright_headless, args=launch_args)
        ctx = await browser.new_context(user_agent=user_agent)
        try:
            try:
                return await _try_urls_in_context(ctx, headless=playwright_headless)
            except _NeedHeadful:
                await ctx.close()
                await browser.close()
                browser = await pw.chromium.launch(headless=False, args=launch_args)
                ctx = await browser.new_context(user_agent=user_agent)
                return await _try_urls_in_context(ctx, headless=False)
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass


async def crawl_xhs_note_from_cdp_async(source_text: str) -> dict[str, Any]:
    """Extract note via existing local Chrome using CDP.

    This is the most reliable way if the user can open the note in their Chrome.
    It relies on the local CDP endpoint exposed by OpenClaw gateway (default: http://127.0.0.1:18792).
    """
    source_url = _extract_xhs_url(source_text)
    note_id = _extract_note_id(source_url)
    canonical_url = _build_canonical_url(note_id)
    url_candidates = _build_url_candidates(source_url=source_url, note_id=note_id)

    # OpenClaw chrome CDP endpoint (local)
    cdp_http = os.getenv("OPENCLAW_CHROME_CDP_HTTP") or "http://127.0.0.1:18792/cdp"

    try:
        from playwright.async_api import async_playwright
    except ModuleNotFoundError as exc:
        raise XHSCrawlError(
            "缺少 playwright 依赖（需要安装 playwright 并执行 playwright install chromium）" + _relay_hint(),
            status_code=502,
        ) from exc

    timeout = int(os.getenv("XHS_CRAWL_TIMEOUT") or "20")
    timeout_ms = int(timeout * 1000)

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(cdp_http)
        except Exception as exc:
            raise XHSCrawlError(
                "无法连接到本机 Chrome CDP（Relay）。请确认 OpenClaw Gateway 运行中，且 Browser Relay 已安装/启用。" + _relay_hint(),
                status_code=502,
            ) from exc

        # Reuse existing context if present, else create one.
        contexts = browser.contexts
        context = contexts[0] if contexts else await browser.new_context(user_agent=_http_headers().get("User-Agent", ""))

        # Try to find an already-open tab for this note.
        target_page = None
        for p in context.pages:
            try:
                u = p.url or ""
            except Exception:
                u = ""
            if note_id in u or canonical_url in u:
                target_page = p
                break

        if target_page is None:
            target_page = await context.new_page()
            last_nf = False
            for u in url_candidates:
                try:
                    await target_page.goto(u, wait_until="domcontentloaded", timeout=timeout_ms)
                    await target_page.wait_for_timeout(1200)
                except Exception:
                    continue

                try:
                    body_probe = (await target_page.inner_text("body"))[:4000]
                except Exception:
                    body_probe = ""

                if _looks_like_risk_control_gate(body_probe):
                    raise XHSCrawlError(
                        "当前网络/环境触发小红书风控（300012/IP风险）。请切换网络后重试。" + _relay_hint(),
                        status_code=429,
                    )

                if _looks_like_not_found_page(body_probe):
                    last_nf = True
                    continue

                last_nf = False
                canonical_url = u
                break

            if last_nf:
                raise XHSCrawlError(
                    _not_found_hint(),
                    status_code=404,
                )

        await target_page.wait_for_timeout(1200)

        # If risk control gate, fail fast.
        try:
            body = (await target_page.inner_text("body"))[:4000]
        except Exception:
            body = ""
        if _looks_like_not_found_page(body):
            raise XHSCrawlError(
                _not_found_hint(),
                status_code=404,
            )

        if body and ("安全限制" in body or "IP存在风险" in body or "300012" in body):
            raise XHSCrawlError(
                "当前网络/环境触发小红书风控（300012/IP风险）。请切换网络后重试。" + _relay_hint(),
                status_code=429,
            )

        # Extract title/content/images from DOM (avoid comments)
        result = await target_page.evaluate(
            """() => {
              const docTitle = (document.title||'').trim();
              const ogTitle = document.querySelector('meta[property="og:title"],meta[name="og:title"]')?.getAttribute('content')?.trim() || '';
              const rawTitle = (ogTitle || docTitle).replace(/\\s*-\\s*小红书\\s*$/,'').trim();

              const stopPhrases = ['共 ', '条评论', '登录查看全部评论', '登录后评论', '沪ICP备', '© 2014', '营业执照'];
              const uiTrash = s => /^(登录|马上登录即可|创作中心|业务合作|通知|发现|发布|关注)$/.test((s||'').trim());
              const isDate = s => /^20\\d{2}-\\d{2}-\\d{2}$/.test((s||'').trim());

              // Choose largest block among typical containers
              const sels = ['main article','article','main','[class*="note"]'];
              const blocks=[];
              for (const sel of sels) {
                for (const el of Array.from(document.querySelectorAll(sel))) {
                  const t=(el.innerText||'').trim();
                  if (!t || t.length<80) continue;
                  blocks.push({sel,len:t.length,text:t});
                }
              }
              blocks.sort((a,b)=>b.len-a.len);
              let text = blocks[0]?.text || '';

              let cut=-1;
              for (const p of stopPhrases) {
                const i=text.indexOf(p);
                if (i!==-1) cut = (cut===-1) ? i : Math.min(cut,i);
              }
              if (cut>0) text=text.slice(0,cut).trim();

              const lines = text.split(/\n+/).map(s=>s.trim()).filter(Boolean).filter(l=>!uiTrash(l));

              let title = rawTitle;
              let date = '';
              let bodyLines = lines.slice(0);
              if (bodyLines.length>=2 && isDate(bodyLines[1]) && bodyLines[0].length<=120 && !uiTrash(bodyLines[0])) {
                title = bodyLines[0];
                date = bodyLines[1];
                bodyLines = bodyLines.slice(2);
              } else if (bodyLines.length>=1 && isDate(bodyLines[0])) {
                date = bodyLines[0];
                bodyLines = bodyLines.slice(1);
              }
              bodyLines = bodyLines.filter(l=>!uiTrash(l));
              const content = bodyLines.join('\n').trim();

              const imgs = Array.from(document.images||[])
                .map(i=>i.currentSrc||i.src)
                .filter(u=>u && /^https?:\\/\\//.test(u))
                .filter(u=>!/avatar|icon|logo|favicon/.test(u))
                .filter(u=>!/\\/comment\\//.test(u));
              const imageUrls=[];
              for (const u of imgs) if (!imageUrls.includes(u)) imageUrls.push(u);

              return {title, date, content, imageUrls};
            }"""
        )

        title_best = (result.get("title") or "").strip()
        content_best = (result.get("content") or "").strip()
        if not title_best and not content_best:
            raise XHSCrawlError("Relay 页面中未提取到标题/正文（可能未滚动到正文或页面结构变化）" + _relay_hint(), status_code=422)

        image_urls = result.get("imageUrls") or []
        image_urls = [str(u) for u in image_urls if isinstance(u, str) and _looks_like_image_url(u)]
        image_urls = _dedupe_keep_order(image_urls)[:12]

        reference_text = f"{title_best}\n{content_best}".strip()
        reason = _gate_reason(reference_text) or _gate_reason(content_best)
        if reason == "risk":
            raise XHSCrawlError(
                "Relay 页面触发风控（300012/IP 风险）。建议切换网络/代理后重试。" + _relay_hint(),
                status_code=429,
            )
        if reason in {"login", "consent"}:
            raise XHSCrawlError("Relay 页面疑似仍为登录/协议页" + _relay_hint(), status_code=401)
        if reason == "placeholder":
            raise XHSCrawlError("Relay 页面疑似仍为拦截页/占位内容" + _relay_hint(), status_code=401)

        # Do not close the user's browser.
        return {
            "title": title_best,
            "content": content_best,
            "reference_text": reference_text,
            "canonical_url": canonical_url,
            "note_id": note_id,
            "crawl_mode": "relay_cdp",
            "image_urls": image_urls,
            "image_count": len(image_urls),
        }


async def crawl_xhs_note_light_async(source_text: str, trace_id: str | None = None) -> dict[str, Any]:

    task = TaskLogger(trace_id) if trace_id else None
    t0 = time.monotonic()

    source_url = _extract_xhs_url(source_text)
    note_id = _extract_note_id(source_url)
    canonical_url = _build_canonical_url(note_id)
    url_candidates = _build_url_candidates(source_url=source_url, note_id=note_id)

    cookie = (os.getenv("XHS_COOKIE") or "").strip() or None
    timeout = int(os.getenv("XHS_CRAWL_TIMEOUT") or "20")

    _stage_log(
        task,
        "extract_start",
        source_url=_redact_url(source_url),
        note_id=note_id,
        timeout_s=timeout,
        has_cookie=bool(cookie),
        user_data_dir_set=bool((os.getenv("XHS_USER_DATA_DIR") or "").strip()),
        headless=_env_bool("XHS_PLAYWRIGHT_HEADLESS", True),
    )

    resp = requests.get(source_url, headers=_http_headers(cookie), timeout=timeout)
    if resp.status_code >= 400:
        raise XHSCrawlError(f"小红书页面请求失败 status={resp.status_code}", status_code=502)

    html_text = resp.text or ""

    meta_title = _extract_meta(html_text, "og:title")
    title_tag = _extract_title_tag(html_text)
    desc = _extract_meta(html_text, "og:description")
    og_image = _extract_meta(html_text, "og:image")
    if "3 亿人的生活经验" in (desc or ""):
        desc = ""

    fields = {"titles": [], "contents": [], "images": []}
    for cand in _extract_json_candidates(html_text):
        _collect_fields(cand, fields)

    titles = fields["titles"] + ([meta_title] if meta_title else []) + ([title_tag] if title_tag else [])
    contents = fields["contents"] + ([desc] if desc else [])

    title_best = _choose_best_title(meta_title=meta_title, title_tag=title_tag, candidates=titles)
    content_best = _normalize_content_text(_choose_best_text(contents))
    content_best = _normalize_content_text(_strip_leading_title_from_content(title_best, content_best))
    content_best = _normalize_content_text(_dedupe_content_text(title_best, content_best))
    title_best, content_best = _prefer_split_title_content(title_best, content_best)
    title_best = _clean_title(title_best)
    content_best = _normalize_content_text(content_best)

    image_urls = []
    for raw in fields["images"]:
        if not isinstance(raw, str):
            continue
        if not _looks_like_note_image_url(raw):
            continue
        image_urls.append(_normalize_image_url(raw))
    if og_image and _looks_like_image_url(og_image) and "avatar" not in og_image.lower():
        image_urls.append(_normalize_image_url(og_image))

    image_urls = _dedupe_keep_order(image_urls)[:12]

    reference_text = f"{title_best}\n{content_best}".strip()

    # If HTTP got gated/placeholder page, force Playwright fallback (so auto-login can kick in).
    http_reason = _gate_reason(reference_text) or _gate_reason(content_best)
    _stage_log(
        task,
        "http_fetch_done",
        status_code=resp.status_code,
        html_len=len(html_text),
        http_reason=http_reason or "none",
        title_len=len((title_best or "").strip()),
        content_len=len((content_best or "").strip()),
        image_count=len(image_urls or []),
    )
    if http_reason == "risk":
        raise XHSCrawlError(
            "采集被风控页拦截（300012/IP 风险）。建议切换网络/代理或使用 Browser Relay 从你可见页面提取正文。"
            + _relay_hint(),
            status_code=429,
        )

    http_not_found = _looks_like_not_found_page(reference_text) or _looks_like_not_found_page(title_best)

    needs_fallback = (
        (not content_best.strip())
        or (len(image_urls) == 0)
        or http_not_found
        or (http_reason in {"login", "consent", "placeholder"})
    )
    if needs_fallback:
        import asyncio

        auto_login_wait_s = 0.0
        if _env_bool("XHS_AUTO_LOGIN_ON_401", True):
            auto_login_wait_s = max(_env_int("XHS_AUTO_LOGIN_WAIT_MS", 120000), 0) / 1000.0
        wrapper_timeout_s = float(timeout + 15) + auto_login_wait_s + 10.0
        _stage_log(
            task,
            "fallback_playwright_start",
            wrapper_timeout_s=wrapper_timeout_s,
            candidates_count=len(url_candidates or []),
            reason=http_reason or ("not_found" if http_not_found else "empty_or_no_images"),
        )
        try:
            ref2, imgs2, used_url = await asyncio.wait_for(
                _crawl_with_cookie_playwright_async(
                    url_candidates=url_candidates,
                    timeout=timeout,
                    cookie_header=cookie,
                    trace_id=trace_id,
                ),
                timeout=wrapper_timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise XHSCrawlError(
                f"playwright 抓取超时（{wrapper_timeout_s:.0f}s）。建议切换网络/稍后重试，或使用 Browser Relay 提取正文。" + _relay_hint(),
                status_code=504,
            ) from exc
        if used_url:
            canonical_url = used_url
        if ref2.strip():
            reference_text = ref2.strip()
        if imgs2:
            image_urls = imgs2
        _stage_log(
            task,
            "fallback_playwright_done",
            used_url=_redact_url(canonical_url),
            title_len=len((ref2.split("\n", 1)[0] if "\n" in (ref2 or "") else "").strip()),
            content_len=len((ref2.split("\n", 1)[1] if "\n" in (ref2 or "") else "").strip()),
            image_count=len(image_urls or []),
        )

        if "\n" in reference_text:
            t, c = reference_text.split("\n", 1)
            if t.strip():
                title_best = t.strip()
            if c.strip():
                content_best = c.strip()

    reason = _gate_reason(reference_text) or _gate_reason(content_best)
    if reason == "risk":
        raise XHSCrawlError(
            "采集被风控页拦截（300012/IP 风险）。建议切换网络/代理或使用 Browser Relay 从你可见页面提取正文。"
            + _relay_hint(),
            status_code=429,
        )
    if reason in {"login", "consent"}:
        raise XHSCrawlError(
            "采集被登录/协议页拦截：请先完成登录/同意协议后重试。" + _relay_hint(),
            status_code=401,
        )
    if reason == "placeholder":
        raise XHSCrawlError(
            "采集结果疑似被拦截（只拿到口号/占位内容），请先登录或切换网络后重试。" + _relay_hint(),
            status_code=401,
        )

    if _looks_like_not_found_page(reference_text) or _looks_like_not_found_page(title_best):
        raise XHSCrawlError(
            _not_found_hint(),
            status_code=404,
        )

    if not title_best and not content_best:
        raise XHSCrawlError("结构化数据中未找到标题或正文（可能需要 cookie/playwright）" + _relay_hint(), status_code=422)

    _stage_log(
        task,
        "extract_done",
        crawl_mode="light_http+playwright" if needs_fallback else "light_http",
        final_title_len=len((title_best or "").strip()),
        final_content_len=len((content_best or "").strip()),
        final_image_count=len(image_urls or []),
        duration_ms=int((time.monotonic() - t0) * 1000),
    )

    return {
        "title": title_best,
        "content": content_best,
        "reference_text": reference_text,
        "canonical_url": canonical_url,
        "note_id": note_id,
        "crawl_mode": "light_http+playwright" if needs_fallback else "light_http",
        "image_urls": image_urls,
        "image_count": len(image_urls),
    }
