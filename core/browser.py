"""Single source of truth for the persistent Chromium profile.

The scanner and the manual sign-in helper share ONE profile so the logged-in
LinkedIn session persists between runs. Override the location with the
BROWSER_PROFILE env var (useful for pointing at an existing logged-in profile);
otherwise it lives at .browser_profile/ in the repo root (gitignored).
"""
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
PROFILE = Path(os.environ.get("BROWSER_PROFILE") or _ROOT / ".browser_profile")
