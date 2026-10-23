# Contributing

Thanks for your interest. This is a personal project made public; contributions are
welcome, but the maintainer's time is limited and the project has firm design rules.
Please open an issue to discuss anything larger than a small fix before writing code.

## Ground rules

- **Read the design first.** [README](README.md), [docs/architecture.md](docs/architecture.md)
  and the [ADRs](docs/adr/README.md) describe decisions that are deliberate: deterministic
  gates decide eligibility and AI stages only enrich and fail open; the stored score is
  the deterministic score; strategy changes go through the lab and human approval. A
  change that contradicts an ADR needs a new ADR, discussed first.
- **Local-first.** No paid or cloud inference, telemetry, analytics or new external
  services. New network calls must be optional, documented in the README's
  *Privacy and network behaviour* table, and fail gracefully.
- **No personal data.** Never commit `.env`, `data/`, `logs/`, `config/*.local.yaml`,
  real profiles, real job-search data, Discord IDs, e-mail addresses or machine paths.
  Tests use synthetic data and temporary databases; `tests/conftest.py` fails any test
  that touches the real `data/` or `config/`, and
  `tests/test_no_personal_profile_in_tests.py` keeps tests off the profile.
- **Small, focused changes.** One issue per branch and pull request; no drive-by
  refactors or reformatting mixed with behaviour changes.
- **Old issue numbers.** References such as "issue #9" in code comments and docs predate
  this repository and point to the private tracker it was developed in, not to issues here.

## Setup

```bash
git clone https://github.com/ItackFPS/Find-Your-Job-With-AI-Public.git
cd Find-Your-Job-With-AI-Public
python -m venv .venv            # Python 3.12
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
git config core.hooksPath .githooks   # blocks direct pushes to main
cd frontend && npm ci
```

On Windows, `FindYourJob.cmd dev` runs the backend and the Vite dev server together;
elsewhere run `uvicorn src.api.app:app --host 127.0.0.1 --port 8000` and
`cd frontend && npm run dev`. More in [docs/development.md](docs/development.md).

## Checks

Run these before opening a pull request — they are what CI runs:

```bash
pytest -m "not network"          # stop a running local backend first
ruff check src tests             # advisory: don't add findings (baseline docs/baselines/ruff.md)
mypy src                         # advisory: don't add errors (baseline docs/baselines/mypy.md)
cd frontend && npm run lint && npx tsc -b && npx vite build && npm test
```

Start with the smallest relevant test file (`pytest tests/test_<area>.py -q`), then the
full suite. Tests marked `network` hit live sites; run them only when working on a
collector. Never skip, xfail or weaken an existing test to get a green run.

## Workflow

1. An issue describes the problem, context and acceptance criteria.
2. Branch from an up-to-date `main`: `<type>/<issue>-<short-slug>` (types `feat`, `fix`,
   `refactor`, `perf`, `test`, `docs`, `chore`, `ci`).
3. Commit in small green steps with [Conventional Commits](https://www.conventionalcommits.org):
   `type(scope): imperative summary`, scope = the `src/` package or `frontend`, `ci`, …
   Tests go in the same commit as the change they cover.
4. Open a **draft** pull request with the template, `Closes #<issue>`, and the exact
   tests you ran with their results. Mark it ready when CI is green and you have
   re-read the whole diff.
5. Pull requests are squash-merged; the title becomes the commit on `main`.

Changes to a contract — a Pydantic model, the database schema, a config schema, an API
request/response, scoring output, a notification payload — need the plan discussed in
the issue first, and must update every caller and consumer.

## Collectors

Collectors talk to unreliable external sites: handle timeouts, HTTP errors and
malformed responses; never let one source crash a run; respect the site's terms and
rate limits. Pipeline tests use mocked data, not live sites.

## Security

Report vulnerabilities privately — see [SECURITY.md](SECURITY.md).

## Licence

By contributing you agree that your contribution is licensed under the
[MIT licence](LICENSE).
