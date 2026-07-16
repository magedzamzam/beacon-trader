"""Layer-neutral config helpers (#75).

`overlay_config` lived in `analysis/_util.py` (#69), but that module is scoped
research-only ("nothing here touches execution/ingest/monitor"), so the identical
known-keys overlay in `execution/trend_filter.py` could not import it without
crossing the execution↔research boundary tracked by the #60 ADR.

This module sits at the package root with ZERO dependencies on either layer, so
execution and analysis can both import it. `analysis/_util.py` re-exports it
(analysis call sites unchanged); execution imports it directly.
"""
from __future__ import annotations


def overlay_config(defaults: dict, stored) -> dict:
    """A copy of `defaults` overlaid with the known keys from a stored config.

    Only keys already in `defaults` are copied over (unknown keys ignored); the
    returned dict is a fresh copy so callers never mutate `defaults`. A bool value
    passes through unchanged (it is an int subclass) — semantics preserved exactly
    from the #69 original so the analysis refactor stays zero-diff."""
    cfg = dict(defaults)
    if isinstance(stored, dict):
        for k in defaults:
            if k in stored:
                cfg[k] = stored[k]
    return cfg
