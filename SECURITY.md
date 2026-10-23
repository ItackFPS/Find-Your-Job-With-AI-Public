# Security policy

## Reporting a vulnerability

Please report security problems **privately**, through GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability):
the repository's **Security** tab → **Report a vulnerability**. Do not open a public
issue, discussion or pull request for a security problem.

Include what you found, how to reproduce it, the affected commit or version, and its
impact as you understand it. Never include real credentials, personal data or someone
else's job-search data in a report.

This is a personal, volunteer-maintained project: expect an acknowledgement within a
couple of weeks, not hours. There is no bug bounty. Fixes land on `main`; there are no
maintained release branches.

## Scope and threat model

Find Your Job With AI is a **single-user application that runs on your own computer**.
Its security model, in short:

- **The backend trusts the local machine.** It listens on `127.0.0.1` only and has no
  login. It defends against *web pages* you visit (it refuses foreign `Host` headers —
  DNS rebinding — and cross-site write requests), not against other programs running on
  the same computer. `API_SAME_USER_ONLY=1` additionally refuses processes of other
  Windows accounts. Exposing port 8000 to a network is unsupported.
- **Secrets live in `.env` only** (for example France Travail credentials). They are never written to tracked files or logs by the
  application; `.env`, `config/*.local.yaml`, `data/` and `logs/` are git-ignored.
- **Collected job postings are untrusted input.** They are parsed, stored and rendered
  as text; the dashboard never renders posting HTML.
- **The optional local LLM is untrusted.** AI stages can enrich results but cannot make
  a job eligible on their own and fail open to the deterministic result
  ([ADR 0001](docs/adr/0001-deterministic-gates-before-optional-ai.md)). The LLM
  endpoint is user-configured; there is no cloud fallback.
- **The experiment lab is untrusted** ([ADR 0011](docs/adr/0011-experiment-lab.md)): its
  output is data, production strategy changes need explicit human approval, and the lab
  API is read-only. OS-level isolation of an autonomous lab agent is still in progress
  and is not a security guarantee yet.

In scope: anything that lets a web page, a collected job posting, a model response, lab
output or another local account read or change your data or configuration, run code,
or leak secrets; secrets or personal data committed to the repository; unsafe defaults
in the scripts, launcher or CI workflows.

Out of scope: attacks that require running code as your own user account, exposing the
backend to a network yourself, and the security of third-party services (job sites,
LM Studio/Ollama) themselves.

## If a secret was committed

Treat it as compromised even if the commit was removed: **revoke or rotate it first**,
then remove it from the branch with a new commit. The CI secret scan
(`.github/workflows/secret-scan.yml`, Gitleaks) checks every pull request and push to
`main`.
