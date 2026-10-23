# Find Your Job With AI: final update plan

Status: **approved direction (owner, 2026-10-23), the implementation roadmap.** Clarifications and resolved defaults are in §0b–§0c. Written 2026-10-23 against the
public repository `ItackFPS/Find-Your-Job-With-AI-Public`, `main` = `c7bbe43` (tag `v1.0.0`).

How to read it: **[measured]** means checked in this investigation (code, the running v1.0.0
app, the owner's local database opened read-only, or a live request). **[source]** means it comes
from external documentation, cited at the end. **[hypothesis]** means it must be tested before we
rely on it. **[owner]** means you decided it. **[my call]** is a technical choice I made that
you can overturn.

---

## 0. Decisions so far

| # | Question | Decision |
|---|---|---|
| D1 | Job sources | [owner] France Travail and LinkedIn are your best sources. New users must get them **without credentials**, by scraping if that is the only way. A thorough search of how other projects do it is in §C1. |
| D2 | France Travail vs EURES | [owner] Separate sources. France Travail publishes far more offers than EURES, so it needs its own credential-free route. |
| D3 | Desktop shell | [owner] A real window (WebView2) plus a tray icon. Scans continue after the window closes, the app can start with Windows, and it sends Windows notifications. |
| D4 | Role of AI | [owner] AI is a core principle, not decoration. It reflects on the offers you like and finds more like them, and it analyses each offer in depth, proposing solutions grounded in evidence. |
| D5 | Discovery reach | [owner] Enabled sources plus company career pages, with no general web search. Also: upload a CV, and the app produces a tailored CV and cover letter for each job, ready to apply. |
| D6 | Machines that cannot run a model | [owner] Degrade gracefully and stay local. There is never a cloud fallback. |
| D7 | Local AI runtime | [owner] Ollama first, and detect Ollama, LM Studio or any local OpenAI-compatible server. Reuse existing models; guided install only with consent. |
| D8 | Removals | [owner, in the brief] Remove the weekly digest and n8n. Disconnect Discord (it can come back much later). |
| D9 | Bot protection | [my call, continuing an existing project rule] Never bypass a CAPTCHA, WAF or login wall, and never use rotating or residential proxies. "Scraping" here means reading pages and endpoints that answer an anonymous request. This keeps the app from being an evasion tool, and it is also what keeps it maintainable. |

### 0b. Owner clarifications after review (2026-10-23): approved direction

- **France first.** French job coverage is the priority for sources, matching and testing.
  International remote offers stay available, but as a configurable remote policy (§C2), never
  at the expense of French coverage.
- **The full vision is kept, but release work is tiered** (§F, "Release tiers"). A usable,
  reliable application ships before secondary features are complete.
- **No weakening of guarantees.** Matching safeguards (ADR 0004, the unsafe-rejection gate, abstention),
  the decision and feedback journals, local-only AI (loopback only, no cloud fallback) and
  production/lab isolation (ADR 0011, the sandbox) are invariants. A change that improves apparent
  results by loosening one of them is rejected.
- **Execution:** the plan is the roadmap. Implementation proceeds in dependency order (§G)
  without a new planning cycle, with focused commits and draft PRs on the public repository.
  Owner input is requested only for product-changing, legal or security-significant, or costly
  decisions.

### 0c. Resolved defaults for the former open questions (§I)

| # | Default chosen | Why it is the low-risk choice |
|---|---|---|
| I1 | Unofficial sources (LinkedIn, Indeed, HelloWork) are **opt-in**: off on a fresh install, one switch in the wizard and in Settings, with a one-sentence explanation. Existing installs that already ran a LinkedIn or HelloWork scan keep them enabled (an observed choice). The France Travail website collector (`public-page`, robots-respecting) is **on by default**. | The legal risk sits with the user's informed choice; French coverage still works out of the box |
| I2 | The project does **not** apply for France Travail partner access now. The API collector stays for users who already hold credentials. | An external commitment the owner can make later; the web route does not depend on it |
| I3 | Strategy promotion stays **CLI-only**. The Quality page shows the exact command with a copy button and explains why. | Keeps the agent-cannot-approve guarantee intact without new attack surface |
| I4 | **No code signing for now** (cost). Updates: a manual "Check for updates" button that opens the GitHub release page and shows the published SHA-256; no automatic network calls. | No cost, and no background traffic |
| I5 | English and French are complete and reviewed. Spanish, Italian and German ship as **machine-drafted, marked "beta"** in the language picker, with a missing-key CI check; community review is welcome. | Five languages available without blocking the release on reviewers |
| I6 | Generated documents: **DOCX** (python-docx, small, pure Python) and **PDF through the window's print-to-PDF**. No heavy PDF stack. | Smallest dependency footprint; both formats users expect |

---

## A. Current-state assessment

### A1. What exists and works (keep)

- **Deterministic pipeline** (`src/pipeline/orchestrator.py`): collection, normalization,
  validation, deduplication, hard filter, requirements gate, scoring, then storage. It is
  fail-open per stage and per collector. Tests: 5,296 passed, 9 skipped on `c7bbe43`.
- **Conservative rejection philosophy**: three-valued evidence (SATISFIED, VIOLATED or UNKNOWN). Unknown is
  never a rejection. ADR 0004 distinguishes mandatory, preferred and non-eliminatory education.
  A CI unsafe-rejection gate exists (`tests/test_rejection_safety_gate.py`).
- **Decision journal** (ADR 0008): every accepted and rejected job keeps its evidence, strategy version and
  run id. Rejected text is kept as snapshots for auditing false negatives. **This is the
  provenance backbone of the explainability requirements, and it already exists.**
- **Feedback journal** (ADR 0009): append-only. Explicit, behavioural and outcome signals each carry
  the decision and strategy they reacted to.
- **Disagreement and abstention** (ADR 0010): accept, review or reject, run in shadow mode.
- **Evaluation and lab** (ADR 0007, 0011): self-check, frozen datasets, experiments, human-only
  promotion, and an AppContainer sandbox for the lab agent. The sandbox passed security checks on 2026-10-23.
- **Security**: loopback-only binding, Host and Origin guards, and refusal of non-loopback LLM endpoints.
  Gitleaks, pip-audit and npm audit run in CI.
- **Windows installer**: per-user, no admin rights. Upgrades and uninstalls keep data. The acceptance script passes 60/60.
- **Scan machinery**: LinkedIn and HelloWork scanners with checkpoints, pause and resume, and a scheduler
  with backoff (`src/scheduler/service.py`).

### A2. Measured weaknesses

| Area | Finding | Evidence |
|---|---|---|
| Fresh-install results | With no credentials, a run collects 2,562 offers, and EURES supplies 1,910 of them. France Travail and Indeed are *skipped*, APEC *fails*, and LinkedIn and HelloWork are manual. | [measured] dashboard of the running v1.0.0 |
| Where good matches come from | Run-score rows ≥ 0.75 in your database: LinkedIn 14,605, Indeed 8,054, France Travail 5,558, HelloWork 5,132, APEC 539, EURES 181. **New users get none of the top four by default.** | [measured] `jobs.db` opened read-only |
| France Travail API | No longer offered to new subscribers ("plus exposée sur francetravail.io"), after massive automated use by AI tools. Access is on request only. | [source] data.gouv.fr |
| Remote-job false positive | The **#1 ranked job (90/100)** is a Hong Kong office job with one work-from-home day a week. Because its `countries` field is empty, it matched the "Remote France (preferred)" preference. The code says so: "no country data at all" counts as France. | [measured] job `1da0d83a…`; `src/pipeline/location_match.py` |
| Location model | Location is a department-centric ordered list (ADR 0005), and it is coupled to Discord channel routing (`slug_for` builds "dept-83" channel ids). This is unintuitive for users and tangles location with notifications. | [measured] code, Profile page |
| Profile UX | Raw keyword lists ("Excluded seniority keywords: senior, lead, manager…"), a `bac+N` ceiling, and "drag with ↑/↓" priorities. The Profile page tells users about Discord channels and the weekly email. | [measured] Profile page text |
| AI Understanding | Analyses only the **top 100** by deterministic score (`PHASE2_MAX_CANDIDATES`, a fixed default added to bound latency: one call takes about 8–32 s and 4 run in parallel). Its output only enriches the text used for semantic matching. **No evaluation shows it improves relevance or reduces false negatives**: the self-check measures the deterministic strategy, and ranking quality is "not measured". | [measured] `src/ai/config.py`, `docs/v1-status.md` |
| Install weight | Installer 210 MB. In a dev venv, `torch` is 536 MB and `transformers` 99 MB, out of a 1.3 GB site-packages; the embedding model cache is 458 MB. Cold start takes 41–76 s. | [measured] |
| Accuracy | All quality figures come from 50 *synthetic* jobs. Real-world precision and ranking are **unmeasured** (no human labels). | [measured] `docs/v1-status.md` |
| Notifications | Discord (engine of 1,628 lines, reconciler, `discord_admin`, MCP admin server), n8n auth and digest routes, `n8n_weekly_digest.json`, an "n8n offline" sidebar badge, and launcher `--n8n`. About 60 `src`/`frontend` files mention them. | [measured] grep |
| UI | Developer-tool look: dark only, Vite's default favicon still in place (`frontend/public/favicon.svg`), and "Pipeline/Stats" pages aimed at engineers. The Lab page tells users to run CLI commands. English only. | [measured] screenshots |
| Desktop | Opens in the default browser (`webbrowser.open`). There is no tray. Closing the tab does nothing, and "Stop" kills scans. The single instance is checked by app id and pid on a fixed port, 8000. | [measured] `src/launcher/run.py` |

### A3. Hidden dependencies that make changes risky

- `location_match.py` is shared by **scoring and Discord routing**, so removing Discord means
  re-homing the location logic first.
- `tests/test_indeed_redundancy_coverage.py` pins APEC as active, so source removals need that
  tripwire updated deliberately.
- `strategy_version` hashes the profile, filters and scoring config. **Any profile-model change creates a
  new strategy version**, so the self-check baseline must be re-recorded explicitly, not silently.
- Personalization re-applies scoring gates (`cap_personalization_at_gates`), so scoring changes
  must be mirrored there.
- The lab's isolation assumes fixed paths and ports (8000, 5173, 5678), which the sandbox
  blocks. A dynamic port (§E4) must be fed to the sandbox's block list.

---

## B. Target product

**One sentence:** a Windows app that, from a CV or a short guided setup, finds French and
French-compatible remote job offers across the main boards. It keeps scanning in the
background, explains each offer against your profile with quoted evidence, helps you act on it
(tailored CV, cover letter, gap plan), and learns from your feedback, with a visible way to
check that its matching stays good.

### B1. The user journey

1. **Install** (one click, under 100 MB target [hypothesis], no admin). The first window opens in
   seconds with a splash while the backend starts.
2. **Welcome wizard** (in their language: EN by default, plus FR, ES, IT and DE):
   1. Language.
   2. *Import your CV* (PDF or DOCX) **or** answer a few questions. The CV becomes a **draft
      profile**, and the user confirms each field.
   3. What you're looking for: target roles, career stage, contract types (CDI, CDD, alternance,
      stage, freelance).
   4. Where: an address or town and a max distance slider, plus remote: "France only", "EU",
      "anywhere I'm allowed".
   5. Sources: the official sources are always on. **"Also search LinkedIn, Indeed, HelloWork and
      France Travail's website"** is one switch with a one-line, plain explanation of the risk.
   6. AI: the app detects the hardware and installed runtimes and recommends a model, or offers to
      continue without AI. Nothing is downloaded without an explicit click that shows the size.
   7. Background: "Keep searching when the window is closed", "Start with Windows".
   8. The first search starts, with live progress.
3. **Home**: new matches since the last visit, grouped *Strong / Worth a look / Needs your
   judgement*, plus background status ("LinkedIn: scanning, 3 of 12 searches") and offers that
   are about to expire.
4. **Jobs**: search, filters (distance, remote type, contract, level, language, source, date,
   decision), saved views, side-by-side compare (2–3 offers), save and track.
5. **Offer page**: the original posting (translate on demand, original always visible), a **fit
   map** (each requirement → met / partly / missing / unknown, with the quoted sentence), a
   score explanation in plain words, an **action plan** (how to close each gap, what to
   highlight, what to ask the employer), "Make my CV and letter for this job", feedback, and
   the application status.
6. **Applications**: a tracker (saved → applied → interview → offer or closed) with dates and the
   generated documents attached.
7. **More like this**: from liked or applied offers, AI explains what they have in common and
   runs new searches (synonym titles in FR and EN, adjacent roles, similar companies' career pages).
   Results are labelled "found by discovery" so their yield can be measured.
8. **Quality** (plain-language lab): a health check, "is matching getting better or worse?",
   runs history, false positives and negatives to review, and strategy comparison. An
   *Advanced* toggle exposes the full lab.
9. **Settings**: language, sources (with health), AI (runtime, model, benchmark), background
   behaviour, data (export, delete).

### B2. What it should feel like

Calm and trustworthy, like a good bank or tax app rather than a cockpit. Plain words. The app
never says "rejected" without a reason the user can read, and it says "we're not sure" when it
isn't. A light theme by default with dark available, readable at 125 % Windows scaling,
keyboard-navigable, and WCAG 2.2 AA contrast.

---

## C. Research findings and recommended solutions

### C1. Job sources (the most important section)

Principle: **every source is a plug-in with a declared access class, health, and limits.** A
broken source never breaks a run.

| Access class | Meaning | Default |
|---|---|---|
| `official` | Documented API or feed meant for reuse | on |
| `public-page` | Anonymous pages that robots.txt allows (HTML or JSON-LD) | on |
| `unofficial` | Anonymous endpoints the site's own apps use, against its terms but not access-controlled | **opt-in at setup** |
| `blocked` | CAPTCHA, WAF or login required | not shipped |

#### France Travail: credential-free route found

- The official API is closed to new subscribers [source data.gouv]. Shipping one shared
  secret in a public app would breach the API's terms and would be revoked.
- **France Travail's public website exposes robots-allowed listing pages per occupation ×
  département and occupation × town**, listed in its own sitemaps
  (`croisements-metiers-departements.xml`: **27,454 URLs**). [measured] One page
  (`/offres/emploi/developpeur-web/var/s28m7d83`) returned **20 offers** anonymously, and each
  offer page (`/offres/recherche/detail/<id>`) is server-rendered HTML (about 38 KB) with
  title, place, contract and description. Only keyword search (`motsCles=`) is disallowed.
- Each listing page shows about 20 of the most recent offers, with no deeper pagination
  [source morvanvivien/francetravail-scraper]. Coverage therefore comes from *many narrow pages*:
  the user's occupations × départements within their radius, plus towns for dense areas, run
  daily. Because new offers are what matter, daily runs accumulate full coverage of the
  relevant slice. [hypothesis: measure against your 10,965 API-collected offers]
- **Recommendation:** a new `france_travail_web` collector (class `public-page`, **on by
  default**, since it respects robots.txt). It maps the profile's roles to France Travail
  occupation slugs (the sitemap gives the full list) and the radius to départements and towns,
  with a polite rate (≥ 1 s between requests, a cap per run) and incremental runs (offer ids
  already known are not re-fetched). Keep the API collector for users who hold credentials,
  and dedupe the two by France Travail offer id.
- EURES stays as its own source (it carries France Travail offers too: 3,112 of 3,113
  EURES-France offers share one provider code, and 1,827 matched by title and company)
  [measured]. That is useful as a second path if France Travail changes its site.

#### LinkedIn

- The current method, python-jobspy calling the logged-out guest endpoint
  `jobs-guest/jobs/api/seeMoreJobPostings/search` [measured in the bundled jobspy], is **what
  essentially every open-source tool uses** (JobSpy, about 4k stars, and its JS port). LinkedIn
  rate-limits around the 10th page per IP, and results are capped at about 1,000 per search
  [source JobSpy docs]. LinkedIn's User Agreement forbids scraping. hiQ v. LinkedIn ended
  with a judgment for LinkedIn on the contract claim [source].
- The alternatives are worse: paid APIs (Unipile, Apify actors) need accounts and money, and
  logged-in scraping risks the user's own account.
- **Recommendation:** keep the guest-endpoint scanner as `unofficial`, opt-in, run by the
  background agent. Use small batches, adaptive pacing, stop on the first 429 and resume later
  from its checkpoint (already implemented), and **never use the user's LinkedIn session**.
  Also add a credential-free *complement*: when the user pastes or shares a LinkedIn job URL,
  fetch that single public job page (one request per user action).

#### Indeed

- No public API exists. The Publisher API ended and a partner programme is gated [source].
  Every open tool uses Indeed's mobile GraphQL endpoint with the iOS app's key, which
  python-jobspy ships (`jobspy/indeed/constant.py`, [measured]). JobSpy reports no rate limiting
  [source]. Your database holds 8,673 Indeed offers [measured].
- **Recommendation:** route Indeed **through the bundled python-jobspy scraper** instead of our
  own GraphQL client plus an `INDEED_API_KEY`. We stop handling a third-party credential, the
  user configures nothing, and jobspy maintainers track key changes. Class `unofficial`,
  opt-in. Delete `src/collectors/indeed.py`'s key handling.

#### HelloWork

- The current collector reads listing pages and JSON-LD on offer pages, and works (fixed in #7). It is
  `public-page` *if* the listing URLs it uses are robots-allowed; the collector must check
  robots.txt at runtime (§E2). [hypothesis: verify before classifying]
- **Recommendation:** keep it, run it in the background agent, classify after the robots check.

#### APEC

- DataDome CAPTCHA on every automated request [measured in v1, #28]. Third parties get round it with
  residential proxies or real browsers, which is outside D9.
- **Recommendation:** remove it from the shipped sources (update the tripwire test). APEC offers
  that also flow to EURES (APEC is part of EURES France [source]) are covered there.

#### Additional sources worth adding (ranked by value for France)

1. **Company career boards (ATS)**: Greenhouse, Lever, Ashby, SmartRecruiters, Workable,
   Recruitee, Teamtailor and Welcome Kit. These are `official` public JSON APIs. The collectors
   already exist for four of them, configured by hand for about 15 companies [measured]. **Make it
   automatic:** when a company appears in a liked offer or in discovery, probe its ATS slug and
   follow its board. This is the "company pages" part of D5, and its data quality is the best
   available (direct from the employer, fresh, never expired).
2. **La Bonne Alternance** (official, apprenticeships, aggregates France Travail and others) for
   alternance seekers. It needs a free per-user token [source], so make it an optional
   `official` source with a 2-minute guided signup. [hypothesis: verify whether its public
   search pages are usable without a token]
3. **Public sector**: Choisir le service public / Place de l'emploi public. [hypothesis:
   check for a feed]
4. **Welcome to the Jungle**: its WAF blocks pages [measured, project notes]. Some tools use its
   embedded Algolia search key [source Zeffut/JobScraper]. That is `unofficial`, and also
   largely redundant with the ATS boards of the same companies. **Defer.**
5. **Adzuna / Jooble**: legitimate aggregator APIs with a free key per developer [source]. Keep
   as an optional "bring your own key" source, not a default.

Keep the remote boards (Jobicy, RemoteOK, WWR, Working Nomads).

#### Expired offers, duplicates, failures

- **Expiry:** mark an offer `expired` when its `validThrough` date passes, when its source stops
  listing it on N consecutive successful runs of the same query, or when the user reports
  it. Stale offers fade out of Home but stay in the history. The `last_seen_at` column already
  exists [measured].
- **Duplicates:** the existing identity hierarchy plus a cross-source key (normalized
  title, company and town, within 30 days). France Travail web/API and EURES copies are merged as
  one offer with **several provenances**: the UI shows "seen on France Travail, EURES".
- **Failures:** per-source circuit breaker (after 3 consecutive failures, pause it with
  exponential backoff and show it in Settings). A *parser canary* runs: each collector ships a
  recorded fixture page plus a live smoke check that says "the site changed" instead of
  silently returning 0 jobs (the v1 HelloWork bug #7 was exactly that).

### C2. Location model

- **Replace** department priorities with **home place + max distance** (km), plus a remote policy.
  Internally: a bundled table of French communes (INSEE code, name, postcodes, centroid, and
  département; about 35k rows, a few MB) [my call]. Distances use great-circle (haversine) ×
  1.25 as a road-distance proxy [hypothesis: validate on 50 real pairs; travel time is a later
  option].
- **Address entry:** offline autocomplete over communes (no network needed). An optional exact
  address uses the national geocoder, now IGN Géoplateforme `data.geopf.fr/geocodage`. The old
  `api-adresse.data.gouv.fr` was decommissioned in January 2026 [source]. Only the typed text is
  sent, never the profile.
- **Offer locations:** resolve the town or postcode to a commune. A département only gives a
  coarse centroid with a "±" distance. Unknown places are **unknown, never excluded**.
- **Remote eligibility** becomes a typed field with evidence:
  `onsite | hybrid(n days) | remote_france | remote_eu | remote_worldwide | remote_restricted(country)
  | unknown`. Extract it from structured fields first, then text patterns ("France only",
  "EU timezone", "must reside in the US"), then optionally AI. **`unknown` remote is "Worth a
  look", never a preferred match.** This fixes the Hong Kong false positive.
- **Scoring:** distance decays the location score smoothly inside the radius. Beyond it there is
  no rejection for remote or hybrid roles the user accepts. The only hard exclusion is "on-site,
  and beyond max distance, and the user ticked 'strict'".

### C3. Profile: experience, level, education

- **Career stage instead of keyword lists.** The profile stores *facts*: experiences (role,
  employer, dates, skills, responsibilities), education entries and skills (ESCO-linked,
  which already exists). It also stores *targets*: the levels the user wants, chosen from
  `internship, alternance, graduate/entry, junior, mid, senior, lead, manager` (multi-select,
  with plain descriptions), and whether to show "stretch" roles one level up.
- Years of experience are **computed** from experiences (relevant years = overlap with target
  skills or roles), and the user can override them.
- **Job side:** level is inferred from *several* signals with weights: stated years (already
  parsed, with ranges fixed in v1), title words, responsibilities (manages people, owns
  budget), and contract (alternance, stage). A title word alone is **weak evidence**: a
  conflict between a title and stated years goes to *review*, never *reject* (it extends ADR 0004's logic).
- **Education:** represent qualifications by the **French national nomenclature levels 3–8,
  which are aligned one-to-one with EQF levels 3–8** (CAP 3, Bac 4, BTS/DUT 5, Licence/BUT/Licence
  pro 6, Master/ingénieur 7, Doctorat 8; décret n° 2019-14, from prior knowledge, not fetched in
  this investigation, so verify against France Compétences before coding). The user picks a qualification name; the level is
  derived. Foreign degrees: the user picks an EQF level with help text. The app never claims two
  qualifications are equivalent. It only compares levels, and says "level 6 required, you
  declared level 6 (BUT)". Mandatory vs preferred vs "or equivalent experience" stays as in ADR
  0004. The `bac+N` UI goes; `bac+N` in offers maps to levels.
- **Migration:** convert existing `profile.local.yaml` automatically (departments become the
  département-centroid home with a radius covering them; seniority exclusions become target
  levels; `max_education` becomes a level). Show a one-time "we updated your profile, please
  check" screen.

### C4. AI: a core analyst, grounded, never a judge

What measurement says today: deterministic gates do the filtering; AI Understanding only
enriches the embedding text of the top 100, with no measured gain. **Recommendation: retire that
stage as it stands** (keep its prompt and schema learnings), and build AI around five
user-visible jobs where an LLM really beats rules. Each has a deterministic fallback (D6).

| Feature | What the AI does | Grounding rule (machine-checked) | Without AI |
|---|---|---|---|
| **Offer analysis** | Extracts mandatory, preferred and implicit requirements, maps each to the profile (met / partly / missing / unknown), and writes an action plan: transferable experience to cite, how to close each gap, questions for the employer | Every requirement carries a **quote that must be an exact substring of the offer**, and every "you have X" must point to a profile entry id. Items failing either check are dropped and counted | The rule-based fit map from the requirements gate (already produces evidence) and a generic gap list |
| **More like this** | Explains what liked offers share; proposes title synonyms (FR and EN), adjacent ROME/ESCO occupations and companies; these feed the collectors and ATS probing | Proposals are *queries*, not facts. Yield (new relevant offers per query) is measured, and low-yield queries are retired automatically | ESCO/ROME neighbours of liked offers' occupations, plus co-occurring skills (skill graph exists) |
| **CV import** | PDF/DOCX text → structured draft profile | Each field shows the CV line it came from; the user confirms | Text extraction + ESCO skill matching + date regex, then manual completion |
| **Tailored CV + cover letter** | Selects and rephrases **only facts from the confirmed profile** to fit an offer; writes a letter citing the offer's needs | Every claim in the output maps to a profile entry; a checker flags any sentence with skills, employers, dates or numbers absent from the profile. **The app never invents experience** | Template CV ordered by relevance to the offer, letter skeleton with placeholders |
| **Translation** | Translates a posting into the UI language on demand | Original always shown; cached per offer + language + engine version | Optional offline translation pack (§C5), else "open original" |

Rules that carry over: AI **never rejects**. It can add evidence that moves an offer to
*review* (ADR 0010 abstention). It only ever calls loopback endpoints (v1 #6, #18). Every output
records model, prompt version and input hashes in the decision journal.

**No fixed "top 100".** The background agent runs analysis as a queue ordered by score within a
time and energy budget (for example "while idle, max N minutes per day", or only when on AC power on laptops),
and on demand when the user opens an offer. That removes the arbitrary cap while bounding cost.

**The LLM eligibility gate** stays off, and it moves to the lab: it can only be promoted if the
lab shows fewer false negatives *and* zero new unsafe rejections on labelled data.

### C5. Local AI runtime and setup (inspired by Locally Uncensored's first-start flow)

- **Detection:** Ollama (`GET :11434/api/tags`, `/api/ps`), LM Studio (`:1234/v1/models`; the
  `lms` CLI lists models), and any OpenAI-compatible server the user enters (loopback only).
  Show what was found and reuse it. [source Ollama/LM Studio docs]
- **Hardware probe** (standard library plus `wmic`/CIM and `nvidia-smi` when present): CPU
  cores and AVX2, RAM, GPU vendor and **VRAM** (the CIM `AdapterRAM` field caps at 4 GB, so use
  DXGI or nvidia-smi), and free disk.
- **Model catalogue** (a versioned JSON in the app, not hard-coded): 3 tiers, *Small*
  (about 3–4B, CPU or 4 GB VRAM), *Standard* (about 7–9B, 6–8 GB VRAM), and *Large* (about 14B+, 12 GB+), each with
  disk size, RAM/VRAM need and recommended quantization. The exact model names are chosen at
  implementation time with a mini benchmark on our tasks (JSON validity, quote-grounding rate,
  FR quality). [hypothesis]
- **Install:** if no runtime exists, offer Ollama's official installer (shown size, launched
  only after consent), then `POST /api/pull` with streamed progress (resumable, since Ollama
  resumes pulls), cancel and retry.
- **Verify, then benchmark:** load the model, run 3 fixture offers, and check valid JSON, grounding rate
  and seconds per offer. Show the result in words: "About 12 s per offer: fine for on-demand
  analysis; background analysis of new offers will take about 20 min per day". If it is too slow
  or invalid, recommend the smaller tier or "continue without AI".
- **Rejected alternatives:** bundling llama.cpp (adds GPU-build maintenance, a larger installer
  and duplicate models); supporting every engine natively (the OpenAI-compatible API already
  covers LM Studio, llama.cpp server, vLLM and others).

**Translation pack** (optional, for users without an LLM): an offline NMT model via CTranslate2
(OPUS-MT en↔fr and the UI-language pairs, int8, roughly 70–100 MB per direction)
[hypothesis: measure quality on 50 postings vs the LLM, and the size].

### C6. Embeddings: same model, a fraction of the weight

Replace `sentence-transformers` + `torch` (about 640 MB on disk [measured]) with **ONNX Runtime**
running the *same* `paraphrase-multilingual-MiniLM-L12-v2` exported to ONNX (a quantized variant
exists, supported by fastembed [source]). Expected: installer and installed size cut by more
than half, and a much faster cold start (torch import dominates) [hypothesis].
**Acceptance:** cosine similarities on 1,000 stored jobs vs the current model show Spearman ≥ 0.99, the
self-check is unchanged, and existing cached embeddings are re-computed once in the background.

### C7. Desktop architecture (D3)

```
FindYourJob.exe (launcher stub)
   ├─ starts or reuses ── Agent process (pythonw, no console)
   │                     · FastAPI backend on 127.0.0.1:<dynamic port>
   │                     · scheduler + scans (LinkedIn, HelloWork, FT web, …)
   │                     · AI queue, notifications (Windows toasts)
   │                     · tray icon (pystray, own thread): Open · Pause scans · Quit
   └─ opens ──────────── Window process (pywebview → WebView2, built into Windows 11)
                          closing it ends only the window; the agent keeps running
```

- **Why two processes:** the window can crash, close or update without stopping scans, and
  the agent survives the window. WebView2 is preinstalled on Windows 11 and
  evergreen on Windows 10, so no Chromium is bundled (unlike Electron). [my call]
- **Single instance and stale processes:** a named mutex plus a lock file
  `{pid, port, instance_token, install_path, version}`. The window connects only if `/api/health`
  returns the **same instance token**. Otherwise it treats the process as foreign or stale, never connects,
  and reports it. A dynamic port removes the fixed-8000 conflict. This builds on the existing launcher
  checks (`src/launcher/core.py`).
- **Start with Windows:** an HKCU `Run` key starting the agent minimized (no admin rights, removed
  by the uninstaller). A Windows service is **not** needed (scans must run as the user, with the
  user's network and profile).
- **Quit:** tray → Quit stops scans at the next checkpoint (≤ 5 s, as in v1 #37), then exits.
  Closing the window only hides it; a first-time toast explains "Still searching in the
  background — right-click the tray icon to quit".
- **Recovery:** scans resume from checkpoints after a crash or reboot. The scheduler catches up at most
  one missed run (no burst). The agent restarts itself once after a crash; if that fails, a toast
  links to the logs.
- **Notifications:** Windows toasts only for things the user opted into: "5 new strong
  matches", "an offer you saved expires tomorrow", "LinkedIn paused: rate-limited". The
  Discord-era notification engine is not reused.

### C8. Removals (D8)

Remove **Discord** (`src/notifications/discord*`, `src/discord_admin/`, the `mcp_admin` extra),
**n8n** (`src/notifications/n8n.py`, `src/api/auth.py`, the `/digest/*` and `/runs/{id}/jobs/n8n` routes,
`config/n8n_weekly_digest.json`, launcher `--n8n`, port 5678 in the lab block list, the sidebar
badge), the **weekly digest**, and **location-first notification routing** (ADR 0006 →
superseded).

**Must be re-homed first:** `location_match.py` (scoring needs it, rewritten by §C2),
`source_health.py` and `system_status_worker.py` (they feed Settings › Sources and the toasts),
and `notification_history` (keep the table, stop writing). The DB tables `department_notification_state`
and `notification_history` are left in place and dropped in a later migration, so no data is lost
silently.

Also remove: the `browser` extra (crawl4ai, inactive, an unaudited advisory), the WTTJ browser
collector, APEC, and `src/collectors/indeed.py` (replaced via jobspy).

### C9. Internationalisation

- **UI:** `i18next` + `react-i18next` with ICU message format, one JSON file per language, and
  English as the source and fallback [my call: the most widely used, with tooling for missing-key checks].
- **Backend strings:** the API returns **codes and parameters**, not sentences (rejection reasons,
  source errors, evidence items). `frontend/src/utils/rejectionLabels.ts` already does this
  partly. This change also makes explanations translatable.
- **CI:** a missing-key check, and a pseudo-locale build (`[!! Ëxpåñdëd !!]`) to catch hard-coded
  strings and overflow (German is about 30 % longer).
- **Translation of the catalogue:** first drafts by machine, then a human review per language
  before release. [owner: who reviews ES, IT and DE?]
- **Posting language ≠ UI language:** detect the posting language (a small offline detector) and
  store it. Filters: "Languages I can work in" comes from the profile's languages. An English posting
  is never hidden for a French user who reads English; for an unread language it is
  *flagged*, not dropped.

### C10. Evaluation, Quality page and feedback

- **Labels first.** The most valuable missing asset is **real labels**. Add a 10-minute
  "help calibrate" flow in Quality that shows 30 diverse offers (stratified across accepted,
  rejected, review and source) and asks *relevant / not relevant / not sure + why*. These
  labels, plus explicit feedback, form a **personal evaluation set**, frozen per snapshot.
- **Metrics in plain words:** "Of the offers you marked relevant, the app had rejected 0"
  (safety: unsafe-rejection rate); "Of the offers in Strong, you found 8 of 10 relevant"
  (precision@Strong); "Good offers appear in your top 20: 7 of 9" (recall@20, nDCG once
  graded labels exist).
- **Regression gate:** any strategy, model or app version runs the self-check on the frozen synthetic
  benchmark (CI) **and** on the personal set (locally). Promotion requires 0 new unsafe
  rejections, no drop beyond noise on precision@Strong (with a bootstrap CI shown), and a human click.
  Small samples say "not enough evidence yet" (this already exists as `insufficient_evidence`).
- **UI over the existing lab:** run self-check and evaluations as background jobs from the UI,
  list runs, compare two strategies, and browse disagreements (FP, FN, uncertain) with the decision record.
  Lab output stays *untrusted data* (ADR 0011). **Promotion moves from CLI-only to a UI action
  only when the sandbox is in use** [open question I3].
- **Feedback as evidence:** explicit labels count fully. Behaviour (opened, applied) is a weak
  positive. "Not interested" without a reason is not treated as "irrelevant". Personalization stays
  capped by the gates (as today) and only turns on after N ≥ 20 explicit labels [hypothesis
  threshold], and its effect is itself measured on held-out labels.

### C11. Explainability and provenance

Already present: the decision journal (evidence, quotes, strategy version, run id) and
feedback linked to decisions. Add: source provenance list per offer (all sources it was seen
on, first and last seen), extraction method per field (structured / rule / AI + model), and a
**plain-language explanation generated from codes** (works without AI; AI may rephrase it but
must keep every cited item). Show "Why this score" as the 5 factors with the user's own
words ("15 km from Toulon, within your 30 km").

### C12. Installation and distribution

- **Core** (always): bundled CPython, backend, built UI, ONNX embeddings and model, communes table, and
  jobspy. Target installer under 100 MB, installed under 400 MB [hypothesis, to be measured after C6].
- **Optional components** (downloaded on demand from the app, with size shown, resumable, and
  SHA-256 verified against a manifest shipped in the installer): translation pack, Ollama (official
  installer), and LLM models (via Ollama).
- **Updates:** keep the Inno Setup installer and add an in-app "update available" check against GitHub
  Releases (a download link only, verified with the published SHA-256). **Code signing** would remove the
  SmartScreen warning; it is an owner cost decision [I4].
- **Measure every release:** download size, installed size, install time, first window time and
  backend-ready time (the acceptance script already measures some of these).

### C13. Visual redesign (from the actual pages)

| Page today | What works | Problem | Proposal |
|---|---|---|---|
| Dashboard | Live run progress, per-source status with reasons | It is an engineering funnel (Collected/Validated/Deduplicated…), dev error text, and "n8n offline" | **Home**: new matches by confidence group, background status chips, "needs your judgement" queue. The funnel moves to Quality › Runs |
| Jobs | Score ring, chips, filters | "All departments" filter; "Exceptional" badge on a false positive; no save or compare | Distance and remote filters, saved views, compare tray, confidence group instead of a raw score first |
| Job detail | Decision record, "Things to check", status buttons | Engineer wording ("weight share 0.30", "sv-e0e8…", "agreement-based, not calibrated"); 9 status buttons in one row | Fit map with quotes, plain explanation, action plan, documents, a compact status stepper; the technical record goes in a collapsible "Details" |
| Profile | ESCO-backed skills | Raw keyword lists, bac+N, priority arrows, Discord/email text | A wizard-style profile in sections (About me, Experience, Education, What I want, Where), and CV import |
| Review | Disagreement queue | Unclear to non-technical users | Merged into Home's "needs your judgement" |
| Stats / Skills / Pipeline | Real data | Developer analytics | **Insights** (skills in demand vs yours: the skill graph exists) and **Quality** (runs, lab) |
| Lab | Honest about isolation | CLI instructions, empty states without actions | Quality page with buttons, plain metrics, and Advanced |

Design system first: tokens (colour, spacing, type scale), light and dark themes, and components
(cards, chips, stepper, toasts, empty, loading and error states with a recovery action). Replace the Vite
favicon and give the app real icon branding. Keyboard focus rings, `prefers-reduced-motion`, and 44 px targets.

---

## D. Keep, improve, replace, remove, defer

| Component | Verdict | Why |
|---|---|---|
| Deterministic pipeline, gates, ADR 0004 safety | **Keep** | Measured safety, tested |
| Decision and feedback journals, disagreement | **Keep, expose** | Provenance backbone |
| Lab, sandbox, self-check | **Keep, add UI** | Works; invisible to users |
| Installer | **Keep, slim** | Reliable (60/60) |
| Scheduler, scan checkpoints | **Keep, move into agent** | Already resumable |
| Semantic matching | **Keep model, replace runtime** (ONNX) | Size and start time |
| Location preferences (departments) | **Replace** (home + radius + remote typing) | UX, and the remote false positive |
| Experience keyword lists, bac+N | **Replace** (career stages, EQF levels) | UX, and evidence-based matching |
| AI Understanding (top-100 enrichment) | **Replace** (offer analysis and other features, §C4) | No measured gain; arbitrary cap |
| LLM eligibility gate | **Move to lab** | Unproven; may only reduce false negatives |
| France Travail API collector | **Keep (optional)** + **add web collector** | API closed to new users |
| Indeed own GraphQL client | **Replace** via jobspy | No third-party key handling |
| LinkedIn, HelloWork | **Keep as background, opt-in** | Top sources |
| APEC, WTTJ browser collector, crawl4ai | **Remove** | Blocked by CAPTCHA or WAF |
| ATS collectors | **Improve** (auto-follow companies) | Best data quality |
| Discord, n8n, weekly digest, ADR 0006 routing | **Remove** | Owner decision; simplifies |
| Launcher (browser open, fixed port) | **Replace** (agent + window + tray) | D3 |
| Pipeline/Stats/Skills pages | **Merge** into Insights and Quality | Audience |
| Docker files | **Defer** (keep for developers) | Not user-facing |
| Code signing, auto-update | **Defer, owner cost decision** | I4 |
| Discord, Telegram, email alerts | **Defer** (much later) | Owner |
| Travel-time distance, salary normalization | **Defer** | Nice to have |

---

## E. Architecture and integration design

### E1. Processes and modules

```
[Window: React UI, i18n] ──HTTP+SSE (loopback, instance token)──▶ [Agent]
Agent
 ├─ api/            FastAPI (existing routes minus n8n/digest; + profile v2, documents, ai, quality jobs)
 ├─ scheduler/      runs pipeline + background scans + AI queue (existing service, extended)
 ├─ sources/        plug-ins: official | public-page | unofficial; health + circuit breaker
 ├─ pipeline/       normalize → validate → dedupe → filter → gate → score → store (unchanged order)
 ├─ geo/            communes table, resolver, distance, remote-eligibility extractor   (new)
 ├─ profile/        v2 model + migration from v1 YAML                                 (new)
 ├─ ai/             runtime detection, hardware probe, catalogue, benchmark,
 │                  features: analysis, discovery, cv_import, documents, translate    (rebuilt)
 ├─ decisions/ feedback/ evaluation/ lab/                                             (existing)
 └─ desktop/        tray, toasts, autostart, single-instance                          (new)
```

### E2. Interfaces that change (contracts: each needs its own plan, §11.4)

1. **Profile model v2** (Pydantic + YAML + `PUT /api/profile`): adds experiences, education entries
   (qualification and level), target levels, home place, radius and remote policy; removes `location_preferences`,
   `excluded_seniority` and `max_education` (migrated). This bumps `strategy_version`.
2. **Job model**: add `geo` (commune code, lat/lon, precision), `remote_eligibility` (typed +
   evidence), `posting_language`, `provenances[]`, `expires_at`, `expired`.
3. **Source plug-in interface**: `access_class`, `robots_check()`, `rate_policy`, `health()`,
   `fixture()` for the parser canary; the collector still never raises.
4. **API**: remove the n8n and digest routes; add `/api/ai/*` (detect, hardware, catalogue, pull,
   benchmark, analyse, translate), `/api/documents/*`, `/api/quality/jobs` (start self-check or eval,
   status), `/api/sources` (health, enable/disable); all messages as codes + params.
5. **Decision journal**: new evidence kinds (distance, remote_eligibility, AI requirement with
   quote) using the existing `EvidenceItem` shape.
6. **Scoring output**: the location factor is redefined (distance-based). It needs a baseline
   re-record, done in its own PR.

### E3. Data flow of AI features

New offers stored → queue (ordered by score) → analysis (grounding checker) → stored as
`ai_analysis` v2 rows linked to `decision_id` → shown on the offer page; it can only add *review*
evidence. Liked offers → discovery job → queries and companies → sources and ATS probe → normal
pipeline (provenance `discovery:<id>`).

### E4. Security deltas

Dynamic port plus an instance token (no connection to foreign processes). The lab sandbox block list
gets the live port. Documents and CVs are stored under `data/` and never leave the machine. CV text is
personal data, so add an "erase my CV and generated documents" control. The Ollama installer and model
downloads come only from official URLs and are verified against hashes where published.

---

## F. Implementation backlog

Each item becomes a GitHub issue. **E** = essential for the update; **O** = optional in
this update; **F** = future. "Verify" lists the acceptance check.

### Release tiers (owner clarification 0b)

- **R, release-blocking:** the 2.0 release does not ship without it. F0.1, F1.1–F1.4,
  F2.1–F2.5, F3.1, F4.1–F4.3, F5.1–F5.4, F6.1–F6.6, F7.1, F7.2 (EN + FR only), F8.1–F8.4,
  F9.1, F10.1, F10.2, F10.4, F11.1.
- **T, release-targeted:** planned for 2.0 and shipped behind its own toggle when its acceptance
  criteria are met; otherwise it moves to 2.x without blocking: F0.2 (needs the owner's labels),
  F0.3, F5.5, F7.2 (ES/IT/DE beta), F7.3, F8.5, F8.6, F8.7, F9.2, F9.3, F10.3.
- **O/F, optional or later:** F6.7–F6.9, F8.8, F8.9, F9.4 (closed by I3: CLI-only), F11.2,
  F11.3.

A tier never relaxes an acceptance criterion: a feature either meets its criterion or is reported
as not done.

### F0. Baselines and guardrails (do first)

| ID | Item | Verify |
|---|---|---|
| F0.1 E | Record baselines: installer size, install size, cold and warm start, run time, per-source yield, self-check | Numbers committed to `docs/baselines/` |
| F0.2 E | Personal labelling flow (30 stratified offers) and a frozen personal eval set | The owner labels ≥ 100 offers; the self-check reports on them |
| F0.3 E | France Travail web coverage benchmark vs the 10,965 API offers in the owner DB (same occupations and départements, 7 days) | Report: % of API offers also found by the web collector |

### F1. Removals

| F1.1 E | Remove Discord, `discord_admin`, the `mcp_admin` extra | Suite green; grep shows no `discord` in `src` except migrations |
| F1.2 E | Remove n8n, digest routes, auth, config, launcher flag, sidebar badge, port 5678 | Same; launcher tests updated |
| F1.3 E | Re-home source health and system status for Settings and toasts | Settings shows source health |
| F1.4 E | Remove APEC, the WTTJ browser collector and the crawl4ai extra; update the tripwire test | Suite green |

### F2. Desktop agent and window

| F2.1 E | Agent process: backend + scheduler + tray, dynamic port, lock file, instance token | Tests: second launch reuses; foreign process on the port is refused; stale lock recovered |
| F2.2 E | pywebview window process; close = hide; tray Open, Pause and Quit | Manual + automated: scan continues after the window closes (process alive, scan progress advances) |
| F2.3 E | Start with Windows (HKCU Run), removed on uninstall | Acceptance script checks the key |
| F2.4 E | Windows toasts (opt-in categories) | Toast fires on a fixture run |
| F2.5 E | Crash restart and checkpoint resume after reboot | Kill the agent mid-scan; it resumes without duplicate jobs |

### F3. Embedding runtime

| F3.1 E | ONNX Runtime embeddings, same model; re-embed in background | Spearman ≥ 0.99 on 1,000 jobs; self-check identical; installer size measured |

### F4. Location

| F4.1 E | Communes table + resolver + distance | Unit tests on 50 known pairs; resolver ≥ 95 % on 500 real FT locations |
| F4.2 E | Remote-eligibility extractor (typed + evidence) | Labelled set of 100 remote postings: ≥ 90 % correct, **0 "remote_france" for restricted-elsewhere offers** (includes the HK case) |
| F4.3 E | Scoring and filter on distance + remote policy; `unknown` ≠ preferred | Unsafe-rejection gate green; baseline re-recorded in its own PR |

### F5. Profile v2 and onboarding

| F5.1 E | Profile v2 model + v1→v2 migration + strategy bump | Migration tests on 3 fixture profiles; round-trip |
| F5.2 E | Career stages + level inference with multi-signal evidence | Benchmark: title-only conflicts go to review, never reject |
| F5.3 E | Education levels (FR nomenclature = EQF 3–8), foreign degrees by EQF | Tests on the ADR 0004 cases + new qualification names |
| F5.4 E | Onboarding wizard (language → CV or questions → targets → place → sources → AI → background) | Usability test: 3 non-technical users finish in under 10 min |
| F5.5 E | CV import (PDF and DOCX text; rules fallback; AI when available) | 10 sample CVs: ≥ 80 % of fields correct before user edits |

### F6. Sources

| F6.1 E | Source plug-in contract (access class, robots, rate, health, circuit breaker, canary) | All collectors migrated; a canary detects a changed fixture |
| F6.2 E | France Travail web collector (sitemap-driven, robots-respecting, incremental) | F0.3 coverage report; ≤ 1 req/s; zero requests to disallowed paths (test) |
| F6.3 E | Indeed via jobspy (opt-in) | Fixture + live smoke (network mark) |
| F6.4 E | LinkedIn and HelloWork in the background agent, opt-in, adaptive pacing | A 429 pauses and resumes; no burst after resume |
| F6.5 E | Unofficial-sources consent (wizard + Settings, plain text, per source) | Off unless chosen; consent recorded |
| F6.6 E | Expiry + cross-source provenance merge | Duplicate FT web/API/EURES shown once with 3 provenances |
| F6.7 O | ATS auto-follow (company → probe Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee, Teamtailor) | ≥ 50 % of companies with a known ATS are followed automatically |
| F6.8 O | La Bonne Alternance (token-guided), public-sector source | Live smoke |
| F6.9 O | LinkedIn or other job URL paste → single fetch | Works for the 4 main boards |

### F7. Internationalisation

| F7.1 E | i18next setup, string extraction across all pages, API codes + params | Pseudo-locale shows no untranslated UI string |
| F7.2 E | EN + FR complete; ES, IT, DE machine draft + human review | Missing-key CI = 0 |
| F7.3 E | Posting-language detection + language filter | 95 % accuracy on 200 postings |

### F8. AI platform and features

| F8.1 E | Runtime detection (Ollama, LM Studio, OpenAI-compatible loopback) + hardware probe | Tests with mocked servers; probe on 3 machines |
| F8.2 E | Model catalogue + guided Ollama install + pull with progress, cancel and resume, and consent | Nothing downloads without a click showing the size; resume after network cut |
| F8.3 E | Verify + benchmark + plain verdict | Slow or invalid model → recommendation shown |
| F8.4 E | Offer analysis with a grounding checker; queue with budget; retire Phase 2 enrichment | Grounding rate ≥ 95 %; 0 rejections caused by AI; per-offer latency recorded |
| F8.5 E | Tailored CV + cover letter (DOCX + PDF), fact checker, never invent | 20 offers: 0 unsupported claims after the checker; the user can edit before export |
| F8.6 E | More-like-this discovery + yield tracking | Lab report: new relevant offers per discovery run vs baseline expansion |
| F8.7 E | On-demand translation (LLM) with cache, original always visible | Cached second open is under 100 ms |
| F8.8 O | Offline translation pack (CTranslate2) | Quality check on 50 postings vs LLM; size measured |
| F8.9 O | LLM eligibility gate as a lab experiment | Promotion only if FN↓ and unsafe = 0 |

### F9. Quality (lab UI) and feedback

| F9.1 E | Quality page: health, run self-check and eval from the UI, history, plain metrics | No terminal needed for any listed action |
| F9.2 E | Disagreement browser (FP, FN, uncertain) using the decision records | Opens any rejected offer's snapshot + evidence |
| F9.3 E | Strategy comparison + regression verdict | Two strategies compared on the same frozen set |
| F9.4 O | UI promotion (only when the sandbox is enforced) | Depends on I3 |

### F10. Visual redesign

| F10.1 E | Design system (tokens, light/dark, components), branding, favicon | Storybook-like page or tests; contrast checks pass AA |
| F10.2 E | Home, Jobs (filters, saved views, compare), Offer page, Applications tracker | Usability test tasks (find, compare, apply) |
| F10.3 E | Settings (sources, AI, language, background, data), Insights, Quality | All settings previously YAML-only (needed by users) are editable in the UI |
| F10.4 E | Empty, loading and error states with recovery everywhere | Checklist per page |

### F11. Packaging

| F11.1 E | Slim core installer, optional components manifest (SHA-256), resumable downloads | Installer < 100 MB [target]; acceptance passes |
| F11.2 O | Update check | Detects a newer release, verifies the hash |
| F11.3 F | Code signing | Owner decision I4 |

---

## G. Implementation order and migration strategy

Ordered by dependency. The app stays releasable after every block.

1. **F0** (baselines, labels, FT coverage benchmark). Everything later is judged against these.
2. **F1 removals** + **F3 ONNX**: independent, shrink the surface and the weight, low risk.
   They can run in parallel.
3. **F2 desktop agent**: independent of the matching work. Lands early so background scans exist
   before more sources move there.
4. **F10.1 design system** + **F7.1 i18n skeleton**: before any page is rebuilt, so new
   pages are born translatable and consistent (avoids redoing every page twice).
5. **F4 location** → **F5 profile v2 + onboarding**: location first (smaller contract), then
   the profile migration that uses it. Each is its own PR with a baseline re-record.
6. **F6 sources**: the plug-in contract first, then FT web (informed by F0.3), Indeed via jobspy,
   LinkedIn and HelloWork in the agent, consent, expiry and provenance.
7. **F8 AI**: platform (detect, install, benchmark) first, then analysis → documents → discovery →
   translation. Each feature behind its own toggle and measured in the lab.
8. **F9 Quality UI**, then **F10.2–F10.4** page rebuilds (they consume everything above).
9. **F7.2 translations** + **F11 packaging** + full acceptance → release 2.0.

Migration rules: profile v1 → v2 is automatic, with a backup file and a one-time review screen. DB
changes are additive (new columns or tables); old tables are dropped only one release later. Each
contract change gets its own PR (CLAUDE.md §11.4), with refactors separate from behaviour changes.

---

## H. Testing and success criteria

| Dimension | Criterion (baseline → target) |
|---|---|
| Safety | Unsafe rejections on the synthetic benchmark = 0 (today 0) **and** on the owner's personal set = 0 |
| Matching quality | precision@Strong on the personal set ≥ the v1 value measured in F0.2 (not a made-up number); recall@20 reported with a CI |
| Remote false positives | 0 offers restricted to another country labelled remote_france (today ≥ 1 at rank #1) |
| Fresh-install yield | With no credentials and unofficial sources **off**: France Travail web offers present (today 0). With them **on**: LinkedIn, Indeed and HelloWork present |
| Source robustness | One source failing → the run completes; a changed page → canary error, never a silent 0 |
| Background | Window closed for 1 h → scans progressed; reboot → resumes; Quit → no process left (acceptance script) |
| Startup | First window ≤ 3 s; backend ready warm ≤ 10 s, cold ≤ 25 s (today 20 s and 41–76 s) [targets to confirm after F3] |
| Size | Installer and installed size measured each release; target < 100 MB and < 400 MB |
| AI grounding | ≥ 95 % of AI requirement items carry a valid quote; 0 invented facts in generated CVs (checker + 20 manual reviews) |
| AI cost | Seconds per offer reported by the benchmark; analysis never blocks a run |
| i18n | 0 missing keys in 5 languages; the pseudo-locale walkthrough is clean |
| Accessibility | axe-core: 0 serious issues; keyboard-only walkthrough of onboarding and apply |
| Security | Existing probes (Host, Origin, loopback, traversal) + instance-token test + no outbound request carrying profile data (test with a recording proxy) |
| UX | 3 non-technical users: onboarding < 10 min, find and save 3 relevant offers < 5 min, unaided |

All existing suites stay green (`pytest -m "not network"`, frontend lint, typecheck and vitest, audits).

---

## I. Risks, open questions, assumptions

### Open decisions for you

**Resolved on 2026-10-23 with the defaults in §0c.** The original questions are kept below for
the record.

- **I1. Unofficial sources default.** I recommend **opt-in in the wizard** (one switch, one
  plain sentence), since they breach those sites' terms and the user, not us, should decide. You asked
  me to find every possible method first. §C1 is the result: no credential-free *permitted*
  method exists for LinkedIn or Indeed; France Travail has one (public pages). Confirm opt-in vs
  on-by-default.
- **I2. France Travail partner access.** Should the project also apply via francetravail.io's
  contact form for partner access? It cannot be embedded in a public app, but it would let us run
  coverage comparisons properly. Optional.
- **I3. Lab promotion in the UI.** Today only the CLI can promote (an agent cannot). Allow a UI
  button when the sandbox is enforced, or keep the CLI only?
- **I4. Code signing** (a yearly cost) and auto-update: now or later?
- **I5. Who reviews ES, IT and DE translations** before release?
- **I6. Generated documents**: DOCX + PDF both, or PDF only? Any template style preference?

### Risks

- **Site changes and blocking** (LinkedIn, Indeed, HelloWork, FT web). Mitigation: canaries,
  circuit breakers, opt-in labelling, EURES and ATS as stable backbones.
- **Legal exposure** of shipping unofficial collectors in a public repo (takedown requests).
  Mitigation: opt-in, no bypass of protections, an easy kill-switch per source in a remote-free
  config, and README disclosure.
- **Local LLM quality in French** at small sizes. Mitigation: the benchmark gates the feature; the
  deterministic fallback stays.
- **Generated CVs overstating the user.** Mitigation: the fact checker, mandatory user review, and
  never auto-sending.
- **Scope**: this is a 2.0. The ordering in §G keeps each step shippable.

### Assumptions to validate (hypotheses listed above)

FT web coverage; ONNX equivalence and size; road-distance proxy; translation-pack quality;
model tiers; HelloWork robots status; personalization threshold.

---

## J. Definition of done

A new user downloads one installer (size shown on the release page, SHA-256 published),
installs without admin rights, and sees a window in their language within seconds. In under 10
minutes they import a CV or answer a few questions, confirm their profile, set a town and a
distance, choose whether to include LinkedIn, Indeed and HelloWork, and either set up local AI
with a clear recommendation and visible download progress, or skip it.

The first search shows live progress per source. France Travail offers arrive without any
credentials. Results are grouped *Strong / Worth a look / Needs your judgement*. A remote job open
only to US residents is never presented as "Remote France". Every offer shows where it was found,
which requirements are met with quoted sentences, and what is uncertain. With AI, it also shows an
action plan, a tailored CV and letter built only from their real profile, and a French translation
next to the original.

Closing the window keeps the app in the tray. Scans continue, a notification announces new strong
matches, and the app can start with Windows. Quitting from the tray leaves nothing running.
After a crash or reboot, scans resume without duplicates.

The user can mark offers relevant or not and track applications. The Quality page shows, in plain
words and without a terminal, whether matching is safe and whether a change made it better or
worse, based on their own labels. Experiments never change the active profile, database or matching
without an explicit human approval.

Discord, n8n and the weekly digest are gone. The app works entirely offline-capable, with no cloud AI.
Every test suite, audit and the installer acceptance script pass, and each success criterion
in §H is met or explicitly reported as not met.

---

## Sources

- data.gouv.fr, *API Offres d'emploi* (suspension notice): https://www.data.gouv.fr/dataservices/api-offres-demploi
- France Travail CGU (offers transmitted to EURES; reuse licence): https://www.francetravail.fr/informations/informations-legales-et-conditio/conditions-generales-dutilisatio.html
- France Travail robots.txt and sitemaps (fetched 2026-10-23): https://candidat.francetravail.fr/robots.txt
- morvanvivien/francetravail-scraper (listing pages, about 20 per page, `motsCles` disallowed): https://github.com/morvanvivien/francetravail-scraper
- Zeffut/JobScraper (French aggregator; WTTJ via Algolia; Adzuna key): https://github.com/Zeffut/JobScraper
- JobSpy docs (LinkedIn guest endpoint, rate limits, Indeed without limits, about 1,000 cap): https://mintlify.wiki/speedyapply/JobSpy/troubleshooting · https://mintlify.wiki/speedyapply/JobSpy/guides/proxies
- LinkedIn scraping and terms (hiQ outcome): https://jobspipe.dev/answers/can-i-scrape-linkedin-jobs
- Indeed Publisher API status: https://jobspipe.dev/blog/indeed-publisher-api
- EURES data flow from France Travail: https://www.francetravail.fr/international/mobilite-internationale/travailler-a-letranger/eures-la-mobilite-en-europe.html
- EURES API access is for members and partners: https://eures.europa.eu/data-protection-statements/data-protection-statement-and-specific-terms-and-conditions-use-eures-portal-services_pt
- API Adresse moved to IGN Géoplateforme: https://www.data.gouv.fr/posts/lapi-adresse-de-la-base-adresse-nationale-est-transferee-a-lign-10
- La Bonne Alternance API terms: https://api.apprentissage.beta.gouv.fr/en/cgu
- LM Studio CLI: https://lmstudio.ai/docs/lms
- Quantized ONNX MiniLM (fastembed): https://huggingface.co/Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q
- Locally Uncensored first start (UX reference): https://locallyuncensored.com/guide/first-start/
