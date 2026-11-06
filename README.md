# QuantBet NBA

QuantBet studies whether limit orders chosen **before an NBA game** can buy a team's
game-winner contract at a price below its probability of winning, after accounting
for fees and the situations in which the order fills. The exchange under study is
Kalshi's `KXNBAGAME` series.

The project is for research and hypothetical order plans. The current code reads
market and basketball data and generates diagnostics; it does not place orders.

## Current status

| Part | Status |
|---|---|
| Stage 0 data and timing spike | Implemented and run on 10 games from 2025–26 |
| Findings, split decisions, and training plan | Documented in [`docs/`](docs/) |
| Backtest engine and 2025–26 exchange data pipeline | Implemented and run on 1,230 games; the first fixed-offset ladder shows no edge. See [0005](docs/decisions/0005-backtest-engine-v1.md) |
| Feature tables and models A/B/C | Planned; no trained models yet |
| Model-based order selector and forward paper plans | Planned |

Stage 0 validates data access, alignment, and source quirks. Its games were selected
to include comebacks, blowouts, and other unusual outcomes. They are engineering
fixtures, not a representative sample or evidence of a profitable strategy.

## Intuition

A team can start as a favorite, fall behind, and still recover. A resting bid below
the pregame price could buy during that decline. Some short price drops may also
come from a sweep of thin exchange liquidity rather than a lasting change in the
team's prospects. These are different possible sources of an advantage, and both
need testing.

The central difficulty is **selection by execution**. Suppose a team has a 70%
pregame win probability and we leave a bid at \$0.50. The apparent 20-cent discount
is misleading: the bid may fill mainly when the team is playing badly. If the team
wins only 45% of the games in which that bid fills, paying \$0.50 loses money even
before fees. A lower price alone does not establish value.

The pregame decision therefore needs two estimates:

1. How likely is this particular bid to fill?
2. Among the situations where it fills, how often does the team eventually win?

A separate basketball model tests whether prior second-half tendencies improve
recovery predictions beyond pregame strength and the halftime score. The project
does not assume those tendencies persist or that a favorite retains its original
win probability after falling behind.

The proposed first experiment makes a plan two hours before scheduled tipoff,
using only information available then. It considers one-contract bids 5, 10, and
15 cents below the observed Kalshi mid, skipping invalid or marketable bids.
Keeping orders active through the game is a research assumption pending
verification of order survival through tipoff. There is no live repricing model
or automatic trading in the current implementation.

## Mathematical framework

The equations below describe the planned research models and accounting. They are
not models already fitted by the Stage 0 script. Prices and costs are in dollars
per contract; ordinary binary contracts pay \$1 on a win and \$0 on a loss.

### 1. Value depends on the outcome conditional on a fill

| Symbol | Meaning |
|---|---|
| $X$ | Information available at the pregame planning time |
| $L$ | Candidate buy limit, with $0 < L < 1$ |
| $Y$ | Final outcome: 1 if the selected team wins, otherwise 0 |
| $F_L$ | Indicator that the order meets the declared execution rule |
| $\Phi_L$ | Fee per executed contract |
| $a(X,L)$ | Fill probability, $P(F_L=1\mid X,L)$ |
| $b(X,L)$ | Conditional win probability, $P(Y=1\mid F_L=1,X,L)$ |
| $\bar\phi(X,L)$ | Expected fee conditional on execution |

For one contract, assuming execution at the limit and no cost when unfilled,
settlement profit is

$$
\Pi_L = F_L\left(Y-L-\Phi_L\right).
$$

Taking expectations gives the decision quantity:

$$
\begin{aligned}
\operatorname{EV}(X,L)
&= E[\Pi_L\mid X,L] \\
&= a(X,L)\left[b(X,L)-L-\bar\phi(X,L)\right].
\end{aligned}
$$

With positive fill probability, break-even requires

$$
b(X,L) > L + \bar\phi(X,L).
$$

For a hypothetical \$0.50 bid, a 30% fill probability, a 54% conditional win
probability, and a \$0.01 fee, EV is
$0.30\times(0.54-0.50-0.01)=0.009$ dollars per submitted contract. If the
conditional win probability is 46%, EV becomes $-0.015$. Unfilled orders contribute
zero in this model. The one-cent fee is an illustration, not a verified Kalshi rate.

Actual accounting must use observed execution prices, actual costs, and cash
settlement. Exceptional or nonbinary settlements stay out of binary win-model
fitting but remain in strategy accounting.

### 2. Pregame strength is an anchor

For American moneyline odds $m$, the raw implied probability is

$$
q(m)=
\begin{cases}
\dfrac{100}{m+100}, & m>0, \\
\dfrac{-m}{-m+100}, & m<0.
\end{cases}
$$

For the two teams, a simple proportional removal of the sportsbook margin gives

$$
p_{0,i}=\frac{q_i}{q_i+q_j},\qquad p_{0,j}=1-p_{0,i}.
$$

This is a market-based estimate, not known true probability. Use an odds snapshot
published at or before planning time. A later closing line cannot be an input to
a plan made two hours before tipoff.

### 3. Three models answer different questions

| Model | Prediction | Information used at prediction |
|---|---|---|
| A: score-based recovery | Eventual win probability from a basketball checkpoint | Pregame strength, halftime margin, venue, and prior team tendencies |
| B: touch/fill | $a(X,L)$ | Pregame information and the candidate order |
| C: pregame post-fill | $b(X,L)$ | Pregame information and the candidate order |

**Model A** initially covers halftime. An illustrative logistic specification is

$$
\hat p_A=\sigma\!\left(
\alpha+\beta\operatorname{logit}(p_0)+\gamma d_{\mathrm{half}}
+\theta^\top h+\delta(\tilde u_i-\tilde u_j)
\right),
$$

where $\sigma(z)=1/(1+e^{-z})$, $\operatorname{logit}(p)=\log[p/(1-p)]$,
$d_{\mathrm{half}}$ is the selected team's halftime lead, $h$ encodes home/neutral
status, and $\tilde u_i,\tilde u_j$ describe prior second-half performance.
Inputs to the logit must be bounded away from 0 and 1.

Small team samples need shrinkage. One possible estimate is

$$
\tilde u_i=\frac{n_i}{n_i+\kappa}\bar r_i
+\frac{\kappa}{n_i+\kappa}u_{\mathrm{prior}},\qquad\kappa>0.
$$

Here $n_i$ counts prior eligible games, $\bar r_i$ is average second-half
performance after adjustment for opponent and game context, and $u_{\mathrm{prior}}$
is a league or permitted prior-season estimate. All inputs and adjustment models
must use earlier games only. With no prior games, use the prior directly.
The training plan calls for at least 10 prior games before trusting team-specific
estimates; the shrinkage strength is chosen inside development folds.

Compare A0 (unchanged pregame probability), A1 (strength, halftime margin, venue),
and A2 (A1 plus prior team tendencies). Keep the extra features only if they help
on later games. A halftime model does not automatically apply to fourth-quarter
fill states.

**Models B and C** can start as separate regularized logistic regressions:

$$
\hat a(X,L)=\sigma\!\left(\beta_B^\top z(X,L)\right),\qquad
\hat b(X,L)=\sigma\!\left(\beta_C^\top z(X,L)\right).
$$

The feature vector $z$ includes pregame strength, limit price, price offset, and
observed pregame spread/liquidity, with optional prior team features. B trains on
all eligible candidate orders with target $F_L$. C trains only on filled orders
with target $Y$. Unfilled orders are not losses in C's training set.

Neither model may use the eventual score at fill, the future minimum price, or
future volume as an input. B and C must use exactly the same execution definition
for their product to estimate EV. Holding other inputs fixed, a higher buy limit
should not lower B's touch probability; C has no corresponding monotonic rule.

A cannot replace C directly: before the game, the score and time at execution are
unknown. Using a recovery model for pregame selection would also require a model
of the distribution of fill states and validation at those checkpoints. The first
version estimates the conditional outcome directly with C.

### 4. A price touch is an execution proxy

The initial strict-touch label is

$$
T_L=\mathbf{1}\!\left\{\exists t\in(t_{\mathrm{plan}},t_{\mathrm{end}}]:
P_t^{\mathrm{trade}}<L\right\}.
$$

The proposed conservative fill proxy additionally requires enough observed traded
volume below the limit for the fixed research size. An optimistic touch-at-limit
scenario is reported separately. These are historical assumptions: prints and
volume do not reconstruct queue position or prove an order would have executed.
Missing coverage produces an unknown label or exclusion, not an assumed nonfill.

Sweep and drift classifications are diagnostics. A classification that uses
subsequent price recovery cannot become a pregame feature.

### 5. A ladder needs joint accounting

For fixed, fully filled-or-unfilled rung sizes $q_k$, the binary-settlement model is

$$
\Pi_{\mathrm{ladder}}=\sum_{k=1}^{K}q_kF_{L_k}
\left(Y-L_k-\Phi_{L_k}\right).
$$

Expected profits add by linearity, but risk depends on covariance:

$$
\operatorname{Var}(\Pi_{\mathrm{ladder}})
=\sum_k\operatorname{Var}(\Pi_k)
+2\sum_{j<k}\operatorname{Cov}(\Pi_j,\Pi_k).
$$

Rungs on the same game share its result and often fill together. The future
backtest must replay them jointly, handle partial fills if sizes expand, and
apply game and portfolio exposure limits. An empty ladder is a valid decision
when estimated value or sample support is insufficient.

### 6. Predictions and profits need separate checks

For predicted probabilities $\hat p_i$ and binary labels $y_i$, report

$$
\operatorname{Brier}=\frac{1}{N}\sum_{i=1}^{N}(\hat p_i-y_i)^2,
$$

$$
\operatorname{LogLoss}=-\frac{1}{N}\sum_{i=1}^{N}
\left[y_i\log\hat p_i+(1-y_i)\log(1-\hat p_i)\right].
$$

Clip probabilities away from 0 and 1 for numerical evaluation. Use final outcome
labels for A, fill labels for B, and final outcomes among fills for C. Check
calibration: predictions near 60% should win about 60% of the time in comparable
unseen cases, allowing for sampling uncertainty.

Prediction quality alone does not establish profitability. Also report realized
net P&L, fill rate, exposure, costs, and uncertainty against no-order, fixed-offset,
and market-only baselines. Group related rows by game and bootstrap whole games;
many thresholds from one game are not independent evidence.

### 7. Stage 0 checks score/price timing

The implemented spike computes the quote midpoint $m_t$ and its clipped log-odds:

$$
m_t=\frac{\operatorname{bid}_t+\operatorname{ask}_t}{2},\qquad
\ell_t=\operatorname{logit}\!\left(\operatorname{clip}(m_t,0.02,0.98)\right).
$$

On a one-minute wall-clock grid, let $d_t$ be the home team's score margin. The
timing gate compares three-minute changes at lags $k$ from $-10$ to $+10$ minutes:

$$
\rho(k)=\operatorname{Corr}\!\left(
d_t-d_{t-3},\;\ell_{t+k}-\ell_{t+k-3}
\right).
$$

Positive lags mean prices move after scoring. A strong peak at a clearly negative
lag calls for a timestamp/alignment investigation. This checks data timing; it
does not measure a tradable advantage. The spike also produces signed price paths
around scoring plays and candle/trade consistency diagnostics.

## Validation boundaries

- **Development:** eligible 2025–26 regular-season games, with chronological
  fit/calibration/evaluation periods. Keep every row from a game in the same split.
- **Tuning:** 2026–27 regular-season games with a ticker date through 2026-12-03.
- **Holdout:** later 2026–27 games, tested once after the strategy is frozen. See
  [`configs/splits.yaml`](configs/splits.yaml) and the
  [decision record](docs/decisions/0004-data-scope-and-tuning-games.md).
- **Data availability:** join features by when they were available, not when a
  later download describes the event. Candle timestamps mark interval ends.
- **Evidence:** the training plan sets a reporting floor of 200 fills; reaching it
  alone establishes neither significance nor an advantage.
- **Open execution questions:** maker fee rates/history and order survival through
  tipoff remain unresolved. Testing treats limit orders as free, so results are
  reported gross and labelled as such; Kalshi lists this series as charging maker
  fees, so add the verified rate back before presenting net results.

The [training and validation plan](docs/resting-ladder-training-plan.md) specifies
the proposed datasets, model comparisons, chronological folds, and acceptance
gates. The [post-spike changes](docs/decisions/0003-plan-changes-after-stage0.md)
remain proposals awaiting review.

## Run the existing Stage 0 spike

Requires Python 3.12 and `uv`. From the repository root:

```bash
uv sync --locked
uv run python scripts/stage0_spike.py all
```

The script selects 10 fixture games using basketball outcomes before inspecting
their exchange prices, downloads and caches public responses, checks timing and
settlement, compares candle lows with trades, flags possible sweeps, and compares
available Polymarket history. It reads Kalshi, ESPN, and Polymarket endpoints;
the Stage 0 workflow does not require API keys.

| Command | Action |
|---|---|
| `select` | Select and save the 10 fixture games |
| `fetch` | Load/download data for the saved selection |
| `analyze` | Load the selection and write analysis results |
| `chart` | Analyze and generate the HTML charts |
| `all` | Select, load, analyze, and chart |

Pass the command to `uv run python scripts/stage0_spike.py`. Run `select` or `all`
first on a fresh checkout. Later commands can download responses missing from the
cache; they are not strictly offline commands.

Outputs are `reports/stage0/index.html` (interactive charts) and
`reports/stage0/summary.json` (diagnostics). Raw responses are cached under
`data/raw/spike/`, and the fixture selection is `data/spike_selected_games.json`.
Data, reports, environments, and secrets are excluded from Git; a fresh checkout
rebuilds the outputs from the providers. `.env.example` lists placeholders for
possible future adapters, and the spike does not load it.

Run the existing lint check with `uv run ruff check .`.

Run the tests with `uv run pytest`.

## Run a backtest

The engine replays history against a strategy, with no model required, and reports what
would have filled and settled under a stated execution rule. Results are hypothetical
and gross of fees. Pull the data once (about an hour, and safe to resume), check its
coverage, then run a strategy config:

```bash
uv run python scripts/quantbet.py pull
```

```bash
uv run python scripts/quantbet.py coverage
```

```bash
uv run python scripts/quantbet.py backtest configs/strategies/fixed_offset.yaml
```

Data goes to `~/QuantBetData/history/`. Each run writes its orders, fills, a report,
and a manifest to `runs/`, and adds a line to `runs/experiment_log.jsonl`. The commands
refuse holdout games.

A strategy subclasses `Strategy` and acts through a `Context`, which only exposes what
has already been replayed:

```python
class FixedOffsetLadder(Strategy):
    def on_game_start(self, ctx):
        ctx.schedule(ctx.game.scheduled_start_us - TWO_HOURS, self.place)

    def place(self, ctx):
        for market in ctx.game.markets:
            quote = ctx.quote(market.ticker)
            ctx.buy_limit(market.ticker, price=quote.mid - 0.10, size=1)
```

See [`strategies/fixed_offset.py`](src/quantbet_nba/strategies/fixed_offset.py) for the
full version.

## Repository guide

```text
README.md                              Project intuition and mathematical framework
pyproject.toml / uv.lock                Python package and locked dependencies
scripts/stage0_spike.py                 Implemented exploratory data/timing workflow
scripts/quantbet.py                     Backtest command line: pull, coverage, backtest
src/quantbet_nba/data/                  Kalshi and ESPN adapters, dataset, coverage
src/quantbet_nba/engine/                Replay, fill rules, fees, settlement, portfolio
src/quantbet_nba/strategies/            Strategy interface and the fixed-offset baseline
src/quantbet_nba/analytics/             Metrics, game bootstrap, reports, run logging
configs/splits.yaml                     Tuning and holdout boundaries
configs/strategies/                     Backtest scenarios
docs/decisions/                         Findings, split decisions, proposed changes
docs/resting-ladder-training-plan.md    Proposed model and validation work
```

The [Stage 0 findings](docs/decisions/0001-stage0-findings.md) record the observed
data behavior and remaining uncertainties. They support the next research stage;
they do not establish a trading edge.
