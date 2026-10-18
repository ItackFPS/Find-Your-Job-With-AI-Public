"""Ground truth labels for the 50-job AI-filter benchmark.

STRICT SEPARATION: this file is the ONLY place expected verdicts live. It is
never imported by, or passed into, the production filter/gate/LLM code path —
see ``scripts/benchmark_ai_filter_50.py`` for the enforced separation (jobs
are fed to the pipeline as plain ``Job`` objects built from ``jobs.py`` only;
this file is joined in a separate step, after inference is complete).

Every label was assigned by hand, by reading the profile (now pinned as
``profile.yaml`` next to this file) and the
CURRENT implementation of ``src/pipeline/filter.py`` and
``src/requirements/{gate,education,principal_skill,experience}.py`` — never
by asking the AI/LLM being evaluated to grade its own benchmark.

``expected`` is one of:
    "SHOULD_ACCEPT"  — the job should pass all filtering stages
    "SHOULD_REJECT"  — the job should be rejected by at least one stage
    "BORDERLINE"     — a reasonable outcome could go either way; not
                       scored as a hard pass/fail in accuracy tallies,
                       reported separately.
"""

from __future__ import annotations

GROUND_TRUTH: dict[str, dict] = {

    # ---------------- 1-10: perfect matches ----------------
    "bm01": {
        "category": "perfect_match",
        "expected": "SHOULD_ACCEPT",
        "reason": "Junior Python developer, Bac+2, France, exact skill/domain overlap.",
    },
    "bm02": {
        "category": "perfect_match",
        "expected": "SHOULD_ACCEPT",
        "reason": "Junior web developer (JS/HTML/CSS/Git), remote France, Bac+2, entry-level.",
    },
    "bm03": {
        "category": "perfect_match",
        "expected": "SHOULD_ACCEPT",
        "reason": "QA automation alternance using Python/Git — genuine fit; mentions Bac+3 "
                  "alongside Bac+2 so a minor soft education penalty may legitimately apply, "
                  "but the job must not be hard-rejected.",
    },
    "bm04": {
        "category": "perfect_match",
        "expected": "SHOULD_ACCEPT",
        "reason": "Automation developer role using n8n/Python/Bash — direct target_domain and "
                  "target_skill overlap, Bac+2, junior.",
    },
    "bm05": {
        "category": "perfect_match",
        "expected": "SHOULD_ACCEPT",
        "reason": "Junior data analyst using Python/MySQL (both target skills), Bac+2, France.",
    },
    "bm06": {
        "category": "perfect_match",
        "expected": "SHOULD_ACCEPT",
        "reason": "IT support technician — direct it_support domain match, Bac+2, junior, France.",
    },
    "bm07": {
        "category": "perfect_match",
        "expected": "SHOULD_ACCEPT",
        "reason": "Junior Node.js/JavaScript backend developer, remote France/EU, Bac+2.",
    },
    "bm08": {
        "category": "perfect_match",
        "expected": "SHOULD_ACCEPT",
        "reason": "Cybersecurity internship — direct cybersecurity domain match, no education "
                  "or experience violation, France.",
    },
    "bm09": {
        "category": "perfect_match",
        "expected": "SHOULD_ACCEPT",
        "reason": "Junior network technician — réseaux domain match, Bac+2, France.",
    },
    "bm10": {
        "category": "perfect_match",
        "expected": "SHOULD_ACCEPT",
        "reason": "Junior PHP/Symfony developer — PHP is a target skill, Symfony is in the PHP "
                  "family, Bac+2, France.",
    },

    # ---------------- 11-20: partial / borderline matches ----------------
    "bm11": {
        "category": "partial_match",
        "expected": "SHOULD_ACCEPT",
        "reason": "IT support role with a real (if secondary) scripting component; matches "
                  "it_support domain, no hard-constraint violation.",
    },
    "bm12": {
        "category": "partial_match",
        "expected": "BORDERLINE",
        "reason": "Primarily a BI/reporting role; Python use is explicitly described as "
                  "occasional. Passes the keyword filter on MySQL/Python but the actual job "
                  "is business analysis more than software engineering — reasonable graders "
                  "could accept or deprioritize it.",
    },
    "bm13": {
        "category": "partial_match",
        "expected": "SHOULD_ACCEPT",
        "reason": "Manual QA role transitioning toward Python automation; genuine (if partial) "
                  "skill overlap, no hard-constraint violation.",
    },
    "bm14": {
        "category": "partial_match",
        "expected": "SHOULD_ACCEPT",
        "reason": "Junior DevOps alternance explicitly built on Linux/Docker/Bash, all target "
                  "skills; framed as an entry point for a junior profile.",
    },
    "bm15": {
        "category": "partial_match",
        "expected": "SHOULD_REJECT",
        "reason": "Title-centered on Ruby/Ruby on Rails, which has zero overlap with the "
                  "candidate's Python/JavaScript/PHP skills and is not in any language family "
                  "the candidate knows; description explicitly says no onboarding time is "
                  "available for someone without prior Ruby knowledge. Correctly caught by the "
                  "principal-skill gate.",
    },
    "bm16": {
        "category": "partial_match",
        "expected": "SHOULD_ACCEPT",
        "reason": "Junior network engineering role — réseaux domain match; programming is "
                  "explicitly secondary but present (Bash), no hard-constraint violation.",
    },
    "bm17": {
        "category": "partial_match",
        "expected": "SHOULD_ACCEPT",
        "reason": "Cybersecurity-adjacent role (awareness/audit rather than deep pentesting) — "
                  "still matches the cybersecurity domain, junior, no violation.",
    },
    "bm18": {
        "category": "partial_match",
        "expected": "SHOULD_ACCEPT",
        "reason": "PHP/Symfony maintenance work — narrower in scope (bug-fixing vs. new "
                  "features) but a genuine skill match, Bac+2, junior.",
    },
    "bm19": {
        "category": "partial_match",
        "expected": "SHOULD_ACCEPT",
        "reason": "IT support role, minimal programming — matches it_support domain directly, "
                  "no hard-constraint violation.",
    },
    "bm20": {
        "category": "partial_match",
        "expected": "SHOULD_ACCEPT",
        "reason": "Junior data engineering role built on Python/MySQL/Bash (all target "
                  "skills) despite 'data' not being a selected target_domain by name.",
    },

    # ---------------- 21-30: clearly irrelevant occupations ----------------
    "bm21": {
        "category": "irrelevant",
        "expected": "SHOULD_REJECT",
        "reason": "Accounting role, no IT content whatsoever.",
    },
    "bm22": {
        "category": "irrelevant",
        "expected": "SHOULD_REJECT",
        "reason": "B2B sales representative, no IT content.",
    },
    "bm23": {
        "category": "irrelevant",
        "expected": "SHOULD_REJECT",
        "reason": "IT recruiter — sources/interviews candidates but performs no IT work "
                  "itself; the word 'IT' in the title describes the recruiting specialty, not "
                  "the candidate's own job function.",
    },
    "bm24": {
        "category": "irrelevant",
        "expected": "SHOULD_REJECT",
        "reason": "Brand/marketing management role, no IT content, also senior/manager-level.",
    },
    "bm25": {
        "category": "irrelevant",
        "expected": "SHOULD_REJECT",
        "reason": "Generalist volume recruiter, no IT content or specialization.",
    },
    "bm26": {
        "category": "irrelevant",
        "expected": "SHOULD_REJECT",
        "reason": "Graphic design role (Adobe Creative Suite), no overlap with target skills.",
    },
    "bm27": {
        "category": "irrelevant",
        "expected": "SHOULD_REJECT",
        "reason": "Registered nurse role, no IT content.",
    },
    "bm28": {
        "category": "irrelevant",
        "expected": "SHOULD_REJECT",
        "reason": "Corporate lawyer role, no IT content.",
    },
    "bm29": {
        "category": "irrelevant",
        "expected": "SHOULD_REJECT",
        "reason": "Administrative assistant role (Pack Office), no IT/dev content.",
    },
    "bm30": {
        "category": "irrelevant",
        "expected": "SHOULD_REJECT",
        "reason": "Senior enterprise sales/account management role, no IT content, also "
                  "manager-level.",
    },

    # ---------------- 31-40: keyword traps ----------------
    "bm31": {
        "category": "keyword_trap",
        "expected": "SHOULD_REJECT",
        "reason": "Primarily a pre-sales/sales role despite Python and JavaScript keywords in "
                  "the description; the candidate would not write production code.",
    },
    "bm32": {
        "category": "keyword_trap",
        "expected": "SHOULD_REJECT",
        "reason": "Project management role overseeing Python/JavaScript developers; the "
                  "candidate would manage, not build. Title contains 'chef de projet', which "
                  "is itself a manager-level exclusion.",
    },
    "bm33": {
        "category": "keyword_trap",
        "expected": "SHOULD_REJECT",
        "reason": "Technical Product Manager role — reads code to write specs, does not write "
                  "production code; also a 'manager' title, seniority-excluded.",
    },
    "bm34": {
        "category": "keyword_trap",
        "expected": "SHOULD_REJECT",
        "reason": "Recruiter sourcing Python/DevOps candidates; the candidate performs "
                  "recruiting work, not engineering work.",
    },
    "bm35": {
        "category": "keyword_trap",
        "expected": "SHOULD_REJECT",
        "reason": "Customer Success Manager for a Python-based SaaS platform — relationship "
                  "management, not engineering; also a 'manager' title.",
    },
    "bm36": {
        "category": "keyword_trap",
        "expected": "SHOULD_REJECT",
        "reason": "Business/process analyst role; SQL and Python are explicitly described as "
                  "producing occasional reporting deliverables, not the core of the job.",
    },
    "bm37": {
        "category": "keyword_trap",
        "expected": "SHOULD_REJECT",
        "reason": "Technical writer producing API documentation — writes prose about APIs, "
                  "explicitly does not write or ship production code.",
    },
    "bm38": {
        "category": "keyword_trap",
        "expected": "SHOULD_REJECT",
        "reason": "Enterprise software sales/account management role; no hands-on technical "
                  "work; also a 'manager' title.",
    },
    "bm39": {
        "category": "keyword_trap",
        "expected": "SHOULD_REJECT",
        "reason": "Sales engineering role selling cybersecurity software, reporting to the VP "
                  "of Sales — not a security engineering role, despite the word "
                  "'cybersecurity' in the title.",
    },
    "bm40": {
        "category": "keyword_trap",
        "expected": "SHOULD_REJECT",
        "reason": "Pre-sales solutions architect producing commercial proposals for cloud "
                  "migrations — not a hands-on cloud engineering role.",
    },

    # ---------------- 41-50: hard-constraint traps ----------------
    "bm41": {
        "category": "hard_constraint_trap",
        "expected": "SHOULD_REJECT",
        "reason": "Bac+5 stated as a strict, non-negotiable mandatory requirement ('minimum "
                  "obligatoire... sans exception possible'), exceeding the candidate's Bac+2 "
                  "— otherwise a perfect technical fit. Must be hard-rejected on education.",
    },
    "bm42": {
        "category": "hard_constraint_trap",
        "expected": "SHOULD_REJECT",
        "reason": "Senior-level title and 7 years of required experience, both exceeding the "
                  "candidate's junior/≤3-years profile — otherwise a perfect skill match. Must "
                  "be rejected on seniority/experience.",
    },
    "bm43": {
        "category": "hard_constraint_trap",
        "expected": "SHOULD_REJECT",
        "reason": "Explicit '5+ years required' exceeds the candidate's 3-year experience cap "
                  "— otherwise a perfect skill/education match. Must be rejected on experience.",
    },
    "bm44": {
        "category": "hard_constraint_trap",
        "expected": "SHOULD_REJECT",
        "reason": "On-site position in Morocco with no remote option — outside France/Europe, "
                  "physical presence required. Must be rejected on location.",
    },
    "bm45": {
        "category": "hard_constraint_trap",
        "expected": "SHOULD_REJECT",
        "reason": "Remote position explicitly restricted to US-based/authorized applicants — "
                  "not eligible from France. Must be rejected on location/remote-scope.",
    },
    "bm46": {
        "category": "hard_constraint_trap",
        "expected": "SHOULD_ACCEPT",
        "reason": "Bac+5 explicitly described as 'apprécié... absolument pas un critère "
                  "éliminatoire' (preferred, not eliminatory) — must NOT be hard-rejected; a "
                  "soft score penalty is acceptable, rejection is not.",
    },
    "bm47": {
        "category": "hard_constraint_trap",
        "expected": "SHOULD_ACCEPT",
        "reason": "Bac+5 'ou expérience équivalente' is an explicit OR-alternative — the "
                  "candidate may satisfy the alternative branch. Must NOT be hard-rejected or "
                  "score-penalized for education.",
    },
    "bm48": {
        "category": "hard_constraint_trap",
        "expected": "SHOULD_ACCEPT",
        "reason": "Bac+5 is listed as a bare bullet point with no mandatory/preferred "
                  "qualifier word anywhere nearby — importance is genuinely UNKNOWN under the "
                  "current rules and must default to a soft penalty at most, never a hard "
                  "rejection.",
    },
    "bm49": {
        "category": "hard_constraint_trap",
        "expected": "SHOULD_ACCEPT",
        "reason": "Alternance requiring 'Bac+2 minimum validé' — exactly equal to the "
                  "candidate's max education (Bac+2). Must not be rejected or penalized merely "
                  "because the word 'minimum' appears; this tests that appropriate "
                  "internships/alternances are not wrongly rejected on education wording.",
    },
    "bm50": {
        "category": "hard_constraint_trap",
        "expected": "SHOULD_REJECT",
        "reason": "Generic 'Ingénieur Logiciel' title (no red flag), but the description "
                  "reveals the role is centered on Rust ('notre stack repose principalement "
                  "sur Rust', 'la maîtrise de Rust est indispensable') — a technology with no "
                  "overlap with the candidate's profile. Must be caught by the description-"
                  "based mandatory+principal skill gate despite the innocuous title.",
    },
}
