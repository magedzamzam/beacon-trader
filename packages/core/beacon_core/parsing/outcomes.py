"""Parse channel outcome follow-ups ('TP2 HIT', 'SL HIT 80 PIPS', 'all TP done ✅')
into a structured claim. Pure; no side effects. Returns None for non-outcome text
so it can be run over every non-signal message cheaply."""
from __future__ import annotations

import re
from typing import Optional

# superscript digits -> ascii, so "TP²" reads as "TP2"
_SUP = str.maketrans("¹²³⁴⁵⁶⁷⁸⁹⁰", "1234567890")
# words/emoji that mean "this target was reached"
_HIT = r"(?:hit|done|reached|achieved|smashed|secured|bagged|profit|✅|🎯|✔|☑)"
_TP_NUM = re.compile(r"tp\s*([1-9])")
_TP_GENERIC = re.compile(r"(?:tp|take\s*profit|target)\b[^\n]{0,14}?" + _HIT)
_ALL_TP = re.compile(r"\ball\s*(?:tp|tps|targets|target)\b|all\s*(?:tp|target)s?\s*(?:done|hit|reached)")
_SL = re.compile(r"\b(?:sl|stop[\s-]*loss)\b[\s:.\-]*(?:hit|hunt|taken|out)|stopped\s*out|\bsl\s*hit\b")


def parse_outcome(text: Optional[str]) -> Optional[dict]:
    """{"tp_hits": [1,2], "max_tp": 2, "all_tp": False, "sl_hit": False,
        "tp_generic": False}  or None if the text isn't an outcome message."""
    if not text:
        return None
    low = text.translate(_SUP).lower()

    tp_hits = set()
    for m in _TP_NUM.finditer(low):
        window = low[m.start():m.start() + 20]      # a hit-word must be near the TPn
        if re.search(_HIT, window):
            tp_hits.add(int(m.group(1)))

    all_tp = bool(_ALL_TP.search(low))
    sl_hit = bool(_SL.search(low))
    tp_generic = bool(_TP_GENERIC.search(low)) and not tp_hits

    if not (tp_hits or all_tp or sl_hit or tp_generic):
        return None
    return {
        "tp_hits": sorted(tp_hits),
        "max_tp": max(tp_hits) if tp_hits else 0,
        "all_tp": all_tp,
        "sl_hit": sl_hit,
        "tp_generic": tp_generic,
    }
