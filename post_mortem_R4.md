# Post-mortem: `FinalTrader_v3.py`, IMC Prosperity 4 Round 4

## Outcome

| Run | File | Data | Result PnL |
|---|---|---|---|
| In-sample | `498931.json` | Round 4, Day 3 | **+56,976.82** |
| Out-of-sample | `542890.json` | Round 4, Day 4 | **−4,225.42** |

The in-sample run was profitable on the same window the model parameters were fit to. The out-of-sample run, on a single forward-held day, lost money. The 13:1 in-sample-to-out-of-sample ratio combined with the sign reversal is indistinguishable from the canonical pattern of a strategy whose "edge" does not survive parameter-window expiry.

This document diagnoses the failure to a level of specificity that lets each bug map to a structural prevention in the successor framework `regime-first-options`.

## The asset

`VELVETFRUIT_EXTRACT` is approximately Ornstein-Uhlenbeck on 100ms ticks. AR(1) MLE fits across days 1–3:

| Day | θ̂ | κ̂ (per tick) | σ̂_stat | half-life (ticks / seconds) |
|---|---|---|---|---|
| 1 | 5249.09 | 0.002954 | 14.78 | 235 / 23.5 s |
| 2 | 5256.79 | 0.001994 | 18.03 | 348 / 34.8 s |
| 3 | 5236.44 | 0.002332 | 16.69 | 297 / 29.7 s |
| Pooled (days 1–3) | **5247.43** | 0.001970 | **18.13** | 352 / 35.2 s |

Half-life ~30 s at 100 ms ticks. OU is a defensible model class for the dynamics. The bug was not in the choice of model class — it was in everything downstream of that choice.

## Bug 1 — In-sample contamination

`FinalTrader_v3.py` lines 8–9:

```python
GLOBAL_THETA = 5247.43
GLOBAL_SIGMA_STAT = 17.0
```

`5247.43` matches the pooled days 1–3 OU fit to two decimals. `17.0` is a rounding of σ̂_stat = 18.13. Both values were fit on the very data the strategy was backtested against.

The +56,977 in-sample run was not a measure of edge. It was a measure of how well a constant equal to the in-sample mean predicts a series that has that constant as its in-sample mean. A no-look-ahead protocol would have used days 1–2 to fit (θ̂ = 5252.94) and tested on day 3 (actual mean 5239.16). Day 3 mid would have read 14 points lower than θ throughout — many multiples of `OPTION_EDGE = 1.0` — and the strategy would have systematically taken the wrong side of every options trade where the fair-value gap was driven by stale θ.

## Bug 2 — Parameter drift much larger than trading edge

Two views of θ̂ instability:

- **Day-by-day:** {5249.09, 5256.79, 5236.44} → range 20.4
- **Within-day rolling 5000-tick window (~8.3 min):** [5226.24, 5263.69] → range 37.4, std 10.7

`FinalTrader_v3.py` defines:

```python
VE_EDGE     = 1.5    # underlying edge
OPTION_EDGE = 1.0    # options edge
```

**The intended edge is one to two orders of magnitude smaller than the noise in the parameter that defines it.** A signal-to-noise ratio of ~1:30 cannot produce a profitable strategy regardless of execution quality. The point estimate of θ was treated as truth; the real quantity needed for sizing was its uncertainty.

## Bug 3 — The OU pricer uses the stationary distribution, not the conditional one

`FinalTrader_v3.py` lines 25–30:

```python
def ou_call_fair_value(strike, theta, sigma_stat):
    if sigma_stat <= 0:
        return max(theta - strike, 0.0)
    d = (theta - strike) / sigma_stat
    price = (theta - strike) * norm_cdf(d) + sigma_stat * norm_pdf(d)
    return max(price, 0.0)
```

This is the Bachelier-style call price assuming the underlying at expiry is distributed N(θ, σ²_stat) — the **stationary** distribution of the OU process. That distribution holds in the limit τ → ∞.

For an OU process with finite time-to-expiry τ and current spot S, the conditional distribution at expiry is:

```
m(τ) = θ + (S − θ) · exp(−κτ)
v(τ) = (σ²/2κ) · (1 − exp(−2κτ))
```

As κτ → ∞: m → θ, v → σ²/2κ = σ²_stat. As κτ → 0: m → S, v → 0.

With κ ≈ 0.002 per tick and τ = 10,000 ticks (start of round), κτ ≈ 20 and exp(−κτ) ≈ 2 × 10⁻⁹ — the stationary approximation is excellent. By tick 9,900 with τ = 100, κτ = 0.2 and exp(−κτ) ≈ 0.82 — the conditional mean is much closer to S than to θ, and the conditional variance is a small fraction of σ²_stat. **The pricer never reflects this. It returns the same fair value whether τ is 10,000 ticks or 100 ticks remaining.**

The further consequence: the fair value depends only on `(strike, θ, σ_stat)`. It does not depend on current spot S. This is mathematically correct only as κτ → ∞. Equivalently, the strategy priced every option in the round as if infinite time remained.

## Bug 4 — Symptom-patch on high strikes

`FinalTrader_v3.py` lines 132–134:

```python
dynamic_edge = OPTION_EDGE
if strike >= 5400:
    dynamic_edge = OPTION_EDGE * 2.0
```

Under the stationary distribution with θ = 5247.43 and σ_stat = 17, Φ((5247.43 − 5500) / 17) ≈ 10⁻⁵. The model prices `VEV_5500` at near-zero. The market traded `VEV_5500` between $1.50 and $7.00 across the three days. The strategy reads this as wildly overpriced and would short aggressively.

The doubled edge is a brake on this misreading. The market is correct (positive probability of S reaching 5500 within the round under the conditional distribution); the model is wrong; the patch addresses neither. Hardcoded asymmetric edges are a symptom that the underlying model is mis-specified.

## Bug 5 — No delta hedging

The strategy trades options when their market price diverges from the OU fair value, and separately trades the underlying when its mid diverges from θ. These are independent trades, not hedges.

A long call has positive delta. If the strategy buys `VEV_5300` because it reads as "cheap", it acquires positive delta. If spot subsequently moves toward θ (which the strategy expects, since spot was above θ when the trade fired), the call loses on delta. A correct vol trade would short the underlying in proportion to the call's delta at trade time. `FinalTrader_v3.py` does not.

Every options trade was therefore a coupled bet on (vol mispricing) AND (directional spot move) — and when fair-value gaps are 1 point against parameter noise of 20+ points, the directional component dominates by orders of magnitude. The reported vol-arb strategy was, structurally, a directional bet with options wrapping.

## Bug 6 — The circuit breaker is an admission

```python
CIRCUIT_BREAKER_DEV = 68.0
if deviation <= CIRCUIT_BREAKER_DEV:
    # ...trade...
```

68 ≈ 4 · σ_stat. The strategy halts trading when `|spot − θ| > 68`. Read what this acknowledges: "my θ might be wrong, so when spot disagrees with my θ by enough, I shut off." It is a confidence interval on θ wired into the code as a binary switch.

The correct version of this idea is to scale position size inversely with the recent variance of θ̂, not to toggle trading on and off. As implemented, the circuit breaker is the strategy's own admission that its central parameter is unreliable, hardcoded as a binary defence rather than as a graded sizing input.

## Day 4 mechanism of loss

`542890.json` contains the day-4 backtest. The visible day-4 data shows `VEV_4000` mid at 1233, implying spot ≈ 5233. Hardcoded θ is 5247. Under the OU model with stationary distribution, the strategy reads "spot below θ by 14, slightly cheap, buy spot."

But day-by-day fits in days 1–3 had θ at {5249, 5257, 5236}. The regime by day 4 was somewhere new, and "below 5247" no longer carried the implication the in-sample fit suggested. The strategy continues to take the same trades it did in-sample and is paid market prices that reflect the new regime. The −4,225 follows directly.

## What `regime-first-options` is built to prevent

Each of the six bugs above maps to a specific architectural decision in the successor framework:

| Bug | Architectural prevention |
|---|---|
| 1. In-sample contamination | `backtest/replay_round_4.py` enforces strict no-look-ahead. Day N+1 parameters use only data ending at day N. |
| 2. Edge < parameter noise | `src/sizing.py` scales position size by `\|edge\| / fv_std`. When uncertainty exceeds edge, no trade. |
| 3. Stationary-distribution pricer | `src/pricing.py.ou_call(S, K, tau, theta, kappa, sigma_inst)` requires `S` and `tau`; uses the conditional distribution; reduces to stationary only as `κτ → ∞`. |
| 4. Symptom-patch on high strikes | The corrected pricer with conditional distribution prices high strikes correctly; no asymmetric edge needed. |
| 5. No delta hedging | `src/strategy_core.py.decide()` computes net portfolio delta and emits a hedging order in the underlying alongside every options trade. |
| 6. Circuit breaker as binary switch | Position sizing scales continuously with the bootstrap SE on θ̂. There is no "trade / don't trade" toggle; there is "size proportional to confidence." |

Committing the same bug a second time would require deliberately bypassing a structural protection, not merely failing to think of it.

---

*Drafted as the foundation document for `regime-first-options`. Every commit in the 20-day sprint and the 114-day arc that follows answers to this document.*
