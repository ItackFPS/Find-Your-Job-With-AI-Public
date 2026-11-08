# Safe passive personalization for a single-user job recommender — research package

Date: 2026-11-06. Repository: `ItackFPS/Find-Your-Job-With-AI-Public`, read at `origin/main` 94578c6.
Status: **research only**. Nothing in this package changes code, schema, configuration, datasets,
flags, the L5 pre-registration or ADR 0019. L3 stays SHADOW, activation stays OFF, L6–L8 stay
blocked. Every recommendation below needs owner approval.

Labels used throughout:

- Provenance of a source claim: **[V]** primary source retrieved and the claim verified in it;
  **[V-META]** bibliographic record/abstract verified, full text not examined; **[SECONDARY]**
  secondary source only; **[UNVERIFIED]** not independently verified. A suffix says who read it:
  `main` (re-opened by the main session, passage checked in raw `pdftotext` output or raw HTML) or
  `agent` (read in full by a research sub-agent only).
- Kind of statement: **Evidence** (sourced), **Repo** (read in the code at 94578c6),
  **Experiment** (run for this package, §4.4), **Inference** (reasoning from the above),
  **Hypothesis** (unproven), **Owner decision** (needs approval).

---

## 1. Executive summary

**What the research establishes**

1. **False activation fails in three scenario families, for three different reasons.** (Repo +
   Experiment.) The reported 21.6 % is one cell (SR-1 β = 2, high activity, ρ = 0, with G3b and a
   per-session cap of 1); the baseline worst is 7.2 % and the SR-2 worst 13.0 % (L3 report).
   - *Optional stopping (baseline).* G3 re-checks a Beta posterior after every session. On the
     repo's own generator, G3 alone (≥ 12 discordant, cap 3, no G3b) activates a null challenger
     in **16–25 %** of 180-day histories. A valid sequential test (mixture SPRT / e-process) on the
     same data stays at **2.5–3.1 %**.
   - *Wrong estimand (SR-1).* The challenger genuinely wins more than half of the
     production-favouring (PF) positives. A pooled test — however valid — is *right* that the
     pooled win rate exceeds ½: the valid pooled e-process "activates" in **23–51 %** of SR-1
     histories (51.3 % in the 21.6 % cell: worse than the current rule with G3b). **No test on
     pooled data fixes this.** A valid test restricted to randomized evidence does; it is what G3b
     was trying to be, made valid and sufficient.
   - *Dependence (SR-2).* Copied outcomes break the conditional-mean null that every anytime-valid
     guarantee rests on: the valid pooled e-process rises to **4–20 %** (session units: 3–11 %).
     Copying in the generator is stratum-blind, so **randomized exposure does not remove outcome
     dependence**; only the unit/cluster definition can.
2. **Anytime-valid inference is real and deployed, but its guarantee is narrow.** It holds at
   any stopping time if, under the null, each new observation has conditional mean ≤ ½ given the
   past (Evidence: Howard et al. 2021; Waudby-Smith & Ramdas 2024; Ramdas et al. 2023). It is the
   statistical core of Optimizely's and Adobe's A/B platforms (Evidence). It does not create
   power and does not remove bias in what is measured.
3. **The least-biased evidence FYJ has is too thin to activate on, at the simulator's volumes.**
   The LB stratum (S4 plus user-chosen date/distance-sorted search) yields about 5–13 discordant
   units in 180 days at typical activity. A valid test on it rarely fires at all — its low false
   activation is mostly *zero power*, not safety — and a genuinely better challenger (0.70)
   activates in **1–14 %** of histories. Even an oracle fixed-n exact test at the expected unit
   count would have power **0.17–0.42** at typical activity (0.31–0.64 at high). At ~1–2 LB units
   a month, 37–42 units (0.70) take roughly **1.5–4 years** (Experiment, §4.4–4.5). These volumes
   are the generator's placeholders, not measured ones.
4. **Production systems use FYJ's model structure but validate it with populations.** LinkedIn's
   GLMix and Dionysius are global/profile prior + per-user deviation with posterior shrinkage,
   deployed in job recommendations (Evidence). Their prior variances were tuned on millions of
   users and their launches validated by population A/B tests (Evidence). No source found studies
   per-user activation gating for one user.
5. **Within-user causal evaluation exists (interleaving) and is the only lever found that
   changes the order of magnitude.** Team-draft interleaving is paired and position-fair
   (Evidence, shown for clicks in web search). Every positive-bearing interleaved session becomes
   a unit: about 11 a month at typical activity, 5–10× the LB rate, diluted where the two lists
   agree (Inference). Its power at FYJ volumes is unsimulated.

**Conclusion.** At the simulator's placeholder volumes, with honest ≤ 5 % false activation, a
*statistical activation gate on passive shadow data* cannot reach useful power: pooled evidence
measures the wrong thing, and unbiased evidence is too scarce even for an oracle test. More
tuning of G3 cannot change that; more randomized volume, or real volumes far above the grid,
could. **Recommendation:** keep ranking personalization off (Option A) and improve
instrumentation. If user-visible personalization is wanted, extend the **confirmed-suggestion
path that ADR 0017/0019 already define** (Option D), with pre-registered triggers and a
false-suggestion budget. If ranking influence is wanted later, the only defensible route found is
**randomized evidence (Découverte, a randomized challenger slot, or team-draft interleaving) + a
valid sequential test on session/cluster units**, simulated first. Continuous ungated influence
(Option B) is not recommended — on INV-6 and the owner's evidence-first rule, not on the
literature, which neither supports nor refutes its safety for one user.

**What remains uncertain:** real event volumes and dependence (no owner replay yet); whether
the owner's real behaviour resembles SR-1/SR-2; interleaving power and user cost in FYJ's UI;
the relative value of save/apply/interview as preference evidence (no source measures it).

---

## 2. Scope and method

**Question.** Which passive preference-learning methods are statistically defensible and
implementable for FYJ (one user, sparse delayed feedback, changing inventory, authoritative
deterministic baseline, local-first)? Deliverable: evidence for an architecture decision.

**Non-goals.** No implementation, no parameter tuning, no change to L5 or to evaluation data.

**First-party evidence read (Repo).** ADR 0014, 0016, 0018, 0019, 0020;
`docs/plans/2026-11-05-learning-architecture-decisions.md` §4.5; `src/learning/{config,model,
gates,simulator,exploration,shadow,health}.py`; `src/feedback/exposure.py`;
`config/personalization.yaml`; `docs/reports/2026-11-05-l3-simulator.md`.

**Research questions.**

| # | Question | Who |
|---|---|---|
| Q1 | What makes repeated testing safe, under which assumptions; do they hold for FYJ? | main |
| Q2 | What do production recommenders do for per-user effects, exploration, evaluation? | agent 1 |
| Q3 | How reliable is each passive signal; item-level vs general preference? | agent 2 |
| Q4 | Are interleaving, OPE, safe bandits usable at n = 1? | agent 3 |
| Q5 | How does each option fare on FYJ's own generator? | main (experiment) |
| Q6 | How would options fit the current code? | main (repo) |

**Search strategies.** Academic route (arXiv API + full PDFs), official engineering pages,
challenge sites, GitHub (`gh api`), failure-mode searches ("peeking", "optional stopping problem
for Bayesians", "degenerate feedback loops"). Agents returned claim ledgers; the main session
re-opened every decisive claim (§9).

### 2.1 The search module (step 1 of the brief)

The "newly installed search module" is the `research` skill installed 2026-11-06
(`~/.claude/skills/research/`, with `web-researcher` and `research-reviewer` agents). It has no
search engine of its own; it orchestrates these tools:

| Capability | Tool | Finding |
|---|---|---|
| Web search | `WebSearch` (standard/extended) | US-only; returns titles + URLs and a model-written summary. No dates or authors as structured metadata. Snippets are leads only: one summary mis-attributed a paper's venue. |
| Page reading | `WebFetch` | Converts a page to markdown and answers through a small model, so quotes are paraphrase-prone. 403 on Medium/Netflix blog, ACM DL, Springer. |
| Full text, verbatim | `curl` + `pdftotext` / PyMuPDF (Bash) | Works for arXiv and author PDFs. **Used for every [V] main check**: passages grepped from raw text. |
| Bibliographic metadata | arXiv API (`https://export.arxiv.org/api/query`), Crossref API | Work. Semantic Scholar API returned 429 without a key, so citation-graph / reference discovery is limited to reading reference lists. |
| Code / repos | `gh api` | Works (XING challenge repo, Photon ML README). |
| Patents | none dedicated | Not covered; not needed for this decision. |
| Provenance | skill rules | Discovered ≠ inspected; agent claims stay `agent` until re-opened; evidence ledger with weight and check columns. |

Reproducibility: every source below has a stable URL or DOI; quoted passages can be re-found by
grepping the raw PDF text. Search results themselves are not reproducible (engine-side ranking).

---

## 3. Verified source catalogue

Ordered by how much the decision depends on them. "Shows / does not show" is what the source
supports for FYJ, not a summary of the paper.

### 3.1 Sequential inference (decisive)

| Source | Type | Prov. | Shows | Does not show | FYJ relevance |
|---|---|---|---|---|---|
| Howard, Ramdas, McAuliffe, Sekhon. *Time-uniform, nonparametric, nonasymptotic confidence sequences.* Ann. Statist. 49(2), 2021. arXiv [1810.08240](https://arxiv.org/abs/1810.08240) | theory | [V] main | CS coverage holds "for all stopping times" (Lemma 3 discussion); estimand is the *average conditional expectation* given a filtration; built on Ville's inequality for nonnegative supermartingales. | Anything about recommender data or dependence beyond the martingale structure. | Foundation for replacing G3. |
| Ramdas, Grünwald, Vovk, Shafer. *Game-theoretic statistics and safe anytime-valid inference.* Statist. Sci., 2023. arXiv [2210.01948](https://arxiv.org/abs/2210.01948) | review | [V] main | e-processes and CS "remain valid at all stopping times"; "a bet on an outcome must be made before the outcome is observed"; averaging e-values is valid under unknown dependence (citing Vovk & Wang). | Small-sample power. | Rules for bets, delayed outcomes, combining metrics. |
| Waudby-Smith, Ramdas. *Estimating means of bounded random variables by betting.* JRSSB 86(1), 2024. arXiv [2010.09686](https://arxiv.org/abs/2010.09686) | theory | [V] main | Null class = all distributions on [0,1] with `E(X_t | X_1..X_{t-1}) = μ`; betting wealth is a nonnegative martingale under it. | That real user outcomes satisfy it. | The exact assumption FYJ must maintain. |
| Johari, Koomen, Pekelis, Walsh. *Always valid inference: continuous monitoring of A/B tests.* Oper. Res. 70(3), 2022. arXiv [1512.04922](https://arxiv.org/abs/1512.04922) | theory + deployed | [V] main | Peeking: "Type I error can easily increase fivefold"; mSPRT always-valid p-values; methodology "forms the core of the statistical backend of the Optimizely platform". | Single-user or correlated data. | Explains 21.6 %; mSPRT = candidate test. |
| Maharaj et al. *Anytime-valid confidence sequences in an enterprise A/B testing platform.* arXiv [2302.10108](https://arxiv.org/abs/2302.10108), 2023 | deployed | [V] main | Adobe Experience Platform service on (asymptotic) CS; "peeking leads to inflated type-I error". | Small samples (asymptotic CS needs large n). | Production precedent; asymptotic variant unsuitable for FYJ. |
| Waudby-Smith et al. *Time-uniform central limit theory and asymptotic confidence sequences.* arXiv [2103.06476](https://arxiv.org/abs/2103.06476) | theory | [V-META] main | Asymptotic CS exist. | Validity at tens of observations. | Reject for FYJ (n too small). |
| de Heide, Grünwald. *Why optional stopping can be a problem for Bayesians.* Psychon. Bull. Rev., 2021. arXiv [1708.08278](https://arxiv.org/abs/1708.08278) | theory | [V] main | With "default or pragmatic priors … resilience to optional stopping can break down"; only some Bayes-factor methods handle optional stopping in the frequentist sense. | Anything specific to Beta–Binomial win rates. | Why a posterior threshold is not an error guarantee. |
| Grünwald, de Heide, Koolen. *Safe testing.* JRSSB, 2024. arXiv [1906.07801](https://arxiv.org/abs/1906.07801) | theory | [V-META] main | E-variable framework; optional continuation. | — | Background. |
| Vovk, Wang. *E-values: calibration, combination and applications.* Ann. Statist. 49(3), 2021. arXiv [1912.06116](https://arxiv.org/abs/1912.06116) | theory | [V] main | Arithmetic mean is the essentially only useful symmetric e-merging function; valid without dependence assumptions. | — | Combining several metrics or model versions. |
| Wang, Ramdas. *False discovery rate control with e-values.* JRSSB 84(3), 2022. arXiv [2009.02824](https://arxiv.org/abs/2009.02824) | theory | [V] main | e-BH controls FDR "even under unknown arbitrary dependence". | — | Multiple simultaneous hypotheses (per-feature effects). |
| Lindon, Ham, Tingley, Bojinov. *Anytime-valid inference in linear models and regression-adjusted causal inference.* arXiv [2210.08589](https://arxiv.org/abs/2210.08589) | theory + applied | [V] main | Applied to "real A/B test data from Netflix". | Production deployment details. | Netflix uses the family on population A/B tests. |
| Karampatziakis, Mineiro, Ramdas. *Off-policy confidence sequences.* ICML 2021. arXiv [2102.09540](https://arxiv.org/abs/2102.09540) | theory | [V] main | "Gated deployment": replace h "as soon as (if ever) we can reject the hypothesis that h is better"; assumes **iid** contextual-bandit data. | Non-iid single-user data. | Closest published analogue of FYJ's gate; its iid assumption fails here. |
| Waudby-Smith et al. *Anytime-valid off-policy inference for contextual bandits.* ACM/IMS JDS 2024. arXiv [2210.10768](https://arxiv.org/abs/2210.10768) | theory | [V] main | Valid "at arbitrary stopping times"; logging must assign probabilities — UCB-style "conditionally deterministic actions" are excluded. | — | OPE only on Découverte (known π); deterministic surfaces give no support. |
| Stucchio. *Bayesian A/B testing at VWO* (whitepaper), 2015 | industry | [SECONDARY] | Expected-loss stopping rule ("threshold of caring"). | Error control. | Decision-theoretic alternative; not checked in the PDF. |

### 3.2 Production and job-domain systems

| Source | Type | Prov. | Shows | Does not show | FYJ relevance |
|---|---|---|---|---|---|
| Zhang, Zhou, Ma, Chen, Zhang, Agarwal. *GLMix: generalized linear mixed models for large-scale response prediction.* KDD 2016. [PDF](https://www.kdd.org/kdd2016/papers/files/adf0562-zhangA.pdf), DOI 10.1145/2939672.2939684 | **deployed** (self-report) | [V] main | Global fixed effect + per-member/per-job random effects with Gaussian priors; sparse member "will fall back to the global fixed effect"; deployed in LinkedIn job recommendations, "20% to 40% more job applications"; baseline LR-Cosine does first-stage retrieval, GLMix re-ranks; AUC 0.799 → 0.765 without retraining for 20 days. Data: 500M samples, 5M members [V] agent. | Single-user validity; how the prior would be set without a population (they tuned it on validation AUC [V] agent). | Same structure as FYJ's model; validates "baseline retrieves, bounded learned re-rank". |
| Wang, Kenthapadi, Rangadurai, Hardtke. *Dionysius: a framework for modeling hierarchical user interactions in recommender systems.* arXiv [1706.03849](https://arxiv.org/abs/1706.03849), 2017 | **deployed** (self-report) | [V] main (design) / [V] agent (A/B figures) | Profile fields as prior; "explicit feedback > job application > job view"; prior influence decreases with data. A/B: applications per impression +3.6 % [agent]. | Venue; single-user gating. | Closest analogue of FYJ's design. |
| [Photon ML README](https://github.com/linkedin/photon-ml) | vendor | [V] agent | GLMix adopted in jobs search/recommendation, feed, ads. Repo last pushed 2021. | — | Not a dependency candidate (Spark scale). |
| XING RecSys Challenge 2017, [site](http://2017.recsyschallenge.com/) | contest design | [V] main | Scoring: click 1, bookmark/reply 5, recruiter interest 20, delete-only −10. | Measured preference strength (organiser choice). | Only job-domain weight ladder found; heuristic only. |
| XING RecSys Challenge 2016, [dataset doc](https://github.com/recsyschallenge/2016/blob/master/TrainingDataset.md) | contest design | [V] agent | Click/bookmark/reply = positives; impressions carry "no guarantee that the item was in the viewport". | — | Supports FYJ's visibility gate. |
| Covington, Adams, Sargin. *Deep neural networks for YouTube recommendations.* RecSys 2016. [PDF](https://static.googleusercontent.com/media/research.google.com/en//pubs/archive/45530.pdf) | deployed | [V] agent | CTR ranking promotes clickbait; watch time used; offline/online results not always correlated. | — | Opens ≠ usefulness. |
| Chen et al. *Top-K off-policy correction for a REINFORCE recommender system.* WSDM 2019. arXiv [1812.02353](https://arxiv.org/abs/1812.02353) | deployed (live exp.) | [V] agent | ε-greedy "not viable in a production system"; Boltzmann top-K; capped importance weights matter. | n = 1. | Exploration must be gentle and logged. |
| McInerney et al. *Explore, exploit, explain* (Spotify), RecSys 2018 — slides | experimental | [V-META] agent (slides only) | Warns of "auto-confounding"; counterfactual evaluation. | Numbers (paper blocked). | Confounding warning. |
| Netflix tech blog, *Interleaving in online experiments at Netflix*, 2017 | vendor blog | [SECONDARY] (agent read via a proxy; direct 403) | ">100× fewer users" than best A/B metric; relative measure only. | — | Population interleaving only. |
| Shalaby et al. (CareerBuilder), arXiv [1801.00377](https://arxiv.org/abs/1801.00377), 2017 | deployed system paper | [V] agent | Applications = explicit, clicks = implicit; parameters tuned "through trial and error". | — | Job-domain signal semantics. |
| Mao, Spina et al. (SEEK), CIKM 2019. [PDF](https://marksanderson.org/files/papers/CIKM2019.pdf) | log study | [V] agent | Job seekers filter heavily; criteria evolve over days–weeks; some early applications may "test" the system. | — | Drift within a search; applies not uniformly strong. |
| Kenthapadi, Le, Venkataraman. *Personalized job recommendation system at LinkedIn.* RecSys 2017, DOI 10.1145/3109859.3109921 | industry abstract | [UNVERIFIED] (ACM 403) | — | — | Gap. |

### 3.3 Implicit feedback (signal semantics)

| Source | Prov. | Shows | FYJ relevance |
|---|---|---|---|
| Joachims et al. *Accurately interpreting clickthrough data as implicit feedback.* SIGIR 2005; TOIS 2007 ([PDF](https://www.cs.cornell.edu/people/tj/publications/joachims_etal_05a.pdf)) | [V] agent | Clicks "informative but biased"; trust bias; relative not absolute judgements. | "opened" stays weak. |
| Hu, Koren, Volinsky. *Collaborative filtering for implicit feedback datasets.* ICDM 2008 ([PDF](http://yifanhu.net/PUB/cf.pdf)) | [V] main | Implicit values indicate **confidence**, not preference; "a recurring event is more likely to reflect the user opinion" (repeated consumption of the same item). | Single events are weak; generalizing across items is an inference, not this paper's claim. |
| Liang, Charlin, McInerney, Blei. *Modeling user exposure in recommendation.* WWW 2016 (arXiv [1510.07025](https://arxiv.org/abs/1510.07025)) | [V] main | Treating unclicked items as disliked "is mistaken"; unseen ≠ rejected. | Visibility log is the right exposure variable; "ignored" only with proven exposure. |
| Joachims, Swaminathan, Schnabel. *Unbiased learning-to-rank with biased feedback.* WSDM 2017 (arXiv [1608.04468](https://arxiv.org/abs/1608.04468)) | [V] agent | Propensity estimation needs intervention (swaps); unbiased only if relevant items have positive observation propensity. | Position correction needs randomization FYJ mostly lacks. |
| Kim et al. *Modeling dwell time to predict click-level satisfaction.* WSDM 2014 | [V] agent | Needed dwell depends on topic, length, readability. | Do not add raw dwell thresholds. |
| Yi et al. *Beyond clicks: dwell time for personalization.* RecSys 2014 | [V-META] agent | Dwell normalized across devices/contexts. | Same. |
| Google, *Learning from negative user feedback…* RecSys 2023 (arXiv [2308.12256](https://arxiv.org/abs/2308.12256)) | [V] agent | Dislikes as training labels reduced dislike rate 2.44 %; as features only, not significant. | Negative item-level evidence is useful. |
| Li et al. CHI 2025 (arXiv [2502.09869](https://arxiv.org/abs/2502.09869)) | [V] agent | "Not interested" used for mixed reasons (content, ads, quality). | Reasonless hide must not move general features. |
| Chen, Pu. *Critiquing-based recommenders: survey.* UMUAI 2012 | [V-META] agent | Critiques learn profiles more accurately. | Supports reason-routed signals. |
| Antognini, Faltings. *Positive and negative critiquing for VAE-based recommenders.* arXiv [2204.02162](https://arxiv.org/abs/2204.02162), 2022 | [V] agent | Representing positive and negative attribute critiques differently improves results. | Reason-routed signals; Option D. |
| Koren. *Collaborative filtering with temporal dynamics.* KDD 2009 | [V] agent | Plain exponential decay hurt; separate transient from long-term effects. | Recency half-life needs care. |

### 3.4 Exploration, interleaving, OPE, safe policy improvement

| Source | Prov. | Shows | FYJ relevance |
|---|---|---|---|
| Chapelle, Joachims, Radlinski, Yue. *Large-scale validation and analysis of interleaved search evaluation.* TOIS 30(1), 2012 ([PDF](https://www.cs.cornell.edu/people/tj/publications/chapelle_etal_12a.pdf)) | [V] main | Team-draft credit by contributing ranker; "data requirements on the order of thousands to tens of thousands of queries for detecting even small differences"; sensitivity from pairing "on both queries and users" (authors' conjecture). | Only paired within-user design; slow at FYJ volume. |
| Radlinski, Kurup, Joachims. CIKM 2008 ([PDF](https://www.cs.cornell.edu/people/tj/publications/radlinski_etal_08b.pdf)) | [V] agent | ~1,100–1,400 clicked queries per pair for large constructed differences; per-user majority vote. | Lower bound on volume. |
| Hofmann, Whiteson, de Rijke. *Probabilistic interleave.* CIKM 2011 | [V] agent | Unbiased, more sensitive; softmax sampling can place a ranker's top item low. | Weaker exposure guarantee than team-draft. |
| Li, Chu, Langford, Wang. *Unbiased offline evaluation of contextual-bandit-based news article recommendation.* WSDM 2011 (arXiv [1003.5956](https://arxiv.org/abs/1003.5956)) | [V] agent | Replay needs uniform random logging; keeps 1/K of events; ~40M events. | OPE impossible on deterministic surfaces. |
| Swaminathan et al. *Off-policy evaluation for slate recommendation.* NeurIPS 2017 (arXiv [1605.04812](https://arxiv.org/abs/1605.04812)) | [V] agent | Support required; IPS variance exponential in slate size. | Same. |
| Wu, Shariff, Lattimore, Szepesvári. *Conservative bandits.* arXiv [1602.04282](https://arxiv.org/abs/1602.04282) (ICML 2016 per PDF format; venue not confirmed) | [V] agent | Return must stay ≥ (1−α) of default arm. | Idea "fall back to baseline when uncertain". |
| Kazerouni et al. *Conservative contextual linear bandits.* NeurIPS 2017 (arXiv [1611.06426](https://arxiv.org/abs/1611.06426)) | [V] agent | Play challenger only if safe under the worst parameter in the confidence set. | Same; "sparse data → almost never deploys" is an agent inference. |
| Thomas et al. HCOPE (AAAI 2015), HCPI (ICML 2015) | [V] agent | Millions of trajectories; HCPI returns "NO SOLUTION FOUND" when it cannot certify. | At FYJ volume, NSF is the honest answer. |
| Laroche et al. SPIBB, ICML 2019 (arXiv [1712.06924](https://arxiv.org/abs/1712.06924)) | [V] agent | Copy baseline where counts < N∧. | Design pattern only. |
| Li et al. LinUCB, WWW 2010; Chapelle & Li, TS, NeurIPS 2011 | [V] agent | 33–34M events. | Not for n = 1. |

### 3.5 Feedback loops and risk-aware ranking

| Source | Prov. | Shows | FYJ relevance |
|---|---|---|---|
| Chaney, Stewart, Engelhardt. *How algorithmic confounding in recommendation systems increases homogeneity and decreases utility.* RecSys 2018 (arXiv [1710.11214](https://arxiv.org/abs/1710.11214)) | [V] main | Simulated multi-user communities: confounded training "homogenizes user behavior without increasing utility"; content filtering homogenized "least of all". | Context only: population-level, weak evidence for a one-user content model. |
| Jiang, Chiappa, Lattimore, György, Kohli. *Degenerate feedback loops in recommender systems.* AIES 2019 (arXiv [1902.10730](https://arxiv.org/abs/1902.10730)) | [V] main | Degeneracy driven by the user's interest dynamics; the recommender "can only slow down or accelerate" it; even uniform random recommendation degenerates "quite slowly"; a growing candidate pool slows it. | Context only: FYJ's churning inventory is the mitigating regime. |
| Wang, Zhu. *Portfolio theory of information retrieval.* SIGIR 2009 ([PDF](https://lintool.github.io/robust04-analysis-papers/p115-wang.pdf)) | [V] main | Rank by expected relevance and variance (risk). | Uncertainty-penalized adjustment idea. |

---

## 4. Statistical safety analysis

### 4.1 What G3 actually is (Repo)

`gates.p_better` computes P(p > ½) under Beta(1 + wins, 1 + losses) exactly. `simulator.snapshots`
recomputes cumulative tallies at the end of every session day and `first_activation` takes the
first snapshot passing G1–G3 and G3b. That is a fixed-n decision rule applied at every look — the
textbook peeking pattern (Johari et al.: Type I error "can easily increase fivefold"). The
posterior is a coherent description of belief; it is not a frequentist error guarantee under
optional stopping with a uniform prior chosen for convenience (de Heide & Grünwald). ADR 0019's
criterion ("false activation ≤ 5 % … at true win rate 0.5") is frequentist, so it needs a
frequentist-valid sequential procedure.

### 4.2 What makes a repeated test safe

A **test martingale** (or supermartingale) `K_t` is nonnegative, starts at 1, and under the null
has `E[K_t | past] ≤ K_{t−1}`. Ville's inequality then gives `P(sup_t K_t ≥ 1/α) ≤ α`: the
procedure "activate when `K_t ≥ 1/α`" has false activation ≤ α at **any** stopping time,
however often it is checked (Howard et al.; Ramdas et al.). For FYJ's win/loss outcomes:

- **Mixture SPRT (Robbins; Johari et al.).** `K_t = ∫ Π_i q^{X_i}(1−q)^{1−X_i} / ½^t dF(q)` with
  F on (½, 1]. Each factor has conditional mean ≤ 1 when `E[X_i | past] ≤ ½`, so it is valid for
  the composite null "win probability ≤ ½ at every step".
- **Betting (Waudby-Smith & Ramdas).** `K_t = Π_i (1 + λ_i (X_i − ½))`, λ_i chosen before X_i is
  seen, X_i ∈ [0, 1]. Works for bounded per-session scores (e.g. a session's share of wins).

**The assumption that must hold** is the conditional one: under the null, *each new unit's
expected outcome given everything before it* is ≤ ½. Dependence is allowed; a *conditional*
advantage is not. This matters for FYJ:

| FYJ issue | Does the guarantee survive? | Why / maintenance rule |
|---|---|---|
| Repeated checks, any cadence | **Yes** | Ville's inequality. Cadence becomes a UX choice. |
| Delayed feedback (applies, interviews) | **Yes, if** the unit enters the sequence when its outcome is first observed and the bet for it was fixed before (Ramdas et al.: "a bet on an outcome must be made before the outcome is observed"). | Freeze bet/λ from strictly earlier outcomes; never re-order units by outcome; ADR 0019's "judge against the ranking stored for that run" already enforces no leakage. Units whose outcome arrives later than the window are simply not units (decided by a pre-registered rule, never by their outcome). |
| Within-session correlation | **No, per positive.** Copying an earlier outcome makes `E[X_t | past] > ½` under a marginal-½ null. | Use the session (or a coarser cluster) as the unit; experiment §4.4 shows session units halve but do not remove the inflation. |
| Cross-session dependence (same company, same topic, the same offer re-shown) | **No**, unless clusters are units. | Cluster rule (e.g. one unit per company-week or occupation-group-week), pre-registered. Real dependence structure is unknown until owner replay. |
| Exposure bias (PF surfaces) | Validity of the test is intact; **the tested quantity is wrong**. | Only randomized exposure (S4, interleaving) gives an estimand that production did not shape. |
| Model retraining (challenger changes over time) | **Yes** for the null "every version's conditional win prob ≤ ½". Rejection then means "some versions were better", not "the current version is". | Restart the process at each algorithm/config release (ADR 0020 already starts releases in shadow); within a release, the continuous posterior update is part of the challenger definition. |
| Several metrics / per-surface tests | Combine e-values by averaging (valid under any dependence, Vovk & Wang); e-BH for many hypotheses (Wang & Ramdas). | Pre-register which e-value activates; report others descriptively. |
| Pause/reset and "fresh window" | Valid if the restart rule does not depend on the null process's value. | Pre-registered restart triggers only. |

Asymptotic confidence sequences (Waudby-Smith et al. 2021/2024; Adobe's deployment) rely on
large-sample approximations and are **not** appropriate at tens of units.

### 4.3 Why no pooled test fixes SR-1 (Inference + Experiment)

In SR-1 the generator sets `p_LB = 0.5` and `p_PF = 0.5 + 0.2(1 − 2^−β) > 0.5`
(`simulator.sr1_pf_rate`). The pooled win rate is then truly above ½; a valid test on pooled data
*should* reject. The "false" activation is a mismatch between what is tested (agreement with
choices made under production's exposure) and what ADR 0019 means by better (agreement that
production's exposure did not create). ADR 0019 does intend pooled evidence to count, with G3b
as the self-reinforcement detector and a minimum LB volume (Repo). G3b is a heuristic on Beta
posteriors and is itself checked at every look; with thin LB it is weak (L3 report: SR-3 block
rate 89.4 % only where LB reached n_LB,min). The clean version is to **test only
randomized-exposure evidence** with a valid sequential test. Dropping pooled evidence costs no
power: any rule requiring "pooled AND LB" has power ≤ the LB test alone.

Caveat: in the generator, `p_LB = 0.5` is fixed by construction in SR-1 (`sr1_pf_rate`), so the LB
test's immunity to SR-1 is an input of the simulation, not a finding about real behaviour.

### 4.4 Experiment: valid tests on FYJ's own generator

Script: §13 (scratchpad, not committed). It imports `src.learning.simulator.generate` at
94578c6 unchanged, 1,000 histories per cell, 180 days, α = 0.05, per-session cap 3, two activity
levels (typical, high) × two click models (wide-LB/high-discord, thin-LB). Columns:

- **G3rep** — repeated G3 posterior ≥ 0.95 with ≥ 12 discordant (no G1, G2-positives, G3b);
- **e_pooled** — mixture SPRT over q ∈ (½, 1) on every discordant positive;
- **e_sess** — betting mixture on per-session win shares;
- **e_LB / e_LBsess** — the same restricted to the LB stratum. LB here = S4 **plus** S2 sorted by
  date/distance (the generator's LB share), so it is *not* purely randomized and over-states an
  S4-only design.

Ranges over the four cells; activation within 180 days (within 90 days in brackets for 0.70).

| Scenario | G3rep | e_pooled | e_sess | e_LB | e_LBsess |
|---|---|---|---|---|---|
| baseline (p = 0.5) | 16.1–25.4 % | 2.5–3.1 % | 2.1–3.1 % | 0.0–2.3 % | 0.0–2.4 % |
| SR-1 β = 2 | 67.6–92.8 % | 22.6–51.3 % | 20.6–46.4 % | 0.0–2.3 % | 0.0–2.4 % |
| SR-2 ρ = 0.2 | 21.9–33.0 % | 4.1–7.2 % | 3.0–4.0 % | 0.1–3.1 % | 0.1–2.3 % |
| SR-2 ρ = 0.5 | 27.2–48.6 % | 7.7–19.8 % | 4.4–11.2 % | 0.1–4.6 % | 0.0–3.7 % |
| true 0.70, typical | 93.0–97.3 % | 59.7–77.0 % (35–49 %) | 57.3–72.1 % (32–41 %) | 1.5–14.4 % (0.1–4.0 %) | 1.3–13.5 % (0.1–2.9 %) |
| true 0.70, high | 99.9–100 % | 94.1–99.0 % (67–83 %) | 91.3–97.4 % (62–75 %) | 13.2–44.2 % (2.5–21 %) | 12.1–43.5 % (1.7–20 %) |

Oracle reference (exact one-sided sign test at the *expected* LB unit count, α = 0.05, true
0.70, cap ignored): typical wide-LB/high-discord 13.4 units → power 0.42; typical thin-LB 4.6 →
0.17; high wide-LB/high-discord 32.1 → 0.64; high thin-LB 11.1 → 0.31. No test, sequential or
not, does better than this on that data.

Readings:

1. A valid test removes the optional-stopping inflation exactly as theory predicts (baseline).
2. Pooled data cannot separate a genuinely better challenger from an exposure-fitting one
   (SR-1 vs true-0.70 both activate on pooled data).
3. Outcome copying (SR-2) violates the conditional null; session units help, not enough at
   ρ = 0.5, because copying also crosses sessions (same topic, same week).
4. The LB columns' low false activation is **mostly a low-volume artefact**: at 5–13 units the
   uniform-mixture SPRT needs ~7 straight wins to reach 20, so e_LB is 0.0 % even at baseline in
   thin-LB cells. SR-2 copying is stratum-blind; at higher randomized volume (more S4 slots,
   interleaving) the same dependence inflation would appear in randomized evidence.
5. e_LB power (1–14 % typical) is about a third of the oracle: the uniform mixture on (½, 1) and
   the anytime-valid price cost the rest. A mixture concentrated on plausible effects (0.6–0.75)
   or a few-look group-sequential design would recover part of it; even the oracle stays below
   80 %.

Limits: synthetic generator (all volumes and click models are placeholders, Repo: L3 report);
low activity not run; G3rep is not the full ADR 0019 rule (which adds G1, G2-positives, G3b and
a searched cap of 1 per session); the per-session cap is applied before the stratum filter, so PF
positives can crowd LB units out (slightly understates an LB-only design); SR-2's copying model
is one guess at dependence. Monte Carlo SE at 1,000 histories is ≈ 0.7 pp near 5 % (≈ 1.6 pp
near 50 %); all cells share seed 7 (common random numbers: good for comparing columns; cells are
correlated).

### 4.5 Power at FYJ's volume

Exact one-sided sign test, α = 0.05, power 0.8 (Experiment), true win rates 0.60 / 0.65 / 0.70
/ 0.80: the *first* n reaching 0.8 is **158 / 69 / 37 / 18**; because exact-test power is
sawtooth-shaped in n, the n *beyond which power stays ≥ 0.8* is **169 / 76 / 42 / 18** (agent 3's
figures). Both are correct; the second is the conservative planning value.

What matters is the evidence rate. At typical activity the generator gives ~17 positives a month,
~8–11 discordant, but only **~0.8–2.2 LB discordant units a month**. So 37–42 units at 0.70 take
roughly **17–55 months** on LB evidence; 158–169 units at 0.60 take **6–18 years**. All of this is
before the price of sequential validity, and with placeholder volumes.

### 4.6 Remaining biases no test addresses

- **Exposure ≠ examination.** Visibility ≥ 50 % for 1 s (ADR 0016) is a proxy, not attention.
- **Position bias inside S4.** Fixed slot positions; order random within S4 (Repo), so slot
  effects average out only across draws.
- **Selection into search sorts.** S2 date/distance sorts are user-chosen; the query depends on
  the user's state. "LB" for S2 is less biased, not randomized.
- **Attribution.** Multi-surface exposure; the "any PF exposure ⇒ PF" rule is conservative.
- **Engagement ≠ usefulness.** Saves/applies are proxies for finding a job; interviews mix in the
  employer's decision (Evidence: XING models recruiter interest separately).

---

## 5. Method comparison

| Criterion | Repeated posterior gate (current) | Valid e-process on pooled shadow | Valid e-process on randomized evidence (S4 ± interleaving) | Continuous capped influence, no gate | Team-draft interleaving | User-confirmed suggestions |
|---|---|---|---|---|---|---|
| Statistical validity | ✗ optional stopping | ✓ under conditional null | ✓ under conditional null | n/a (no test) | ✓ paired, position-fair (shown for clicks) | trigger rule is a repeated multi-feature test: needs its own error budget |
| False activation risk | high (21.6 %) | high under SR-1/SR-2 | low if units are conditionally independent (in the experiment, low mostly because it rarely fires) | every change unvalidated | low with session e-process | none statistical; user can be wrong |
| Data efficiency | — | moderate | very low (≈ 1–2 units/month) | high | low (months) | high (one confirmation) |
| User safety | shadow only | shadow only | S4 slots already accepted | bounded by cap; unvalidated | half the list from challenger; production top-k stays within top-2k (proof §7 C) | an accepted wrong suggestion can exclude offers |
| Baseline protection | ✓ | ✓ | ✓ | partial (cap) | partial (bounded displacement) | ✓ (acts through profile → deterministic engine) |
| Delayed feedback | ok with anchor rule | ok if bets predictable | ok | ok | credit to session; delayed applies need a window | n/a |
| Drift | posterior recency | restart per release | same | half-life | per-period tests | user revokes |
| Interpretability | descriptive | e-value + wording | same | "what the app learned" panel | "which list did you prefer" | each suggestion in words |
| Complexity (Repo fit) | exists | small (replace `p_better` gate) | small + needs interleaving if more volume | exists (`visible_ranking`) | medium (new merge, logging, UI) | medium (new UI + profile write path) |
| Privacy | local | local | local | local | local | local |
| Can show it helps? | no | no (biased estimand) | yes, slowly | no | yes, slowly | indirectly (acceptance rate, later outcomes) |

---

## 6. Production implementation review

- **Same model family, deployed (Evidence).** GLMix (LinkedIn jobs, 2016) and Dionysius (LinkedIn,
  2017): global/profile prior + per-user deviation; sparse users fall back to the prior through
  the posterior; per-user effect grows with activity (GLMix Fig. 8 [agent]). FYJ's ridge with the
  profile embedding as prior mean (`model.py`) is this structure for one member.
- **What they had that FYJ lacks (Evidence + Inference).** Prior variances tuned on validation AUC
  over millions of members; launch decisions by population A/B tests; frequent retraining
  (GLMix AUC fell 0.799 → 0.765 after 20 days stale). FYJ's `cap(n)` is an extra rail on top of
  shrinkage; defensible precisely because the prior strength cannot be tuned on a population.
- **Two-stage serving (Evidence).** GLMix re-ranked candidates retrieved by the old baseline — the
  same "baseline authoritative, bounded learned re-rank" pattern as FYJ.
- **Exploration in production is gentle and logged (Evidence).** YouTube rejected ε-greedy and
  used Boltzmann top-K with capped importance weights; Yahoo's offline evaluation required a
  uniformly random bucket; Spotify warned of auto-confounding.
- **Anytime-valid testing in production (Evidence).** Optimizely (mSPRT), Adobe (asymptotic CS),
  Netflix (applications on real A/B data). All are randomized population experiments with
  roughly independent users — the regime where the conditional-mean assumption is plausible.
- **Interleaving in production (Evidence/Secondary).** Bing/Yahoo/arXiv search (Chapelle 2012),
  Netflix (2017 blog, secondary). Always population-aggregated.
- **Status by kind.** Deployed: GLMix, Dionysius, YouTube watch time and REINFORCE, Optimizely,
  Adobe. Experimentally studied: interleaving sensitivity, replay OPE, LinUCB/TS, ExpoMF,
  negative-feedback labels. Theoretical: conservative bandits, HCPI, SPIBB, hierarchical Bayes
  bandits, e-process theory. Nothing found deployed or studied for per-user activation gating of a
  single user.

---

## 7. FYJ architecture comparison

### Option A — Conservative SHADOW (status quo, improved)

- **Does:** personal model observes; nothing changes ranking. Improve event semantics, exposure
  and evaluation logging.
- **Evidence:** §4 shows passive shadow data cannot validate activation at this volume; keeping
  it observational is the only option with zero ranking risk.
- **Data/event changes:** see §8.2 (instrumentation list).
- **Assumptions:** none statistical (no decision is made).
- **Benefits:** zero risk; builds the real-volume dataset the simulator lacks; ADR-consistent.
- **Risks:** no personalization benefit; owner may wait indefinitely.
- **Cost:** low.
- **Test without affecting ranking:** by construction.

### Option B — Continuous baseline-centred personalization (no binary gate)

- **Does:** let `cap(n)·tanh(raw/clamp)` act on the visible ranking from n = 1, growing with
  evidence; no activation event.
- **Evidence for:** GLMix/Dionysius apply per-user effects continuously with shrinkage (but
  validated by population A/B tests). **Context only, neither for nor against:** Chaney et al.
  simulate 100-user communities and measure population homogenization (content filtering
  homogenized users "least of all"); Jiang et al. show degeneracy is driven by the user's own
  interest dynamics — the recommender "can only slow down or accelerate" it — and that a growing
  candidate pool slows it, which is FYJ's natural regime (offers churn). No source shows that a
  bounded, shrunk per-user adjustment is safe or harmful without a gate.
- **Data changes:** none beyond A.
- **Assumptions:** that the prior and the cap make an unvalidated change harmless. Unprovable for
  one user.
- **Benefits:** immediate, smooth personalization; no false-activation concept.
- **Risks:** violates INV-6 / ADR 0019's principle (positive evidence before change); the
  challenger learns from what it surfaced (self-reinforcement, the reason SR-1 exists); a relevant
  offer can drop below the fold; harm is undetectable without randomized evaluation.
- **Already in the architecture:** ADR 0019's EXPERIMENTAL state (small capped adjustment,
  labelled "pas encore confirmée", ships disabled, enabled only after SR-4/SR-5 pass) is the
  in-architecture version of B. ADR 0017 designs a small automatic rule (−0.2 per hidden offer on
  a structured feature after ≥ 3 hides, under `cap(n)`); it is not implemented at 94578c6 (Repo:
  no such code path found), and whether it would bypass INV-6 is an open owner question.
- **Cost:** low (the code path `visible_ranking` exists).
- **Test without affecting ranking:** shadow only — which is exactly the evidence problem.
- **Verdict:** not recommended, **because it contradicts INV-6 and the owner's evidence-first
  rule** — not because the literature shows harm. If the owner wants it anyway, use the existing
  EXPERIMENTAL state with a deterministic displacement bound (no offer in the production top-k
  falls below position k + d; or promote-only) and run it only together with randomized
  evaluation.

### Option C — Controlled experimentation (randomized evidence + valid sequential test)

- **Does:** evidence only from randomized exposure: (C1) Découverte as today (S4 only; S2 date
  sorts are not randomized); (C1b) optionally more S4 slots or a **randomized challenger slot**
  (one slot, π = ½, shows either production's next item or the challenger's best item absent from
  production's list; propensities logged as for S4; the rest of the list untouched); (C2)
  optionally team-draft interleaving of production and challenger in some sessions. Activation
  by a valid sequential test on session/cluster units (`K_t ≥ 1/α`, or a pre-registered few-look
  group-sequential design with α spending, which recovers more power), pause by a second test for
  harm with a laxer threshold (pausing is the safe direction).
- **Evidence:** §4.2 (validity), §4.4 (C1 alone: valid, very low power), Chapelle 2012
  (interleaving efficiency at population scale).
- **Data changes:** per-session interleaving flag and team assignment per displayed position;
  unit/cluster id; e-process state and its history per release; credit rule version.
- **Assumptions:** conditional-mean null at the session/cluster level; credit rule pre-registered;
  no outcome-dependent restarts.
- **Benefits:** the only design that can *show* the challenger is better for this user.
- **Risks:** C1 alone needs years at placeholder volumes (§4.5); C2's unit rate is ~11 positive
  sessions a month at typical activity, diluted by list overlap (unsimulated); **dependence
  survives randomization** (SR-2 copying is stratum-blind), so the unit/cluster definition stays
  decisive; interleaving and the challenger slot show unvalidated challenger picks before any
  evidence — the same INV-6 question as Option B, but bounded in exposure and justified as the
  experiment that produces the evidence (owner decision); team-draft can return the wrong
  preference in constructed ambiguous-intent cases (Chapelle 2012, [V] agent) and its
  position-fairness was shown for clicks, not saves/applies; credit for delayed applies needs a
  window.
- **Exposure bound (proof).** Team-draft (Chapelle 2012, Algorithm 2) alternates picks so that
  after 2k positions each team has made k picks, and each pick is that team's highest-ranked item
  not yet shown. A team that has made k picks has therefore shown (itself or via the other team)
  all of its own top-k. So production's top-k always lies within the first 2k positions; an item
  can still move from position k to 2k.
- **Cost:** C1 small (swap the G3 computation, restrict evidence); C2 medium (merge, logging, UI).
- **Test without affecting ranking:** simulator first (add interleaving and cluster-dependence
  scenarios); then replay on the owner's logged data.

### Option D — User-confirmed preference suggestions (human-in-the-loop)

- **Does:** the personal model never moves the ranking. It turns repeated, *reasoned* evidence
  into explicit, revocable suggestions in words, e.g. "Vous avez masqué 3 offres en indiquant
  « trop loin », toutes à plus de 40 km : préférer les offres à moins de 40 km ?" An accepted
  suggestion becomes a profile edit and acts through the deterministic engine. This **extends a
  path the ADRs already define**: ADR 0017 ("After ≥ 3 hides of one occupation group or company
  within 30 days the app *asks*"), ADR 0019 ("profile suggestions they confirm and Hide still
  work") and ADR 0014 (profile *suggestions* as a Personal-plane output). Reason-less hides never
  feed a general suggestion (ADR 0017: Hide "produces no generalising negative").
- **Not gate-free.** "When to suggest" is itself a repeated test over several features
  (distance, contract, remote, occupation group, company, salary), checked every session:
  multiplicity and optional stopping apply. Triggers must be pre-registered with a
  false-suggestion budget (e.g. per-feature e-values combined with e-BH, Wang & Ramdas), and
  rejected suggestions suppress re-asking.
- **Evidence:** critiquing-based recommenders learn profiles more accurately from attribute-level
  feedback (Chen & Pu, [V-META]); attribute-level positive and negative critiques are best
  modelled separately (Antognini & Faltings 2022, [V] agent); implicit counts express confidence,
  not preference (Hu et al., [V] main — their "recurring event" means repeated consumption of the
  same item, so it supports caution about single events, not directly repetition across items).
  No source tests this design for job search (Hypothesis).
- **Data changes:** suggestion log (shown, accepted, rejected, revoked) with the evidence offers
  and the trigger rule version.
- **Assumptions:** the user can judge a stated preference better than a test can infer it.
- **Benefits:** explainable; drift handled by revocation; the user, not a statistic, authorizes
  each change. ADR 0018's "no learning settings / no label requests" is largely settled by ADR
  0017's ask rule (Owner decision to confirm the extension).
- **Risks:** prompt fatigue; anchoring; **an accepted wrong suggestion can be worse than a capped
  re-rank** if it becomes a STRONG constraint that removes offers from eligibility — so accepted
  suggestions should default to soft preferences (WEAK) and show how many current offers a hard
  constraint would exclude; suggestions only cover features with a deterministic counterpart, not
  embedding similarity.
- **Cost:** medium (UI + profile write path + tests).
- **Test without affecting ranking:** generate suggestions in shadow and log what *would* have been
  suggested; review them with the owner before any UI.

### Recommendation

1. **Now: Option A.** Keep SHADOW and activation OFF. This is the safest decision and the evidence
   does not support activating anything.
2. **Next, if the owner wants user-visible personalization: Option D** as an extension of ADR
   0017's ask rule, built shadow-first, with pre-registered triggers and a false-suggestion budget.
3. **Replace G3 in ADR 0019 by a valid sequential procedure restricted to randomized evidence
   (Option C1)**, keeping the current rule computed as a comparator (owner decision 2026-11-06).
   α stays 0.05: no threshold is lowered. Expect it to rarely activate at typical activity; state
   that as an accepted consequence.
4. **Randomized challenger slot (C1b) or team-draft interleaving (C2)** only after simulator
   evidence of power (with list overlap and dependence) and an owner decision on showing
   unvalidated picks; they are the only routes found to faster valid evidence.
5. **Do not adopt Option B** outside the existing, disabled EXPERIMENTAL state.

### 7.5 Other alternatives considered

| Alternative | Verdict | Reason |
|---|---|---|
| Fixed-horizon single pre-registered test | possible, weaker UX | Valid and more powerful than an always-valid test, but one look only; the L3 report already names "an error budget spent across looks". A few-look group-sequential design is the middle ground (in C). |
| Bayesian expected-loss stopping (VWO-style) | reject for activation | Controls expected loss under the prior, not the frequentist false-activation rate ADR 0019 requires. Usable as descriptive wording. |
| Randomizing production order (near-tie dithering, Plackett–Luce, YouTube-style Boltzmann) | owner decision; not recommended | Would make PF positives usable through IPS, but makes the deterministic baseline stochastic, contrary to "baseline authoritative". |
| Off-policy evaluation (IPS/SNIPS/DR) | offline diagnostic on S4 only | Needs logged propensities > 0; deterministic surfaces give no support; weights N/2 are large. |
| Conservative bandits, CLUCB, HCPI, SPIBB | reject as mechanisms | Validated at millions of events; borrow only "fall back to baseline when uncertain". The claim that CLUCB would almost never deploy at FYJ volume is an agent inference. |
| Contextual bandits / Thompson sampling | reject for n = 1 policy learning | 33–34M events in validation; randomization useful only to log propensities. |
| Asymptotic confidence sequences | reject | Large-sample approximations. |
| Explicit pairwise choices ("which of these two?") | out of scope | ADR 0018 rules out label requests; would be the strongest evidence per interaction. |

---

## 8. Recommended implementation strategy (no code in this task)

### 8.1 How the current code maps (Repo)

| Area | Existing | Reuse |
|---|---|---|
| Model | `src/learning/model.py` (pm1): dual ridge on embeddings around the profile; shrunk per-value means; `cap(n)·tanh`; `ALGORITHM_VERSION`, input hash | Unchanged for A/C/D; D reads its structured tables to propose suggestions. |
| Signals | `src/learning/signals.py`, weights in `config.py` (`DEFAULT_WEIGHTS`) | Keep; see §8.2 for semantics. |
| Exposure | `exposure_log` (`src/feedback/exposure.py`): job, surface, position, visible_ms, sort_key, randomized, session_id, generation | Add run id and interleaving team (see below). |
| Randomization | `s4_draw` (`src/learning/exploration.py`): run, job, slot, π, seed, pool size | Already the randomized evidence source for C1. |
| Shadow | `ranking_shadow` (run, job, both positions, adjustment, model_version, S4 fields) | Unchanged. |
| Evidence | `shadow_positive` (one row per offer, anchor run, stratum, session) | Add unit/cluster id for the e-process. |
| Gate | `gates.decide`, `p_better`, `g3b`; state in `personal_health` | Replace G3/G3b by an e-process value; keep the old rule computed for comparison. |
| Visible ranking | `health.visible_ranking` returns production unless ACTIVE and `activation_enabled` | Unchanged; the safety switch stays. |
| Versions | `personal_model_version` = algorithm + config digest + input hash; `release_key` in `personal_health` | Use `release_key` to restart the e-process per release. |
| Rollback/deletion | Pause/Reset (`health.pause/resume`, reset marker), ADR 0015 erasure | E-process state and suggestion log are derived personal data: erased with the rest, reset with the model. |

### 8.2 Instrumentation (Option A work, owner-approvable separately)

1. `exposure_log.run_id` — ADR plan §4.5 refers to `exposure_log.run_id`, but the table at
   94578c6 has no such column (Repo): `health._exposures` reads exposures without a run and `_run_at` infers the run as the latest one started before the exposure. Add the run id at logging time so anchoring is exact.
2. Unit/cluster id on `shadow_positive` (session, plus company and occupation-group-week) so the
   dependence of real data can be measured before any test is chosen.
3. Owner replay: compute, offline, the within-session and cross-session agreement of outcomes on
   real logs, to replace the SR-2 guess.
4. Hide without reason: keep 0 for general features (Evidence: mixed reasons); consider an
   **item-level-only** negative (the offer itself), which ADR 0017's Hide already applies by hiding
   it. "Ignored" stays inert (owner decision); revisit only with the visibility gate.
5. Interview/offer outcomes: record that they mix the employer's decision; consider capping them at
   the "applied" level for *preference* learning (Hypothesis).
6. Shadow-only suggestion log for Option D.

### 8.3 Validation stages

1. **Simulator.** Add: e-process gate (session units, LB/randomized only); cluster-dependence
   scenarios; interleaving scenario with team-draft credit; delayed-outcome scenario. Criteria
   unchanged (≤ 5 % false activation at 0.5; report power honestly, including "never").
2. **Owner replay** on real logs once volumes exist; measure dependence and LB volume.
3. **Shadow suggestions** reviewed by the owner (Option D) — no UI.
4. Only then: owner decision on any user-visible change.

---

## 9. Evidence ledger (decisive claims)

| # | Claim | URL | Inspected by | Evidence | Date | Weight | Check |
|---|---|---|---|---|---|---|---|
| 1 | Peeking inflates Type I error several-fold | https://arxiv.org/abs/1512.04922 | agent → main | "Type I error can easily increase fivefold" (§1) | v3 2019 | decisive | main re-opened: confirmed |
| 2 | mSPRT/always-valid p-values control error at any stopping time; deployed at Optimizely | same | main | "Continuous monitoring does not inflate Type I error"; "forms the core of the statistical backend of the Optimizely platform" | v3 2019 | decisive | main: confirmed |
| 3 | CS/e-process validity at all stopping times via test martingales | https://arxiv.org/abs/2210.01948 | main | "remain valid at all stopping times … rely on test martingales" (Abstract) | v2 2023 | decisive | main: confirmed |
| 4 | Null = conditional mean given past | https://arxiv.org/abs/2010.09686 | main | "EP (Xt \| X1, . . . , Xt-1) = μ" (setting) | v7 2022 | decisive | main: confirmed |
| 5 | Bets must be fixed before outcomes | https://arxiv.org/abs/2210.01948 | main | "a bet on an outcome must be made before the outcome is observed" | 2023 | decisive | main: confirmed |
| 6 | Bayesian optional stopping can fail with default priors | https://arxiv.org/abs/1708.08278 | main | "resilience to optional stopping can break down" (Abstract) | v5 2021 | supporting | main: confirmed |
| 7 | Averaging e-values valid under any dependence | https://arxiv.org/abs/1912.06116 | main | "symmetric merging functions are essentially dominated by the arithmetic mean" | v4 2020 | supporting | main: confirmed |
| 8 | Off-policy gated deployment assumes iid data | https://arxiv.org/abs/2102.09540 | agent → main | "We have iid CB data" | 2021 | decisive (no off-the-shelf n=1 method) | main: confirmed |
| 9 | Anytime-valid OPE excludes deterministic logging | https://arxiv.org/abs/2210.10768 | agent → main | "cannot be the result of Upper Confidence Bound (UCB)-style algorithms since they take conditionally deterministic actions" | v3 2024 | decisive | main: confirmed |
| 10 | Interleaving needs thousands–tens of thousands of queries | https://www.cs.cornell.edu/people/tj/publications/chapelle_etal_12a.pdf | agent → main | "data requirements on the order of thousands to tens of thousands of queries for detecting even small differences" (§12) | 2012 | supporting | main: quote confirmed; reviewer: web-search effect sizes, not a proxy for FYJ volume — used only as context |
| 11 | GLMix deployed in LinkedIn jobs; sparse users fall back to global | https://www.kdd.org/kdd2016/papers/files/adf0562-zhangA.pdf | agent → main | "We deployed the model in the LinkedIn job recommender system"; "fall back to the global fixed effect" | 2016 | decisive | main: confirmed |
| 12 | GLMix re-ranks after baseline retrieval; staleness | same | agent → main | "We run the baseline model LR-Cosine in this stage"; "drops from 0.799 to 0.765 … 20 days" | 2016 | supporting | main: confirmed |
| 13 | Dionysius: profile as prior; signal hierarchy | https://arxiv.org/abs/1706.03849 | agent → main | "treats the user's original profile-based fields as the prior"; "explicit feedback > job application > job view" | 2017 | supporting | main: confirmed |
| 14 | XING 2017 weights 1/5/20/−10 are a contest scoring rule | http://2017.recsyschallenge.com/ | agent → main | "if (clicked) 1 else 0 + if (bookmarked \|\| replied) 5 … - if (delete only) 10" | 2017 | supporting | main: confirmed |
| 15 | Implicit counts = confidence; a single event is weak evidence | http://yifanhu.net/PUB/cf.pdf | agent → main | "implicit feedback indicates confidence"; "a recurring event is more likely to reflect the user opinion" (same item) | 2008 | supporting | main: confirmed; reviewer: over-interpreted in the draft as repetition across items — corrected |
| 16 | Unclicked ≠ disliked | https://arxiv.org/abs/1510.07025 | agent → main | "this assumption is mistaken" | 2016 | supporting | main: confirmed |
| 17 | Feedback loops homogenize without utility gain (population simulation) | https://arxiv.org/abs/1710.11214 | main | "homogenizes user behavior without increasing utility"; content filtering "least of all" | 2018 | context | main: confirmed; reviewer: draft weighted it "decisive against B" — downgraded |
| 18 | Degeneracy follows user dynamics; growing pool slows it | https://arxiv.org/abs/1902.10730 | main | "can only slow down or accelerate the process of degeneration"; uniform random also degenerates "quite slowly" | 2019 | context | main: confirmed; reviewer: draft quote was out of context — corrected |
| 22 | ADR 0017 already defines an ask-after-3-hides rule and a −0.2 capped pattern | `docs/adr/0017-validity-constraints-and-hiding.md` | main (repo) | "After ≥ 3 hides of one occupation group or company within 30 days the app *asks*" | 2026-11-05 | decisive for D framing | read; −0.2 rule not implemented at 94578c6 |
| 23 | Positive and negative critiques are best modelled separately | https://arxiv.org/abs/2204.02162 | agent | "representing positive and negative critiques differently enables M&Ms-VAE+ to significantly outperform" | 2022 | supporting | — |
| 19 | Adobe deployed anytime-valid CS | https://arxiv.org/abs/2302.10108 | main | "released an experimentation service on the Adobe Experience Platform based on anytime-valid confidence sequences" | 2023 | supporting | main: confirmed |
| 20 | Valid e-process holds ≤ 5 % on FYJ generator at baseline; fails under SR-1 pooled; LB-only valid but powerless | §4.4 | main (experiment) | table §4.4 | 2026-11-06 | decisive | reproducible script §13 |
| 21 | G3 is checked at every session snapshot | `src/learning/simulator.py` `snapshots`/`first_activation`; `gates.p_better` | main (repo) | code at 94578c6 | 2026-11-06 | decisive | read |

Verification summary: 19 external claims re-opened by the main session in raw text, all quotes
confirmed; the reviewer re-opened 12 rows and found three over-interpretations (#10 use, #15,
#17/#18 weight and context), all corrected above. Agent 3's sample sizes were not wrong (two valid
definitions, §4.5). Agent claims marked `[V] agent` were not re-opened and do not carry the
recommendation alone.

---

## 10. Unresolved questions and needed experiments

| Gap | Material? | What would resolve it |
|---|---|---|
| Real activity, LB share, discordance rate | yes | Owner replay on fresh-copy logs (L3 report already lists it as not run). |
| Real dependence structure (session, company, topic, re-shown offers) | yes | §8.2 items 2–3. |
| Interleaving power and user cost in FYJ | yes, for C2 | Simulator scenario + small UI test. |
| Whether Option D fits ADR 0018's "no learning settings / no label requests" | yes | Owner decision. |
| Relative preference value of save / apply / interview | moderate | No source found; would need the owner's own outcome data. |
| Kenthapadi et al. RecSys 2017; Netflix interleaving blog (direct); Spotify paper | low | ACM/Medium access; not expected to change the decision. |
| Single-user activation literature | — | None found in this search; absence is itself the finding. |
| Dependence *within* randomized evidence at realistic volume | yes, for C | Simulator: S4-only and interleaving scenarios with stratum-blind copying; owner replay. |
| Power of C1b/C2 with list overlap | yes, for C | Simulator scenario. |
| Would ADR 0017's −0.2 hide pattern count as ungated ranking influence under INV-6? | moderate | Owner decision before it is implemented. |
| Would the owner accept a stochastic production order (dithering)? | low | Owner decision; not recommended here. |

---

## 11. Proposed ADR 0019 amendments (recommendations only — not applied)

1. **G3 replaced (for comparison, the current rule stays computed and reported).** Activation
   evidence is an anytime-valid e-process (or a pre-registered few-look group-sequential test)
   for H0 "conditional challenger win probability ≤ ½", α = 0.05. The Beta posterior stays as the
   descriptive wording only. This **overrides ADR 0019's clause** "If correlated outcomes fail,
   the clustering cap or G2 is revised through the simulator, never G3"; it does not lower any
   threshold.
2. **Evidence restricted to randomized exposure.** Only S4 (and interleaved sessions if adopted)
   feed the activation e-process. PF and IM positives are reported, never evidence. G3b becomes
   unnecessary (it existed to detect PF inflation).
3. **Unit of analysis.** The session or a coarser pre-registered cluster (company-week,
   occupation-group-week), not the positive; chosen from measured dependence in the owner replay.
   Session units alone do not remove cross-session dependence (§4.4). The per-session cap is then
   superseded.
4. **Restart rules.** A new e-process per release (`release_key`), after Reset, and after a
   pause; restart triggers never depend on the process value.
5. **Health monitor.** A second e-process for "challenger worse" with a laxer threshold
   (asymmetric, keeps "pauses more easily than it activates").
6. **Simulator criteria.** Add cluster-dependence and delayed-outcome scenarios and an
   interleaving scenario; criteria stay as written; power is reported including "never".
7. **Accepted consequence restated.** Ranking personalization may never activate for a
   typical-activity user; personalization is delivered through confirmed suggestions (if the
   owner adopts Option D).
8. **Interleaving.** Replace "causal evaluation (interleaving) is not done now; revisit after L4"
   with "team-draft interleaving is the candidate causal evaluation; adoption needs simulator
   evidence and an owner decision".

Decisions requiring owner approval: all eight items; Option D at all; any instrumentation change;
any user-visible change.

---

## 12. Independent review and how it was handled

A `research-reviewer` agent audited the draft (full text, ledger, experiment script and output,
ADRs 0014/0017/0018/0019, raw PDF text). Verdict: the bottom line (keep SHADOW; no honest and
useful passive pooled gate at simulated volumes) holds; several supporting arguments needed fixing.
The main session re-checked the reviewer's decisive claims (ADR 0017 text, L3 report cap of 1,
Chaney and Jiang passages, oracle power) before accepting them.

| Finding | Handling |
|---|---|
| "LB-only ≤ 4.6 %" is circular for SR-1 (`p_LB = 0.5` by construction) and mostly zero power; LB includes non-randomized S2 sorts; dependence survives randomization | Accepted: §1.1, §1.3, §4.3 caveat, §4.4 readings 4–5, §5, §7 C risks. |
| "Property of the data" asserted, not shown; e-process wastes power | Accepted: oracle fixed-n power added (§4.4); claim made conditional on placeholder volumes; mixture choice / group-sequential named as design levers. |
| 21.6 % decomposition misframed (three cells, cap 1 vs 3); "no sequential method fixes SR-1" wrong | Accepted: §1.1 rewritten; §4.3 retitled "no pooled test"; intersection argument added. |
| Option B rejection over-reached on Chaney/Jiang; C2 has the same INV-6 issue; EXPERIMENTAL and ADR 0017 −0.2 not discussed | Accepted: literature downgraded to context; rejection based on INV-6; C2 INV-6 question stated; EXPERIMENTAL and ADR 0017 discussed (−0.2 rule confirmed not implemented at 94578c6). |
| Option D not gate-free, example contradicted ADR 0017, already in ADRs; Hu over-read; Antognini missing | Accepted: D reframed as extension of ADR 0017's ask rule with a false-suggestion budget and soft-preference default; example uses reasoned hides; Hu reworded; Antognini added to §3 and §9. |
| §4.5 "correction" was not a correction; LB-only time missing | Accepted: both n definitions; LB-only months/years added. |
| Interleaving undersold; Chapelle quote misapplied | Accepted: unit-rate estimate; Chapelle used as context only. |
| ADR 0019 "never G3" clause not flagged | Accepted: §11 amendment 1. |
| Minor: cap applied before stratum filter, MC SE, common seed, "7.2 %" includes cap change, Kazerouni inference, Johari context | Accepted: §4.4 limits, §1.1, §7.5. |
| Missing alternatives: fixed-horizon / α-spending, more S4, randomized challenger slot, dithering, promote-only, expected loss | Accepted: §7 C (C1b, group-sequential), §7.5 table, §7 B verdict. |

## 13. Appendix: experiment script

Saved outside the repository (session scratchpad) as `eproc_exp.py`; run with the project venv:
`python eproc_exp.py <path-to-public-checkout> 1000`. Core of the two test statistics:

```python
QGRID = np.linspace(0.505, 0.995, 99)   # alternative win rates q > 1/2, uniform mixture
LGRID = np.linspace(0.05, 1.9, 38)      # betting fractions for [0,1] session scores

def mix_bern(xs):                        # mixture SPRT, valid if E[X_t | past] <= 1/2
    logw = np.zeros_like(QGRID); out = []
    for x in xs:
        logw += np.log(np.where(x, QGRID, 1 - QGRID) / 0.5)
        m = logw.max(); out.append(math.exp(m) * np.mean(np.exp(logw - m)))
    return out

def mix_bet(xs):                         # betting mixture, x in [0,1], same null
    logw = np.zeros_like(LGRID); out = []
    for x in xs:
        logw += np.log1p(LGRID * (x - 0.5))
        m = logw.max(); out.append(math.exp(m) * np.mean(np.exp(logw - m)))
    return out
# activate at the first unit where the e-value >= 1/alpha = 20
```
