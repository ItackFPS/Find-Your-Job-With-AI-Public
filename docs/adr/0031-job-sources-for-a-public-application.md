# 0031 — Which job sources a public application can rely on

Status: accepted for France Travail and the employers' boards (2026-11-09). The removal of LinkedIn,
HelloWork and Indeed awaits the owner's decision; until then they stay opt-in, off by default.

## Context
The owner asked for a source strategy usable by a public application without each user creating
private API credentials. Sources were checked on 2026-11-09.

## Findings
**France Travail.** The API "Offres d'emploi" is "plus exposée" on francetravail.io while its
distribution and reuse are reviewed, citing massive automated use by AI systems; access is
restricted and requested through a contact form (data.gouv.fr dataservice). A key shared by every
user of an application cannot be obtained or justified in that state. The site's terms
(`conditions générales d'utilisation`, §3.2) say offers "pourront être réutilisées pour tout usage et
notamment être rediffusées sur des sites tiers" and contain no clause against automated access.
`robots.txt` allows the occupation pages and offer pages, and forbids `motsCles=` and `t:ac=`. The
full text of an offer is loaded through `t:ac=`, so only a roughly 200-character extract is available.
The sitemap publishes 219 occupations, so this route is thin for lines of work outside them.

**LinkedIn.** The User Agreement (§8.2) forbids scraping and bots and binds visitors (§1.2). The only
jobs API is for posting, for approved partners, and LinkedIn is "not currently accepting new
partnerships". No official way to search offers exists. Observed: a scan open since 2026-11-08 had
found 0 offers, stopped by HTTP 429.

**HelloWork.** Its CGU treat automated software as illegitimate users (art. 1) and prohibit
extraction by software or scraping without written licence (art. 9.2), under the database right
(art. 9.3). `robots.txt` does not forbid offer pages, but the terms do. Observed: 46 minutes for 1,275
offers found and 35 accepted.

**Employers' tools.** Greenhouse documents that "authentication is not required for any GET endpoints";
Recruitee's offers endpoint declares no security; Lever, SmartRecruiters and Ashby publish equivalent
read APIs. Observed on boards found in one real database: 25 boards, 23 answered, 857 offers in
France, 174 in the profile's domain, in 47 seconds.
In a full pipeline run: 3,873 offers collected in about a minute, 120 accepted.

**Database right.** The CJEU (C-762/19, CV-Online Latvia) allows a database maker to prohibit
extraction by a job search engine when it harms the investment. A tool running for one person is a
weaker case than a commercial engine, but a tool published to do it at scale is not a safe footing.

## Decision
- France Travail stays, without per-user credentials: the public occupation pages are the default and
  the API is used only where someone already holds access. Asking France Travail for an application
  access is an action for the owner (`francetravail.io/contact`).
- Employers' boards are a default source, found from links in collected offers.
- RemoteOK, Jobicy and We Work Remotely leave the product (owner decision).
- A collector never circumvents a block: on a 429 or a challenge it stops and waits.

## Consequences
Offers known only through France Travail's public pages have an extract, not a full text, so their
skills and requirements are read as unknown. Coverage from employers' boards grows with what other
sources find and is thin for employers without one of these tools.
