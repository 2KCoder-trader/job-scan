"""job-scan — LinkedIn job scanner + scorer.

Walks a LinkedIn jobs search, reads every posting's description, scores it
against your resume with Claude, and prints / exports a ranked CSV.
Scan and score only — this tool never applies to anything.

Run:
    python job_scan.py --list --jitter 0     # scan now, write data/job_scan_<ts>.csv
    python job_scan.py --list out.csv        # custom CSV path
    python job_scan.py --max 10              # stop after 10 scored jobs
"""

import argparse
import csv
import logging
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

from core import human
from core import job_score
from core.browser import PROFILE
from core.job import Job
from core.notifications import tg_send
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

logging.basicConfig(
    filename=_ROOT / "job_scan.log",
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    encoding="utf-8",
)
log = logging.getLogger()

EMAIL    = os.environ.get("LI_EMAIL", "").strip().strip('"')
PASSWORD = os.environ.get("LI_PASSWORD", "").strip().strip('"')

JOBS_URL = os.environ.get(
    "LI_JOBS_URL",
    "https://www.linkedin.com/jobs/search-results/?keywords=data&f_TPR=r86400",
)
BLACKLIST_FILE = _ROOT / "data" / "blacklist_companies.txt"

MAX_PAGES = 5


# ── Blacklist ─────────────────────────────────────────────────────────────────

def load_blacklist() -> set[str]:
    """Lowercase set of company names to skip. Empty if file missing."""
    if not BLACKLIST_FILE.exists():
        return set()
    out = set()
    for line in BLACKLIST_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line.lower())
    return out


def is_blacklisted(company: str, blacklist: set[str]) -> bool:
    """True if the company matches a blacklist entry exactly OR starts with one
    followed by a space. Catches 'Haystack', 'Haystack App', 'Haystack AI' from
    a single 'Haystack' entry without false-positive on 'Squarespace' from
    'Square' (no trailing space to separate the suffix).
    """
    c = (company or "").strip().lower()
    if not c:
        return False
    if c in blacklist:
        return True
    for entry in blacklist:
        if c.startswith(entry + " "):
            return True
    return False


# ── Auth ──────────────────────────────────────────────────────────────────────

def login(page):
    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
    human.read(mean=4.0, sd=0.8)
    ef = page.locator('input[autocomplete="username webauthn"]')
    ef.wait_for(state="visible", timeout=15000)
    ef.click(); human.think()
    human.type_text(page, EMAIL)
    human.pause(mean=0.7, sd=0.2)
    page.locator('input[autocomplete="current-password"]').click(); human.think()
    human.type_text(page, PASSWORD); human.think()
    page.locator('button[data-litms-control-urn="login-submit"]').click()
    page.wait_for_url("**/feed/**", timeout=120000)
    print("Logged in.")


# ── JS helpers ────────────────────────────────────────────────────────────────

# Pull every visible card on the current page. Easy-Apply badge and "Applied"
# status both live inside the card itself, so we read them up front.
EXTRACT_CARDS_JS = r"""
() => {
    const out = [];
    const seen = new Set();
    for (const el of document.querySelectorAll('[componentkey*="job-card-component-ref-"]')) {
        const m = (el.getAttribute('componentkey') || '').match(/job-card-component-ref-(\d+)/);
        if (!m || seen.has(m[1])) continue;
        seen.add(m[1]);
        if (el.children.length === 0) continue;
        const txt = (el.innerText || '').trim();
        const lines = txt.split('\n').map(s => s.trim()).filter(Boolean);
        // LinkedIn renders the title TWICE — once in a screen-reader span (often
        // with "(Verified job)" appended) and once visually. Both end up in
        // innerText. Dedupe by dropping any later line equal to the cleaned title.
        const titleRaw = lines[0] || '';
        const title    = titleRaw.replace(/\s*\(Verified job\)\s*$/i, '').trim();
        const rest     = lines.slice(1).filter(s => s.trim() !== title);
        const company  = rest[0] || '';
        const isEasyApply = /\bEasy Apply\b/.test(txt);
        let status = null;
        for (const tag of ['Applied', 'Saved', 'Viewed']) {
            if (new RegExp(`(^|\\s)${tag}(\\s|·|$)`).test(txt)) { status = tag; break; }
        }
        out.push({ id: m[1], title, company, isEasyApply, status });
    }
    return out;
}
"""

# Scroll the left list panel (cards lazy-load).
SCROLL_LIST_JS = r"""
() => {
    const item = document.querySelector('[componentkey*="job-card-component-ref-"]');
    if (item) {
        let el = item.parentElement;
        while (el && el !== document.body) {
            const s = window.getComputedStyle(el);
            if ((s.overflowY === 'auto' || s.overflowY === 'scroll')
                    && el.scrollHeight > el.clientHeight + 50) {
                el.scrollBy(0, 600);
                return 'list-panel';
            }
            el = el.parentElement;
        }
    }
    window.scrollBy(0, 600);
    return 'window-fallback';
}
"""

# Read job-detail panel: { description, applicants, applyHref, isEasyApply }.
DETAIL_JS = r"""
() => {
    const panel = document.querySelector(
        '[data-sdui-screen*="JobDetails"], [role="main"]') || document.body;
    const text = (panel.innerText || '').trim();

    const description = text;

    // Applicants live in a small span near the top of the panel:
    //   "97 people clicked apply"
    //   "Over 100 applicants"
    //   "Be an early applicant"
    //   "27 applicants"
    // Phrasing varies; the integer is the only reliable signal. Walk every
    // visible leaf-ish element in the panel and pick the first short string
    // matching the applicant/click/people vocabulary.
    let applicants = null;
    const APPL_RE = /(applicant|clicked|viewed|people|early)/i;
    for (const el of panel.querySelectorAll('span, p, div')) {
        if (!el.offsetParent) continue;
        const t = (el.innerText || '').trim();
        if (!t || t.length > 80) continue;
        if (!APPL_RE.test(t)) continue;
        if (/early applicant/i.test(t)) { applicants = 0; break; }
        const m = t.match(/(\d[\d,]*)/);
        if (m) { applicants = parseInt(m[1].replace(/,/g, ''), 10); break; }
    }
    if (applicants === null && /be an early applicant/i.test(text)) applicants = 0;

    // Apply button — read href, don't click.
    let applyHref = null;
    let isEasyApply = false;
    for (const el of panel.querySelectorAll('a, button')) {
        if (!el.offsetParent) continue;
        const txt = (el.innerText || el.textContent || '').trim();
        if (/^easy apply$/i.test(txt)) {
            isEasyApply = true;
            applyHref = el.getAttribute('href') || '';
            break;
        }
        if (/^apply$/i.test(txt)) {
            applyHref = el.getAttribute('href') || el.href || '';
            break;
        }
    }
    return { description, applicants, applyHref, isEasyApply };
}
"""


# ── Source classification ─────────────────────────────────────────────────────

def decode_safety_url(href: str) -> str | None:
    """LinkedIn wraps external links as /safety/go/?url=<encoded>. Return decoded."""
    if not href:
        return None
    if "/safety/go/" not in href:
        return href  # already a direct URL
    qs = parse_qs(urlsplit(href).query)
    val = qs.get("url", [None])[0]
    return val


_ATS_HOST_PATTERNS = [
    # (source name, host substring)
    ("workday",    "myworkdayjobs.com"),
    ("workday",    "workday.com"),
    ("greenhouse", "greenhouse.io"),      # boards.greenhouse.io, job-boards.greenhouse.io
    ("ashby",      "ashbyhq.com"),        # jobs.ashbyhq.com, *.ashbyhq.com
    ("ashby",      "ashby.com"),
    ("lever",      "lever.co"),           # jobs.lever.co
    ("icims",      "icims.com"),          # careers-*.icims.com
    ("gem",        "gem.com"),            # jobs.gem.com
    ("smartrecruiters", "smartrecruiters.com"),
    ("taleo",      "taleo.net"),
    ("successfactors", "successfactors.com"),
    ("breezy",     "breezy.hr"),
    ("jobvite",    "jobvite.com"),
    ("bamboohr",   "bamboohr.com"),
    ("recruitee",  "recruitee.com"),
    ("rippling",   "ats.rippling.com"),
    ("adp",        "workforcenow.adp.com"),
]


def classify_source(apply_url: str | None, is_easy_apply: bool) -> str:
    if is_easy_apply:
        return "linkedin_easy"
    host = (urlsplit(apply_url or "").hostname or "").lower()
    if not host:
        return "custom"
    for name, needle in _ATS_HOST_PATTERNS:
        if needle in host:
            return name
    return "custom"


# ── Per-card flow ─────────────────────────────────────────────────────────────

def click_card(page, job_id: str) -> bool:
    """Click the card whose componentkey matches job_id, then wait for the detail
    panel's Apply button to appear so DETAIL_JS sees a fully-rendered right pane."""
    sel = f'[componentkey="job-card-component-ref-{job_id}"]'
    try:
        page.locator(sel).first.click(timeout=5000)
    except PWTimeout:
        log.warning(f"click_card timeout | id={job_id}")
        return False
    # Wait for any visible Apply or Easy Apply control in the right panel.
    try:
        page.locator(
            'button:has-text("Easy Apply"), a:has-text("Apply on company website"), '
            'button:has-text("Apply"), a[href*="/safety/go/"]'
        ).first.wait_for(state="visible", timeout=8000)
    except PWTimeout:
        log.info(f"click_card: apply control not visible in 8s | id={job_id}")
    human.read(mean=1.5, sd=0.4)
    return True


def build_job(page, card: dict) -> Job | None:
    """Click into the card, fill out a Job from the detail panel."""
    job_id = card["id"]
    if not click_card(page, job_id):
        return None

    try:
        detail = page.evaluate(DETAIL_JS)
    except Exception as e:
        log.warning(f"DETAIL_JS failed | id={job_id} err={e}")
        return None

    apply_url = decode_safety_url(detail.get("applyHref"))
    is_easy = bool(detail.get("isEasyApply"))

    # No Apply / Easy Apply button in the detail panel means the "Applied"
    # badge state — nothing to score.
    if not is_easy and not apply_url:
        log.info(f"Already applied | id={job_id} title={card.get('title','')!r} — skipping")
        return None

    return Job(
        id=job_id,
        title=card.get("title", "") or job_id,
        company=card.get("company", ""),
        li_url=f"https://www.linkedin.com/jobs/view/{job_id}/",
        source=classify_source(apply_url, is_easy),
        apply_url=apply_url if not is_easy else None,
        job_description=detail.get("description") or "",
        applicants=detail.get("applicants"),
    )


# ── Pagination ────────────────────────────────────────────────────────────────

def goto_next_page(page) -> bool:
    """Click the pagination Next button. Return False if there's no visible Next."""
    try:
        btn = page.locator(
            'button[data-testid="pagination-controls-next-button-visible"]'
        ).first
        btn.wait_for(state="visible", timeout=4000)
        btn.click()
        page.wait_for_load_state("domcontentloaded")
        human.read(mean=2.5, sd=0.6)
        return True
    except Exception as e:
        log.info(f"goto_next_page: no next ({e})")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def _kill_stale_browser() -> None:
    """Kill any Chromium left holding our profile dir from a prior run.

    A hard Ctrl-C can orphan the persistent-context browser: Python dies but
    the chromium process keeps running and keeps the profile dir open. On the
    next run, `launch_persistent_context` hands off to the half-dead orphan and
    the first real page op throws "Page crashed". Killing the orphan first
    guarantees a clean browser. Best-effort, Windows-only.
    """
    if sys.platform != "win32":
        return
    import subprocess
    needle = str(PROFILE.resolve()).replace("/", "\\")
    # Match only chrome.exe whose command line points at *our* profile dir, so
    # we never touch the user's own Chrome windows.
    ps = (
        "Get-CimInstance Win32_Process "
        "| Where-Object { ($_.Name -eq 'chrome.exe' "
        "-or $_.Name -eq 'chrome-headless-shell.exe') -and $_.CommandLine "
        f"-and $_.CommandLine.Contains('{needle}') }} "
        "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
        "-ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            timeout=20, capture_output=True,
        )
        log.info("cleared any stale browser processes holding the profile")
    except Exception as e:
        log.warning(f"_kill_stale_browser failed (continuing): {e}")


def run(max_jobs: int | None = None, pages: int = MAX_PAGES,
        export_csv: Path | None = None) -> None:
    """Walk JOBS_URL, score every card, print each job, optionally write a CSV."""
    seen_ids: set[str] = set()
    scored: list[Job] = []
    source_counts: Counter[str] = Counter()
    blacklist = load_blacklist()
    if blacklist:
        print(f"Loaded blacklist: {len(blacklist)} companies")
        log.info(f"Blacklist: {sorted(blacklist)}")

    _kill_stale_browser()
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE), headless=False,
            args=["--start-maximized"], viewport=None,
        )
        page = ctx.new_page()
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        human.pause()
        if "feed" not in page.url:
            login(page)

        page.goto(JOBS_URL, wait_until="domcontentloaded")
        human.read(mean=3.0, sd=0.8)

        for pg in range(1, pages + 1):
            print(f"\n=== Page {pg} ===")
            log.info(f"Scanning page {pg} | url={page.url}")

            # Lazy-load all cards on this page.
            for _ in range(6):
                page.evaluate(SCROLL_LIST_JS)
                human.pause(mean=1.0, sd=0.3)

            cards = page.evaluate(EXTRACT_CARDS_JS)
            print(f"  found {len(cards)} cards")

            for card in cards:
                if card["id"] in seen_ids:
                    continue
                seen_ids.add(card["id"])
                if card.get("status") == "Applied":
                    print(f"  - skip (already applied): {card.get('title','')[:60]}")
                    continue
                if is_blacklisted(card.get("company", ""), blacklist):
                    print(f"  - skip (blacklisted company {card.get('company')!r}): "
                          f"{card.get('title','')[:60]}")
                    log.info(f"Blacklist skip | id={card['id']} company={card.get('company')!r}")
                    continue

                job = build_job(page, card)
                if not job:
                    continue
                source_counts[job.source] += 1
                job.score, job.score_reason = job_score.score(job.job_description, job.title)
                scored.append(job)

                url = job.apply_url or job.li_url
                print(
                    f"  • id:         {job.id}\n"
                    f"    title:      {job.title}\n"
                    f"    company:    {job.company}\n"
                    f"    type:       {job.source}\n"
                    f"    applicants: {job.applicants}\n"
                    f"    score:      {job.score}\n"
                    f"    reason:     {job.score_reason}\n"
                    f"    url:        {url}"
                )
                log.info(
                    f"Job | id={job.id} source={job.source} score={job.score} "
                    f"reason={job.score_reason!r} applicants={job.applicants} "
                    f"title={job.title!r} url={url!r}"
                )

                if max_jobs and len(scored) >= max_jobs:
                    print(f"\nReached --max={max_jobs}, stopping.")
                    _finish(scored, len(seen_ids), source_counts, export_csv)
                    return

                human.between_pages()

            if not goto_next_page(page):
                break

    _finish(scored, len(seen_ids), source_counts, export_csv)


def _finish(scored: list[Job], total_seen: int,
            source_counts: Counter, export_csv: Path | None) -> None:
    _print_summary(scored, total_seen, source_counts)
    if export_csv is not None:
        _write_scored_csv(scored, export_csv)


def _write_scored_csv(jobs: list[Job], path: Path) -> None:
    """Write every scored job to `path` as CSV, sorted by score descending."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ranked = sorted(jobs, key=lambda j: (-(j.score or 0), j.id))
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "score", "id", "title", "company", "source", "applicants",
            "url", "score_reason",
        ])
        for j in ranked:
            w.writerow([
                j.score, j.id, j.title, j.company, j.source,
                "" if j.applicants is None else j.applicants,
                j.apply_url or j.li_url,
                j.score_reason,
            ])
    print(f"\n[list] wrote {len(ranked)} scored jobs -> {path}")
    log.info(f"--list wrote {len(ranked)} rows to {path}")


def _print_summary(scored: list[Job], total_seen: int,
                   source_counts: Counter) -> None:
    print(f"\n=== Summary ===")
    print(f"Total cards seen:  {total_seen}")
    print(f"Scored:            {len(scored)}")

    if source_counts:
        total = sum(source_counts.values())
        print(f"\nApplication types observed ({total} jobs):")
        for src, n in source_counts.most_common():
            pct = (n / total * 100) if total else 0
            print(f"  {src:18s} {n:4d}  ({pct:5.1f}%)")

    top = sorted(scored, key=lambda j: -(j.score or 0))[:5]
    if top:
        print("\nTop scores:")
        for j in top:
            print(f"  {j.score:3d}  {j.title[:50]:50s}  {j.company[:30]}")

    try:
        breakdown = ", ".join(f"{s}:{n}" for s, n in source_counts.most_common())
        tg_send(f"job_scan done: {total_seen} seen, {len(scored)} scored | {breakdown}")
    except Exception as e:
        log.warning(f"tg_send failed: {e}")


def main():
    ap = argparse.ArgumentParser(description="Scan + score LinkedIn job postings.")
    ap.add_argument("--max", type=int, default=None,
                    help="stop after N scored jobs")
    ap.add_argument("--pages", type=int, default=MAX_PAGES,
                    help=f"maximum result pages to walk (default {MAX_PAGES})")
    ap.add_argument("--jitter", type=int, default=30, metavar="MIN",
                    help="sleep a random 0..MIN minutes before starting "
                         "(default 30; pass 0 to run immediately).")
    ap.add_argument("--list", nargs="?", const="__default__", default=None,
                    metavar="PATH", dest="list_path",
                    help="write a score-descending CSV of every scored job. "
                         "PATH optional; defaults to data/job_scan_<timestamp>.csv.")
    args = ap.parse_args()

    export_csv: Path | None = None
    if args.list_path is not None:
        if args.list_path == "__default__":
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            export_csv = _ROOT / "data" / f"job_scan_{ts}.csv"
        else:
            export_csv = Path(args.list_path)

    if args.jitter > 0:
        import random, time
        delay = random.randint(0, args.jitter * 60)
        msg = f"[jitter] sleeping {delay}s ({delay/60:.1f} min) before scan start"
        print(msg)
        log.info(msg)
        time.sleep(delay)

    run(max_jobs=args.max, pages=args.pages, export_csv=export_csv)


if __name__ == "__main__":
    main()
