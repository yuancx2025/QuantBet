# Resting ladder: training and validation plan

Date: 2026-11-06. Scope: research and paper plans for orders chosen before tipoff and held through the game. This document proposes the next implementation steps from the current discussion; it does not start training, change the existing holdout, approve purchases, or mark other Stage 0 proposals accepted.

## 1. What exists and what comes next

The repository currently contains the Stage 0 data/timing spike, its cached responses, charts, and decision records. It has no production ingestion pipeline, point-in-time feature tables, training modules, or trained models. The next implementation milestone is a validated training dataset and one reproducible baseline training run.

The 10 spike games were selected to include comebacks, blowouts, and other unusual outcomes. Keep them as engineering fixtures. Do not use their recovery frequencies as a representative training sample or as evidence of profitability. Ingest all eligible regular-season games in each selected development period, and report coverage and missingness.

## 2. Models and their roles

| Model | Target | Inputs at prediction | Training data | Role |
|---|---|---|---|---|
| A. Score-based recovery | Probability of eventual victory from a specified basketball state | Score margin, checkpoint/time, pregame strength, home/neutral status, and both teams' prior tendencies | Licensed scores and results, with pregame strength from dated odds | Test the second-half recovery hypothesis |
| B. Touch/fill | Probability a candidate resting bid meets a declared historical execution rule after placement | Pregame information and candidate price/size only | Historical exchange paths paired with pregame features | Estimate whether the order is likely to execute |
| C. Pregame post-fill | Probability of eventual victory conditional on that candidate order meeting the same execution rule | Pregame information and candidate price/size only | The filled subset of the order dataset, labeled by actual game result | Estimate the outcomes of the situations a resting bid selects |

Start with three small pooled regressions, not separate models for each team. B and C are the two quantities needed by the pregame selector. A independently tests the basketball hypothesis and helps interpret fills. It is deliberately a narrow recovery model, not a full live pricing engine.

All three can fit independently once their datasets exist. B and C share the same order rows and execution definition. C does not need A's predictions as an input. This keeps the first version simple and avoids using a future fill state in a pregame recommendation.

A cannot simply replace C: before tipoff, the score and time at which an order might fill are unknown. Replacing C would require an additional model for the distribution of fill states, then averaging A's recovery predictions over that distribution. That is a later experiment, not part of the initial training cycle.

Defer a score-path simulator, a comprehensive minute-by-minute winner model, and state-triggered order placement. They are not prerequisites for this resting-ladder MVP.

## 3. Freeze the data and prediction rules first

1. Use 2025-26 regular-season games for development. Leave 2026-27 behind the existing holdout boundary. Preserve the excluded game in `docs/decisions/0002-holdout-exclusion.md` when implementing split configuration.
2. Use licensed basketball data for model fitting. Older licensed quarter scores may support halftime research and team priors once their coverage and usage rights are confirmed. Older scores cannot supply exchange execution labels.
3. Define one planning time: two hours before scheduled tipoff, using the schedule information available then. Use the recorded planning timestamp when available. Actual first-play time belongs to alignment and grading; do not use future schedule revisions to construct pregame features.
4. Use sportsbook probabilities captured at or before planning time, with the margin removed. Do not use the later closing line for an earlier plan. Store source, publication timestamp, snapshot age, and missingness.
5. Define maximum snapshot age and coverage requirements in configuration before scoring models. Start with the plan's 95% coverage target, and report whether missing games differ systematically from covered games.
6. Start with one-contract hypothetical orders and a small fixed price grid: 5, 10, and 15 cents below the Kalshi mid observed at planning time. Keep prices in the valid range; skip marketable bids and bids outside the declared research range. Log every later grid expansion.
7. Specify order expiry, eligible game window, and execution rules before building labels. Research can proceed while order-survival verification is outstanding, but plans remain hypothetical until that gate is resolved.

If historical odds have no reliable as-of timestamps, mark the corresponding experiment as a reduced-data study. It cannot validate a two-hour-before-tipoff selector against sportsbook information it did not actually have.

## 4. Build the shared tables

| Table | One row per | Required fields and checks |
|---|---|---|
| Games | Game | Stable ID, season/type, teams, home/neutral status, schedule version, actual start/end, quarter scores, final result including overtime, settlement, source/license lineage |
| Pregame snapshots | Game/team/planning time | Dated sportsbook fair probability, Kalshi bid/ask/mid, spread, observed volume, snapshot ages, all source availability timestamps |
| Team features | Team/as-of time | Prior games played, prior second-half performance and uncertainty, pace/variability if supported, rest, prior-source date and shrinkage parameters |
| Recovery examples | Game/team/checkpoint | Pregame features, signed score margin at that checkpoint, eventual winner label, source availability time |
| Candidate orders | Game/team/limit/size/planning time | Pregame features, future touch/fill labels under each declared rule, result if filled, execution timestamp/range and score for diagnostics only |

Keep final results, minimum future price, eventual volume, fill time, and fill-state score out of the pregame feature columns. They are labels or diagnostics. A recovery row may use its specified checkpoint score; B/C may not use a future score from the same game.

Preserve event, publication, and ingestion timestamps where available. Flag reconstructed latency. Join features backward by availability time, and end the exchange timeline at the final play. Store ambiguous mappings and data gaps as explicit exclusions, not silent matches or invented fills.

For nonbinary or exceptional settlements, exclude the event from binary win-model fitting and win-rate calculations. Keep actual cash settlement and costs in strategy accounting and report the exclusion counts.

## 5. Model A: score-based recovery

### First version

Start at halftime, where the second-half hypothesis is directly measurable and quarter-score data suffice. Use one row per game/team at halftime, with the target equal to the final winner, including overtime. Both team perspectives stay in the same split; weight them so each game has equal total influence.

Compare three versions:

1. A0: pregame sportsbook probability left unchanged. This explicitly tests the proposed approximation.
2. A1: regularized logistic regression using pregame probability, halftime margin, and home/neutral status.
3. A2: A1 plus a small set of both teams' prior second-half tendencies and uncertainty indicators.

Estimate team tendencies from games completed before the current game's planning time. Adjust second-half performance for opponent strength, pregame expectations, and the halftime situation using a baseline fitted only on earlier games. A good second-half record against weak opponents or after large deficits is not automatically a persistent team effect.

Shrink each team's tendency toward a league prior, or a down-weighted prior-season estimate. Use the existing plan's minimum of 10 prior games for trusting a team-specific estimate; below that, fall back toward the prior and expose the limited support. Tune shrinkage and decay only within the development folds.

### Expected behavior and validation

- For the same matchup, a larger halftime lead should not reduce the predicted chance of winning. Test a representative score-margin grid.
- Predictions stay in [0, 1], avoid unsupported certainty, and depend on the opponent as well as the selected team.
- A2 is useful only if it improves later-game predictions over A1. A0 is a baseline, not a probability assumed to remain correct after falling behind.
- Report Brier score, log loss, reliability curves, independent-game counts, and uncertainty by deficit bucket and pregame-strength bucket. Inspect the trailing-at-halftime subset separately.
- A reported 60% should correspond to roughly 60% wins in comparable unseen examples, within sampling uncertainty.
- Keep a halftime model scoped to halftime. Do not apply it to arbitrary fourth-quarter fills. Extend to additional checkpoints only as a separately logged experiment.

If team tendencies add no reliable predictive value, remove those features from recommendations and record that result. Do not force a positive second-half effect.

## 6. Model B: touch/fill

### Labels and first version

Build candidate orders for every eligible game, not just games with price drops. Start with a touch label: whether a trade prints strictly below L after planning time and before the final play. Train a small regularized logistic regression with pregame sportsbook probability, L, distance below the planning-time Kalshi price, pregame spread/liquidity, and optional prior team tendencies.

Touch is an observable price event, not proof of execution. The primary backtest uses the declared conservative proxy: a trade strictly below L, with sufficient observed volume for the fixed research size. Track an optimistic touch-at-L scenario separately. Missing trade coverage cannot establish a nonfill. Order-book data and measured latency can later refine the execution model; volume checks alone do not reconstruct every queue position.

If the selector uses the conservative fill proxy, fit B to that fill label, or estimate a separately validated touch-to-fill adjustment. Do not multiply touch probability by C trained on a different fill event and call the result execution EV.

### Expected behavior and validation

- With the matchup, size, planning time, and other inputs fixed, raising a resting buy price should not reduce touch probability. Encode a monotone price term or constrain a later tree model.
- Evaluate fill counts and reliability by price offset, month, and pregame liquidity. Use Brier score/log loss against a simple historical rate by offset and pregame-strength band.
- Compare the market-only version with a version adding team behavior. Improvement is an empirical question.
- Missing coverage produces an unknown label or exclusion, not a fabricated negative example.
- Report disagreement between conservative and optimistic execution assumptions as uncertainty.

Do not impose the same monotonic relationship on C's conditional win probability: changing the limit changes the selected situations.

## 7. Model C: pregame post-fill

Train only on candidate orders meeting the execution rule selected for the corresponding B run. The target is 1 for a subsequent team win and 0 for a loss. Unfilled orders are absent from this conditional fit, not labeled as losses.

Use the same pregame features and candidate price as B. Start with a regularized logistic regression, benchmarked against a smoothed historical win rate among fills in comparable price/strength bands. Add prior second-half tendencies as the specific hypothesis under test. Actual fill time, score at fill, future injury information, and future volume are forbidden inputs.

C learns the average eventual outcome across the situations that a pregame bid selects. It needs historical exchange execution labels, even though A's basketball training does not.

Validate conditional Brier score, log loss, and calibration on later filled games. Report support by limit-price band, pregame strength, month, and liquidity. A large number of nearby thresholds on the same game does not create independent evidence. Use a small fixed grid and equalize game influence during fitting.

Compare A's predictions at legitimate matching checkpoints with realized outcomes as a diagnostic. Do not treat that retrospective score knowledge as the pregame prediction of C. A good halftime calibration curve does not establish good calibration at selected fill events.

If conditional samples are sparse, pool more broadly and abstain in unsupported regions. More model complexity does not solve missing independent games.

## 8. What can run at the same time

| Work | Can proceed independently? | Dependency |
|---|---|---|
| Licensed score ingestion and recovery examples | Yes, alongside exchange ingestion | Source access, game IDs, as-of contract |
| Exchange ingestion and order labels | Yes, alongside score research | Mapping, planning time, execution rule, timestamp QA |
| A fitting | Yes, alongside B/C | Validated recovery examples and chronological folds |
| B and C fitting | Yes, alongside each other and A | Validated order rows; C uses the filled subset |
| Calibration | Separately per model | Its fitted model plus a later, disjoint calibration period |
| Combined selector/backtest | After B/C produce validated out-of-time predictions | Compatible execution target, fees, risk limits, baselines |

Parallel execution saves elapsed time; it does not replace the shared data checks. Reuse one immutable dataset manifest and one split assignment across all runs.

## 9. Chronological validation

Use whole-game, date-ordered splits; all teams, price thresholds, and checkpoints from a game stay together. Implement a game-level splitter rather than applying random row splits or unmodified row-based time splits.

An initial monthly development schedule is:

| Evaluation month | Fit | Calibrate |
|---|---|---|
| January 2026 | October-November 2025 | December 2025 |
| February 2026 | October-December 2025 | January 2026 |
| March 2026 | October 2025-January 2026 | February 2026 |
| April 2026 regular-season dates | October 2025-February 2026 | March 2026 |

Only licensed, eligible data enter each period. Older permitted basketball data may extend A's fit window, not B/C's exchange history. Earlier development results may inform later development choices; label them tuning results and log every choice. They are not a substitute for the untouched final holdout.

Fit imputation, scaling, feature selection, opponent adjustments, hyperparameters, and any calibration strictly inside their allowed periods. Recompute team features causally before every game using the fixed feature rule; this does not authorize refitting model parameters on the evaluation month.

Start with uncalibrated logistic baselines. Add sigmoid calibration only if later-period checks support it. Avoid flexible isotonic calibration on a small number of independent games. Calibration data must be disjoint from base-model fit data, and its quality must be checked on the next period.

Report paired differences in prediction scores and bootstrap whole games for uncertainty. Do not count thousands of thresholds or snapshots as thousands of independent games. A two-percentage-point calibration target is an aspiration for sufficiently supported buckets, not a statistically credible pass/fail claim for every small bucket.

## 10. Build and validate the selector

For ordinary binary settlements, fixed one-contract orders, price L, and fee f per executed contract, estimate:

`EV per submitted contract = P(fill) * [P(win | fill) - L - E(fee | fill)]`

Use compatible B/C targets and fee assumptions. Initially grade each price separately. Before producing a multi-rung ladder, replay simultaneous rungs together, because their fills and payoffs are correlated. Apply fixed game-level and portfolio exposure limits before calculating realized returns.

Compare these variants on the same eligible games and execution assumptions:

1. No orders: zero trading P&L.
2. Fixed-offset naive ladder.
3. Market-only B/C selector.
4. B/C selector with the tested team-behavior features.

Report net P&L, EV per submitted order, return per filled contract, fill rate, unique games, costs, exposure, and confidence intervals. Split diagnostics by month, liquidity, limit price, and sweep/drift classification where measurable. Sweep/drift labels based on future behavior are diagnostics, not pregame features. Define these tags before evaluating them.

Expected behavior: lower bids may fill less often; more fills need not mean better returns; positive predicted EV must survive realized outcomes and costs. An empty ladder is a valid output when support or estimated advantage is insufficient.

Keep actual execution P&L distinct from entry quality against an external reference. A sportsbook or Polymarket snapshot is a benchmark, not ground truth; disclose its age, coverage, and relation to Kalshi. Without a usable external reference, report realized settlement P&L and clearly labeled exchange markouts instead of claiming sportsbook-relative entry quality.

Fee rate/history and order survival through tipoff are unresolved in Stage 0. Resolve them before presenting fee-adjusted resting-order results as executable evidence. Meanwhile, report gross results and explicitly labeled fee sensitivities. Do not silently assume free execution.

## 11. Acceptance and failure behavior

| Gate | Required evidence | If it fails |
|---|---|---|
| Data | Coverage report, correct mappings, licensed lineage, valid as-of times, explicit gap handling | Repair or narrow the study; no model-performance claim from compromised rows |
| No lookahead | Deleting future games does not change past team features or pregame predictions; future labels remain unavailable to the selector | Stop integration and fix the leaking feature or join |
| A hypothesis | Out-of-time comparison of A0/A1/A2 with support and uncertainty | Drop unsupported tendencies or record the approximation as rejected/inconclusive |
| B/C quality | Calibration and prediction losses versus simple baselines on later games, including the selected-fill subset | Keep the simpler baseline, abstain, or collect more data |
| Strategy | Full portfolio replay after costs, conservative/optimistic scenarios, comparisons with naive and no-order baselines | Report negative or inconclusive results; do not search the final holdout for a better strategy |
| Evidence | Independent-game counts and game-bootstrap intervals; at least the existing 200-fill reporting floor | No positive-edge conclusion below the floor; passing 200 alone proves nothing |
| Holdout | Frozen config/data/model versions, one test run, exclusion honored, tuning interface cannot access it | Repair enforcement before testing; never rerun to improve the result |
| Forward | Fixed daily pregame paper plans, timestamped before games, at least four weeks | Report operational gaps and continue the log; do not reconstruct missing plans afterward |

Label a positive edge as supported only when its uncertainty and execution/cost assumptions support that conclusion. Better predictions alone do not establish profitability. A valid final result can be negative or unable to distinguish the edge from zero.

The default holdout uses no refitting on 2026-27 labels. Any future walk-forward refit must be specified before freeze and separately audited. Current-season rolling features may update from earlier completed games under a feature rule frozen beforehand; this is different from changing that rule or tuning model parameters after seeing test results.

## 12. Concrete implementation sequence

The modules below are proposed work, not tools already available in the repository.

1. Add split/data/feature/execution configurations and an immutable dataset manifest. Record the holdout exclusion and feature availability contracts.
2. Build licensed games/quarter-score and dated pregame-odds adapters, plus normalized exchange candles/trades adapters. Reuse the spike only as fixtures and source-quirk evidence.
3. Build mapping, as-of joins, coverage reports, halftime examples, and candidate-order labels. Implement future-deletion, chronological-grouping, gap-handling, touch/fill, settlement, and schema tests.
4. Add `features/team.py`, `models/recovery.py`, `models/touch.py`, `models/post_fill.py`, `models/calibration.py`, and `training/splits.py`. Start with regularized logistic regression and small prespecified feature comparisons. Add a shallow monotonic tree candidate only after baseline error analysis justifies it.
5. Run the A baseline as soon as the licensed halftime table passes QA. Run B/C once exchange order labels pass QA; they may overlap A. Save held-forward predictions and a short model report for every run.
6. Add `selection/ladder.py` and a portfolio replay. Use B/C's pregame predictions for recommendations; keep retrospective recovery diagnostics separate.
7. Freeze one strategy after development, run the protected holdout once, report the outcome either way, then log forward paper plans.

Each run must save config/data hashes, code version or a source snapshot while the repository has uncommitted work, training/calibration/evaluation ranges, random seed, exclusions, independent-game counts, fitted pipeline, out-of-time predictions, and metric tables. Repeating the same run must reproduce predictions within a declared numerical tolerance.

The first reviewable delivery is a coverage report, a causally computed team-tendency table, and A0/A1/A2 comparisons on later games. B/C reports follow as soon as exchange labels are ready. No profitability claim is required for this milestone.

## Sources and current project references

- `README.md`: current Stage 0 status.
- `scripts/stage0_spike.py`, `select_games`: deliberate selection of fixture outcomes.
- `docs/decisions/0001-stage0-findings.md`: timing, data quirks, sweeps, and unresolved fees/order-survival questions.
- `docs/decisions/0002-holdout-exclusion.md`: existing holdout exclusion.
- `docs/decisions/0003-plan-changes-after-stage0.md`: proposals remain subject to their recorded status.
- [scikit-learn: time-ordered validation](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html): chronological evaluation principle; this project additionally requires grouping all rows of each game.
- [scikit-learn: probability calibration](https://scikit-learn.org/stable/modules/calibration.html): disjoint calibration data and overfitting considerations for flexible calibration.
