"""The reporting logic, which is the part that can be wrong quietly."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from experiments import report


def _rows(arm, passed, n, cost=1000):
    return [{"arm": arm, "passed": i < passed, "cost_tokens": cost, "turns": 10}
            for i in range(n)]


def test_overlapping_intervals_refuse_a_verdict():
    """At small n the sign of a gap is noise, and a benchmark number
    gets quoted far from its context."""
    rows = _rows("served", 6, 10) + _rows("held_out", 5, 10)
    v = report.verdict(report.summarise(rows))
    assert v["verdict"] is None
    assert "no measurable difference" in v["note"].lower()


def test_a_clear_separation_is_reported():
    rows = _rows("served", 95, 100) + _rows("held_out", 50, 100)
    v = report.verdict(report.summarise(rows))
    assert v["verdict"] == "better"
    assert v["pass_rate_delta"] == 0.45


def test_harm_is_reported_as_worse_not_hidden():
    """The row that matters most: memory making things worse."""
    rows = _rows("served", 40, 100) + _rows("held_out", 80, 100)
    v = report.verdict(report.summarise(rows))
    assert v["verdict"] == "worse"


def test_cost_is_reported_even_when_pass_rate_is_inconclusive():
    rows = _rows("served", 5, 10, cost=700) + _rows("held_out", 5, 10, cost=1000)
    v = report.verdict(report.summarise(rows))
    assert v["verdict"] is None
    assert v["cost_delta"] == -0.3


def test_wilson_stays_inside_zero_and_one():
    for n in (1, 5, 50):
        for k in (0, n):
            lo, hi = report._wilson(k, n)
            assert 0.0 <= lo <= hi <= 1.0
