"""Structured requirement extraction from job postings.

Provides a fast heuristic extractor (always available) and an optional
LLM-based enrichment pass (requires a running LM Studio/Ollama server,
enabled via ``matching.llm_extraction_enabled`` in profile.yaml — OFF by
default; see ``build_requirements_llm_provider``).

The heuristic extractor handles:
  - Education level + importance (regex patterns for French & English)
  - Principal skill detection from job title
  - Minimum experience (delegated to ``experience.py`` — deterministic-only
    by design, see ``ExperienceRequirement``'s docstring)

The LLM enrichment (when enabled) additionally extracts:
  - Mandatory vs preferred classification for skills mentioned in description
  - Centrality signals from natural language ("principalement", "mainly", etc.)
  - Principal-technology judgment for roles the title alone doesn't reveal

Evidence-aware reconciliation (the corroboration step)
--------------------------------------------------------
The LLM is a language-understanding aid, not a source of new evidence. Every
claim the LLM returns is checked against the actual job title/description
before it is allowed to matter for a hard-constraint decision:

  - If the claim has a literal match in the text → ``EvidenceSource.LLM_CORROBORATED``,
    with a real ``EvidenceSpan`` attached. This is "LLM interpretation of
    evidence that exists" — e.g. reading "you'll be comfortable shipping
    production Rust services" and correctly identifying Rust as central, or
    reading indirect phrasing and correctly classifying it as mandatory.
  - If no textual anchor is found → ``EvidenceSource.LLM_UNCORROBORATED``. The
    claim is retained (for audit/observability) but ``HardConstraintGate``
    treats it as UNKNOWN and it can never independently cause a VIOLATED
    state or a score penalty — see ``gate.py``'s source-aware checks.

``_merge()`` additionally enforces precedence: deterministic (heuristic)
evidence is never overwritten by the LLM. The LLM may only fill a gap the
heuristic left empty, or clarify importance for the *same* level/skill the
heuristic already found — and only when corroborated. See ``_merge()`` for
the exact rule.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from src.models.job import Job
from src.requirements.education import extract_education, find_level_evidence, parse_education_level
from src.requirements.experience import extract_experience
from src.requirements.llm_corroboration import evaluate_llm_tech_claim
from src.requirements.models import (
    EducationRequirement,
    EvidenceSource,
    Importance,
    JobRequirements,
    SkillRequirement,
)
from src.requirements.principal_skill import (
    _CORE_LANGUAGES,
    _user_knows_tech,
    _normalize_user_skills,
    _norm as _norm_skill,
    detect_missing_principal_skills_with_or,
)

if TYPE_CHECKING:
    # Type-only: the provider is constructed lazily in
    # build_requirements_llm_provider().
    from src.ai.interfaces import LLMProvider

logger = logging.getLogger(__name__)

# Text window used to corroborate LLM claims against the actual job posting.
# Matches the window the heuristic extractor itself scans for education, so
# corroboration is never stricter than what the deterministic path already sees.
_CORROBORATION_WINDOW = 5000

# ---------------------------------------------------------------------------
# Mandatory skill signals in description
# Used to detect when the description explicitly flags a skill as required.
# ---------------------------------------------------------------------------

# Patterns that, when appearing in close proximity to a skill mention,
# indicate the skill is MANDATORY.
_DESC_MANDATORY_PHRASES = [
    re.compile(r"\bimpératif\b", re.I),
    re.compile(r"\bimperatif\b", re.I),
    re.compile(r"\bobligatoire\b", re.I),
    re.compile(r"\bindispensable\b", re.I),
    re.compile(r"\bexig[eé]\b", re.I),
    re.compile(r"\bnécessaire\b", re.I),
    re.compile(r"\bnecessaire\b", re.I),
    re.compile(r"\bmandatory\b", re.I),
    re.compile(r"\bessen(?:tiel|tially?)\b", re.I),
    re.compile(r"\brequis\b", re.I),
    re.compile(r"\brequired\b", re.I),
    re.compile(r"\bmust\s+have\b", re.I),
    re.compile(r"\bmust[-\s]have\b", re.I),
]

# Patterns indicating the skill is PREFERRED / optional.
_DESC_PREFERRED_PHRASES = [
    re.compile(r"\bsouhait[eé][e]?\b", re.I),
    re.compile(r"\bid[eé]alement\b", re.I),
    re.compile(r"\bde\s+pr[eé]f[eé]rence\b", re.I),
    re.compile(r"\bappr[eé]ci[eé]\b", re.I),
    re.compile(r"\bun\s+plus\b", re.I),
    re.compile(r"\ba\s+plus\b", re.I),
    re.compile(r"\bnice\s+to\s+have\b", re.I),
    re.compile(r"\bpreferred\b", re.I),
    re.compile(r"\bideally\b", re.I),
    re.compile(r"\bdesirable\b", re.I),
    re.compile(r"\bserait\s+un\s+atout\b", re.I),
    re.compile(r"\bun\s+atout\b", re.I),
    re.compile(r"\bbienvenu[e]?\b", re.I),
    re.compile(r"\bbonus\b", re.I),
    re.compile(r"\boptionnel\b", re.I),
]


# ---------------------------------------------------------------------------
# LLM claim safety guards
#
# The LLM extraction step (_extract_llm/_parse_llm_response) is a language-
# understanding aid layered on top of deterministic evidence, not a source of
# new hard-rejection authority — see the module docstring. The 2026-09-05
# real-job benchmark (scripts/benchmark_real_jobs_50.py) found three narrow,
# nameable mechanisms by which the LLM's claims manufactured false hard
# rejections despite the existing corroboration check (the claim DID have a
# literal textual match, so LLM_CORROBORATED alone wasn't a sufficient guard):
#
# 1. Domain-vocabulary-as-unknown-tech: the model flags the candidate's own
#    target-domain words ("système", "réseau", "pentest", "sécurité réseau",
#    "antivirus", "SIEM", "XDR") as an unknown "principal technology" simply
#    because they are technical-sounding nouns present in the text.
# 2. Generic-term hallucination: the model names a generic role/duty word
#    ("front-end", "back-end", "bases de données") as if it were a specific
#    named technology, in direct violation of its own prompt instructions.
# 3. Inclusive-list collapse: a posting offers "plusieurs technologies parmi
#    Java/PHP/JavaScript/Python/Node/..." (candidate needs ONE of several) and
#    the model picks a single item as the one mandatory requirement.
#
# Each guard is deterministic and evidence-based (never a second LLM call),
# applied in _parse_llm_response before a claim is allowed to become a
# principal_skill_mismatches/SkillRequirement entry — see
# ``src.requirements.llm_corroboration.evaluate_llm_tech_claim``, the single
# shared implementation both _build_llm_skill and the principal-technologies
# loop below call.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# LLM requirement extraction prompt
# ---------------------------------------------------------------------------

_LLM_EXTRACTION_PROMPT = """\
You are a job requirement extraction assistant. Extract structured requirements from the following job posting.

Job title: {title}
Job description (excerpt):
{description}

Return ONLY a valid JSON object with this exact schema (no extra text, no markdown):
{{
  "education": {{
    "level": "bac+2" | "bac+3" | "bac+5" | "phd" | null,
    "importance": "mandatory" | "preferred" | "unknown"
  }},
  "required_skills": ["skill1", "skill2"],
  "preferred_skills": ["skill3", "skill4"],
  "is_principal_tech_role": true | false,
  "principal_technologies": ["tech1"]
}}

Rules:
- education.level uses French Bac+N notation. Use null if no education level is mentioned.
- education.importance is "mandatory" when the job says "minimum Bac+5", "Bac+5 requis", etc.
- education.importance is "preferred" when the job says "Bac+5 souhaité", "idéalement Bac+5", etc.
- required_skills and preferred_skills list ONLY named, recognizable technologies: programming
  languages, frameworks, libraries, databases, cloud platforms, infrastructure/networking/DevOps
  tools, software platforms, technical protocols, technical standards, or technical certifications.
  A technology does NOT need to be a famous or common one — naming a specific tool/product/language/
  standard is what matters, not whether it is widely known.
  NEVER list an ordinary job duty, task, or soft skill, even if it is stated emphatically or worded
  like a requirement. If nothing in the text names an actual technology, return an empty list.
  GOOD examples (named technologies — the kind of thing that belongs in these lists):
    "Python", "Rust", "React", "Kubernetes", "PostgreSQL", "Terraform", "C++", "SAP", "UiPath",
    "Camunda", "SCADA", "Cisco IOS"
  BAD examples (duties/tasks/soft skills — NEVER list these, even if literally present in the text):
    "pick", "pack", "physical stamina", "communication", "analysis", "work independently",
    "customer service", "resolve problems", "manage schedules", "teamwork", "organization",
    "front-end", "back-end", "frontend", "backend", "database", "bases de données", "web", "mobile",
    "cloud", "architecture", "devops", "testing" (these name a CATEGORY or generic role area, not a
    specific product/language/framework — only list the actual named technology within that area,
    e.g. "React" or "PostgreSQL", never the category word itself)
- If the posting lists several alternative technologies as acceptable (e.g. "plusieurs technologies
  parmi Java/PHP/JavaScript/Python", "one or more of the following", "l'une des technologies
  suivantes"), list ALL of them in required_skills/principal_technologies exactly as named — do NOT
  pick a single one as if it were the sole mandatory requirement, and do NOT treat this as a
  principal-tech role unless the posting also states one of them is specifically the primary focus.
- is_principal_tech_role is true when the job's primary focus is a specific technology (e.g. "Rust Developer").
- principal_technologies lists the defining technologies if is_principal_tech_role is true — same
  named-technology-only rule as required_skills/preferred_skills above.
- Keep lists short (max 10 items).
"""


class RequirementExtractor:
    """Extracts structured requirements from a job posting.

    Uses fast heuristic extraction as the primary method.
    When LLM is configured and enabled, it enriches the heuristic result.

    Args:
        user_skills:  The candidate's skills from their profile (plain strings).
        llm_provider: Optional LocalLLMProvider instance for LLM enrichment.
                      When None, only heuristic extraction runs.
    """

    def __init__(
        self,
        user_skills: list[str],
        llm_provider: LLMProvider | None = None,
        target_domains: list[str] | None = None,
    ):
        self._user_skills = user_skills
        self._llm = llm_provider
        # Used only by the LLM-enrichment corroboration step (_parse_llm_response)
        # to recognize when a claimed "unknown principal technology" is actually
        # one of the candidate's OWN target domains — see _is_target_domain_term.
        # Optional and additive: heuristic-only extraction never reads this.
        self._target_domains = list(target_domains or [])
        # Precomputed once per instance — user_skills is fixed for the instance's
        # entire lifetime (one RequirementExtractor is built per pipeline run and
        # reused across every job; see orchestrator.py). Performance-only caching:
        # identical values to what _normalize_user_skills()/_user_knows_tech()
        # would recompute from scratch inside _extract_skill_importance() on every
        # single call otherwise.
        self._user_skills_norm = _normalize_user_skills(user_skills)
        self._effective_core_languages: list[tuple[str, set[str]]] = [
            (canonical, variants)
            for canonical, variants in _CORE_LANGUAGES.items()
            if canonical != "r"
            and not _user_knows_tech(canonical, variants, self._user_skills_norm)
        ]

    def extract(self, job: Job) -> JobRequirements:
        """Extract structured requirements for a single job.

        Always runs the heuristic extractor. Runs LLM enrichment only when a
        provider is configured AND ``should_run_llm`` finds the heuristic
        result genuinely uncertain — see that function's docstring. This is
        purely a cost/latency decision: skipping the call never changes what
        a call COULD have produced, since the LLM can only ever fill gaps or
        clarify importance for evidence the heuristic already found (see
        ``_merge``'s precedence rule) — it can never override an existing
        heuristic violation.
        """
        result = self._extract_heuristic(job)

        if self._llm is not None and should_run_llm(job, result):
            llm_result = self._extract_llm(job)
            if llm_result is not None:
                result = self._merge(result, llm_result)
                result.extraction_method = "combined"

        return result

    # ------------------------------------------------------------------
    # Heuristic extractor
    # ------------------------------------------------------------------

    def _extract_heuristic(self, job: Job) -> JobRequirements:
        """Fast regex + vocabulary extraction (no network calls)."""
        text = f"{job.title or ''}\n{(job.description or '')[:5000]}"

        education = extract_education(text)
        experience = extract_experience((job.description or "")[:5000])

        principal_mismatches, or_alternatives = detect_missing_principal_skills_with_or(
            job.title or "", self._user_skills
        )

        # Extract skill importance signals from description (first 3000 chars)
        desc = (job.description or "")[:3000]
        skills = self._extract_skill_importance(job.title or "", desc, job.skills or [])

        return JobRequirements(
            education=education,
            experience=experience,
            skills=skills,
            principal_skill_mismatches=principal_mismatches,
            title_or_alternatives=or_alternatives,
            occupation_title=job.title,
            extraction_method="heuristic",
        )

    # Centrality signals — patterns indicating the skill is the PRIMARY focus.
    _CENTRALITY_PHRASES = [
        re.compile(r"\bprincipalement\b", re.I),
        re.compile(r"\bprimarily\b", re.I),
        re.compile(r"\bmainly\b", re.I),
        re.compile(r"\bessentiellement\b", re.I),
        re.compile(r"\bnotre\s+stack\s+(?:est|principale|repose)\b", re.I),
        re.compile(r"\bour\s+(?:main\s+)?(?:stack|language|tech)\b", re.I),
        re.compile(r"\bwe\s+(?:build|work)\s+(?:primarily|mainly|exclusively)\b", re.I),
    ]

    def _extract_skill_importance(
        self,
        title: str,
        description: str,
        job_skills_tags: list[str],
    ) -> list[SkillRequirement]:
        """Detect known technologies missing from profile that have mandatory signals.

        For each unknown technology token found in the description:
        - Score centrality using: title presence, frequency, "mainly/principalement"
          context, and experience-years wording.
        - centrality >= 5 → is_principal=True,  MANDATORY  → hard reject in gate
        - centrality 2-4 → is_principal=False, MANDATORY  → 40% score penalty
        - centrality <= 1 → is_principal=False, UNKNOWN   → flagged only (no penalty)

        Also adds job_skills_tags with UNKNOWN importance for downstream consumers.
        """
        skills: list[SkillRequirement] = []
        seen: set[str] = set()

        if description:
            desc_lower = _norm_skill(description)
            title_lower = _norm_skill(title)

            for canonical, variants in self._effective_core_languages:
                # Find all positions in description
                positions: list[int] = []
                for variant in variants:
                    # Cheap substring pre-check: the regex below requires this
                    # exact literal substring to be present, so when it's
                    # absent the regex is mathematically guaranteed to find
                    # zero matches — skip the compile+scan entirely. Most
                    # tracked technologies do not appear in any given job
                    # description at all, so this eliminates the bulk of the
                    # wasted regex work without changing which matches are
                    # ultimately found.
                    if variant not in desc_lower:
                        continue
                    escaped = re.escape(variant)
                    pat = r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])"
                    for m in re.finditer(pat, desc_lower):
                        positions.append(m.start())

                if not positions:
                    continue

                # Check for mandatory signal near any occurrence (150-char window)
                mandatory_pos: list[int] = []
                for pos in positions:
                    window = description[max(0, pos - 150):pos + 150]
                    # Skip negations: "not", "no", "sans", "without", "except"
                    pre = description[max(0, pos - 30):pos]
                    if re.search(r"\b(?:not|no|sans|without|except|jamais|never)\b", pre, re.I):
                        continue
                    if any(p.search(window) for p in _DESC_MANDATORY_PHRASES):
                        mandatory_pos.append(pos)

                if not mandatory_pos:
                    continue

                # Score centrality
                centrality = 0

                # Signal 1: tech in title (+3 — strongest signal)
                for variant in variants:
                    if variant not in title_lower:
                        continue
                    escaped = re.escape(variant)
                    pat = r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])"
                    if re.search(pat, title_lower):
                        centrality += 3
                        break

                # Signal 2: frequency in description
                freq = len(positions)
                if freq >= 5:
                    centrality += 3
                elif freq >= 3:
                    centrality += 2
                elif freq >= 2:
                    centrality += 1

                # Signal 3: near "principalement"/"mainly"/"our stack" (±200 chars)
                # Guard: if ≥3 other unknown techs appear in the same window, it's a
                # company-stack listing ("our stack is Rails, Java, Python, Kotlin, Swift…"),
                # not a principal-focus phrase — skip the +2 for each individual tech.
                for pos in positions:
                    window = description[max(0, pos - 200):pos + 200]
                    if any(p.search(window) for p in self._CENTRALITY_PHRASES):
                        window_lower = window.lower()
                        other_unknown_in_window = 0
                        for other_can, other_vars in self._effective_core_languages:
                            if other_can == canonical:
                                continue
                            for ov in other_vars:
                                # Same substring pre-check as the outer scan,
                                # applied to the much smaller ~400-char window.
                                if ov not in window_lower:
                                    continue
                                ep = re.escape(ov)
                                op = r"(?<![a-z0-9])" + ep + r"(?![a-z0-9])"
                                if re.search(op, window_lower):
                                    other_unknown_in_window += 1
                                    break
                            # Only the < 3 comparison below is ever observed —
                            # once the count reaches 3 the outcome is decided
                            # and further counting cannot change it.
                            if other_unknown_in_window >= 3:
                                break
                        if other_unknown_in_window < 3:
                            centrality += 2
                        break

                # Signal 4: high experience years requested near the tech
                for pos in mandatory_pos:
                    window = description[max(0, pos - 150):pos + 150]
                    if re.search(r"\b[5-9]\s*(?:ans?|years?)\b", window, re.I):
                        centrality += 2
                        break
                    if re.search(r"\b[3-4]\s*(?:ans?|years?)\b", window, re.I):
                        centrality += 1
                        break

                raw_snippet = description[
                    max(0, mandatory_pos[0] - 50): mandatory_pos[0] + 50
                ].strip()

                if centrality >= 5:
                    importance = Importance.MANDATORY
                    is_principal = True
                elif centrality >= 2:
                    importance = Importance.MANDATORY
                    is_principal = False
                else:
                    importance = Importance.UNKNOWN
                    is_principal = False

                seen.add(canonical)
                skills.append(SkillRequirement(
                    skill=canonical,
                    importance=importance,
                    is_principal=is_principal,
                    raw_text=raw_snippet,
                ))

        # Add job-tagged skills with UNKNOWN importance (for downstream consumers).
        for skill in job_skills_tags:
            sk = skill.strip()
            if sk and sk.lower() not in seen:
                seen.add(sk.lower())
                skills.append(SkillRequirement(skill=sk, importance=Importance.UNKNOWN))

        return skills

    # ------------------------------------------------------------------
    # LLM extractor (optional enrichment)
    # ------------------------------------------------------------------

    def _extract_llm(self, job: Job) -> JobRequirements | None:
        """Call the local LLM to extract structured requirements.

        Returns None on any failure so the heuristic result is used instead.
        """
        if self._llm is None:
            return None
        try:
            title = (job.title or "").strip()
            description = (job.description or "")[:2000].strip()

            if not description:
                return None

            prompt = _LLM_EXTRACTION_PROMPT.format(
                title=title,
                description=description,
            )

            # Reuse the Phase 2 LLM provider's public completion primitive.
            raw_response = self._llm.complete(prompt)
            logger.debug("LLM requirement extraction: received response for job %s", job.id)
            return self._parse_llm_response(raw_response, job)

        except Exception as exc:
            logger.debug("LLM requirement extraction failed for job %s: %s", job.id, exc)
            return None

    def _parse_llm_response(
        self, response_text: str, job: Job
    ) -> JobRequirements | None:
        """Parse the LLM's JSON response into a JobRequirements.

        Every claim is corroborated against the actual job text before it is
        allowed to carry ``EvidenceSource.LLM_CORROBORATED`` — see the module
        docstring. Uncorroborated claims are kept (tagged
        ``LLM_UNCORROBORATED``) for audit visibility only.
        """
        cleaned = response_text.strip()
        # Strip markdown fences
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]

        try:
            data = json.loads(cleaned.strip())
        except json.JSONDecodeError:
            logger.debug("LLM returned invalid JSON for job %s", job.id)
            return None

        corroboration_text = f"{job.title or ''}\n{(job.description or '')[:_CORROBORATION_WINDOW]}"
        corroboration_text_norm = _norm_skill(corroboration_text)
        user_norm = _normalize_user_skills(self._user_skills)

        # ------------------------------------------------------------------
        # Education
        # ------------------------------------------------------------------
        edu_data = data.get("education") or {}
        education = None
        if isinstance(edu_data, dict) and edu_data.get("level"):
            level = parse_education_level(edu_data.get("level", ""))
            if level is not None:
                imp_str = edu_data.get("importance", "unknown")
                try:
                    imp = Importance(imp_str)
                except ValueError:
                    imp = Importance.UNKNOWN

                evidence_span = find_level_evidence(corroboration_text, level)
                if evidence_span is not None:
                    source = EvidenceSource.LLM_CORROBORATED
                    raw_text = evidence_span.text
                else:
                    source = EvidenceSource.LLM_UNCORROBORATED
                    raw_text = "[llm: no supporting text found for this level]"
                    logger.info(
                        "Requirements gate LLM: education claim '%s' (%s) for job %s "
                        "has no textual evidence — discarded as unsupported inference.",
                        edu_data.get("level"), imp_str, job.id,
                    )

                education = EducationRequirement(
                    level_bac_plus=level,
                    level_label=edu_data.get("level", ""),
                    importance=imp,
                    raw_text=raw_text,
                    span=evidence_span,
                    source=source,
                )

        # ------------------------------------------------------------------
        # Skills (required + preferred)
        # ------------------------------------------------------------------
        skills: list[SkillRequirement] = []
        for sk in data.get("required_skills") or []:
            skill_req = self._build_llm_skill(
                sk, Importance.MANDATORY, corroboration_text, corroboration_text_norm, user_norm, job,
            )
            if skill_req is not None:
                skills.append(skill_req)
        for sk in data.get("preferred_skills") or []:
            skill_req = self._build_llm_skill(
                sk, Importance.PREFERRED, corroboration_text, corroboration_text_norm, user_norm, job,
            )
            if skill_req is not None:
                skills.append(skill_req)

        # ------------------------------------------------------------------
        # Principal technologies
        # ------------------------------------------------------------------
        principal_techs: list[str] = []
        principal_techs_uncorroborated: list[str] = []
        if data.get("is_principal_tech_role") and data.get("principal_technologies"):
            for tech in data.get("principal_technologies") or []:
                if not isinstance(tech, str) or not tech.strip():
                    continue
                verdict = evaluate_llm_tech_claim(
                    tech,
                    target_domains=self._target_domains,
                    user_skills_norm=user_norm,
                    corroboration_text=corroboration_text,
                    corroboration_text_norm=corroboration_text_norm,
                    extractor_id="llm.principal_technology.corroboration.v1",
                )
                # "generic"/"target_domain"/"known" — never valid rejection
                # evidence to begin with (see the module-level "LLM claim
                # safety guards" note and evaluate_llm_tech_claim's docstring).
                if verdict.outcome in ("generic", "target_domain", "known"):
                    continue
                if verdict.outcome == "satisfied_alternative":
                    # The posting offers this as ONE OF SEVERAL acceptable
                    # technologies and the candidate already knows a different
                    # one from the same list — an OR-alternative, not a
                    # missing mandatory requirement.
                    logger.info(
                        "Requirements gate LLM: principal technology claim '%s' for job %s "
                        "sits in an inclusive/alternative list the candidate already satisfies "
                        "via another item — not used for rejection.",
                        tech, job.id,
                    )
                    continue
                if verdict.outcome == "corroborated":
                    principal_techs.append(tech.strip())
                else:
                    principal_techs_uncorroborated.append(tech.strip())
                    logger.info(
                        "Requirements gate LLM: principal technology claim '%s' for job %s "
                        "has no textual evidence — flagged, not used for rejection.",
                        tech, job.id,
                    )

        return JobRequirements(
            education=education,
            skills=skills,
            principal_skill_mismatches=principal_techs,
            principal_skill_uncorroborated=principal_techs_uncorroborated,
            occupation_title=job.title,
            extraction_method="llm",
        )

    def _build_llm_skill(
        self,
        raw_skill: object,
        importance: Importance,
        corroboration_text: str,
        corroboration_text_norm: str,
        user_norm: set[str],
        job: Job,
    ) -> SkillRequirement | None:
        """Build one LLM-claimed SkillRequirement, corroborated against job text.

        Returns None (no requirement at all) when the candidate already knows
        the skill directly or via the same family-expansion the heuristic
        path uses — a skill the candidate has is not a missing requirement,
        regardless of how the job phrases it.
        """
        if not (isinstance(raw_skill, str) and raw_skill.strip()):
            return None
        name = raw_skill.strip()
        verdict = evaluate_llm_tech_claim(
            name,
            target_domains=self._target_domains,
            user_skills_norm=user_norm,
            corroboration_text=corroboration_text,
            corroboration_text_norm=corroboration_text_norm,
            extractor_id="llm.skill.corroboration.v1",
        )
        if verdict.outcome in ("generic", "target_domain", "known"):
            return None
        if verdict.outcome == "satisfied_alternative":
            logger.info(
                "Requirements gate LLM: skill claim '%s' (%s) for job %s sits in an "
                "inclusive/alternative list the candidate already satisfies via another "
                "item — not used for rejection/penalty.",
                name, importance.value, job.id,
            )
            return None
        if verdict.outcome == "corroborated" and verdict.span is not None:
            return SkillRequirement(
                skill=name, importance=importance, raw_text=verdict.span.text,
                span=verdict.span, source=EvidenceSource.LLM_CORROBORATED,
            )
        logger.info(
            "Requirements gate LLM: skill claim '%s' (%s) for job %s has no "
            "textual evidence — flagged, not used for rejection/penalty.",
            name, importance.value, job.id,
        )
        return SkillRequirement(
            skill=name, importance=importance,
            raw_text="[llm: no supporting text found]",
            source=EvidenceSource.LLM_UNCORROBORATED,
        )

    @staticmethod
    def _merge(
        heuristic: JobRequirements,
        llm: JobRequirements,
    ) -> JobRequirements:
        """Merge heuristic and LLM results under an explicit precedence rule.

        Precedence (most to least authoritative):
          1. Deterministic (heuristic) evidence — always attributable, NEVER
             overwritten by the LLM.
          2. LLM interpretation corroborated by literal text evidence — may
             fill a gap the heuristic left empty, or clarify importance for
             the *same* education level the heuristic already found (the
             heuristic can locate "Bac+5" but miss that surrounding prose
             makes it mandatory; the LLM can read that context).
          3. LLM inference with no textual corroboration — retained on the
             merged result for audit visibility, but tagged
             ``LLM_UNCORROBORATED`` so ``HardConstraintGate`` treats it as
             UNKNOWN and it can never independently cause a VIOLATED state
             or a score penalty.

        This function decides what evidence is *worth keeping*; deciding
        whether that evidence is *sufficient for a hard-constraint decision*
        is HardConstraintGate's job, not this one.
        """
        education = heuristic.education
        if heuristic.education is None:
            # Heuristic found no education mention at all — accept the LLM's
            # claim (whatever its corroboration source); the gate enforces
            # the safety rule for LLM_UNCORROBORATED regardless.
            education = llm.education
        elif (
            llm.education is not None
            and llm.education.source == EvidenceSource.LLM_CORROBORATED
            and llm.education.level_bac_plus == heuristic.education.level_bac_plus
            and heuristic.education.importance in (Importance.UNKNOWN, Importance.CORE)
            and llm.education.importance != Importance.UNKNOWN
        ):
            # Same level the heuristic already found; heuristic couldn't tell
            # mandatory vs preferred from local context. The LLM's reading is
            # corroborated (the level itself is confirmed in the text) and
            # only clarifies importance — level and span stay heuristic's own.
            education = EducationRequirement(
                level_bac_plus=heuristic.education.level_bac_plus,
                level_label=heuristic.education.level_label,
                importance=llm.education.importance,
                raw_text=heuristic.education.raw_text,
                span=heuristic.education.span,
                is_or_alternative=heuristic.education.is_or_alternative,
                is_experience_alternative=heuristic.education.is_experience_alternative,
                source=EvidenceSource.LLM_CORROBORATED,
                kind=heuristic.education.kind,
                enrolment_level_min=heuristic.education.enrolment_level_min,
            )
        # else: heuristic evidence (e.g. already MANDATORY, or a different
        # level) stands untouched — deterministic evidence is authoritative.

        # Skills: merge, LLM skills only fill in names the heuristic missed.
        heuristic_skill_names = {s.skill.lower() for s in heuristic.skills}
        merged_skills = list(heuristic.skills)
        for sk in llm.skills:
            if sk.skill.lower() not in heuristic_skill_names:
                merged_skills.append(sk)

        # Principal mismatches: union of both — LLM entries here are already
        # corroborated (uncorroborated ones were routed to the sibling list).
        combined_principals = list(
            {s.lower() for s in heuristic.principal_skill_mismatches}
            | {s.lower() for s in llm.principal_skill_mismatches}
        )
        combined_uncorroborated = list(
            {s.lower() for s in heuristic.principal_skill_uncorroborated}
            | {s.lower() for s in llm.principal_skill_uncorroborated}
        )

        return JobRequirements(
            education=education,
            experience=heuristic.experience,  # deterministic-only, see ExperienceRequirement
            skills=merged_skills,
            principal_skill_mismatches=combined_principals,
            principal_skill_uncorroborated=combined_uncorroborated,
            title_or_alternatives=heuristic.title_or_alternatives,
            occupation_title=heuristic.occupation_title or llm.occupation_title,
            extraction_method="combined",
        )


# ---------------------------------------------------------------------------
# Selective invocation — decide whether an LLM call is worth its latency cost
# ---------------------------------------------------------------------------

# Cheap, deterministic textual cues used ONLY to decide whether calling the
# LLM is worthwhile — never used for extraction itself. A false positive here
# just costs one extra ~5s call; a false negative just costs recall. Neither
# can affect safety: the LLM can never override an existing heuristic
# violation or manufacture a hard rejection (see _merge / gate.py).
_EDUCATION_CONTEXT_CUES = re.compile(
    r"\b(?:dipl[oô]me|diploma|degree|bac\s*\+?\s*\d|master|licence|bachelor|"
    r"ing[eé]nieur|university|universit[eé]|[eé]cole|formation)\b", re.I,
)

_TECHNICAL_CONTEXT_CUES = re.compile(
    r"\b(?:d[eé]velopp(?:er|ement|eur|euse)|develop(?:er|ment|ing)?|program(?:mer|ming)?|"
    r"code|coding|logiciel|software|technolog(?:ie|y)|stack|informatique|"
    r"engineer(?:ing)?|ing[eé]nieur|ing[eé]nierie|architecture|syst[eè]me|infrastructure|"
    r"automat(?:e|isme|iser|ise)|supervision|scada|api|framework)\b", re.I,
)


def should_run_llm(job: Job, heuristic: JobRequirements) -> bool:
    """Decide whether LLM enrichment is worth calling for this job.

    Conceptual flow: deterministic extraction -> clearly resolved? -> skip;
    unknown/ambiguous? -> call. Conservative by design — prioritizes recall
    over call-count reduction, per the project's real-model benchmark
    (Qwen3-8B, ~4.7s/call, making blanket per-job invocation operationally
    unviable at this project's job volume).

    Skips only when the heuristic has already reached a decision the LLM
    could not change anyway:
      - a principal-skill mismatch was already found (hard-reject-eligible;
        the LLM can only ever ADD corroborated evidence, never remove an
        existing one — see _merge), or
      - education was found with MANDATORY importance (already a confident,
        attributable reading; _merge never lets the LLM downgrade it).

    Otherwise calls the LLM whenever the heuristic result is genuinely
    ambiguous AND the text gives some signal worth interpreting:
      - education is absent/UNKNOWN and the text mentions education-adjacent
        vocabulary at all (nothing to call about otherwise), or
      - no principal-tech signal was found and the text reads like a
        technical role (the heuristic's fixed ~26-language title vocabulary
        can miss real principal-tech mentions entirely — RPA/SCADA/UiPath
        have no heuristic representation at all), or
      - the heuristic itself flagged a low-confidence mandatory-skill
        candidate (centrality too low to classify with confidence alone).
    """
    if heuristic.principal_skill_mismatches:
        return False
    if heuristic.education is not None and heuristic.education.importance == Importance.MANDATORY:
        return False

    text = f"{job.title or ''}\n{(job.description or '')[:_CORROBORATION_WINDOW]}"

    education_ambiguous = heuristic.education is None or heuristic.education.importance == Importance.UNKNOWN
    if education_ambiguous and _EDUCATION_CONTEXT_CUES.search(text):
        return True

    if not heuristic.principal_skill_mismatches and _TECHNICAL_CONTEXT_CUES.search(text):
        return True

    if any(s.importance == Importance.UNKNOWN and s.raw_text for s in heuristic.skills):
        return True

    return False


def build_requirements_llm_provider(matching_config: object) -> LLMProvider | None:
    """Construct the local LLM provider for requirements-gate enrichment.

    This is the single wiring point orchestrator.py uses to decide whether
    the gate's LLM enrichment runs at all. Returns None (heuristic-only) when
    ``matching_config.llm_extraction_enabled`` is falsy/absent, or when
    provider construction itself fails for any reason — this function must
    never raise, since the gate has to keep working without it.

    Endpoint/model are overridable via REQGATE_LLM_ENDPOINT / REQGATE_LLM_MODEL_NAME
    so a non-default local server can be targeted without touching profile.yaml.
    """
    if not getattr(matching_config, "llm_extraction_enabled", False):
        return None
    try:
        import os

        from src.ai.interfaces import LocalLLMProvider

        endpoint = os.environ.get("REQGATE_LLM_ENDPOINT", "http://localhost:1234")
        model_name = os.environ.get("REQGATE_LLM_MODEL_NAME", "local-model")
        logger.info(
            "Requirements gate: LLM extraction enabled (endpoint=%s, model=%s)",
            endpoint, model_name,
        )
        return LocalLLMProvider(
            endpoint=endpoint,
            model_name=model_name,
            temperature=0.0,
            max_tokens=512,
            timeout_seconds=20,
            thinking_enabled=False,
        )
    except Exception as exc:
        logger.warning("Requirements gate LLM setup failed: %s", exc)
        return None
