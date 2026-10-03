import json

import pytest
from app import app, build_request, render_result, run_study, select_preset, update_result


def controls(preset="french", frequency="monthly", extra="", advanced=""):
    return (
        "US:AAPL",
        preset,
        select_preset(preset)[1],
        extra,
        advanced,
        frequency,
        "US",
        "2017-01-01",
        "2025-01-01",
        60 if frequency == "monthly" else 252,
        60 if frequency == "monthly" else 252,
        3,
        "",
        "",
    )


@pytest.mark.parametrize("preset", ["french", "us_etf"])
@pytest.mark.parametrize("frequency", ["daily", "monthly"])
def test_demo_end_to_end(preset, frequency):
    report = run_study("demo", *controls(preset, frequency))
    p = report["results"][0]["presentation"]
    assert p["coefficients"]
    assert p["sample_count"] >= 60
    assert render_result(report, "demo")


def test_multiple_extra_factors_and_subset():
    args = list(controls(extra="Pharma | US:IHE\nEnergy | US:XLE"))
    args[2] = ["market_excess", "hml"]
    report = run_study("demo", *args)
    assert len(report["results"][0]["presentation"]["coefficients"]) == 5


def test_ten_custom_factors():
    factors = [
        dict(
            id=f"factor_{i}",
            label=f"Factor {i}",
            kind="asset_return",
            instrument=dict(symbol=f"ETF{i}", market="US"),
        )
        for i in range(10)
    ]
    report = run_study("demo", *controls("custom", advanced=json.dumps(factors)))
    assert len(report["results"][0]["presentation"]["coefficients"]) == 11


def test_bad_input_and_safe_error(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr("app.ctx", SimpleNamespace(triggered_id="run"))
    assert update_result(1, "demo", *controls("custom"))

    def fail(*_):
        raise RuntimeError("secret licensed payload")

    monkeypatch.setattr("app.run_study", fail)
    output = update_result(1, "demo", *controls())
    assert "Reference:" in str(output)
    assert "secret licensed payload" not in str(output)


def test_dash_layout():
    client = app.server.test_client()
    assert client.get("/").status_code == 200
    assert client.get("/_dash-layout").status_code == 200


def test_request_uses_factor_specs():
    request = build_request(*controls())
    assert len(request.skill_parameters["factor-regression"]["factors"]) == 6


def test_comparisons_and_residualization_in_playground():
    args = list(controls(extra="Sector | US:IHE"))
    args[-2] = "Market | market_excess\nMarket and sector | market_excess,extra_0"
    args[-1] = "extra_0 | market_excess,hml"
    report = run_study("demo", *args)
    p = report["results"][0]["presentation"]
    assert len(p["comparisons"]) == 3
    assert p["residualizations"][0]["factor_id"] == "extra_0"
    rendered = str(render_result(report, "demo"))
    assert "Model comparison" in rendered
    assert "Rolling stability" in rendered
    assert "Remaining variation" in rendered


@pytest.mark.parametrize("frequency", ["daily", "monthly"])
def test_demo_histories_are_independent_of_model_selection(frequency):
    from demo_data import demo_request

    base = list(controls(frequency=frequency))
    with_extra = list(base)
    with_extra[3] = "Sector | US:IHE"
    subset = list(with_extra)
    subset[2] = subset[2][:-1]
    studies = [demo_request(build_request(*args)) for args in (base, with_extra, subset)]
    targets = [
        next(s.points for s in r.factor_series if s.instrument == r.instrument) for r in studies
    ]
    assert targets[0] == targets[1] == targets[2]
    for a, b in zip(
        studies[0].research_factors.points, studies[2].research_factors.points, strict=True
    ):
        assert all(a.values[k] == b.values[k] for k in b.values)


def test_demo_date_extension_preserves_overlapping_history():
    from demo_data import demo_request

    args = list(controls())
    first = demo_request(build_request(*args))
    args[7] = "2016-01-01"
    extended = demo_request(build_request(*args))
    a = next(s for s in first.factor_series if s.instrument == first.instrument)
    b = next(s for s in extended.factor_series if s.instrument == extended.instrument)
    by_end = {p.end_date: p for p in b.points}
    assert all(p == by_end[p.end_date] for p in a.points)
