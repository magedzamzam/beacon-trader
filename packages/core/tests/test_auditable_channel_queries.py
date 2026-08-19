"""Per-channel queries run on the auditable basis (#234/#237/#238).

The weekly reads these. Left unfiltered they re-derive the ranking that put
source 12 on the skip list at -0.326R when, on trades whose money is actually
its own, it is +0.027R.
"""
from beacon_core.analysis.report import (_channel_verdict_query, partial_ladder)


def _sql(q) -> str:
    return str(q.compile()).lower()


def test_the_verdict_query_excludes_partial_ladders():
    sql = _sql(_channel_verdict_query())
    assert "pl_attribution" in sql
    assert "not (exists" in sql or "not exists" in sql, sql


def test_the_predicate_names_the_only_auditable_value():
    """`exact` is a whitelist, deliberately: a basis added later is excluded
    until somebody decides it belongs, which is the safe direction for money."""
    sql = str(partial_ladder().compile(compile_kwargs={"literal_binds": True})).lower()
    assert "'exact'" in sql
    assert "pl_attribution is not null" in sql


def test_unclassified_history_is_NOT_treated_as_partial():
    """NULL is what every row predating the column carries. Treating it as
    partial would empty the report the moment this deploys."""
    sql = str(partial_ladder().compile(compile_kwargs={"literal_binds": True})).lower()
    # the predicate fires only on rows that ARE classified and are not exact
    assert "pl_attribution is not null" in sql
    assert "pl_attribution is null" not in sql


def test_it_only_looks_at_closed_legs():
    """An open leg has no money yet and no attribution — counting it would drop
    every live trade out of the report."""
    sql = str(partial_ladder().compile(compile_kwargs={"literal_binds": True})).lower()
    assert "status = 'closed'" in sql


def test_the_verdict_query_still_compiles():
    assert "from signal_analytics" in _sql(_channel_verdict_query())
