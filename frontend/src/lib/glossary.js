/**
 * Glossary (#105) — the single source of plain-language copy for every non-obvious
 * field on the platform. Both the Help page and the inline ⓘ hints read from here,
 * so a tooltip can never drift from the page.
 *
 * Copy is sourced from the implementation, NOT from memory:
 *   analysis/bayes.py (posterior/credible interval/score), analysis/estimators.py,
 *   analysis/report.py (execution tax), execution/guard.py (risk limits),
 *   execution/strategy.py (scope cascade), analysis/reconcile.py (miss taxonomy).
 *
 * Every entry answers the same four questions:
 *   what · how to read it · what it does NOT mean · when to act
 */

// The three rules that matter more than any single number.
export const GUARDRAILS = [
  {
    title: "Shadow analytics never gate a trade",
    body: "Regime, Hurst, Kalman, VWAP-z, k-NN, structure/magnets and the learned P(win) gate are all measured " +
          "side-by-side with live trading and do NOT decide anything. They exist to be validated first " +
          "(measure-before-gate). Only the trend-alignment filter, the news blackout, risk caps and the AI gate " +
          "can actually stop or resize a trade.",
  },
  {
    title: "Leg-level P&L is unreliable — read trade-level P&L",
    body: "A single leg's money figure can be distorted by partial fills and broker allocation. Trust the " +
          "trade-level realized P&L, and use the leg's outcome LABEL (tp_hit / sl_hit / breakeven) rather than " +
          "its P&L number.",
  },
  {
    title: "Don't act below N ≥ 30 — and raw-N overstates what you have",
    body: "Every sample here is the same instrument (XAUUSD) and samples cluster by channel, time and direction, " +
          "so they are correlated: the effective sample size is much smaller than the raw count. A pattern with " +
          "n=8 is a story, not evidence. The learned gate refuses to act below 30 for exactly this reason.",
  },
];

export const SECTIONS = [
  { id: "bayes", title: "Bayesian Analysis", blurb: "Which conditions actually predict a win — and how much of that is real vs small-sample noise." },
  { id: "labels", title: "Signal quality vs bot outcome", blurb: "Two different questions: was the SETUP good, and did WE make money on it?" },
  { id: "analytics", title: "Analytics estimators (shadow)", blurb: "Per-signal market context captured beside every trade. None of it gates." },
  { id: "structure", title: "Structure & magnets (shadow)", blurb: "Where price sits relative to market structure and the levels that attract it." },
  { id: "reconciler", title: "Reconciler", blurb: "Did we do what the channel said — and if not, why not?" },
  { id: "performance", title: "Performance & risk metrics", blurb: "How to read the money numbers." },
  { id: "risk", title: "Risk & Limits", blurb: "The caps that can stop a trade. Read the master-switch rule carefully." },
  { id: "strategies", title: "Strategies (Entry / Filtration / Exit)", blurb: "How a signal is entered, filtered and exited — per Account × Source." },
  { id: "config", title: "Configuration screens", blurb: "What each settings screen is for." },
];

export const GLOSSARY = [
  // ---- Bayesian Analysis ----------------------------------------------------
  {
    id: "base_rate", term: "Base rate", section: "bayes",
    what: "The overall win-rate across every labelled trade in the current view. It is the null hypothesis — the result you'd get with no skill and no filtering.",
    read: "It's the number every condition must beat. A condition at 55% when the base rate is 54% has found nothing.",
    not: "It is NOT a target or a forecast. It's just the average of what already happened in this window.",
    act: "Use it as the reference line. Compare a condition's credible interval to it, never to zero.",
  },
  {
    id: "posterior", term: "Posterior (mean)", section: "bayes",
    what: "The Beta-Binomial shrunk win-rate for a condition. It blends the observed wins with the base rate, weighted by how much evidence there is.",
    read: "This is the honest estimate. With few samples it sits close to the base rate; as samples accumulate it moves toward the raw win-rate.",
    not: "It does NOT equal the raw win-rate, and that's deliberate — 2 wins out of 2 is reported near the base rate, not as 100%. It is not 'the model being pessimistic', it's the maths refusing to over-read a tiny sample.",
    act: "Prefer it over raw WR — but still don't act on the mean alone; read the credible interval.",
  },
  {
    id: "credible_interval", term: "Credible interval (90%) · ci_low / ci_high", section: "bayes",
    what: "The range the true win-rate plausibly lives in, with 90% credibility. Wide = little evidence; narrow = lots.",
    read: "The single most important number on the page is ci_low. **If the interval spans the base rate, there is no evidence of an edge** — the data can't tell that condition apart from average.",
    not: "It is NOT a min/max of observed outcomes, and a high ci_high is NOT a promise. An interval of 40–90% means you know almost nothing.",
    act: "Act only when the LOWER bound clears your threshold. That's what the learned gate does; do the same by eye.",
  },
  {
    id: "lift", term: "Lift", section: "bayes",
    what: "Posterior mean minus the base rate — how much better (or worse) this condition looks than average.",
    read: "Positive = better than average, negative = worse. Sort by it to find candidates.",
    not: "A positive lift is NOT evidence on its own — a thin sample can show lift while its interval still spans the base rate.",
    act: "Use lift to shortlist, then confirm with ci_low and n.",
  },
  {
    id: "raw_wr", term: "n · wins · raw WR", section: "bayes",
    what: "The unadjusted counts: how many trades matched the condition, how many won, and wins ÷ n.",
    read: "n is your evidence budget. Raw WR is the headline that feels convincing.",
    not: "Raw WR is the mirage — it is exactly the number that makes 2/2 look like a 100% edge. It carries no notion of uncertainty.",
    act: "Read n first. If n is small, ignore raw WR entirely and look at the interval.",
  },
  {
    id: "p_win", term: "p_win", section: "bayes",
    what: "A Naive-Bayes probability that a specific signal wins, given its captured features, learned from history.",
    read: "A relative score — compare it to the base rate. Above = the features lean favourable.",
    not: "It is NOT a calibrated guarantee and it does NOT account for correlated samples. It is not currently allowed to gate any trade.",
    act: "Treat as a hint while the learned gate is in shadow. Confirm against the would-block report before trusting it.",
  },
  {
    id: "contributors", term: "Contributors", section: "bayes",
    what: "The individual conditions that moved p_win most, ranked by their log-likelihood ratio.",
    read: "The 'why' behind a score. A large contributor with a small n is a red flag, not a finding.",
    not: "They are NOT causes — they're correlations found in a small, correlated sample.",
    act: "Use them to sanity-check that a score rests on something plausible rather than one freak condition.",
  },
  {
    id: "min_n", term: "min n · significance", section: "bayes",
    what: "The minimum samples a condition needs before it's shown (min_n, default 5) — and the separate, stricter floor the learned gate needs before it may act (30).",
    read: "If a condition you expect is missing, it's below min_n. If the gate says 'observe only', it's below the significance floor.",
    not: "Passing min_n does NOT mean significant — min_n only controls what's displayed.",
    act: "Raise min_n to de-noise the table. Never act on a condition below 30 samples.",
  },

  // ---- Signal quality vs bot outcome ---------------------------------------
  {
    id: "signal_quality_wr", term: "Signal-Quality WR", section: "labels",
    what: "Did the CHANNEL's setup work, judged by the channel's own claimed outcome (it reached TP1+ vs hit SL) — independent of how we executed.",
    read: "This answers 'is this channel any good at picking trades?'. Ambiguous or contradictory claims are excluded, never counted as losses.",
    not: "It is NOT our profit and NOT our fill. A channel can have a great signal-quality WR while we lose money on it.",
    act: "Use it to decide which channels/conditions to TRUST. It's the right label for gating decisions.",
  },
  {
    id: "bot_realized_wr", term: "Bot-realized WR", section: "labels",
    what: "Did WE make money — realized P&L > 0 on our actual trade.",
    read: "This answers 'did our execution capture it?'. It bundles signal quality AND our fills, stops and TTL together.",
    not: "It is NOT a clean measure of the channel — a good signal we mis-executed shows up here as a losing signal.",
    act: "Use it to size the execution-fix backlog, not to judge a channel's edge.",
  },
  {
    id: "execution_tax", term: "Execution tax (the gap)", section: "labels",
    what: "Signal-Quality WR minus Bot-realized WR, on the signals that carry both labels.",
    read: "A positive gap means the setup worked but our execution didn't capture it — missed fills, stops too tight, orders expiring.",
    not: "It is NOT a bad-channel signal. A big gap on a good channel is OUR problem to fix, not a reason to disable it.",
    act: "Fix execution where the tax is biggest. Disable/de-size where signal-quality itself is poor.",
  },

  // ---- Analytics estimators -------------------------------------------------
  {
    id: "regime", term: "Regime (trending / ranging / high_vol)", section: "analytics",
    what: "A label for market conditions at signal time, derived from ADX, ATR% and realized volatility.",
    read: "Context for grouping outcomes — 'does this channel only work in trends?'.",
    not: "It does NOT gate anything and is not a forecast of the next move.",
    act: "Use it as a slice in the correlation report; act only at N ≥ 30 per bucket.",
  },
  {
    id: "hurst", term: "Hurst exponent", section: "analytics",
    what: "A persistence measure of the recent price series, around 0.5.",
    read: "Above 0.5 = trend-persistent (moves tend to continue); below 0.5 = mean-reverting (moves tend to snap back); ~0.5 = random walk.",
    not: "It does NOT predict direction — only the character of the move. It's noisy on short windows.",
    act: "Shadow only. Treat as weak context until validated over many regimes.",
  },
  {
    id: "kalman_slope", term: "Kalman slope", section: "analytics",
    what: "The estimated slope of a smoothed price trend at signal time.",
    read: "Sign = trend direction; magnitude = steepness. Less jumpy than a raw EMA difference.",
    not: "NOT a signal to trade, and not a trend-strength percentile.",
    act: "Shadow only — a context feature for the model.",
  },
  {
    id: "vwap_z", term: "VWAP deviation (z)", section: "analytics",
    what: "How far price sits from VWAP, in standard deviations.",
    read: "Positive = above VWAP (extended up), negative = below. Large |z| = stretched from the volume-weighted mean.",
    not: "A stretched z is NOT automatically a reversal — in a strong trend price stays extended.",
    act: "Shadow only. Useful as a slice for 'do we buy extended?'.",
  },
  {
    id: "knn", term: "k-NN similarity (win_rate / expectancy)", section: "analytics",
    what: "Finds the most similar past signals by a small feature vector and reports how those turned out.",
    read: "'When it looked like this before, here's what happened.' The k is small, so treat it as anecdote-with-numbers.",
    not: "NOT a probability, and highly unreliable at the current sample size — the feature space is far bigger than the data.",
    act: "Shadow only; the weakest of the estimators today. Don't act on it.",
  },
  {
    id: "atr_pct", term: "ATR %", section: "analytics",
    what: "Average True Range as a percentage of price — how much the instrument is moving.",
    read: "Higher = wider swings, so a fixed-distance stop is more likely to be tagged by noise.",
    not: "NOT a direction or a risk limit.",
    act: "Use it to reason about whether a stop distance is sane relative to volatility.",
  },

  // ---- Structure ------------------------------------------------------------
  {
    id: "market_structure", term: "HH / HL / LH / LL", section: "structure",
    what: "Swing-point classification: Higher-High, Higher-Low, Lower-High, Lower-Low. HH+HL = uptrend structure; LH+LL = downtrend.",
    read: "A structural read of trend per timeframe, independent of any indicator.",
    not: "NOT a trade trigger; structure lags and can flip on one swing.",
    act: "Shadow context. Compare against htf_alignment before reading anything into it.",
  },
  {
    id: "htf_alignment", term: "htf_alignment (aligned / counter / mixed)", section: "structure",
    what: "Whether the signal's direction agrees with the higher-timeframe structure.",
    read: "'counter' means the trade fights the bigger trend — historically the expensive bucket.",
    not: "NOT the same as the trend-alignment ENTRY FILTER (which uses an EMA + slope/ATR confirmation and can actually skip a trade). This one is measurement only.",
    act: "Use the outcome report to decide whether to enable the entry filter — don't infer it from this label alone.",
  },
  {
    id: "magnet_zone", term: "Magnet zone & score", section: "structure",
    what: "A cluster of confluent levels (structure, Fibonacci, S/R) that price tends to be drawn to. Score = strength of the confluence.",
    read: "Higher score = more overlapping reasons for that level to matter.",
    not: "NOT a guarantee price reaches it, and not a target.",
    act: "Shadow. Use dist_atr to reason about whether a TP sits behind a wall.",
  },
  {
    id: "dist_atr", term: "dist_atr", section: "structure",
    what: "Distance from price to the nearest magnet zone, measured in ATR units.",
    read: "ATR-normalised so it's comparable across volatility regimes. Small = price is right at a level.",
    not: "NOT in pips/points — it's a volatility-relative measure.",
    act: "A small dist_atr on the ADVERSE side (a zone just above a BUY) is the case the shadow magnet filter is designed to catch.",
  },
  {
    id: "premium_discount", term: "Premium / discount", section: "structure",
    what: "Where price sits inside the current structural range — upper half = premium, lower half = discount.",
    read: "Buying in discount / selling in premium is the conventional read.",
    not: "NOT an edge on its own, and the range definition shifts as structure updates.",
    act: "Shadow context only.",
  },

  // ---- Reconciler -----------------------------------------------------------
  {
    id: "match_rate", term: "Match rate", section: "reconciler",
    what: "The share of the channel's claimed outcomes that our execution actually reproduced.",
    read: "A health check on execution, not on the channel. Low match rate = we are not tracking what we subscribed to.",
    not: "NOT a win-rate and NOT a profit measure.",
    act: "Investigate the miss taxonomy below before changing any strategy.",
  },
  {
    id: "no_fill", term: "no_fill", section: "reconciler",
    what: "The channel claimed a result but our order never filled — the entry never traded, or expired first (TTL).",
    read: "A pure execution miss: the setup may have been fine, we just weren't in it.",
    not: "NOT a bad signal.",
    act: "Look at entry TTL and the chase guard in Strategies → Entry.",
  },
  {
    id: "shortfall_stopped_before_tp", term: "shortfall_stopped_before_tp", section: "reconciler",
    what: "We filled, but our stop took us out before the channel's claimed TP printed.",
    read: "Our stop was too tight (or ratcheted too early) for this channel's geometry.",
    not: "NOT necessarily a bad signal — this is the classic 'winner cut short' pattern.",
    act: "This is the payoff-geometry lever: look at the exit ladder (break-even lock too early?) in Strategies → Exit.",
  },
  {
    id: "executed_no_trade", term: "executed_no_trade", section: "reconciler",
    what: "We marked the signal executed but no trade row exists — an internal/plumbing gap rather than a market event.",
    read: "A bug-shaped symptom, not a trading one.",
    not: "NOT a signal-quality or execution-quality measure.",
    act: "Treat as a defect to investigate, not a config to tune.",
  },

  // ---- Performance ----------------------------------------------------------
  {
    id: "expectancy", term: "Expectancy", section: "performance",
    what: "Average profit or loss per trade — total P&L ÷ number of trades.",
    read: "The one number that says whether a channel makes money per attempt. Negative = it costs you to play.",
    not: "NOT a win-rate. A 70%-win channel can have negative expectancy if the losers are big.",
    act: "This is the primary judgement metric per channel. Read it with n.",
  },
  {
    id: "r_multiple", term: "R multiple", section: "performance",
    what: "Result expressed in units of the trade's initial risk (R = |entry − stop|). +2R = made twice what you risked.",
    read: "Normalises across trade sizes and stop distances so channels are comparable.",
    not: "NOT money. A +3R trade on tiny size is still small money.",
    act: "Use R to compare exit rules fairly — it's the right unit for the A/B.",
  },
  {
    id: "profit_factor", term: "Profit factor (PF)", section: "performance",
    what: "Gross profit ÷ gross loss. Above 1.0 = profitable overall.",
    read: "1.4 means you make 1.40 for every 1.00 you lose.",
    not: "NOT robust at small n — one outlier trade moves it a lot.",
    act: "Sanity-check alongside expectancy and max drawdown.",
  },
  {
    id: "payoff", term: "Payoff ratio", section: "performance",
    what: "Average winner ÷ average loser.",
    read: "The other half of the edge equation. At a 70% win-rate you only need ~0.43 to break even; at 50% you need > 1.0.",
    not: "A low payoff is NOT automatically bad — it can be fine if the win-rate is high enough. Judge them together.",
    act: "A high win-rate with a low payoff is the signature of winners being cut short — look at the exit ladder.",
  },
  {
    id: "max_drawdown", term: "Max drawdown", section: "performance",
    what: "The largest peak-to-trough fall in the equity curve.",
    read: "The pain metric — what you'd have had to sit through.",
    not: "NOT a prediction of the worst case; the future can be worse.",
    act: "Judge risk settings against it; it's the number that ends accounts.",
  },
  {
    id: "calmar_sharpe", term: "Calmar · Sharpe", section: "performance",
    what: "Return per unit of risk: Calmar = return ÷ max drawdown; Sharpe = return ÷ volatility of returns.",
    read: "Higher is better; useful to compare arms with different volatility.",
    not: "Both are unreliable on a short history and Sharpe punishes upside volatility too.",
    act: "Directional only at this sample size. Don't rank channels by Sharpe yet.",
  },
  {
    id: "planned_risk", term: "planned_risk", section: "performance",
    what: "The worst-case loss computed at entry if every leg hit the shared stop, in account currency.",
    read: "Compare it against realized loss: if realized ≫ planned, either the stop slipped (news!) or sizing stacked across legs.",
    not: "NOT a guarantee — slippage, gaps and news can blow past it.",
    act: "A persistent realized ≫ planned gap means fix sizing or add a news gate, not a bigger stop.",
  },

  // ---- Risk -----------------------------------------------------------------
  {
    id: "master_switch", term: "Enforce limits (master switch)", section: "risk",
    what: "The on/off switch for the risk caps below.",
    read: "IMPORTANT and non-obvious: when this is OFF, NOTHING below blocks — the daily-loss floor and every cap are skipped. The ONLY thing that still stops trading is the kill switch.",
    not: "It does NOT leave the daily floor armed. Turning it off disarms the caps entirely.",
    act: "If you want any cap enforced, this must be ON. (A completely missing risk_limits row is the one fail-safe case: conservative defaults apply instead.)",
  },
  {
    id: "kill_switch", term: "Kill switch (trading_halted)", section: "risk",
    what: "The explicit big red button: halts all new trades.",
    read: "It is checked FIRST and works even when the master switch is off — it can never be silently disarmed.",
    not: "It does NOT close existing positions; it only stops new entries.",
    act: "Use it to stop the bleeding now; fix config after.",
  },
  {
    id: "daily_loss_limit", term: "Daily loss limit", section: "risk",
    what: "A floor on realized P&L since UTC midnight. Once today's realized loss reaches it, new trades are blocked.",
    read: "Entered as a magnitude (500 means a −500 floor). 0 = disabled.",
    not: "It does NOT act on open/unrealized P&L, and it does NOT apply when the master switch is off.",
    act: "Set it to a loss you can accept repeatedly — and keep the master switch on.",
  },
  {
    id: "per_signal_max_pct_of_daily", term: "Per-signal ceiling (× daily limit)", section: "risk",
    what: "The most a single signal may risk, as a fraction of the daily loss limit.",
    read: "0.5 = one trade may risk at most half the daily cap.",
    not: "It is NOT a percent of equity — it's relative to the daily limit, so it does nothing if the daily limit is 0.",
    act: "Keep it well under 1.0 so no single trade can spend the whole day's budget.",
  },
  {
    id: "max_open_risk", term: "Max open risk (per account / per symbol)", section: "risk",
    what: "A cap on the summed planned_risk of everything currently open.",
    read: "Stops many simultaneous positions adding up to one oversized bet.",
    not: "NOT a cap on a single trade, and it uses planned risk, not live P&L.",
    act: "Since everything here is XAUUSD, the account and symbol caps are effectively the same limit.",
  },
  {
    id: "max_signal_risk_pct", term: "Per-signal risk cap (% equity)", section: "risk",
    what: "Bounds one signal's ENTIRE fanout (every entry × TP leg) to a percentage of account equity, scaling all legs down proportionally if needed.",
    read: "This is the fix for per-TP allocation stacking, where each leg risks independently and the total quietly multiplies.",
    not: "It is NOT the same as the per-signal ceiling above (which is relative to the daily limit). 0 disables it.",
    act: "Keep it a little above a normal plan's risk so it only bites stacked fanouts.",
  },

  // ---- Strategies -----------------------------------------------------------
  {
    id: "strategy_scope", term: "Strategy scope (Account × Source)", section: "strategies",
    what: "A strategy applies to an Account, a Source, both, or 'Any'. The most-specific match wins: (Account+Source) > (Account+Any) > (Any+Source) > (Any+Any).",
    read: "Anything you leave unset cascades down to the next-less-specific strategy, ending at the (Any, Any) base — so the base is your global default.",
    not: "It is NOT a merge of everything — for filtration and exit, the most-specific block that sets them wins wholesale.",
    act: "Put your defaults in (Any, Any) and override only what differs per channel or per A/B arm.",
  },
  {
    id: "entry_policy_help", term: "Entry Strategy (TTL & chase guard)", section: "strategies",
    what: "How the entry order is placed: how long a working order lives (TTL) and how far past the signal's level we'll still accept a market fill (chase tolerance).",
    read: "The chase guard stops us buying far above the intended entry just because price already ran.",
    not: "It does NOT decide whether to trade — that's Filtration.",
    act: "Tighten the chase tolerance if you see bad fills; shorten TTL if stale orders fill hours late.",
  },
  {
    id: "filtration_help", term: "Entry Filtration", section: "strategies",
    what: "Rules that can skip, de-size or up-size a trade — trend-alignment plus custom rules.",
    read: "These are the only analytics-driven checks that actually change execution.",
    not: "A rule whose inputs aren't available is a no-op (fail-open) — it will not silently block.",
    act: "Enable trend-alignment only after re-validating it; it mis-scored a regime turn once already.",
  },
  {
    id: "exit_policy_help", term: "Exit Strategy (SL ladder)", section: "strategies",
    what: "The stop-loss ratchet: which TP moves the stop to break-even, and how it trails after.",
    read: "Snapshotted at entry — a trade keeps the ladder it started with, so editing this only affects NEW trades. That's what makes an A/B valid.",
    not: "Moving break-even earlier is NOT safer in aggregate — it converts winners into scratches and is the main cause of a low payoff ratio.",
    act: "Lock break-even at the first TP worth ≳1R for that channel, not mechanically at TP1.",
  },

  // ---- Configuration screens ------------------------------------------------
  { id: "cfg_brokers", term: "Brokers & Accounts", section: "config",
    what: "Broker connections and the trading accounts under them. Credentials are entered here and stored encrypted.",
    read: "An account is what trades; a broker is how we reach it.", not: "Not where risk is set.", act: "Keep live and demo brokers clearly separated." },
  { id: "cfg_sources", term: "Signal Sources", section: "config",
    what: "The channels we ingest, whether they're trusted/enabled for trading, and which accounts they route to.",
    read: "A source is identity + trust + routing only.", not: "Entry/filter/exit rules and risk are NOT here any more.", act: "Route a source to two accounts to run an exit A/B on identical signals." },
  { id: "cfg_symbols", term: "Symbols Mapping", section: "config",
    what: "Maps our internal symbol to the broker's instrument, with value-per-point, min lot and lot step.",
    read: "value_per_point is the number that makes sizing correct.", not: "Not a price feed.", act: "Verify it per broker instrument — a wrong value mis-sizes every trade." },
  { id: "cfg_hours", term: "Trading Hours", section: "config",
    what: "Session windows, the high-impact news blackout, and holiday/weekend handling.",
    read: "The news blackout is the only one that blocks entries; sessions carry a risk multiplier.", not: "Sessions do not block trading.", act: "Keep the wider blackout for CPI/NFP/FOMC-grade releases." },
  { id: "cfg_ai", term: "AI Validation", section: "config",
    what: "An LLM review of a signal / the execution plan, which can optionally block.",
    read: "Modes: off, background (advisory) or block (gates).", not: "Not a strategy or a statistical model.", act: "Compare its verdicts against outcomes before letting it gate." },
  { id: "cfg_indicators", term: "Indicators", section: "config",
    what: "Which TA indicators and timeframes are captured per signal for later analysis.",
    read: "This is capture config — it feeds the Bayesian model.", not: "Changing it does not change trading.", act: "Add an indicator before you need it; history can't be back-filled." },
  { id: "cfg_notifications", term: "Notifications", section: "config",
    what: "Which events go to which channel (Telegram / email / SMS).",
    read: "Routing per event type.", not: "Not an audit log — the Activity feed is.", act: "Keep failure events (broker errors, blocks) routed somewhere you read." },
];

export const byId = (id) => GLOSSARY.find((g) => g.id === id);
export const bySection = (sectionId) => GLOSSARY.filter((g) => g.section === sectionId);
