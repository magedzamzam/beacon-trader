"""Technical-analysis feature extraction for signal-time snapshots.

Pure-Python indicators (no numpy/pandas). Used to record the multi-timeframe
market context each signal fired under, for later correlation with outcomes.
SMC (smart-money concepts) is deliberately out of scope for now — the `features`
dict is open for it to be added later without a schema change.
"""
