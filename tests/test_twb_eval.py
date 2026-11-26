from __future__ import annotations

import pytest

from countback.twb_eval import EvalError, evaluate, field_refs, parse


def ev(formula, rows, calcs=None):
    calcs = {k: parse(v) for k, v in (calcs or {}).items()}
    return evaluate(parse(formula), rows, calcs.get)


def test_arithmetic_precedence_and_parentheses():
    assert ev("1 + 2 * 3", [{}]) == 7
    assert ev("(1 + 2) * 3", [{}]) == 9
    assert ev("10 - 4 - 3", [{}]) == 3
    assert ev("8 / 4 / 2", [{}]) == 1
    assert ev("-2 * -3", [{}]) == 6


def test_sum_aggregates_over_the_group_and_bare_fields_need_one_row():
    rows = [{"x": 1.0, "y": 2.0}, {"x": 3.0, "y": 4.0}]
    assert ev("SUM([x]) / SUM([y])", rows) == pytest.approx(4 / 6)
    assert ev("SUM([x] * [y])", rows) == 14
    assert ev("[x] + [y]", rows[:1]) == 3
    with pytest.raises(EvalError, match="outside an aggregate"):
        ev("[x] + 1", rows)


def test_null_propagation_division_by_zero_and_zn():
    assert ev("SUM([x]) / SUM([y])", [{"x": 1.0, "y": 0.0}]) is None
    assert ev("SUM([x]) + 1", [{"x": None}]) is None
    assert ev("ZN(SUM([x])) + 1", [{"x": None}]) == 1
    assert ev("ZN(SUM([x]) / SUM([y]))", [{"x": 1.0, "y": 0.0}]) == 0


def test_calculation_can_reference_another_calculation():
    calcs = {"Ratio": "SUM([a]) / SUM([b])"}
    assert ev("[Ratio] * 100", [{"a": 1.0, "b": 4.0}], calcs) == 25


def test_field_refs_lists_every_reference():
    assert field_refs(parse("ZN(SUM([a])) / (SUM([b]) + SUM([a_b]))")) == ["a", "b", "a_b"]


@pytest.mark.parametrize(
    "formula",
    ["IF [a] > 1 THEN 2 END", "AVG([a])", "SUM([a]", "1 +", "'text'", "{FIXED : SUM([a])}"],
)
def test_unsupported_or_malformed_formulas_raise(formula):
    with pytest.raises(EvalError):
        evaluate(parse(formula), [{"a": 1.0}], lambda name: None)
