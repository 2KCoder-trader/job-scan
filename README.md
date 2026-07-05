# job-scan

Scans LinkedIn job search results and scores every posting against your resume
with Claude, so you spend your time on the applications that are actually worth
it — not on reading 200 job descriptions a day.

For each posting it captures the title, company, applicant count, application
type (Easy Apply / Workday / Greenhouse / Ashby / ...), and a **0–100 fit
score** with a one-line reason, then writes a ranked CSV.

```
score,id,title,company,source,applicants,url,score_reason
88,4383219727,AI Engineer,Acme Corp,greenhouse,12,https://...,Resume shows shipped LLM-agent workflows matching the JD's core requirement.
72,4383220011,Data Analyst,Initech,linkedin_easy,45,https://...,Strong Python and SQL overlap; BI tooling adjacent rather than exact.
31,4383220458,Security Architect,Globex,workday,80,https://...,Role centers on threat modeling and CISSP, neither demonstrated.
```

## How it works

1. Opens your LinkedIn jobs search (`LI_JOBS_URL`) in a persistent Chromium
   profile via Playwright — the session stays logged in between runs.
2. Walks the result pages, lazy-scrolling the list so every card loads.
3. Clicks each card and reads the full job description, applicant count, and
   Apply-button target from the detail panel. External postings are followed
   in a background tab so the description is pulled from the company/ATS page
   itself — and a generic careers-page link gets resolved to the underlying
   ATS (Workday / Greenhouse / ...) for an accurate source label.
4. Sends the job description + your resume (`data/resume_profile.md`) to
   Claude Haiku, which returns a 0–100 fit score and a one-sentence reason.
   The resume rides in a cached system prompt, so scoring hundreds of jobs
   stays fast and cheap.
5. Prints every job as it goes and writes a score-descending CSV at the end.
   Optional Telegram ping with the run summary.

All interaction timing is drawn from gaussian distributions (`core/human.py`)
— keystrokes, scrolls, page dwell — so the scanner behaves like a person, not
a crawler.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium

cp .env.example .env                                 # fill in your values
cp data/resume_profile.example.md data/resume_profile.md   # paste your resume
```

Then log into LinkedIn once — the session persists in `.browser_profile/`:

```bash
python bin/signin.py
```

## Usage

```bash
python job_scan.py
```

That's it — no flags. It scans, scores, prints each job as it goes, and writes
the ranked CSV to `data/job_scan_<timestamp>.csv`.

Companies listed in `data/blacklist_companies.txt` (one per line) are skipped
without scoring.

## Project layout

```
job_scan.py                  # the scanner — scan, score, rank, export
core/
├── browser.py               # persistent Chromium profile location
├── human.py                 # gaussian-distributed human-like timing
├── job.py                   # the Job record
├── job_score.py             # Claude Haiku resume-vs-JD scorer
└── notifications.py         # optional Telegram pings
bin/signin.py                # one-time manual LinkedIn login
data/                        # resume, blacklist, CSV outputs (gitignored)
```

## Notes

- The scorer is skills-first by design: its rubric weighs demonstrated
  projects and shipped work over years-of-experience lines in the JD.
- If `ANTHROPIC_API_KEY` or the resume file is missing, the scan still runs —
  every job just scores 0 with a "scorer skipped" reason.
- Automating LinkedIn is against their terms of service; use at your own risk,
  keep volumes low, and keep the jitter on for scheduled runs.
