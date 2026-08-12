# OrdCast RET architecture

Status: design contract for an incremental research build. Sections marked **v0.1**
exist in code; the rest must earn promotion through rolling hindcasts.

## Product boundary

OrdCast is a local probabilistic layer, not a replacement for global numerical weather
prediction. It consumes issued ensembles and observations available at the forecast
cut-off, learns Ord-specific errors and regimes, constructs local event trajectories,
and translates them into agricultural impacts.

The system supports distinct products because useful resolution changes with lead:

| Lead | Output resolution | Defensible targets |
|---|---:|---|
| 0–2 h | 10 min | Storm motion/arrival, rain onset/cessation, gust and heat risk |
| 2–6 h | 30 min | Initiation/decay probability, accumulation, spray-window survival |
| 6–72 h | Hourly | Rain events, temperature/dewpoint, wind/gust, radiation and ETo |
| Days 4–10 | 3-hour/daily | Rain thresholds, heat hours, dry windows and field water balance |
| Days 11–42 | Weekly | Rain total/wet days, dry-spell and heat risk, wet-season progress |
| 1–6 months | Monthly | Terciles/exceedances, onset distribution and heat-day counts |

Exact storm timing is not advertised beyond about three days, nor daily weather in
weeks two to six.

## Regime–Event–Twin model

For issue time `t0`, the intended factorisation is:

\[
p(Y,L \mid D_{\leq t0}) =
\sum_r p(r \mid X,O)
\sum_m w_{m,r,h}\,
p(Y \mid X_m,r,O,L_{t0})\,
p(L \mid Y,I,C)
\]

Where `X_m` is an issued global ensemble member, `O` is currently available local
evidence, `r` is an Ord regime, `Y` is a joint weather trajectory, `L` is field water
state, `I` is a known irrigation plan, and `C` is crop/field configuration.

### 1. Regime

A soft, multilabel gate assigns probabilities to locally meaningful and potentially
overlapping environments rather than one hard class: dry trades, build-up convection,
monsoon break, active monsoon, and tropical-low/cyclone influence. Candidate inputs include:

- moisture depth and precipitable water;
- temperature/dewpoint profiles and instability;
- low/mid/upper winds and shear;
- pressure tendency and convergence;
- recent Himawari cloud growth;
- local heat, humidity, soil moisture, and antecedent rainfall;
- continuous RONI, DMI, RMM1/RMM2, and seasonal encodings at suitable leads.

Clusters or latent regimes are fitted inside each training fold. Regime-specific
experts shrink toward a pooled Ord model so rare cyclone-like cases cannot overfit.

### 2. Event

Rain is represented as a coherent event with onset, duration, motion, footprint,
intensity and accumulation—not a set of independent hourly regressions.

- 0–6 h: radar/satellite optical flow supplies motion; cloud-top cooling and local
  boundary-layer state supply initiation/decay probability.
- 6 h–10 d: issued global members provide synoptic trajectories; calibrated event
  occurrence and amount models add local uncertainty.
- Weeks/months: predict distributions of totals, wet days, dry spells, and onset; do
  not manufacture daily sequences until a validated stochastic disaggregator exists.

Marginals use logistic/distributional boosting, Gamma/Tweedie or quantile models. A
validated ensemble-copula/analog coupling stage is required before they become joint
trajectories. Physical projection then enforces non-negative rain/wind, RH in 0–100%,
dewpoint no higher than temperature, solar below a clear-sky envelope, and monotonic
accumulated rain.

**v0.1:** `ordcast.fusion` creates independently weighted marginals at each horizon.
It deliberately does not claim joint temporal dependence.

### 3. Twin

Each validated joint trajectory will drive FAO-56 Penman–Monteith ETo and a
mass-conserving root-zone bucket:

\[
S_{t+1}=S_t+P_{effective}+I-ET_c-runoff-drainage
\]

Derived outputs are probabilities for effective rain, root-zone deficit, irrigation
requirement, trafficability/waterlogging, hot hours, and a configurable valid spray
window. Chemical/operator thresholds belong to field configuration; they are never a
universal hard-coded rule.

## Evidence weighting (**v0.1**)

Every input source contains ensemble or analog members for identical lead times. The
system scores distributions with continuous ranked probability score (CRPS), maintains
a horizon-specific exponentially weighted loss, applies a source-specific freshness
decay, and converts comparable losses into bounded weights.

The algorithm is intentionally conservative:

- an unscored source receives a neutral median-loss prior;
- every quality-accepted source retains a small floor weight so short bad runs do not
  permanently kill it; failed, corrupt, or explicitly rejected sources are excluded first;
- each horizon enters the ledger only after its own target interval is available;
- a local analog is eligible only when its target was available before the current cut-off;
- marginal probabilities and quantiles remain explicit.

In v0.1, rainfall at each horizon means cumulative millimetres from issue time through
that horizon; verifying observations must use the identical interval. The EWMA learning
rate, softmax temperature, freshness half-lives, and 2% weight floor are hand-set
baseline heuristics, not calibrated Ord constants. Rolling hindcasts must tune/ablate
them against equal weighting and no freshness decay. v0.1 shrinks ledger losses toward
a neutral prior equivalent to five observations; that strength must also be tuned, then
extended with regime-aware uncertainty. One fresh result must not be treated as strong
evidence in an operational model.

Because lead-specific weights can otherwise make a cumulative rainfall distribution
move backwards, v0.1 takes the union of every weighted empirical CDF's exact mass
breakpoints and applies a cumulative maximum across horizons. This preserves already
ordered marginals and guarantees stochastic ordering without interpolating tail mass.
The rows are an artificial rank coupling—not event trajectories—and cannot drive the
farm twin.

The v0.1 engine accepts supplied `recent_crps` for deterministic experiments. In
operations those values must come from the versioned ledger, not a client assertion.
The current API is stateless and does not supply that persistence or provenance.

## Availability-first data contract

Every immutable record requires:

| Field | Meaning |
|---|---|
| `event_time` | Physical observation or model valid time |
| `available_at` | Earliest instant OrdCast actually possessed the value |
| `issue_time` | Forecast cycle time, where relevant |
| `ingested_at` | Receipt timestamp |
| `source` | Provider and product |
| `source_version` | Model/instrument processing version |
| `member` | Ensemble member, where relevant |
| `units` / `crs` | Explicit scientific metadata |
| `qc` | Missing, stale, suspect and correction flags |
| `licence` | Attribution and redistribution constraints |

Feature views enforce `available_at <= forecast_issue_time`. Reanalysis and final
satellite/gauge products may label historical cases, but cannot masquerade as live
features. Original issued NWP cycles and their publication latency are archived.
`AnalogLibrary` itself protects target maturity only; production feature-vintage and
receipt-time enforcement belongs to this availability-aware feature view.

The SILO daily baseline is restricted to Kununurra Aero station 2056 and uses a
conservative 09:00 AWST convention. Features end at `issue_date - 1`; the issue-date
row is excluded; and cumulative targets start at `issue_date + 1`. A normal query is
issued on the day after the snapshot's final date. The downloader writes raw bytes and
a retrieval-time/hash/request manifest without overwriting an existing snapshot. A
newly downloaded, revised SILO history cannot reproduce what a forecaster knew years
ago: current snapshots support present-day analog inference and climatology only.
Historical hindcasts require archived snapshot vintages, so the baseline CLI rejects
historical issue dates rather than silently leaking revised history.

## Forecast flow

```python
def issue_forecast(issue_time, fields, planned_irrigation):
    inputs = load_only_available_data(issue_time)
    assert inputs.available_at.max() <= issue_time

    local_state = bias_filter.update(inputs.observations)
    regime_probability = regime_gate.predict_proba(inputs, local_state)
    marginals = weather_models.predict_distributions(
        inputs, local_state, regime_probability
    )
    events = event_model.sample(inputs, regime_probability)
    trajectories = couple(marginals, events, inputs.raw_ensemble)
    trajectories = enforce_physics(trajectories)

    impacts = farm_twin.run(trajectories, fields, planned_irrigation)
    return bundle(issue_time, trajectories, impacts)
```

## Validation design

The initial retrospective experiment uses GEFSv12 reforecasts from 2000–2019. Start
with at least eight complete wet seasons, advance the outer test one wet season at a
time, and tune only on earlier seasons. Fit every scaler, imputer, regime, feature
selector and calibrator inside its fold. Purge overlapping targets and keep entire storm
episodes within one fold.

Mandatory baselines:

1. hour/day-of-year local climatology;
2. persistence;
3. raw NWP ensemble;
4. simple lead-and-season bias correction;
5. radar/satellite advection at short lead;
6. archived public forecast where terms allow a fair external benchmark.

Primary scorecard:

- CRPS for 24-hour rainfall;
- Brier skill and reliability for rainfall above 10 mm at days 1–5;
- onset timing error and event hit/miss;
- CRPS and 80/90% coverage for temperature and wind;
- Brier score and cost/loss value for configurable three-hour spray windows;
- block-bootstrap 95% confidence intervals by storm or wet season.

Promotion requires improvement over the strongest baseline on predeclared targets,
without degraded calibration or important extremes. ROC-AUC alone is insufficient.

## Delivery milestones

| Gate | Deliverable | Promotion evidence |
|---|---|---|
| 0 — foundation | **v0.1:** schemas, marginal fusion, SILO ingestion/analog baseline, CRPS/Brier, maturity-aware hindcast, API | deterministic tests and CI |
| 1 — archive | **Starter implemented:** immutable Kununurra Aero, regional GEFS, and Himawari inputs; farm observations and budgeted ECMWF follow | completeness, latency, gap, and QC dashboards |
| 2 — baseline | climatology, raw/bias-corrected ensemble, trained local analog | rolling 2000–2019 scorecard |
| 3 — nowcast | Himawari growth + Wyndham motion/uncertainty | storm-blocked 0–6 h verification |
| 4 — RET weather | regime gate, event generator, joint coupling and constraints | calibrated joint scenarios, golden issues |
| 5 — farm twin | ETo, soil bucket and configurable operations risks | mass balance and on-farm shadow evaluation |
| 6 — shadow ops | archived 2026–27 wet-season forecasts | live, untouched out-of-sample results |

No gate is skipped because a demo looks convincing.
