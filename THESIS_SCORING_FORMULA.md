# Revision of the Corridor Risk Scoring Function

**Project:** AI-Driven Energy Supply Chain Resilience System
**Component:** `pipeline/score/risk_scorer.py` — corridor risk aggregation
**Revision date:** 26 September 2026
**Status:** Implemented, tested (304 automated tests), validated on live data

---

## 1. Summary

The corridor risk score originally aggregated extracted geopolitical events by
**summation**. Empirical testing established that a summed score measures *how
much news was collected* as much as *how severe the situation is*, to the extent
that the relative risk ranking of two corridors could be inverted purely by
changing the data-collection method while the real-world situation was held
constant.

The aggregation was replaced with a **bounded order statistic**: the zero-padded
mean of a corridor's three highest-weighted distinct news stories. This change

1. removed the measured sensitivity to sampling depth (from a factor of 1.71 to
   1.00);
2. eliminated the one remaining uncalibrated constant in the scoring pipeline;
3. restored physically interpretable magnitudes to the downstream criticality
   analysis; and
4. widened the discriminating gap between the two at-risk corridors by a factor
   of two.

The event-level weighting, the time-decay function and the story-deduplication
stage were **not** changed. Only the aggregation across stories was replaced.

---

## 2. The original formulation

Each extracted event *e* carries an LLM-assigned severity *s(e)* ∈ {1,…,5}, a
confidence *c(e)* ∈ [0,1] and a timestamp. Events are first clustered into
distinct **stories** to remove syndicated duplicate coverage (one wire report
republished by many outlets under different URLs). Each story *i* receives a
time-decayed weight:

> **w(i) = s(i) · c(i) · e^(−λ·Δt(i))**,  λ = 0.1 day⁻¹

where Δt is the story's age in days. The maximum attainable single-story weight
is therefore

> **w_max = 5 × 1.0 × 1 = 5.0**

(severity 5, full confidence, published today).

The original aggregation summed these weights over all stories and compressed the
unbounded result into [0,1] with a saturating exponential:

> **R_raw = Σᵢ w(i)**
>
> **R = b + (1 − b)·(1 − e^(−R_raw / K))**,  K = 25

where *b* is the corridor's `baseline_risk` (Hormuz 0.20, Red Sea 0.15, Cape
0.05), representing structural risk in the absence of any news signal.

### 2.1 A prior deviation from the original specification

The project specification originally called for min–max normalisation across
corridors. This was rejected during implementation and replaced by the saturating
transform above, for reasons that remain valid and are worth recording: min–max
normalisation is purely *relative*, so in any observation window — however calm —
the noisiest corridor is forced to 1.0 and the quietest to 0.0. It is also
undefined at cold start, when all corridors score zero. The saturating transform
is *absolute*: a corridor with no events sits exactly at its own baseline risk.
**The revision described here preserves that absoluteness.**

---

## 3. The defect

### 3.1 Statement of the problem

A sum over stories is **unbounded in the number of stories**. Consequently R_raw
is a function of two independent quantities that the model cannot distinguish:
the severity of events, and the depth to which the news corpus was sampled.
Because the system's ingestion volume is a property of the collection
infrastructure — not of the world — the score inherited an artefact of its own
instrumentation.

Formally, for a fixed real-world situation, R_raw scales approximately linearly
with the number of stories retrieved, whereas the quantity being estimated
(corridor risk) does not.

### 3.2 Demonstration: severity and count are conflated

The clearest statement of the defect is that the summed score cannot distinguish
a large volume of minor events from a small volume of catastrophic ones:

| Scenario | Stories | Per-story weight | R_raw | R (K=25) |
|---|---|---|---|---|
| Widespread minor disruption | 70 | 2 × 0.6 = 1.2 | **84.0** | 0.9722 |
| Severe, narrow crisis | 17 | 5 × 1.0 = 5.0 | **85.0** | 0.9733 |

These two situations are operationally very different and the score reports them
as equivalent.

### 3.3 Demonstration: the corridor ranking inverted with the collection method

The system ingests news through two independent paths against the same underlying
source corpus: the GDELT DOC 2.0 search API, and GDELT GKG 2.0 bulk files. The
GKG path was added to circumvent per-IP rate limiting on the DOC API; it retrieves
a larger number of distinct stories per unit time.

When the second path was introduced, with no change in the real-world situation:

| Corridor | DOC path only | Both paths | Change |
|---|---|---|---|
| Hormuz | 39.4 | 115.6 | ×2.9 |
| Red Sea | 45.1 | 104.7 | ×2.3 |
| **Higher-risk corridor** | **Red Sea** | **Hormuz** | **inverted** |

The two corridors exchanged rank. Since the world had not changed, the ranking
was being determined by the data-collection method. This is a validity failure,
not a precision failure.

### 3.4 Demonstration: controlled comparison across ingestion paths

To quantify the effect under controlled conditions, a purpose-built comparison
harness (`manage.py compare_scoring`) partitioned a single stored corpus by
ingestion path and re-scored each partition. Because all partitions describe the
*same* time window and the *same* events, a statistic that measures the world
should be approximately invariant across them.

Measured on 257 corridor-attributed events (26 September 2026). The ratio column
compares the statistic computed on all paths combined against the single widest
path:

| Corridor | Statistic | DOC path | GKG path | All paths | Ratio |
|---|---|---|---|---|---|
| Hormuz | summed (R_raw) | 16.58 | 44.07 | 75.32 | **1.71** |
| Hormuz | top-3 (revised) | 1.01 | 2.598 | 2.598 | **1.00** |
| Red Sea | summed (R_raw) | 23.97 | 34.73 | 59.63 | **1.72** |
| Red Sea | top-3 (revised) | 1.04 | 2.279 | 2.279 | **1.00** |

The summed statistic rises by ~71% purely from combining collection paths. The
revised statistic is invariant to three decimal places.

### 3.5 Consequence: saturation and loss of discriminating power

Because K = 25 while R_raw had reached the hundreds, the exponential transform was
operating deep in its saturated region, where it is numerically insensitive to its
input. A **10% difference in R_raw compressed to a 0.005 difference in R**, with
both scored corridors reported at ≈0.99. The model was asserting that both the
Strait of Hormuz and the Red Sea were approximately 99% closed simultaneously,
leaving no headroom to represent an actual closure.

### 3.6 Consequence: the criticality analysis became degenerate

The risk score enters the network model through the dynamic edge capacity

> **effective_capacity = volume × (1 − R)**

At R ≈ 0.99 the Strait of Hormuz retained ≈1% of its modelled capacity. The
criticality engine ranks corridors by the additional maximum-flow loss caused by
removing each one; removing an already-eliminated corridor removes almost nothing.
The reported marginal capacity loss for Hormuz fell to **0.009 mb/d on a corridor
carrying 2.316 mb/d** of India's modelled crude inflow — mechanically correct
given the input, and physically meaningless. The model simultaneously reported
that India had already lost ~50% of crude deliverability.

### 3.7 Alternative explanations excluded

Three plausible alternative causes were investigated and ruled out with evidence
before the formula itself was implicated:

- **LLM extraction quality.** Severity distributions use the full rubric rather
  than clustering at the extremes (Hormuz mean 3.07/5, Red Sea 3.55/5; only 18 of
  149 and 4 of 129 events received severity 5). Across all extraction runs there
  were zero unparseable responses and zero API call failures.
- **Duplicate coverage.** Story clustering was verified to be functioning, and
  its compression ratio was comparable across corridors (2.07× and 1.89×), so it
  could not account for a directional bias between them.
- **Sampling bias between corridors.** A separate earlier defect, in which
  rate-limiting silently starved individual corridor queries, had already been
  identified and corrected, with explicit `sampled` / `not sampled` reporting
  added to distinguish "quiet corridor" from "unobserved corridor".

The residual effect was therefore attributable to the aggregation function.

### 3.8 Why recalibrating the constant was not a solution

The natural first response — increasing K — does not address the defect. K ≈ 95
would have placed the then-current corpus near 0.80, but doubling the ingestion
rate for an unchanged world would have required K ≈ 190. **Any value of K is
correct for exactly one corpus size.** Per-story weighting is bounded; a sum over
stories is not. The problem is the functional form, not its parameter.

---

## 4. Candidate formulations evaluated

Eight aggregation functions were implemented and evaluated against the same
stored corpus, holding the event weighting, time decay and story clustering
constant so that only the aggregation varied. Three criteria were applied:

1. **Invariance** — does the statistic move when only sampling depth changes?
2. **Discrimination** — does it separate the two at-risk corridors, and does a
   corridor with no events remain at its structural baseline?
3. **Downstream validity** — does it restore interpretable magnitudes to the
   criticality analysis?

| Candidate | Sampling ratio (target 1.00) | Corridor gap | Hormuz marginal loss (mb/d) |
|---|---|---|---|
| Summed + saturating (original) | 1.71 | 0.039 | 0.087 |
| log(1 + sum) + saturating | 1.14 | 0.018 | 0.202 |
| Share of total articles (GPR-style) | 0.80 | 0.057 | 0.174 |
| Decay-weighted mean | 0.85 / 0.69 | 0.040 | 1.268 |
| Highest single story (top-1) | 1.00 | 0.061 | 0.806 |
| **Top-3, zero-padded (adopted)** | **1.00** | **0.078** | **0.833** |
| Top-5, zero-padded | 1.00 | 0.075 | 0.873 |

All candidates correctly held the Cape corridor (zero events) at its 0.05
baseline, confirming that absoluteness was preserved throughout.

### 4.1 Rejected: logarithmic compression

Applying log(1 + R_raw) is the most economical modification and it does reduce
saturation, but it does not address the underlying conflation, because the
logarithm is monotone in the sum. The two scenarios of §3.2 remain
indistinguishable (4.443 against 4.454). Rejected.

### 4.2 Rejected: the share-of-articles approach, despite its published precedent

The established treatment of this problem in the economics literature is
normalisation by total article volume. Caldara and Iacoviello's **Geopolitical
Risk (GPR) index** (Federal Reserve International Finance Discussion Paper 1222;
subsequently *American Economic Review*) counts risk-related articles per
newspaper per month *as a share of the total number of articles in that
newspaper*. Baker, Bloom and Davis's **Economic Policy Uncertainty (EPU) index**
applies the same device. This is strong precedent that raw counts are
volume-contaminated and that a text-derived risk index must control for it.

The share formulation was implemented and measured. It was **not adopted**, for
three reasons:

1. **The required denominator is not observable in this system.** The GPR
   denominator is the total article population scanned. The GKG ingestion path
   examines approximately 61,000 records per 24-hour window and retains only the
   ~113 that match a corridor; the size of the examined population is not
   recorded, so the true share cannot be computed without additional
   instrumentation.
2. **The available proxy performed poorly.** Substituting total *ingested*
   articles left the scores still saturated (0.92 and 0.86), left the downstream
   criticality figure still degenerate (0.174 mb/d), and its own sampling ratio
   moved to 0.80 rather than remaining at 1.00.
3. **A conceptual mismatch.** The GPR denominator is well defined because its
   corpus is a *fixed* set of ten newspapers whose total output is externally
   observable. This system's corpus is assembled by its own keyword matcher, so
   the denominator is a property of the matcher rather than of the press.

**The precedent is nonetheless retained in the thesis argument.** GPR and EPU
establish the *diagnosis* — that a text-derived risk index must be insensitive to
how much text was read. This system satisfies that requirement by **bounding**
the statistic rather than by dividing it, which achieves the same invariance
without requiring an unobservable denominator.

### 4.3 Rejected: the mean

The decay-weighted mean is bounded and produced the healthiest downstream
magnitudes, but it **dilutes**: adding a low-severity story *reduces* the
corridor's risk score. This is not a theoretical concern — it was observed
directly, as the only candidate whose sampling ratio fell materially below 1.00
(0.85 and 0.69), meaning that combining ingestion paths *lowered* the score. A
risk measure that can be suppressed by the arrival of innocuous news is a worse
failure than the defect being corrected. Rejected.

---

## 5. The adopted formulation

Let w₍₁₎ ≥ w₍₂₎ ≥ … ≥ w₍ₙ₎ be the story weights of a corridor in decreasing
order, and let k = 3. The corridor risk score is

> **R_raw = (1/k) · Σ_{i=1}^{k} w₍ᵢ₎**,  where w₍ᵢ₎ = 0 for i > n
>
> **R = b + (1 − b) · min(1, R_raw / w_max)**,  w_max = 5.0

In words: **the mean weight of a corridor's three most severe distinct stories,
divided by three even when fewer than three exist, mapped linearly onto the
interval between the corridor's baseline risk and 1.0.**

### 5.1 Boundedness, and the elimination of the free constant

Because each w(i) ≤ w_max, the statistic satisfies 0 ≤ R_raw ≤ w_max
unconditionally. The normalisation is therefore a **definition rather than a
calibration**: R = 1.0 corresponds precisely to *three severity-5, full-confidence
stories published today* — that is, a corridor reported closed by multiple
independent sources. No constant requires empirical fitting. The parameter K is
removed from the operational pipeline.

This is methodologically significant: the uncalibrated constant K had been a
standing limitation of the project, deferred to historical backtesting. The
revision does not calibrate it; it removes the need for it.

### 5.2 The role of zero-padding

Dividing by k rather than by min(k, n) is deliberate. A single severity-5 story
yields R_raw = 5/3 ≈ 1.67, whereas three such stories yield R_raw = 5.0. The
padding therefore encodes the distinction between **an isolated incident** and a
**sustained, multiply-reported situation**, which an unpadded mean would collapse.
Without padding, one headline and a week-long campaign of identical headlines
score identically.

### 5.3 Choice of k

k ∈ {1, 3, 5} were all measured to be fully sampling-invariant (ratio 1.00). k = 3
was selected as the smallest window that distinguishes an isolated event from a
sustained one, and it produced the widest separation between the two at-risk
corridors (0.078, against 0.061 for k = 1 and 0.075 for k = 5). k remains the one
structural parameter available for sensitivity analysis.

### 5.4 Relationship to the preceding stages

The revision is confined to aggregation. In particular, **story clustering occurs
before the order statistic is taken**, which is essential: without it, a single
heavily syndicated wire report could occupy all three positions and saturate the
score on its own.

---

## 6. What the revision resolved

### 6.1 Sampling invariance

The sampling ratio fell from 1.71 to 1.00 (§3.4). Adding an entire second
ingestion path leaves the score unchanged, because that path does not discover
anything *more severe* than what was already observed.

### 6.2 Restored discriminating power

| | Original | Revised |
|---|---|---|
| Hormuz | 0.9607 | 0.6153 |
| Red Sea | 0.9217 | 0.5372 |
| **Separation** | **0.039** | **0.078** |

The separation doubled, and the scores moved out of the saturated region of the
transform into a range where further deterioration remains representable.

### 6.3 Restored physical interpretability downstream

| Quantity | Original | Revised |
|---|---|---|
| Risk-weighted maximum flow | 2.191 mb/d | 3.039 mb/d |
| Deliverability impaired | 48.2% | 28.1% |
| Hormuz marginal capacity loss | 0.087 mb/d | 0.833 mb/d |
| Hormuz static capacity loss | 1.784 mb/d | 1.784 mb/d (unchanged) |

Static (structural) quantities are by construction unaffected, since they are
computed on the invariant `volume` attribute. Only the risk-weighted quantities
move.

### 6.4 The principal research finding was unaffected

The project's central contribution is the *difference* between the static
criticality ranking and the risk-weighted ranking. That result is **identical
under all eight candidate aggregation functions**:

| Corridor | Static rank | Risk-weighted rank | Shift |
|---|---|---|---|
| Cape of Good Hope | 2 | **1** | **+1** |
| Strait of Hormuz | 1 | **2** | **−1** |
| Red Sea | 3 | 3 | 0 |

The mechanism: the Strait of Hormuz is already so degraded by current conditions
that removing it entirely eliminates comparatively little *additional* flow,
whereas the near-unaffected Cape route has silently become the load-bearing
corridor, and its loss would now be the most damaging.

This constitutes the third independent confirmation of the finding, across a
change in corpus size, a change in ingestion method, and now a change in scoring
function. Its robustness follows from the fact that it reads the network's
structural response to the scores rather than the numerical spread of the scores
themselves.

### 6.5 Live validation

Nine days of additional news (599 new articles, taking the corpus from 257 to 795
corridor-attributed events) arrived after the revision was deployed, providing an
unplanned natural experiment. The new material had a **lower** mean severity than
the existing corpus (2.65 against 3.07), i.e. it was predominantly bulk coverage,
but it contained several genuinely severe recent reports.

The two statistics responded in qualitatively different ways:

| Hormuz | Pre-existing events | All events | Factor |
|---|---|---|---|
| Summed R_raw (superseded) | 75.2 | 263.9 | **×3.51** |
| Top-3 R_raw (adopted) | 3.104 | 4.341 | ×1.40 |
| Score R (adopted) | 0.697 | **0.895** | — |

Under the superseded formulation, R_raw = 263.9 normalises to **0.99998**, with
the Red Sea at 0.9927 — a separation of **0.007**, i.e. a complete return to
saturation after a single day's ingestion. Under the adopted formulation the same
data yields 0.895 against 0.738, a separation of **0.157**, the widest the system
has produced.

The increase was verified to be substantively justified by inspecting the stories
responsible — all severity 5, confidence 0.90, under one day old, and including
one quantitative physical indicator:

- *"Iran Adds Nuclear Inspections to Hormuz Reopening Offer"*
- *"Tehran sets Hormuz deadline as truce hopes grow"*
- *"What's in Iran's seven-day plan to reopen the Strait of Hormuz"*
- *"Hormuz Tanker Transits Crash to Single Digits as Crisis Deepens"*

Further, the newly arrived stories **alone** produce the identical top-3 value of
4.341 that the full corpus produces, confirming that the statistic is driven by
the severe tail rather than by accumulated volume.

### 6.6 Robustness to syndication imbalance

In the enlarged corpus, story clustering compressed Hormuz coverage by 4.03× but
Red Sea coverage by only 1.93× — one report appeared 44 times and another 14
times. An imbalance of this kind (2.1× between corridors) is precisely the
condition previously documented to invert the ranking under a summed score. Under
a bounded order statistic it is structurally irrelevant, since each story
contributes once and only the three largest contributions are read.

The syndication ratio is retained as a reportable measurement in its own right:
it quantifies wire-service replication in energy reporting.

---

## 7. Limitations retained

Honest reporting requires that the following be stated.

1. **The absolute level remains uncalibrated; only the functional form is
   settled.** A score of 0.895 enters the network model as a claim that ~90% of
   the corridor's capacity is unavailable. This follows from the inherited
   modelling assumption `effective_capacity = volume × (1 − R)`, which treats a
   news-derived index as a literal physical closure fraction. The revision does
   not validate that assumption. What it changes is the *character* of the
   remaining question: no longer "what value should an arbitrary constant take?"
   but "is the three-most-severe-stories window the right estimator?" — which is
   answerable against historical episodes on a scale that is already
   interpretable.

2. **A weaker, residual volume effect persists.** The statistic is invariant to
   the addition of stories that are not more severe than those already observed.
   However, the *expected value* of the maximum of n draws increases with n: a
   larger sample is more likely to contain an extreme observation. This is an
   order-statistics property, not a defect of the implementation, and it is
   qualitatively much weaker than a sum's linear growth and bounded above by
   w_max — which the current corpus already approaches (4.34 of 5.0), leaving
   little room for further inflation. It is mitigated by holding the ingestion
   window fixed so that n is approximately stationary between runs.

3. **Stored raw scores are not comparable across the revision date.** Historical
   records written before 26 September 2026 contain unbounded sums (order 10¹–10²);
   subsequent records contain the bounded statistic (0–5). Both are retained as
   genuine history, but they must not be plotted on a common axis.

4. **Zero-event corridors reflect a genuine limitation of the news signal, not of
   the formula.** The Cape route holds no extracted events and therefore sits
   permanently at its 0.05 baseline. This was verified by manual inspection rather
   than assumed: articles matching "Cape" are almost always reports of vessels
   *diverting via* the Cape to avoid the Red Sea, i.e. Red Sea risk under another
   name. The Cape's high criticality is consequently **structural** (it carries
   2.255 mb/d of modelled inflow) rather than news-driven, and the +1 rank shift
   should not be presented as a purely dynamic result.

5. **The secondary centrality measure is not a reliable corroborating indicator.**
   Capacity-weighted betweenness centrality on a 50-node network is effectively a
   step function — it changes only when the shortest-path structure changes. A
   sweep of Hormuz risk shows the Cape/Hormuz centrality ordering flipping between
   risk values of 0.85 and 0.895, while the capacity-loss ordering is stable
   across the entire 0.10–0.95 range. Corridors are therefore ranked by capacity
   loss, which carries physical units; centrality is reported only as a
   diagnostic, and the centrality crossover is **not** claimed as a result in
   either direction.

6. **An unrelated defect in the response-layer trigger remains open.** The
   specified conditional (`centrality > 0.65 AND risk > 0.50`) cannot fire: the
   maximum observed centrality on this network is 0.0756, while the risk clause
   now passes for both scored corridors. Both thresholds require respecification
   before the response layer is activated. This is independent of the revision
   described here.

---

## 8. Verification

- **Automated tests:** 304, all passing. The load-bearing regression test asserts
  that adding twenty further stories leaves the score numerically unchanged while
  the superseded sum grows more than sixfold on the identical fixture.
- **Comparison harness:** `manage.py compare_scoring` re-scores the stored corpus
  under all eight candidates and reproduces every table in §4. It performs no
  writes, no network access and no API calls, and can be re-run at any time.
- **Implementation coupling:** the harness's adopted-candidate column delegates to
  the production function rather than reimplementing it, so the two cannot diverge.
- **Cross-validation:** static (risk-independent) capacity-loss figures continue to
  reproduce the values independently computed at graph-construction time
  (Hormuz 1.784, Cape 1.609, Red Sea 0.202, baseline flow 4.228 mb/d).

---

## 9. References

Caldara, D. and Iacoviello, M. *Measuring Geopolitical Risk.* Board of Governors
of the Federal Reserve System, International Finance Discussion Paper No. 1222;
later published in the *American Economic Review*.
https://www.federalreserve.gov/econres/ifdp/files/ifdp1222.pdf

Baker, S. R., Bloom, N. and Davis, S. J. *Measuring Economic Policy Uncertainty.*
Index and methodology at https://www.policyuncertainty.com

GPR index data and online methodological appendix:
https://www.matteoiacoviello.com/gpr.htm
