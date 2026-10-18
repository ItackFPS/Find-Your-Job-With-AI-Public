"""Prompt templates for Phase 2: Local AI Job Understanding."""

from __future__ import annotations

from typing import Any

PROMPT_VERSION = "job_understanding_v1"
PERSONALIZED_PROMPT_VERSION = "personalized_matching_v1"
ELIGIBILITY_PROMPT_VERSION = "eligibility_gate_v1"


def _format_profile_section(profile: dict[str, Any]) -> str:
    """Render a ``JobSearchProfile.model_dump()`` dict as a readable text block."""
    lines: list[str] = ["USER PROFILE:"]

    domains = profile.get("target_domains") or []
    if domains:
        lines.append(f"  Target domains: {', '.join(domains)}")

    skills = profile.get("target_skills") or []
    if skills:
        lines.append(f"  Target skills: {', '.join(skills)}")

    languages = profile.get("languages") or []
    if languages:
        lines.append(f"  Languages: {', '.join(languages)}")

    geo = profile.get("geography") or {}
    preferred = geo.get("preferred_countries") or []
    if preferred:
        lines.append(f"  Preferred countries: {', '.join(preferred)}")
    allowed = geo.get("allowed_regions") or []
    if allowed:
        lines.append(f"  Allowed regions: {', '.join(allowed)}")

    work_modes = profile.get("work_modes") or []
    if work_modes:
        lines.append(f"  Work modes: {', '.join(work_modes)}")

    contracts = profile.get("contract_types") or []
    if contracts:
        lines.append(f"  Contract types: {', '.join(contracts)}")

    exp = profile.get("experience") or {}
    levels = exp.get("preferred_levels") or []
    if levels:
        lines.append(f"  Experience levels: {', '.join(levels)}")
    max_years = exp.get("max_years")
    if max_years is not None:
        lines.append(f"  Max years of experience: {max_years}")
    excluded = exp.get("excluded_seniority") or []
    if excluded:
        lines.append(f"  Excluded seniority: {', '.join(excluded)}")

    return "\n".join(lines)


def build_job_understanding_prompt(
    job_description: str,
    job_title: str | None = None,
    job_company: str | None = None,
) -> str:
    """Build the system prompt for job understanding.

    The prompt is deterministic and versioned so it can be tracked and
    updated independently of the model itself.
    """
    return f"""<|system|>
You are a local AI assistant that analyzes job descriptions to extract structured information.

INSTRUCTIONS:
- Analyze ONLY the supplied job description. Do not use external knowledge or invent information.
- Distinguish clearly between REQUIRED skills and PREFERRED/nice-to-have skills.
- Extract explicit experience requirements (years, level). If not stated, return null.
- Identify seniority only when the description explicitly supports it (e.g., "senior", "junior", "entry-level").
- Return ONLY valid JSON matching the schema below. Do not include any text outside the JSON.

SCHEMA:
{{
  "seniority_level": null | string,
  "required_skills": ["skill1", "skill2"],
  "preferred_skills": ["skill1", "skill2"],
  "programming_languages": ["python", "javascript"],
  "frameworks": ["react", "django"],
  "tools": ["git", "docker"],
  "technologies": ["aws", "kubernetes"],
  "language_requirements": ["english", "french"],
  "responsibilities": ["responsibility1", "responsibility2"],
  "domain": null | string,
  "confidence": number (0.0-1.0)
}}

<|user|>
Job Title: {job_title or "(not provided)"}
Company: {job_company or "(not provided)"}
Description: {job_description}

Return ONLY valid JSON matching the schema above. Do not include any text outside the JSON.
"""


def build_prompt_v2(
    job_description: str,
    job_title: str | None = None,
    job_company: str | None = None,
) -> str:
    """Build an alternative prompt variant (v2).

    Use this when you want to experiment with different prompting strategies.
    This variant is not currently wired into the pipeline — callers must invoke
    it explicitly, since PROMPT_VERSION/prompt_version only tag results for
    tracking and do not switch which builder function runs.
    """
    return f"""<|system|>
You are a local AI assistant that analyzes job descriptions to extract structured information.

INSTRUCTIONS:
- Analyze ONLY the supplied job description. Do not use external knowledge or invent information.
- Distinguish clearly between REQUIRED skills and PREFERRED/nice-to-have skills.
- Extract explicit experience requirements (years, level). If not stated, return null.
- Identify seniority only when the description explicitly supports it (e.g., "senior", "junior", "entry-level").
- Return ONLY valid JSON matching the schema below. Do not include any text outside the JSON.

SCHEMA:
{{
  "seniority_level": null | string,
  "required_skills": ["skill1", "skill2"],
  "preferred_skills": ["skill1", "skill2"],
  "programming_languages": ["python", "javascript"],
  "frameworks": ["react", "django"],
  "tools": ["git", "docker"],
  "technologies": ["aws", "kubernetes"],
  "language_requirements": ["english", "french"],
  "responsibilities": ["responsibility1", "responsibility2"],
  "domain": null | string,
  "confidence": number (0.0-1.0)
}}

<|user|>
Job Title: {job_title or "(not provided)"}
Company: {job_company or "(not provided)"}
Description: {job_description}

Return ONLY valid JSON matching the schema above. Do not include any text outside the JSON.
"""


def build_personalized_matching_prompt(
    job_description: str,
    profile: "dict[str, Any]",
    job_title: "str | None" = None,
    job_company: "str | None" = None,
) -> str:
    """Build a prompt for personalized job matching (``personalized_matching_v1``).

    This differs from ``build_job_understanding_prompt`` only in that it also
    receives the user profile as context for the extraction — the output
    schema is the same factual-extraction schema as the base prompt. Profile-
    fit scoring (``profile_match_score``/``profile_match_reasons``/
    ``profile_mismatch_reasons``) was removed from LLM generation in the
    2026-09-04 personalization audit: fit is already computed twice
    downstream (the deterministic scorer's profile-aware factors in
    ``src/pipeline/scoring.py``, and Phase 3's profile-embedding cosine
    similarity in ``src/pipeline/semantic_matching.py``), and human-readable
    "why this job" reasons already ship via the deterministic, API-wired
    ``src/pipeline/explain.py``. A third, LLM-judged fit signal would have
    duplicated both without composing cleanly into the existing scoring
    formula — see that audit for the full analysis.

    The deterministic pipeline has already run — this model must NOT repeat or
    second-guess those hard-constraint decisions.

    The response is consumed from normal choices[0].message.content JSON.
    """
    profile_section = _format_profile_section(profile)

    return (
        "<|system|>\n"
        "You are a local AI assistant that analyzes job descriptions to extract structured information.\n"
        "\n"
        "IMPORTANT:\n"
        "- Hard constraints (location, seniority, freshness) have already been evaluated by\n"
        "  the deterministic pipeline. Do not repeat or override those decisions.\n"
        "- Use the user profile below only as context to disambiguate the job description;\n"
        "  do not evaluate or score the candidate's fit — only extract facts about the job.\n"
        "- Use ONLY the information provided in the user profile and the job below.\n"
        "- Do not invent candidate qualifications or job requirements.\n"
        "- Return ONLY valid JSON matching the schema. No text outside the JSON.\n"
        "\n"
        "SCHEMA:\n"
        "{\n"
        '  "seniority_level": null,\n'
        '  "required_skills": [],\n'
        '  "preferred_skills": [],\n'
        '  "programming_languages": [],\n'
        '  "frameworks": [],\n'
        '  "tools": [],\n'
        '  "technologies": [],\n'
        '  "language_requirements": [],\n'
        '  "responsibilities": [],\n'
        '  "domain": null,\n'
        '  "confidence": 0.0\n'
        "}\n"
        "\n"
        "<|user|>\n"
        + profile_section
        + "\n\nJOB:\n"
        + "Title: " + (job_title or "(not provided)") + "\n"
        + "Company: " + (job_company or "(not provided)") + "\n"
        + "Description: " + job_description + "\n"
        + "\nReturn ONLY valid JSON matching the schema above. No text outside the JSON.\n"
    )


def _format_compact_profile_line(profile: dict[str, Any]) -> str:
    """One line of profile context for the eligibility gate — not a YAML dump.

    Only the dimensions the eligibility gate actually reasons about
    (education/seniority/experience level compatibility) belong here; skills
    and domain matching are handled elsewhere (deterministic filter, Phase 2 /
    embeddings) and are deliberately NOT included, so this gate can't
    duplicate or second-guess that work.
    """
    parts: list[str] = []
    max_education = profile.get("max_education")
    if max_education:
        parts.append(f"max education {max_education}")
    exp = profile.get("experience") or {}
    max_years = exp.get("max_years")
    if max_years is not None:
        parts.append(f"max {max_years} years experience")
    levels = exp.get("preferred_levels") or []
    if levels:
        parts.append("prefers " + "/".join(lvl.replace("_", " ") for lvl in levels))
    return "; ".join(parts) if parts else "no education/experience constraints stated"


_ELIGIBILITY_SYSTEM_PROMPT = """<|system|>
You are a strict, narrow classifier used inside a job-matching pipeline. Your ONLY job: \
decide whether the ROLE LEVEL of a job posting (education requirement and seniority/\
experience level) is compatible with a candidate profile — nothing else.

Do NOT judge skills, domain, location, contract type, or general fit — those are handled \
elsewhere. Judge role level ONLY.

Reason about IMPLICIT signals, not just explicit statements: a job title can \
conventionally imply an education or seniority level even when the posting never states \
it directly (e.g. "Ingenieur"-titled roles conventionally expect an engineering degree; \
"Senior/Lead/Staff/Principal/Architect/Manager" titles conventionally expect more \
experience than an entry-level candidate has). A title-implied level is only a WEAK \
DEFAULT, not a verdict — treat it as the starting assumption you are actively looking \
for evidence to overturn.

The posting's own context ALWAYS wins over a title-level assumption, with no exception: \
explicit lower-level acceptance, internship/alternance/apprenticeship framing, or a \
junior/entry-level focus in the actual responsibilities all mean the role IS accessible \
even if the title alone would suggest otherwise — even when the title itself sounds \
senior. A title can be a template, a typo, or aspirational; the actual role description \
is what the candidate would actually be doing, and is always the deciding evidence. When \
the evidence lists BOTH a senior/high-level signal (from the title) AND an explicit \
internship/alternance/junior/lower-level signal (from real context), the explicit \
context signal wins — do not average them or default to the title.

Example: title "Senior Backend Engineer", but the evidence notes the posting explicitly \
describes a summer internship open to Bac+2/Bac+3 students with no professional \
experience required → PASS (the internship framing overrides the title), NOT REJECT.

Do not apply a fixed list of "risky" title words — reason freely from the evidence given \
for THIS job.

Output ONLY one line of compact JSON, nothing else:
{"decision":"PASS|REJECT|UNCERTAIN","confidence":0.0-1.0,"reason":"short_snake_case_reason"}

Rules:
- REJECT only when you are confident (>=0.75) the role level is incompatible AND no
  explicit lower-level/internship/alternance context overrides the title.
- If the evidence is thin, genuinely mixed with no clear override, or you are not sure:
  output UNCERTAIN, not REJECT.
- PASS when the role level is compatible, an explicit lower-level context overrides an
  implied higher title, or no role-level signal exists at all.
"""


def build_eligibility_prompt(
    job_title: str,
    evidence: str,
    profile: dict[str, Any],
) -> str:
    """Build the compact prompt for the LLM semantic eligibility gate.

    ``evidence`` is pre-assembled by the caller (``src/ai/eligibility.py``)
    from the job's already-extracted deterministic requirements (education/
    experience spans, seniority-indicator hits, internship/alternance
    context) — this function only templates it, it never re-reads the full
    job description itself. Target size: a few hundred tokens, not the
    full posting.
    """
    profile_line = _format_compact_profile_line(profile)
    return (
        _ELIGIBILITY_SYSTEM_PROMPT
        + "\n<|user|>\n"
        + f"CANDIDATE PROFILE\n{profile_line}\n\n"
        + f"JOB TITLE\n{job_title or '(not provided)'}\n\n"
        + f"EVIDENCE\n{evidence}\n\n"
        + "Return ONLY the JSON object described above."
    )
