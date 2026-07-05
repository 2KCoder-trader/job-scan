"""LLM scorer: job_description × resume → (0..100, reason).

Uses Claude Haiku with the applicant's resume (data/resume_profile.md) as a
cached system context. The model returns one integer 0..100 plus a one-sentence
justification. If the API key or resume is missing, falls back to (0, "...").
"""

import os
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
RESUME_FILE = _ROOT / "data" / "resume_profile.md"

_resume_cache: str | None = None
_client = None

SYSTEM_PROMPT = """\
You are a pragmatic job-fit evaluator for an early-career applicant.

# Output format (STRICT)

Reply with EXACTLY two lines, nothing else. No preamble. No markdown. No quotes.

SCORE: <integer 0-100>
REASON: <one declarative sentence, at most 30 words, naming the single strongest signal>

Examples of correct replies:

SCORE: 82
REASON: Resume shows shipped LLM-agent workflows and Cloud Run deploys that match the JD's core requirement.

SCORE: 25
REASON: Role demands 10+ years infosec and CISSP, neither of which the resume demonstrates.

# Scoring rubric (skills-first; years are weak signal)

- 80-100: required skills clearly demonstrated in the resume — projects, internships, or shipped work that map directly to what the role does.
- 60-80: solid skill overlap; most required tech/tools present; concrete adjacent work exists even if the exact stack differs.
- 40-60: partial skill match; some required skills missing but the applicant is plausibly trainable on them given existing technical foundation.
- 0-40: wrong domain (e.g. backend role for a designer applicant) OR the role's CORE skill is entirely absent from the resume with no adjacent evidence.

# Evidence weighting

- Score by SKILLS and SHIPPED WORK, not by calendar years.
- One serious internship + concrete projects counts as real experience.
- A "5+ years required" line is a JD wish, not a gate. Score the SKILLS the role needs. If the skills are there, the calendar gap is a minor deduction (5-10 points), not a 40-point penalty.
- A new-grad applicant with strong portfolio + relevant internships can legitimately score 70-85 for senior-titled roles when the skills are demonstrated.
- Penalize hard ONLY for: missing the role's central skill with no adjacent evidence (no ML for an ML-engineer role, no security background for a senior security role, no Rust for a Rust-mandated role).

# Anti-patterns to avoid

- DO NOT cite "lacks X years" as the headline reason when skills are present. That is the lazy answer.
- DO NOT score below 60 just because the resume is early-career; score on skill match.
- DO cite the specific missing skill if you score below 60.

# Examples of correct calibration

SCORE: 78
REASON: Resume shows shipped LLM/agent projects and Python tooling that map directly to the AI Analyst skill requirements.

SCORE: 35
REASON: Senior security architect role centers on threat modeling and CISSP, neither demonstrated in the resume.

# Hard rules

- Output is exactly two lines: a SCORE line and a REASON line. No third line.
- REASON is one sentence. No semicolons chaining clauses. No em-dashes splicing clauses. Pick the single most important factor.
- Do not address the applicant. Do not say "the applicant" or "the candidate" more than once.
- Do not restate the job title or company in the REASON.
"""


def _resume_text() -> str:
    global _resume_cache
    if _resume_cache is None:
        _resume_cache = RESUME_FILE.read_text(encoding="utf-8") if RESUME_FILE.exists() else ""
    return _resume_cache


def _llm():
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            try:
                import anthropic
                _client = anthropic.Anthropic(api_key=api_key)
            except ImportError:
                pass
    return _client


def score(job_description: str, title: str = "") -> tuple[int, str]:
    """Return (0..100, reason) indicating fit between resume and the posting."""
    jd = (job_description or "").strip()
    if not jd:
        return 0, "no job description available"

    client = _llm()
    resume = _resume_text()
    if not client:
        return 0, "scorer skipped — ANTHROPIC_API_KEY missing or `pip install anthropic` not run"
    if not resume.strip():
        return 0, "scorer skipped — data/resume_profile.md is empty"

    # Cap JD length to keep tokens reasonable (~12k chars ≈ 3k tokens).
    if len(jd) > 12000:
        jd = jd[:12000] + "\n…[truncated]"

    user_msg = (
        f"Job title: {title or '(unknown)'}\n\n"
        f"Job description:\n{jd}\n\n"
        "Reply now in the exact SCORE/REASON format."
    )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=[
                {"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": f"Applicant resume:\n\n{resume}",
                 "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": user_msg}],
        )
        text = response.content[0].text.strip()
    except Exception as e:
        return 0, f"scorer error: {e}"

    m_score  = re.search(r"SCORE:\s*(\d{1,3})", text, re.I)
    m_reason = re.search(r"REASON:\s*(.+)", text, re.I)
    if not m_score:
        return 0, f"scorer returned malformed reply: {text[:120]}"
    val = max(0, min(100, int(m_score.group(1))))
    reason = m_reason.group(1).strip() if m_reason else "(no reason given)"
    return val, reason
