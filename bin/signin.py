"""Open the shared Chromium profile so you can log into LinkedIn by hand.

Usage:
    python bin/signin.py                       # opens linkedin.com
    python bin/signin.py https://example.com   # opens any URL

Waits until you close the browser window, so cookies flush to disk cleanly.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from playwright.sync_api import sync_playwright
from core.browser import PROFILE

url = sys.argv[1] if len(sys.argv) > 1 else "https://www.linkedin.com/login"

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(str(PROFILE), headless=False)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(url)
    print(f"Opened {url}. Log in, then CLOSE THE BROWSER WINDOW to save the session.")
    closed = {"v": False}
    ctx.on("close", lambda _: closed.update(v=True))
    while not closed["v"]:
        try:
            page.wait_for_timeout(1000)
        except Exception:
            break
    print("Session saved.")
