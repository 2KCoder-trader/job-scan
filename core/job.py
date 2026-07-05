"""The Job record produced by the scanner."""

from dataclasses import dataclass


@dataclass
class Job:
    id: str                          # LinkedIn job id
    title: str = ""
    company: str = ""
    li_url: str = ""                 # https://linkedin.com/jobs/view/<id>/
    source: str = "unknown"          # "linkedin_easy" | "workday" | "greenhouse" | ... | "custom"
    apply_url: str | None = None     # decoded external URL when source != linkedin_easy
    job_description: str = ""
    applicants: int | None = None    # None if unknown; small int otherwise
    score: int = 0                   # 0..100 from core.job_score.score()
    score_reason: str = ""           # one-line justification from the scorer
