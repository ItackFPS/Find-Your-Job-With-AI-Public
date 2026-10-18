"""Bridge between Phase 2 AI analysis and Phase 3 semantic matching.

Phase 2 extracts structured information from job descriptions (skills,
seniority, responsibilities, etc.). This module converts that structured
output into enriched text that Phase 3 can embed for better semantic
similarity scoring.

The original Job model is never modified — the enriched text is a
separate, additive artifact used only during the current pipeline run.
"""

from __future__ import annotations

from src.ai.models import JobUnderstandingResult
from src.models.job import Job


def build_enriched_job_text(job: Job, result: JobUnderstandingResult) -> str:
    """Build enriched text for Phase 3 from a job and its AI analysis.

    Combines the job's original title and description with AI-extracted
    structured fields (skills, seniority, responsibilities, domain).
    The richer text improves cosine similarity scoring in Phase 3 by
    making relevant skills and context more prominent in the embedding.

    Only called for jobs whose Phase 2 status is "success".
    """
    parts: list[str] = []

    if job.title:
        parts.append(job.title.lower().strip())
    if job.description:
        parts.append(job.description.lower().strip())

    analysis = result.analysis

    if analysis.seniority_level:
        parts.append(f"seniority: {analysis.seniority_level.lower()}")

    all_skills = (
        analysis.required_skills
        + analysis.preferred_skills
        + analysis.programming_languages
        + analysis.frameworks
        + analysis.tools
        + analysis.technologies
    )
    if all_skills:
        parts.append("skills: " + " ".join(s.lower() for s in all_skills))

    if analysis.responsibilities:
        parts.append(
            "responsibilities: " + " ".join(r.lower() for r in analysis.responsibilities)
        )

    if analysis.language_requirements:
        parts.append(
            "languages: " + " ".join(lang.lower() for lang in analysis.language_requirements)
        )

    if analysis.domain:
        parts.append(f"domain: {analysis.domain.lower()}")

    return " ".join(parts)


def build_enriched_texts(
    jobs: list[Job],
    ai_results: dict[str, JobUnderstandingResult],
) -> dict[str, str]:
    """Build enriched texts for all jobs that have successful AI analysis.

    Args:
        jobs: The full list of jobs to consider.
        ai_results: Map of job_id -> JobUnderstandingResult from Phase 2.

    Returns:
        Dict mapping job_id -> enriched text, only for jobs whose
        Phase 2 analysis has status "success". Jobs with failed or
        skipped analysis fall back to the standard title+description
        path in Phase 3.
    """
    enriched: dict[str, str] = {}
    for job in jobs:
        result = ai_results.get(job.id)
        if result is not None and result.status == "success":
            enriched[job.id] = build_enriched_job_text(job, result)
    return enriched
