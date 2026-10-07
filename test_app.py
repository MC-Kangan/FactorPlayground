import json

import plotly.graph_objects as go
import pytest
from app import (
    _p_value,
    app,
    build_cross_section_request,
    build_request,
    bloomberg_is_configured,
    collinearity_recommendations,
    combined_correlation_matrix,
    data_source_guidance,
    describe_pack,
    graph,
    render_cross_section_result,
    render_preview,
    render_result,
    run_preview,
    run_cross_section_study,
    run_study,
    scenario_impact,
    scenario_inputs,
    select_preset,
    study_context,
    update_result,
)


def controls(preset="french", frequency="monthly", extra="", advanced=""):
    presets = [preset] if isinstance(preset, str) else preset
    return (
        "AAPL US Equity",
        presets,
        select_preset(presets)[1],
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
        "original",
        [],
        [],
        [],
    )


@pytest.mark.parametrize("preset", ["french", "us_etf", "msci_europe"])
@pytest.mark.parametrize("frequency", ["daily", "monthly"])
def test_demo_end_to_end(preset, frequency):
    report = run_study("demo", *controls(preset, frequency))
    p = report["results"][0]["presentation"]
    assert p["coefficients"]
    assert p["sample_count"] >= 60
    assert render_result(report, "demo")


def test_multiple_extra_factors_and_subset():
    args = list(controls(extra="Pharma | IHE US Equity\nEnergy | XLE US Equity"))
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
    response = client.get("/_dash-layout")
    assert response.status_code == 200
    layout = json.dumps(response.get_json(), ensure_ascii=False)
    assert '"workspace-tabs"' in layout
    assert "Single-stock attribution" in layout
    assert "Cross-sectional signals" in layout
    assert '"result"' in layout
    assert '"cs-result"' in layout


def test_analysis_callbacks_run_only_from_explicit_buttons():
    single = app.callback_map["result.children"]
    cross_section = app.callback_map["cs-result.children"]

    assert single["inputs"] == [{"id": "run", "property": "n_clicks"}]
    assert cross_section["inputs"] == [{"id": "cs-run", "property": "n_clicks"}]
    assert {item["id"] for item in single["state"]} >= {"source", "stock", "selected"}
    assert {item["id"] for item in cross_section["state"]} >= {
        "cs-source",
        "cs-universe",
        "cs-grouping",
    }


def test_graph_has_a_bounded_dash_container_height():
    props = graph(go.Figure()).to_plotly_json()["props"]

    assert props["className"] == "research-graph"
    assert props["style"] == {"height": "390px"}


def test_yaml_pack_end_to_end_and_subset():
    args = list(controls("pack:energy_yahoo"))
    args[2] = ["brent", "gas"]
    report = run_study("demo", *args)
    coefficients = report["results"][0]["presentation"]["coefficients"]
    assert len(coefficients) == 3
    assert "Fama–French" in data_source_guidance("yahoo")


def test_single_stock_diagnostics_and_scenario_panel():
    report = run_study("demo", *controls())
    rendered = str(render_result(report, "demo"))

    assert "Univariate beta" in rendered
    assert "Residual diagnostics" in rendered
    assert "Factor-move scenario" in rendered


def test_scenario_impact_uses_original_factor_units_and_excludes_intercept():
    rows, total = scenario_impact(
        [0.01, -2.0],
        [
            {"term": "market", "label": "Market", "beta": 1.2},
            {"term": "oil", "label": "Oil change", "beta": 0.003},
        ],
    )

    assert rows[0]["contribution_bps"] == pytest.approx(120)
    assert rows[1]["contribution_bps"] == pytest.approx(-60)
    assert total == pytest.approx(60)


def test_scenario_inputs_use_original_joint_model_not_orthogonalized_coefficients():
    presentation = run_study("demo", *controls())["results"][0]["presentation"]
    expected = presentation["comparisons"][0]["coefficients"][1:]
    for coefficient in presentation["coefficients"][1:]:
        coefficient["estimate"] = 999.0
        coefficient["label"] += " (residualized)"

    inputs = scenario_inputs(presentation)

    assert [row["beta"] for row in inputs] == [row["estimate"] for row in expected]
    assert all("residualized" not in row["label"] for row in inputs)
    assert all(row["unit"] for row in inputs)


def test_p_values_do_not_render_small_results_as_zero():
    assert _p_value(None) == "—"
    assert _p_value(0.0004) == "<0.001"
    assert _p_value(0.0346) == "0.035"


def test_yaml_pack_is_directly_selectable_in_main_factor_controls():
    response = app.server.test_client().get("/_dash-layout")
    layout = json.dumps(response.get_json(), ensure_ascii=False)

    assert "YAML pack · Energy" in layout
    assert "Select factors from these packs" in layout
    assert "Model comparisons only · does not add factors" in layout
    assert "Choose the data source, stock and factor pack in Core setup" in layout
    assert "AAPL US Equity" in layout
    assert "Bloomberg equity identifier" in layout
    assert "MARKET:SYMBOL" not in layout


def test_all_dropdowns_are_non_searchable():
    def visit(component):
        if hasattr(component, "to_plotly_json"):
            data = component.to_plotly_json()
            if data["type"] == "Dropdown":
                assert data["props"]["searchable"] is False
            children = data["props"].get("children", [])
            for child in children if isinstance(children, list) else [children]:
                visit(child)

    visit(app.layout)


def test_msci_europe_preset_is_available_in_factor_selector():
    options, values = select_preset(["msci_europe"])

    assert len(options) == 3
    assert values == ["market", "growth_minus_value", "momentum_minus_market"]


def test_multiple_factor_packs_can_be_combined_and_individually_filtered():
    options, values = select_preset(["msci_europe", "pack:energy_yahoo"])

    assert len(options) == 7
    assert values == [
        "market",
        "growth_minus_value",
        "momentum_minus_market",
        "brent",
        "wti",
        "gas",
        "gasoline",
    ]


def test_clearing_factor_pack_selection_leaves_an_empty_factor_list():
    assert select_preset([]) == ([], [])


def test_duplicate_factor_ids_across_packs_show_a_controlled_message():
    presets = ["msci_europe", "pack:msci_bloomberg"]

    assert select_preset(presets) == ([], [])
    assert "duplicate factor IDs" in str(describe_pack(presets))


def test_bloomberg_becomes_default_candidate_when_environment_configures_it(monkeypatch):
    monkeypatch.setenv("TRADE_RESEARCH_PRICE_PROVIDER", "bloomberg")

    assert bloomberg_is_configured()


def test_bloomberg_stock_identifier_builds_exact_configured_mapping(monkeypatch):
    monkeypatch.setenv("TRADE_RESEARCH_PRICE_PROVIDER", "bloomberg")
    args = list(controls(extra="Pharma | IHE US Equity"))
    args[0] = "REP SM Equity"

    request, settings = study_context("configured", *args)

    assert request.instrument.market == "BME"
    assert request.instrument.symbol == "REP"
    mapping = next(item for item in settings.bloomberg_return_mappings if item.instrument == request.instrument)
    assert mapping.security == "REP SM Equity"
    assert mapping.field == "PX_LAST"
    assert mapping.currency == "EUR"
    assert mapping.return_basis == "adjusted_close_return"
    assert any(item.security == "IHE US Equity" for item in settings.bloomberg_return_mappings)


def test_data_preview_uses_the_same_aligned_model_inputs():
    preview = run_preview("demo", *controls())

    assert preview["schema_version"] == "factor-study-preview-v1"
    assert preview["columns"][0]["term"] == "stock"
    assert len(preview["rows"]) >= 60
    rendered = str(render_preview(preview))
    assert "Aligned model inputs" in rendered
    assert "Hover to inspect the exact model value" in rendered
    assert "Input definitions and exact rows" in rendered
    assert "Vendor field" in rendered
    assert "Research sources and alignment" in rendered


def test_data_preview_keeps_tables_behind_one_closed_drill_down():
    component = render_preview(run_preview("demo", *controls()))
    children = component.to_plotly_json()["props"]["children"]

    assert [child.to_plotly_json()["type"] for child in children] == [
        "H3",
        "P",
        "Graph",
        "Details",
    ]
    assert children[-1].to_plotly_json()["props"].get("open") is not True


def test_single_stock_result_starts_with_plain_language_summary():
    report = run_study("demo", *controls())
    rendered = str(render_result(report, "demo"))

    assert "At a glance" in rendered
    assert "What drives the stock?" in rendered
    assert "Technical diagnostics" in rendered
    assert "Correlation and collinearity checks" in rendered


def test_failed_fit_still_exposes_data_coverage():
    report = run_study("demo", *controls())
    report["results"][0]["presentation"]["coefficients"] = []

    rendered = str(render_result(report, "demo"))

    assert "Data coverage" in rendered
    assert "Missing/invalid" in rendered


def test_combined_matrix_includes_stock_and_collinearity_recommendation():
    presentation = run_study("demo", *controls())["results"][0]["presentation"]
    labels, matrix = combined_correlation_matrix(presentation)

    assert labels[0] == "Stock"
    assert len(matrix) == len(labels)
    assert matrix[0][0] == 1

    review = {
        "relationships": [
            {"term": "oil", "label": "Oil", "incremental_r_squared": 0.08},
            {"term": "sector", "label": "Energy sector", "incremental_r_squared": 0.01},
        ],
        "original_factor_correlations": [[1.0, 0.92], [0.92, 1.0]],
        "comparisons": [
            {
                "factor_ids": ["oil", "sector"],
                "variance_inflation_factors": [12.0, 12.0],
                "condition_number": 8.0,
            }
        ],
    }
    suggestions = collinearity_recommendations(review)

    assert len(suggestions) == 1
    assert "Energy sector" in suggestions[0]
    assert "Oil" in suggestions[0]


def test_cross_section_demo_and_rendering():
    universe = (
        "XOM US Equity | Energy | US\nCVX US Equity | Energy | US\nCOP US Equity | Energy | US\n"
        "SLB US Equity | Energy | US\nEOG US Equity | Energy | US\n"
        "LLY US Equity | Health Care | US\nJNJ US Equity | Health Care | US\n"
        "MRK US Equity | Health Care | US\nABBV US Equity | Health Care | US\n"
        "AMGN US Equity | Health Care | US"
    )
    controls = (
        universe,
        "current_watchlist",
        "sector",
        "2019-01-01",
        "2025-01-31",
        5,
        10,
        5,
        10,
        "2023-01-01",
    )
    request = build_cross_section_request(*controls)
    assert len(request.portfolio_instruments) == 10
    report = run_cross_section_study("demo", *controls)
    presentation = report["results"][0]["presentation"]
    assert presentation["schema_version"] == "cross-sectional-signal-v2"
    assert presentation["grouping"] == "sector"
    assert presentation["segments"]
    assert render_cross_section_result(report, "demo")


def test_request_uses_factor_specs():
    request = build_request(*controls())
    assert len(request.skill_parameters["factor-regression"]["factors"]) == 6


def test_comparisons_and_residualization_in_playground():
    args = list(controls(extra="Sector | IHE US Equity"))
    args[12] = "Market | market_excess\nMarket and sector | market_excess,extra_0"
    args[13:] = ["controls", ["extra_0"], ["market_excess", "hml"], []]
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
    with_extra[3] = "Sector | IHE US Equity"
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


def test_sequential_controls_and_relationship_rendering():
    args = list(controls("us_etf"))
    args[13:] = ["sequential", [], [], list(reversed(args[2]))]
    report = run_study("demo", *args)
    p = report["results"][0]["presentation"]
    assert p["attribution_mode"] == "sequential"
    assert p["sequential_order"] == args[-1]
    assert p["r_squared"] == pytest.approx(p["comparisons"][0]["r_squared"])
    rendered = str(render_result(report, "demo"))
    assert "Incremental R²" in rendered
    assert "Rolling stock–factor correlations" in rendered


def test_attribution_choices_preserve_order_and_remove_stale_factors():
    from app import attribution_choices

    values = attribution_choices(
        ["us_etf"],
        ["market", "growth_minus_value"],
        "",
        "",
        ["missing"],
        ["market"],
        ["growth_minus_value", "missing"],
    )
    assert values[3] == []
    assert values[4] == ["market"]
    assert values[5] == ["growth_minus_value", "market"]


def test_incomplete_sequential_order_and_empty_controls_are_rejected():
    args = list(controls("us_etf"))
    args[13:] = ["sequential", [], [], ["market"]]
    with pytest.raises(ValueError):
        build_request(*args)
    args[13:] = ["controls", [], [], []]
    with pytest.raises(ValueError):
        build_request(*args)


def test_french_and_bloomberg_level_pack_build_one_excess_return_study(monkeypatch):
    import app as module
    from trade_research.skills.factor_parameters import FactorRegressionParameters
    pack = module.FACTOR_PACKS["energy_bloomberg"].model_copy(update={"enabled": True})
    monkeypatch.setitem(module.FACTOR_PACKS, "energy_bloomberg", pack)
    monkeypatch.setenv("TRADE_RESEARCH_PRICE_PROVIDER", "bloomberg")
    monkeypatch.delenv("TRADE_RESEARCH_CONFIG", raising=False)
    args = list(controls(["french", "pack:energy_bloomberg"]))
    args[2] = ["market_excess", "hml", "brent"]
    request, settings = study_context("configured", *args)
    parameters = FactorRegressionParameters.model_validate(request.skill_parameters["factor-regression"])
    assert parameters.return_mode == "excess_return"
    assert [f.id for f in settings.bloomberg_factor_pack.level_factors] == ["brent"]
    assert {f.id for f in parameters.selected_factors()} == {"market_excess", "hml", "brent"}
