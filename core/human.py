"""
Human-like timing and interaction helpers.
All delays use gaussian distributions (bell curve) rather than uniform
ranges -- real humans cluster around a mean with occasional outliers.
"""

import random
import time
import math


def _gauss(mean, sd, lo=None, hi=None):
    """Gaussian sample clamped to [lo, hi]."""
    v = random.gauss(mean, sd)
    if lo is not None: v = max(lo, v)
    if hi is not None: v = min(hi, v)
    return v


def pause(mean=1.2, sd=0.4, lo=0.3):
    """General-purpose pause -- navigating, waiting for things."""
    time.sleep(_gauss(mean, sd, lo=lo))


def read(mean=2.5, sd=0.8, lo=0.8):
    """Simulate reading a page / profile before acting."""
    time.sleep(_gauss(mean, sd, lo=lo))


def think(mean=0.6, sd=0.25, lo=0.15):
    """Short hesitation before clicking a button."""
    time.sleep(_gauss(mean, sd, lo=lo))


def between_pages(mean=3.5, sd=1.2, lo=1.5):
    """Gap between navigating to separate pages."""
    # Occasionally take a longer break (simulate distraction ~8% of the time)
    if random.random() < 0.08:
        time.sleep(_gauss(12.0, 4.0, lo=6.0))
    else:
        time.sleep(_gauss(mean, sd, lo=lo))


def after_action(mean=2.0, sd=0.7, lo=0.8):
    """Gap after clicking Connect / Send."""
    time.sleep(_gauss(mean, sd, lo=lo))


def keystroke():
    """Delay between individual keystrokes -- varies by 'finger dexterity'."""
    # Most keystrokes are fast; occasionally a pause (shift key, thinking)
    if random.random() < 0.05:
        return _gauss(0.25, 0.1, lo=0.12)   # brief pause mid-word
    return _gauss(0.08, 0.03, lo=0.03)


def scroll_pause():
    """Pause mid-scroll, simulating reading while scrolling."""
    time.sleep(_gauss(1.8, 0.6, lo=0.5))


def type_text(page, text):
    """Type text character by character with human-like keystroke timing."""
    for char in text:
        page.keyboard.type(char)
        time.sleep(keystroke())
    # Brief pause after finishing a field
    time.sleep(_gauss(0.4, 0.15, lo=0.1))


def scroll_down(page, amount=None):
    """Scroll down by a human-like random amount."""
    if amount is None:
        amount = int(_gauss(650, 180, lo=200, hi=1200))
    page.evaluate(f"window.scrollBy(0, {amount})")
    scroll_pause()
