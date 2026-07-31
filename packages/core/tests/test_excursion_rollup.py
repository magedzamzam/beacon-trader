"""Per-channel R-ladder reduction (#182). Pure — DB-free (repo convention)."""
from beacon_core.analysis.report import excursion_rollup


def row(channel, tp1_r, mfe_r, race, mae_r=0.2):
    """One reconstructed signal, shaped as the store persists it."""
    return {"channel": channel, "tp1_r": tp1_r, "mfe_r": mfe_r, "mae_r": mae_r,
            "race": race,
            "ladder": {k: mfe_r >= float(k)
                       for k in ("0.25", "0.5", "1.0", "1.5", "2.0", "3.0")}}


def test_ladder_rates_and_tp1_distance_sit_side_by_side():
    """The whole point of the table: a channel's reach curve next to where it
    actually puts TP1, so "take profit earlier on this source" is readable."""
    rows = ([row("Quartz", 1.0, 0.6, "sl")] * 6      # reaches 0.5R, not its 1.0R TP1
            + [row("Quartz", 1.0, 1.2, "tp1")] * 4)
    out = excursion_rollup(rows, significance_n=30)
    q = out["channels"][0]
    assert q["channel"] == "Quartz" and q["n"] == 10
    assert q["median_tp1_r"] == 1.0
    assert q["reach"]["0.5"] == 1.0            # always got half an R
    assert q["reach"]["1.0"] == 0.4            # only 40% got the R its TP1 needs
    assert q["tp1_before_sl"] == 0.4


def test_tp1_distance_spread_across_channels_is_visible():
    """TFXC needs 0.15R to "win", Quartz needs 1.00R — a 6.7x harder bar. The
    rollup must surface that, because it is what qualifies every win rate."""
    rows = [row("TFXC", 0.15, 0.3, "tp1") for _ in range(4)] + \
           [row("Quartz", 1.0, 0.3, "sl") for _ in range(4)]
    out = excursion_rollup(rows, significance_n=30)
    by = {c["channel"]: c for c in out["channels"]}
    assert by["TFXC"]["median_tp1_r"] == 0.15
    assert by["Quartz"]["median_tp1_r"] == 1.0
    # identical excursions, opposite race outcomes — geometry, not signal quality
    assert by["TFXC"]["median_mfe_r"] == by["Quartz"]["median_mfe_r"] == 0.3
    assert by["TFXC"]["tp1_before_sl"] == 1.0 and by["Quartz"]["tp1_before_sl"] == 0.0


def test_significance_state_follows_the_n_30_floor():
    out = excursion_rollup([row("Big", 0.5, 0.8, "tp1")] * 30
                           + [row("Mid", 0.5, 0.8, "tp1")] * 16
                           + [row("Thin", 0.5, 0.8, "tp1")] * 3,
                           significance_n=30)
    by = {c["channel"]: c["state"] for c in out["channels"]}
    assert by == {"Big": "significant", "Mid": "watch", "Thin": "gathering"}
    assert out["channels"][0]["channel"] == "Big"      # significant sorts first


def test_unresolved_signals_are_reported_not_counted_as_losses():
    out = excursion_rollup([row("A", 0.5, 0.4, "horizon")] * 2
                           + [row("A", 0.5, 0.4, "sl")] * 2, significance_n=30)
    a = out["channels"][0]
    assert a["unresolved"] == 0.5 and a["sl_first"] == 0.5
    assert a["tp1_before_sl"] == 0.0


def test_rows_without_a_reconstruction_are_excluded():
    out = excursion_rollup([row("A", 0.5, 0.8, "tp1"),
                            {"channel": "A", "mfe_r": None}], significance_n=30)
    assert out["n_labelled"] == 1
    assert out["channels"][0]["n"] == 1


def test_unattributed_channel_is_named_not_dropped():
    out = excursion_rollup([{"channel": None, "mfe_r": 0.5, "tp1_r": 0.4,
                             "race": "tp1", "ladder": {"0.25": True}}])
    assert out["channels"][0]["channel"] == "Unattributed"
