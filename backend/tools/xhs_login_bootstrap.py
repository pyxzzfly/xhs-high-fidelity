"""Bootstrap a persistent Playwright profile for XHS crawling.

Run:
  backend/venv/bin/python tools/xhs_login_bootstrap.py

It will open a visible Chromium window using XHS_USER_DATA_DIR.
Login / accept agreements in the window, then wait until the script exits.
"""

from __future__ import annotations

import os
import asyncio

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=False)


async def main():
    user_data_dir = (os.getenv("XHS_USER_DATA_DIR") or "").strip()
    if not user_data_dir:
        raise SystemExit("XHS_USER_DATA_DIR not set in backend/.env")

    headless_raw = (os.getenv("XHS_PLAYWRIGHT_HEADLESS") or "false").strip().lower()
    headless = headless_raw not in {"0", "false", "no", "off"}
    # Force visible for bootstrap
    headless = False

    wait_ms = int(os.getenv("XHS_BOOTSTRAP_WAIT_MS") or os.getenv("XHS_PLAYWRIGHT_MANUAL_VERIFY_WAIT_MS") or "600000")

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = await context.new_page()
        await page.goto("https://www.xiaohongshu.com/", wait_until="domcontentloaded")
        print("Opened Chromium with persistent profile.")
        print("Please login / accept agreements in the window.")
        print(f"Waiting {wait_ms/1000:.0f}s...")
        await page.wait_for_timeout(wait_ms)
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
