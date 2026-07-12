# `analysis/` — shadow analytics & reconciliation

Everything here is **observability, measure-before-gate**: computed per signal
side-by-side with live trading, persisted for correlation, and **never allowed to
gate or alter an order**. A layer only graduates to influencing trading after its
edge is measured on realized fills (the explicit lesson from the AI validator).

## Bayesian & reconciliation
| File | Purpose |
|------|---------|
| `bayes.py` | Beta-Binomial per-condition win-rate table with credible intervals (shrinks thin samples toward the base rate) + a Naive-Bayes `P(win\|features)` score. Pure — no numpy/scipy. |
| `reconcile.py` | Reconcile channel-**claimed** outcomes vs bot-**actual** execution, per signal, with a reason for every miss. |
| `claims.py` | Link parsed outcome claims to the signals/legs they refer to. |
| `report.py` | The correlation reports (the payoff): per-channel × regime performance, FVG/OB-vs-outcome, and magnet/structure-vs-outcome — all with credible intervals. |

## Sidecar estimators (Phase-1 shadow suite)
| File | Purpose |
|------|---------|
| `sidecar.py` | The isolation harness: `run_estimators()` runs each estimator best-effort (a failure is swallowed + logged `ANALYTICS-SIDECAR-DEGRADED`), and `capture_analytics()` writes one `signal_analytics` row in its **own** session so an estimator's DB read can't poison the capture/trade transaction. |
| `estimators.py` | The estimators: market **regime** (trending/ranging/high-vol), **Hurst**, **Kalman** slope, **VWAP** z-deviation, **k-NN** over prior signals, and the **structure_magnet** per-signal reference. Pure math; DB reads (k-NN, structure) are lazy. |

## Persistent structure / magnet map (#61)
| File | Purpose |
|------|---------|
| `structure.py` | Pure engine: ATR ZigZag swings → HH/HL/LH/LL → bull/bear/range → Fib ladder (retracement + extension) → cluster levels into confluence "magnet" zones. Plus the shared `feature_contribution()` contract. |
| `structure_map.py` | Persistence + **versioned** recompute of the slow-moving map (weekly/on-demand); `active_map()` reads the current version for the per-signal estimator (point-in-time correctness). |
| `structure_filter.py` | Phase-3 filter scaffolding: config `structure.filter` + a pure `decide()`. **DISABLED and NOT wired into the executor** — filtering is a config flip away once the outcome report shows the edge (N≥30). |

**Rule of thumb:** if a file is under `analysis/`, the execution path does not
depend on it. It can be turned off or fail and trading continues unchanged.
