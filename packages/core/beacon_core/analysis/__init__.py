"""Post-hoc analysis of captured signal features vs. trade outcomes.

`bayes` provides a Beta-Binomial per-condition win-rate table (with credible
intervals that shrink small samples toward the base rate) and a Naive-Bayes
P(win|features) score for a signal. Pure Python — no scipy/numpy.
"""
