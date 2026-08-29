"""The two surfaces the ladder grid is edited through (#267, #268).

This box has no npm, so a JSX change is verified by eye and pinned here — the
same contract `test_analysis_routes` uses for the API client. What these assert
is the SHAPE of the surface, not its styling: that the coverage matrix reads the
whole grid, that the diagram exists and is driven by the same level vocabulary
the engine executes, and that neither invents a value `execution/ladder.py` would
reject.
"""
from pathlib import Path

from beacon_core.execution import ladder as L

REPO = Path(__file__).resolve().parents[3]
STRATEGIES = REPO / "frontend/src/pages/Strategies.jsx"
EDITOR = REPO / "frontend/src/components/LadderEditor.jsx"
DIAGRAM = REPO / "frontend/src/components/LadderDiagram.jsx"


# --- #267: the coverage matrix -------------------------------------------------
def test_the_grid_has_a_coverage_overview():
    """An empty cell is legitimate and load-bearing — it means "no ladder for
    that signal shape, run the ordinary single-shot entry". But the editor
    rendered ONE cell of a 2x8 grid, so learning which of the 16 shapes actually
    runs a ladder was a 16-click audit of a config that silently changes how real
    orders are placed."""
    src = STRATEGIES.read_text(encoding="utf-8")
    assert "ladderSet(" in src, "no coverage matrix over the whole grid"
    assert "LADDER_TPS" in src
    # it must read the WHOLE grid, not just the selected cell
    assert "ladderSet(grid, z, k)" in src


def test_the_matrix_reads_both_key_spellings():
    """The grid arrives keyed by numbers from the API and by strings once it has
    been round-tripped through JSON. A matrix that read only one would show a
    configured ladder as empty, which is worse than no matrix."""
    src = STRATEGIES.read_text(encoding="utf-8")
    assert "grid?.[String(z)]?.[String(k)]" in src
    # and the single-cell read shares the helper, so they cannot disagree
    assert "const cell = ladderCell(grid, zone, n);" in src


def test_the_matrix_doubles_as_navigation():
    src = STRATEGIES.read_text(encoding="utf-8")
    assert "onClick={() => { setZone(z); setN(k); }}" in src


def test_the_matrix_scrolls_inside_itself():
    """Wide content must scroll in its own container; the page body must not."""
    src = STRATEGIES.read_text(encoding="utf-8")
    head = src.split("ladderSet(grid, z, k)", 1)[0]
    assert "overflow-x-auto" in head.rsplit("LadderGrid", 1)[1]


# --- #268: the geometry preview ------------------------------------------------
def test_the_editor_shows_the_ladder_as_well_as_saying_it():
    """The readback described a spatial object in prose. The prose STAYS — it is
    exact and it is what a screen reader gets — and the diagram augments it."""
    src = EDITOR.read_text(encoding="utf-8")
    assert "LadderDiagram" in src and "readback(r)" in src


def test_the_diagram_is_driven_by_the_engine_s_own_vocabulary():
    """A level the diagram can draw that the engine cannot execute is a picture
    of a ladder that will 422 on save."""
    src = DIAGRAM.read_text(encoding="utf-8")
    for lvl in L.LEVELS:
        assert lvl in src, f"the diagram cannot place at {lvl}"
    for order in L.ORDERS:
        assert order in src, f"the diagram has no colour for {order}"
    for when in L.WHENS:
        if when == L.WHEN_SIGNAL:
            continue          # not a price: deliberately has no axis position
        assert when in src, f"the diagram cannot draw a {when} trigger"
    assert L.DO_CANCEL in src, "a cancel_all row must render distinctly"


def test_mid_is_derived_the_way_the_note_defines_it():
    """MID is halfway from the FAR entry edge to the stop. Hard-coding a
    y-position for it would let the picture and the prose drift apart."""
    src = DIAGRAM.read_text(encoding="utf-8")
    assert "const fMid = (fTo + F_STOP) / 2;" in src


def test_a_one_zone_signal_collapses_the_band():
    """ENTRY-TO *is* ENTRY-FROM on a single-level signal, so a shaded band
    between them would draw a range that does not exist."""
    src = DIAGRAM.read_text(encoding="utf-8")
    assert "F_ONE_ZONE" in src and "{!oneZone && (" in src


def test_the_signal_trigger_has_no_price_and_is_not_given_one():
    """`signal` means "immediately", not a level. Putting a wait marker on the
    axis for it would show the operator a price nothing is waiting for."""
    src = DIAGRAM.read_text(encoding="utf-8")
    when_f = src.split("const WHEN_F = {", 1)[1].split("};", 1)[0]
    assert "signal:" not in when_f
    assert "yWhen != null &&" in src, "a rung with no trigger price must skip the marker"


def test_the_diagram_makes_no_network_call_and_shows_no_live_price():
    """It draws only the config being edited, which is why it does not repeat the
    dead-data problem that retired the Analytics placement charts (#175)."""
    src = DIAGRAM.read_text(encoding="utf-8")
    for banned in ("api.", "fetch(", "useEffect", "useState"):
        assert banned not in src, f"the diagram must be pure config: {banned}"
    assert "Schematic" in src, "it must say on its face that it is not to scale"


def test_the_diagram_is_theme_aware_and_needs_no_chart_library():
    src = DIAGRAM.read_text(encoding="utf-8")
    # no imports at all: plain SVG, no chart engine, nothing to keep in step
    code = src.split("*/", 1)[1]
    assert "import " not in code, "the schematic should need no dependency"
    # colours come from the app's CSS variables, so both themes follow
    for tok in ("var(--beacon)", "var(--long)", "var(--short)", "var(--muted)",
                "var(--edge)", "var(--warn)"):
        assert tok in src
    # and it scales on mobile rather than overflowing the modal
    assert 'maxWidth: "100%"' in src and "overflow-x-auto" in src


def test_the_grid_tells_the_diagram_which_shape_is_being_edited():
    """LadderEditor is cell-agnostic by design, so the shape has to come from
    the caller that chose it — otherwise a 1-Zone cell draws a 2-Zone band."""
    assert "zone={zone} tps={n}" in STRATEGIES.read_text(encoding="utf-8")
