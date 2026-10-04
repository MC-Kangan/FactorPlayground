"""Local factor playground; all statistical calculations belong to TradeAgent."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading
import uuid
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path

import plotly.graph_objects as go
from dash import Dash, Input, Output, State, dcc, html
from demo_data import cross_section_demo_request, demo_request
from pydantic import ValidationError

from trade_research.domain import AnalysisRequest, InstrumentId
from trade_research.domain.factors import preset_factors
from trade_research.domain.models import MAX_FACTOR_COUNT, FactorSpec
from trade_research.engine import ResearchEngine
from trade_research.factor_packs import load_factor_packs
from trade_research.settings import Settings
from trade_research.skills.cross_sectional_signal import (
    CrossSectionalSignalParameters,
    CrossSectionUniverseMember,
)
from trade_research.skills.factor_regression import FactorRegressionParameters

WARNINGS = {
    "original_high_collinearity": (
        "Original factors overlap strongly. "
        "Residualization does not create independent information; "
        "review the original-model condition number and VIF alongside the selected basis."
    ),
    "raw_returns_not_alpha": (
        "The intercept is not risk-adjusted alpha: this model has no risk- free rate."
    ),
    "nonsynchronous_closes": (
        "Matching dates do not guarantee that different exchanges close at the same time."
    ),
    "high_collinearity": (
        "Factors are strongly related. Individual exposures may be unstable; "
        "read confidence intervals carefully."
    ),
    "return_basis_mismatch": (
        "Return conventions differ. All inputs must use the same adjusted/gross/net convention."
    ),
    "insufficient_history": (
        "Too few aligned observations. Extend the dates or lower the minimum (at least 60)."
    ),
    "rank_deficient": (
        "Factors duplicate each other or have no variation. Choose distinct benchmark series."
    ),
    "constant_target": (
        "The stock returns have no variation, so a useful regression cannot be fitted."
    ),
    "missing_intervals": (
        "Some expected periods are missing or incomplete. The coverage table "
        "separates missing data, FX losses and alignment losses."
    ),
    "discontinuous_history": (
        "There are gaps between retained intervals, including gaps shared by every series."
    ),
    "short_history": (
        "The selected frequency has a short sample: estimates may be "
        "sensitive to the chosen period."
    ),
    "calendar_unavailable": (
        "Choose a supported exchange calendar. INDEX inputs need an explicit benchmark calendar."
    ),
    "fx_missing": (
        "FX endpoints are missing or an FX provider is not configured. No "
        "currency conversion is guessed."
    ),
    "fx_conversion": (
        "Local returns were converted to the study currency using explicit FX "
        "endpoints. Closing times may differ."
    ),
    "research_data_lag": (
        "The published research factors stop before the requested end date. "
        "The fit uses available observations only."
    ),
    "research_data_revised": (
        "French datasets can be revised. This is historical explanation, not "
        "a point-in-time predictive test."
    ),
    "return_convention_difference": (
        "French research portfolios and vendor adjusted/gross/net returns may "
        "have different dividend conventions."
    ),
    "benchmark_self_inclusion_unchecked": (
        "The target may be a constituent of a selected benchmark; "
        "historical weights have not been checked."
    ),
    "influential_observations": (
        "Some observations have high Cook's distance. Genuine market shocks "
        "are retained, not clipped."
    ),
    "rolling_windows_skipped": (
        "Some rolling windows were singular or had a constant target and could not be fitted."
    ),
    "numerical_failure": (
        "The numerical fit could not produce reliable finite results. Review the input series."
    ),
    "hac_intervals_withheld": (
        "There are missing periods inside the fitted sample. Exposures are "
        "shown, but HAC uncertainty is withheld because dates are irregular."
    ),
    "unsupported_frequency": (
        "Monthly source returns cannot be interpolated into daily observations."
    ),
    "factor_definition_mismatch": (
        "The selected factors or provider metadata do not match the required input contract."
    ),
}

TODAY = datetime.now(UTC).date()
GRAPH_HEIGHT_PX = 390
SOURCE_GUIDANCE = {
    "demo": (
        "Offline deterministic data for learning and UI checks. It does not represent market "
        "history and makes no web requests."
    ),
    "yahoo": (
        "Downloads equity and ETF adjusted closes from Yahoo when you run the study. The "
        "Fama–French preset separately downloads official research files. Yahoo futures and "
        "indexes are rejected because their return conventions are not verified."
    ),
    "configured": (
        "Uses the provider configured for TradeAgent, including entitled Bloomberg return "
        "series. MSCI and commodity indexes require explicit identifier, field, currency and "
        "return-basis mappings; there is no silent public-data fallback."
    ),
}
PACK_DIRECTORY = Path(
    os.environ.get("FACTOR_PLAYGROUND_PACK_DIR", str(Path(__file__).parent / "factor_packs"))
)
FACTOR_PACKS = load_factor_packs(PACK_DIRECTORY)


def available_factors(preset):
    if preset.startswith("pack:"):
        return pack_for(preset).selected_factors()
    return preset_factors(preset)


def pack_for(preset):
    if not preset.startswith("pack:"):
        return None
    if preset[5:] not in FACTOR_PACKS:
        raise ValueError("Unknown YAML factor pack")
    return FACTOR_PACKS[preset[5:]]


def data_source_guidance(source):
    return SOURCE_GUIDANCE.get(source, "Choose a supported data source.")


def instrument(text):
    market, separator, symbol = (text or "").strip().partition(":")
    if not separator:
        raise ValueError("Use MARKET:SYMBOL, for example US:AAPL or XETRA:SAP.")
    return InstrumentId(market=market.upper(), symbol=symbol.upper())


def parse_lines(text):
    """Small explicit editor: a name/target and comma-separated factor IDs."""
    rows = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        name, separator, factors = line.partition("|")
        if not separator or not name.strip():
            raise ValueError("Use name or target | comma-separated factor IDs.")
        rows.append((name.strip(), [v.strip() for v in factors.split(",") if v.strip()]))
    return rows


def editor_factors(preset, selected, extra, advanced):
    factors = [
        f.model_dump(mode="json") for f in available_factors(preset) if f.id in (selected or [])
    ]
    # Each line adds a funded benchmark; two legs make a self-financing spread.
    for i, line in enumerate((extra or "").splitlines()):
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) not in (2, 3):
            raise ValueError(
                "Extra factors use Label | MARKET:SYMBOL | optional short MARKET:SYMBOL."
            )
        long_instrument = instrument(parts[1])
        short_instrument = instrument(parts[2]) if len(parts) == 3 else None
        factors.append(
            dict(
                id=f"extra_{i}",
                label=parts[0],
                kind="spread" if len(parts) == 3 else "asset_return",
                instrument=long_instrument.model_dump(),
                short_instrument=(
                    short_instrument.model_dump() if short_instrument is not None else None
                ),
                calendar="weekdays" if long_instrument.market == "INDEX" else None,
                short_calendar=(
                    "weekdays"
                    if short_instrument is not None and short_instrument.market == "INDEX"
                    else None
                ),
            )
        )
    if (advanced or "").strip():
        factors = json.loads(advanced)
    return factors


def build_request(
    stock,
    preset,
    selected,
    extra,
    advanced,
    frequency,
    region,
    start,
    end,
    minimum,
    rolling,
    lags,
    comparisons,
    attribution_mode,
    residual_targets,
    residual_controls,
    sequential_order,
):
    factors = editor_factors(preset, selected, extra, advanced)
    if attribution_mode not in {"original", "controls", "sequential"}:
        raise ValueError("Choose a supported attribution mode")
    if attribution_mode == "controls" and (not residual_targets or not residual_controls):
        raise ValueError("Choose targets and controls for residualization")
    if attribution_mode == "sequential" and not sequential_order:
        raise ValueError("Choose an explicit factor order")
    params = FactorRegressionParameters.model_validate(
        dict(
            preset="custom" if pack_for(preset) else preset,
            factors=factors,
            frequency=frequency,
            region=region,
            start_date=start,
            end_date=end,
            minimum_observations=minimum,
            rolling_window=rolling,
            hac_lags=lags,
            max_factors=MAX_FACTOR_COUNT,
            comparisons=[
                {"name": name, "factor_ids": ids} for name, ids in parse_lines(comparisons)
            ],
            residualizations=[
                {"factor_id": target, "against": residual_controls}
                for target in (residual_targets or [])
            ]
            if attribution_mode == "controls"
            else [],
            sequential_order=(sequential_order or []) if attribution_mode == "sequential" else [],
        )
    ).resolve_dates(TODAY)
    return AnalysisRequest(
        instrument=instrument(stock),
        analysts=("factor-regression",),
        skill_parameters={"factor-regression": params.model_dump(mode="json")},
    )


def run_study(source, *controls):
    request = build_request(*controls)
    pack = pack_for(controls[1])
    if pack:
        if not pack.enabled or source not in pack.sources:
            raise ValueError("Choose a supported source and a configured, enabled factor pack.")
        if (controls[4] or "").strip():
            raise ValueError("Use Custom factors for JSON overrides, not a YAML pack.")
        selected = {f["id"] for f in request.skill_parameters["factor-regression"]["factors"]}
        # Fetch only selected pack columns; unselected series must not remove observations.
        pack = pack.subset(selected)
        if pack.level_factors and source != "configured":
            raise ValueError("Bloomberg level packs require the configured source.")
    if source == "demo":
        request = demo_request(request)
        settings = Settings()
    elif source == "yahoo":
        settings = Settings(price_provider="yahoo")
    elif source == "configured":
        settings = Settings.from_environment(os.environ)
    else:
        raise ValueError("Choose a data source.")
    if source == "configured":
        settings = Settings.model_validate(
            settings.model_dump()
            | {"bloomberg_factor_pack": pack if pack and pack.level_factors else None}
        )
    return asyncio.run(ResearchEngine.from_settings(settings).analyze(request)).model_dump(
        mode="json"
    )


def parse_universe(text):
    """Parse MARKET:SYMBOL | sector | country | optional from | optional to."""
    members = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split("|")]
        if not 1 <= len(parts) <= 5:
            raise ValueError("Invalid universe row")
        members.append(
            CrossSectionUniverseMember(
                instrument=instrument(parts[0]),
                sector=parts[1] if len(parts) > 1 and parts[1] else None,
                country=parts[2].upper() if len(parts) > 2 and parts[2] else None,
                member_from=parts[3] if len(parts) > 3 and parts[3] else None,
                member_to=parts[4] if len(parts) > 4 and parts[4] else None,
            )
        )
    if len(members) < 3:
        raise ValueError("At least three universe members are required")
    if len({member.instrument for member in members}) != len(members):
        raise ValueError("Universe instruments must be unique")
    return tuple(members)


def build_cross_section_request(
    universe,
    universe_mode,
    grouping,
    start,
    end,
    quantiles,
    minimum_assets,
    minimum_group_assets,
    cost_bps,
    holdout_start,
):
    members = parse_universe(universe)
    parameters = (
        CrossSectionalSignalParameters.model_validate(
            {
                "start_date": start,
                "end_date": end,
                "quantiles": quantiles,
                "minimum_assets": minimum_assets,
                "universe_mode": universe_mode,
                "members": [member.model_dump(mode="json") for member in members],
                "grouping": grouping,
                "minimum_group_assets": minimum_group_assets,
                "transaction_cost_bps": cost_bps,
                "holdout_start_date": holdout_start or None,
            }
        )
        .resolve_dates(TODAY)
        .validate_instruments(tuple(m.instrument for m in members))
    )
    return AnalysisRequest(
        instrument=InstrumentId(symbol="BASKET", market="PORTFOLIO"),
        scope="portfolio",
        portfolio_instruments=tuple(member.instrument for member in members),
        analysts=("cross-sectional-signal",),
        skill_parameters={"cross-sectional-signal": parameters.model_dump(mode="json")},
    )


def run_cross_section_study(source, *controls):
    request = build_cross_section_request(*controls)
    if source == "demo":
        request = cross_section_demo_request(request)
        settings = Settings()
    elif source == "yahoo":
        settings = Settings(price_provider="yahoo")
    elif source == "configured":
        settings = Settings.from_environment(os.environ)
    else:
        raise ValueError("Choose a data source")
    return asyncio.run(ResearchEngine.from_settings(settings).analyze(request)).model_dump(
        mode="json"
    )


def _percent(value):
    return "—" if value is None else f"{100 * value:.2f}%"


def _number(value):
    return "—" if value is None else f"{value:.3f}"


def finish_figure(figure, title, *, yaxis_title=None, percent_axis=False):
    figure.update_layout(
        title={"text": title, "font": {"size": 15, "color": "#17344d"}},
        template="plotly_white",
        height=GRAPH_HEIGHT_PX,
        margin={"l": 52, "r": 22, "t": 62, "b": 45},
        hovermode="x unified",
        colorway=["#116653", "#3c6e9b", "#c08a36", "#765c91", "#819a56"],
        legend={"orientation": "h", "y": 1.1, "x": 1, "xanchor": "right"},
        yaxis={
            "title": yaxis_title,
            "tickformat": ".1%" if percent_axis else None,
            "gridcolor": "#e7ece9",
            "zerolinecolor": "#b9c6c0",
        },
        xaxis={"gridcolor": "#eef2f0"},
    )
    return figure


def graph(figure):
    return dcc.Graph(
        figure=figure,
        config={"displaylogo": False, "responsive": True, "scrollZoom": False},
        className="research-graph",
        style={"height": f"{GRAPH_HEIGHT_PX}px"},
    )


def render_cross_section_result(report, source):
    result = report["results"][0]
    p = result.get("presentation")
    if not p:
        return html.P("The provider could not complete this cross-sectional study.")
    valid = [row for row in p["periods"] if row["rank_ic"] is not None]
    ic_chart = go.Figure()
    ic_chart.add_scatter(
        x=[row["formation_date"] for row in valid],
        y=[row["rank_ic"] for row in valid],
        mode="lines+markers",
        name="Monthly rank IC",
    )
    ic_chart.add_hline(y=0, line_color="#8b98a7", line_dash="dot")
    finish_figure(ic_chart, "Rank IC through time", yaxis_title="Spearman IC")
    quantile_chart = go.Figure(
        go.Bar(
            x=[f"Q{row['quantile']}" for row in p["average_quantile_returns"]],
            y=[row["mean_forward_return"] for row in p["average_quantile_returns"]],
        )
    )
    finish_figure(
        quantile_chart,
        "Average next-period return by score quantile",
        percent_axis=True,
    )
    wealth_top = []
    wealth_universe = []
    top_value = universe_value = 1.0
    for row in valid:
        top_value *= 1 + row["net_top_return"]
        universe_value *= 1 + row["universe_return"]
        wealth_top.append(top_value)
        wealth_universe.append(universe_value)
    portfolio_chart = go.Figure()
    portfolio_chart.add_scatter(
        x=[row["formation_date"] for row in valid], y=wealth_top, name="Top quantile net"
    )
    portfolio_chart.add_scatter(
        x=[row["formation_date"] for row in valid],
        y=wealth_universe,
        name="Eligible universe",
    )
    finish_figure(portfolio_chart, "Growth of one research unit", yaxis_title="Research units")
    metrics = table(
        ("Measure", "Result"),
        (
            ("Mean IC", _number(p["average_rank_ic"])),
            ("Median IC", _number(p["median_rank_ic"])),
            ("IC information ratio", _number(p["rank_ic_information_ratio"])),
            ("Positive IC share", _percent(p["positive_rank_ic_fraction"])),
            (
                "Block-bootstrap 95% interval",
                f"[{_number(p['rank_ic_lower_95'])}, {_number(p['rank_ic_upper_95'])}]",
            ),
            ("Annualized top return after cost", _percent(p["annualized_net_top_return"])),
            ("Annualized universe return", _percent(p["annualized_universe_return"])),
            ("Top portfolio max drawdown", _percent(p["net_top_max_drawdown"])),
            ("Average turnover", _percent(p["average_top_turnover"])),
        ),
    )
    children = [
        html.H2("Cross-sectional results"),
        html.P(
            f"{p['universe_mode'].replace('_', ' ')} · {p['grouping'].replace('_', ' ')} "
            f"ranking · {len(p['coverage'])} stocks · {p['transaction_cost_bps']:.1f} bps cost"
        ),
        html.Ul([html.Li(item.replace("_", " ")) for item in p["diagnostics"]]),
        metrics,
        graph(ic_chart),
        graph(quantile_chart),
        graph(portfolio_chart),
    ]
    if p["segments"]:
        children.extend(
            [
                html.H3("Development and holdout"),
                table(
                    (
                        "Segment",
                        "Dates",
                        "Periods",
                        "Purged",
                        "Mean IC",
                        "Top excess",
                        "Net annualized",
                    ),
                    [
                        (
                            row["name"],
                            f"{row['start_date']} to {row['end_date']}",
                            row["period_count"],
                            row["purged_period_count"],
                            _number(row["average_rank_ic"]),
                            _percent(row["average_top_excess_return"]),
                            _percent(row["annualized_net_top_return"]),
                        )
                        for row in p["segments"]
                    ],
                ),
            ]
        )
    if source == "demo":
        children.append(html.P("Synthetic learning data; these are not market results."))
    children.append(
        html.Details(
            [html.Summary("Full diagnostics and metadata"), html.Pre(json.dumps(p, indent=2))]
        )
    )
    return html.Div(children)


def table(headings, rows):
    return html.Div(
        html.Table(
            [
                html.Thead(html.Tr([html.Th(v) for v in headings])),
                html.Tbody([html.Tr([html.Td(v) for v in row]) for row in rows]),
            ]
        ),
        className="table-scroll",
    )


def render_relationships(p):
    rows = p.get("relationships", [])
    if not rows:
        return []
    labels = [r["label"] for r in rows]
    children = [
        html.H3("Factor relationships · original inputs"),
        html.P(
            "All diagnostics use the regression's aligned sample and configured returns or changes, "
            "not price levels. Partial correlation removes all other selected factors from both "
            "the stock and the factor. Incremental R² removes one factor from the full model; "
            "contributions do not add up when factors share information. These are historical "
            "associations, not significance rankings or forecasts."
        ),
        table(
            ["Factor", "Pearson", "Spearman", "Partial correlation", "Incremental R² · pp"],
            [
                [
                    r["label"],
                    _number(r["pearson"]),
                    _number(r["spearman"]),
                    _number(r["partial_correlation"]),
                    f"{100 * r['incremental_r_squared']:.2f}",
                ]
                for r in rows
            ],
        ),
    ]
    fig = go.Figure()
    for key in ("pearson", "spearman"):
        fig.add_bar(x=labels, y=[r[key] for r in rows], name=key.title())
    finish_figure(fig, "Stock–factor correlations · original inputs", yaxis_title="Correlation")
    fig.update_layout(yaxis_range=[-1, 1], barmode="group")
    children.append(graph(fig))
    for title, matrix in [
        ("Original factor correlations · Pearson", p["original_factor_correlations"]),
        ("Selected attribution basis · Pearson", p["factor_correlations"]),
    ]:
        if title.startswith("Selected") and p["attribution_mode"] == "original":
            continue
        heat = go.Figure(
            go.Heatmap(
                z=matrix, x=labels, y=labels, zmin=-1, zmax=1, colorscale="RdBu", reversescale=True
            )
        )
        finish_figure(heat, title)
        heat.update_layout(hovermode="closest")
        children.append(graph(heat))
    full = p["comparisons"][0]
    children.append(
        table(
            ["Factor", "Original VIF", "Selected-basis VIF"],
            [
                [label, _number(before), _number(after)]
                for label, before, after in zip(
                    labels,
                    full["variance_inflation_factors"],
                    p["variance_inflation_factors"],
                    strict=True,
                )
            ],
        )
    )
    children.append(
        html.P(
            f"Original condition number: {full['condition_number']:.2f}; "
            f"selected basis: {p['condition_number']:.2f}. "
            "Lower collinearity in a transformed basis does not create new information."
        )
    )
    rolling = p.get("rolling_correlations", [])
    if rolling:
        fig = go.Figure()
        for method in ("pearson", "spearman"):
            for i, label in enumerate(labels):
                fig.add_scatter(
                    x=[r["end_date"] for r in rolling],
                    y=[r[method][i] for r in rolling],
                    name=f"{label} · {method}",
                    mode="lines",
                    visible=True if method == "pearson" else "legendonly",
                )
        finish_figure(
            fig, "Rolling stock–factor correlations · original inputs", yaxis_title="Correlation"
        )
        fig.update_layout(yaxis_range=[-1, 1])
        children.extend(
            [
                graph(fig),
                html.P(
                    f"Window: {p['rolling_window']} aligned observations, sampled at each month's last "
                    "retained observation. Spearman lines can be enabled in the legend. "
                    "Overlapping windows are descriptive; missing dates can extend calendar spans."
                ),
            ]
        )
    return children


def render_result(report, source):
    result = report["results"][0]
    p = result.get("presentation")
    if not p:
        return html.P(
            "Provider could not complete this study. Check configuration and entitlements."
        )
    notices = [html.Li(WARNINGS.get(w, w.replace("_", " "))) for w in p["diagnostics"]]
    details = [
        html.H2("Study results"),
        html.P(
            f"{p['frequency']} · {p['study_currency']} · {p['sample_count']} observations · "
            f"{p['return_mode'].replace('_', ' ')}"
        ),
        html.Ul(notices),
    ]
    if source == "demo":
        details.append(
            html.P("Synthetic data for learning only; these are not historical market results.")
        )
    if p.get("error_code"):
        details.append(
            html.P(
                f"Stage: {p['error_stage']} · Code: {p['error_code']} · Reference: {p['error_id']}"
            )
        )
    if p.get("coverage"):
        details.extend(
            [
                html.H3("Data coverage"),
                table(
                    [
                        "Input",
                        "Expected",
                        "Available",
                        "Missing/invalid",
                        "FX losses",
                        "Alignment losses",
                    ],
                    [
                        [
                            c[k]
                            for k in (
                                "role",
                                "expected_periods",
                                "available_periods",
                                "invalid_or_missing_periods",
                                "fx_endpoint_losses",
                                "alignment_losses",
                            )
                        ]
                        for c in p["coverage"]
                    ],
                ),
            ]
        )
    coefficients = p.get("coefficients", [])
    if not coefficients:
        details.append(
            html.P("No fit is shown. Review coverage, factor definitions and minimum observations.")
        )
        return html.Div(details)
    details.append(
        html.P(
            "Attribution: "
            + p["attribution_mode"]
            + (" · " + " → ".join(p["sequential_order"]) if p["sequential_order"] else "")
        )
    )
    details.append(
        html.Details(
            [
                html.Summary("Relationships and overlap · correlations, VIF and incremental R²"),
                *render_relationships(p),
            ]
        )
    )
    details.append(
        html.P(
            f"R²: {p['r_squared']:.1%} · Adjusted R²: {p['adjusted_r_squared']:.1%} · "
            f"Covariance: {p['covariance']} · Selected-basis condition: {p['condition_number']:.2f}"
        )
    )
    details.append(
        html.P(
            "Coefficients describe historical co-movement, not causality or predicted returns. "
            "A funded benchmark becomes an excess return when cash is subtracted; spreads do not."
        )
    )
    details.append(
        table(
            ["Factor", "Coefficient", "95% interval", "Input unit"],
            [
                [
                    c["label"],
                    f"{c['estimate']:.5f}",
                    "Withheld: irregular dates"
                    if c["lower_95"] is None
                    else f"[{c['lower_95']:.5f}, {c['upper_95']:.5f}]",
                    c["unit"],
                ]
                for c in coefficients
            ],
        )
    )
    chart = go.Figure(
        go.Bar(
            x=[c["label"] for c in coefficients[1:]],
            y=[c["standardized_effect"] for c in coefficients[1:]],
        )
    )
    finish_figure(
        chart,
        "Standardized effects (comparable across units)",
        yaxis_title="Standardized coefficient",
    )
    details.append(graph(chart))
    if p.get("rolling"):
        rolling = go.Figure()
        by_date = {v["end_date"]: v for v in p["rolling"]}
        plot_dates = sorted(set(by_date) | set(p["rolling_skipped_end_dates"]))
        for i, c in enumerate(coefficients[1:], 1):
            rolling.add_scatter(
                x=plot_dates,
                y=[by_date[d]["estimates"][i] if d in by_date else None for d in plot_dates],
                connectgaps=False,
                name=c["label"],
            )
        finish_figure(rolling, "Rolling exposures (original units)", yaxis_title="Coefficient")
        details.append(graph(rolling))
    if p.get("comparisons"):
        details.extend(
            [
                html.H3("Model comparison"),
                html.P(
                    "All models below use original factors and exactly the same observations. "
                    "Higher in-sample R² does not establish forecasting value."
                ),
                table(
                    [
                        "Model",
                        "N",
                        "R²",
                        "Adjusted R²",
                        "Residual volatility",
                        "Condition number",
                        "Collinearity",
                    ],
                    [
                        [
                            m["name"],
                            m["sample_count"],
                            f"{m['r_squared']:.1%}",
                            f"{m['adjusted_r_squared']:.1%}",
                            f"{m['residual_volatility']:.4f}",
                            f"{m['condition_number']:.1f}",
                            "High" if m["high_collinearity"] else "Not flagged",
                        ]
                        for m in p["comparisons"]
                    ],
                ),
                html.Details(
                    [
                        html.Summary("How coefficients change across models"),
                        table(
                            ["Model", "Factor", "Coefficient", "VIF"],
                            [
                                [
                                    m["name"],
                                    c["label"],
                                    f"{c['estimate']:.5f}",
                                    "—"
                                    if i == 0
                                    else f"{m['variance_inflation_factors'][i - 1]:.2f}",
                                ]
                                for m in p["comparisons"]
                                for i, c in enumerate(m["coefficients"])
                            ],
                        ),
                    ]
                ),
            ]
        )
    if p.get("residualizations"):
        details.extend(
            [
                html.H3("Remaining variation"),
                html.P(
                    "Residualization removes the specified exposures within this sample. "
                    "It changes attribution, not the full model's fitted returns. "
                    "The remaining movement is not a causal shock. Controls are retained; "
                    "rolling transformations are refitted inside each window."
                ),
                table(
                    ["Factor", "Removed exposures", "Original variance remaining"],
                    [
                        [
                            r["factor_id"],
                            ", ".join(r["against"]),
                            f"{r['remaining_variance_fraction']:.1%}",
                        ]
                        for r in p["residualizations"]
                    ],
                ),
            ]
        )
    details.append(html.H3("Rolling stability"))
    details.append(
        html.P(
            f"Rolling window: {p['rolling_window']} observations; "
            f"successful fits: {len(p['rolling'])}; "
            f"skipped fits: {len(p['rolling_skipped_end_dates'])}."
        )
    )
    if p["rolling_skipped_end_dates"]:
        details.append(
            html.P("Unavailable window end dates: " + ", ".join(p["rolling_skipped_end_dates"]))
        )
    details.append(
        html.P(
            "Ranges and sign shares describe overlapping rolling windows, not confidence intervals "
            "or independent statistical tests. "
            "Small coefficients changing sign may be economically "
            "unimportant. Change the rolling window to assess sensitivity. Residualized control "
            "coefficients use a different attribution basis in each window."
        )
    )
    if p.get("stability"):
        details.append(
            table(
                [
                    "Factor",
                    "Windows",
                    "Minimum",
                    "Median",
                    "Maximum",
                    "Last valid estimate",
                    "Last valid end",
                    "Status",
                    "Positive share",
                ],
                [
                    [
                        v["label"],
                        v["window_count"],
                        f"{v['minimum']:.4f}",
                        f"{v['median']:.4f}",
                        f"{v['maximum']:.4f}",
                        f"{v['latest']:.4f}",
                        v["latest_end_date"],
                        "Current" if v["latest_is_current"] else "Stale",
                        f"{v['positive_fraction']:.1%}",
                    ]
                    for v in p["stability"]
                ],
            )
        )
    else:
        details.append(
            html.P("No valid rolling fits. Extend history or adjust the rolling window.")
        )
    details.append(
        html.Details(
            [
                html.Summary("Full diagnostics and metadata"),
                html.Pre(json.dumps(p, indent=2), style={"overflowX": "auto"}),
            ]
        )
    )
    return html.Div(details)


def field(label, control):
    return html.Div([html.Label(label), control], className="field")


def help_text(text):
    return html.P(text, className="help-text")


def settings_section(title, description, children, *, open_by_default=False):
    return html.Details(
        [
            html.Summary(
                [
                    html.Span(title),
                    html.Small("Hide" if open_by_default else "Show"),
                ]
            ),
            html.P(description, className="section-description"),
            html.Div(children, className="settings-section-body"),
        ],
        open=open_by_default,
        className="settings-section",
    )


def empty_state(title, description, steps):
    return html.Div(
        [
            html.Div("READY", className="empty-kicker"),
            html.H2(title),
            html.P(description),
            html.Div(
                [
                    html.Div([html.Strong(f"0{index}"), html.H3(step_title), html.P(step_body)])
                    for index, (step_title, step_body) in enumerate(steps, start=1)
                ],
                className="steps",
            ),
        ],
        className="empty",
    )


def workspace_header(kicker, title, description, badges):
    return html.Div(
        [
            html.Div(
                [
                    html.Div(kicker, className="eyebrow"),
                    html.H2(title),
                    html.P(description),
                ]
            ),
            html.Div([html.Span(badge, className="badge") for badge in badges], className="badges"),
        ],
        className="workspace-header",
    )


def single_stock_workspace():
    controls = html.Aside(
        [
            html.Div(
                [
                    html.Div("STUDY SETTINGS", className="eyebrow"),
                    html.H3("Configure the regression"),
                    help_text(
                        "Start with the core inputs. Open advanced sections only when needed."
                    ),
                ],
                className="control-heading",
            ),
            settings_section(
                "Core setup",
                "Select the target, source, factor family and analysis frequency.",
                [
                    field(
                        "Data source",
                        dcc.Dropdown(
                            searchable=False,
                            id="source",
                            options=[
                                {"label": "Synthetic demo · offline", "value": "demo"},
                                {"label": "Yahoo + French factors", "value": "yahoo"},
                                {
                                    "label": "Configured providers · Bloomberg ready",
                                    "value": "configured",
                                },
                            ],
                            value="demo",
                            clearable=False,
                        ),
                    ),
                    html.P(
                        data_source_guidance("demo"),
                        id="source-guidance",
                        className="source-guidance",
                    ),
                    field(
                        "Stock identifier",
                        dcc.Input(
                            id="stock",
                            value="US:AAPL",
                            placeholder="MARKET:SYMBOL",
                        ),
                    ),
                    help_text("Examples: BME:REP for Repsol, US:AAPL, XETRA:SAP."),
                    field(
                        "Starting factor set",
                        dcc.Dropdown(
                            searchable=False,
                            id="preset",
                            options=[
                                {"label": "Fama–French 5 + momentum", "value": "french"},
                                {"label": "US ETF proxies", "value": "us_etf"},
                                {
                                    "label": "MSCI Europe · configured provider",
                                    "value": "msci_europe",
                                },
                                {"label": "Custom factors", "value": "custom"},
                                *[
                                    {
                                        "label": pack.label
                                        + (" · configure YAML first" if not pack.enabled else ""),
                                        "value": "pack:" + pack.id,
                                        "disabled": not pack.enabled,
                                    }
                                    for pack in FACTOR_PACKS.values()
                                ],
                            ],
                            value="french",
                            clearable=False,
                        ),
                    ),
                    html.Div(id="pack-summary", className="help-text"),
                    field(
                        "Factors in this run",
                        dcc.Dropdown(searchable=False, id="selected", multi=True),
                    ),
                    help_text(
                        "Remove factors to test a smaller model. IDs remain visible for model comparisons."
                    ),
                    field(
                        "Frequency",
                        dcc.Dropdown(
                            searchable=False,
                            id="frequency",
                            options=[
                                {"label": "Monthly · recommended start", "value": "monthly"},
                                {"label": "Daily", "value": "daily"},
                            ],
                            value="monthly",
                            clearable=False,
                        ),
                    ),
                    field(
                        "Research factor region",
                        dcc.Dropdown(
                            searchable=False,
                            id="region",
                            options=["US", "Europe"],
                            value="US",
                            clearable=False,
                        ),
                    ),
                ],
                open_by_default=True,
            ),
            settings_section(
                "Study window",
                "Define the sample and the minimum evidence required for a fit.",
                [
                    html.Div(
                        [
                            field(
                                "Start date",
                                dcc.Input(
                                    id="start",
                                    value=str(TODAY - timedelta(days=3652)),
                                    type="text",
                                ),
                            ),
                            field(
                                "End date",
                                dcc.Input(
                                    id="end",
                                    value=str(TODAY - timedelta(days=1)),
                                    type="text",
                                ),
                            ),
                        ],
                        className="two-column-fields",
                    ),
                    field(
                        "Minimum observations",
                        dcc.Input(id="minimum", type="number", value=60),
                    ),
                    field(
                        "Rolling window · observations",
                        dcc.Input(id="rolling", type="number", value=60),
                    ),
                    field("HAC lags", dcc.Input(id="lags", type="number", value=3)),
                ],
                open_by_default=True,
            ),
            settings_section(
                "Custom factors",
                "Add funded benchmarks or spreads without changing backend code.",
                [
                    field(
                        "One factor per line",
                        dcc.Textarea(
                            id="extra",
                            value="",
                            placeholder="Pharma | US:IHE\nEnergy spread | US:XLE | US:IWB",
                            className="code-input short",
                        ),
                    ),
                    help_text(
                        "Optional additions: Label | long benchmark | optional short benchmark. "
                        "For reusable definitions, choose a YAML factor pack above."
                    ),
                    field(
                        "Advanced factor definitions · JSON",
                        dcc.Textarea(
                            id="advanced",
                            value="",
                            placeholder='[{"id": "market", "kind": "research", ...}]',
                            className="code-input",
                        ),
                    ),
                    help_text(
                        f"JSON replaces the selections above. Maximum {MAX_FACTOR_COUNT} factors, subject to sample size."
                    ),
                ],
            ),
            settings_section(
                "Model diagnostics",
                "Compare nested models or remove selected benchmark exposure for attribution.",
                [
                    field(
                        "Named factor subsets",
                        dcc.Textarea(
                            id="comparisons",
                            value="",
                            placeholder="Market | market_excess",
                            className="code-input short",
                        ),
                    ),
                    help_text("Format: model name | comma-separated factor IDs."),
                    field(
                        "Attribution method",
                        dcc.Dropdown(
                            id="attribution-mode",
                            searchable=False,
                            clearable=False,
                            value="original",
                            options=[
                                {
                                    "label": "Original factors · reference model",
                                    "value": "original",
                                },
                                {"label": "Remove selected exposures", "value": "controls"},
                                {
                                    "label": "Sequential attribution · ordered",
                                    "value": "sequential",
                                },
                            ],
                        ),
                    ),
                    html.Div(
                        [
                            field(
                                "Factors to residualize",
                                dcc.Dropdown(
                                    id="residual-targets", searchable=False, multi=True, value=[]
                                ),
                            ),
                            field(
                                "Remove exposure to these controls",
                                dcc.Dropdown(
                                    id="residual-controls", searchable=False, multi=True, value=[]
                                ),
                            ),
                            help_text(
                                "Controls stay in the model. Targets cannot also be controls. "
                                "Custom factors can remain correlated with one another."
                            ),
                        ],
                        id="control-attribution-settings",
                        style={"display": "none"},
                    ),
                    html.Div(
                        [
                            field(
                                "Factor priority · select in order",
                                dcc.Dropdown(
                                    id="sequential-order", searchable=False, multi=True, value=[]
                                ),
                            ),
                            html.Div(id="sequential-order-preview", className="help-text"),
                            help_text(
                                "To reorder, clear the selection and select every factor in your "
                                "preferred order. Earlier factors receive shared variation. "
                                "The stock is fitted jointly to the resulting factor basis."
                            ),
                        ],
                        id="sequential-attribution-settings",
                        style={"display": "none"},
                    ),
                    help_text(
                        "All methods retain the original model comparison. Attribution "
                        "changes interpretation, not the fitted returns or R². "
                        "Rolling transformations are refitted inside each window."
                    ),
                ],
            ),
            html.Button("Run single-stock analysis", id="run", n_clicks=0, className="primary"),
            html.P("Results refresh only when you run the study.", className="run-note"),
        ],
        className="control-panel",
    )
    results = html.Main(
        dcc.Loading(
            html.Div(
                id="result",
                children=empty_state(
                    "Explain a stock's historical returns",
                    "Estimate factor exposures, inspect collinearity and see how coefficients change through time.",
                    [
                        ("Choose", "Set a stock, data source and factor family."),
                        ("Refine", "Remove factors or add benchmarks relevant to your thesis."),
                        ("Interpret", "Review coverage first, then exposures and stability."),
                    ],
                ),
            ),
            type="circle",
        ),
        className="results-panel",
    )
    return html.Div(
        [
            workspace_header(
                "TIME-SERIES ATTRIBUTION",
                "Single-stock analysis",
                "Explain one stock's historical returns with research factors, traded benchmarks and custom spreads.",
                ["Regression", "Rolling exposure", "Collinearity"],
            ),
            html.Div([controls, results], className="workspace-grid"),
        ],
        className="workspace",
    )


def cross_section_workspace():
    controls = html.Aside(
        [
            html.Div(
                [
                    html.Div("STUDY SETTINGS", className="eyebrow"),
                    html.H3("Configure the signal test"),
                    help_text("Rank an editable universe and evaluate subsequent returns."),
                ],
                className="control-heading",
            ),
            settings_section(
                "Universe and ranking",
                "Choose the data source, membership history and neutralization groups.",
                [
                    field(
                        "Data source",
                        dcc.Dropdown(
                            searchable=False,
                            id="cs-source",
                            options=[
                                {"label": "Synthetic demo · offline", "value": "demo"},
                                {"label": "Yahoo adjusted returns", "value": "yahoo"},
                                {"label": "Configured backend provider", "value": "configured"},
                            ],
                            value="demo",
                            clearable=False,
                        ),
                    ),
                    html.P(
                        data_source_guidance("demo"),
                        id="cs-source-guidance",
                        className="source-guidance",
                    ),
                    field(
                        "Universe editor",
                        dcc.Textarea(
                            id="cs-universe",
                            value=(
                                "US:XOM | Energy | US\nUS:CVX | Energy | US\n"
                                "US:COP | Energy | US\nUS:SLB | Energy | US\nUS:EOG | Energy | US\n"
                                "US:LLY | Health Care | US\nUS:JNJ | Health Care | US\n"
                                "US:MRK | Health Care | US\nUS:ABBV | Health Care | US\n"
                                "US:AMGN | Health Care | US"
                            ),
                            className="code-input universe-input",
                        ),
                    ),
                    help_text(
                        "MARKET:SYMBOL | sector | country | optional member-from | optional member-to"
                    ),
                    field(
                        "Universe history",
                        dcc.Dropdown(
                            searchable=False,
                            id="cs-universe-mode",
                            options=[
                                {
                                    "label": "Current watchlist · exploratory",
                                    "value": "current_watchlist",
                                },
                                {"label": "Point-in-time membership", "value": "point_in_time"},
                            ],
                            value="current_watchlist",
                            clearable=False,
                        ),
                    ),
                    field(
                        "Ranking basis",
                        dcc.Dropdown(
                            searchable=False,
                            id="cs-grouping",
                            options=[
                                {"label": "Whole universe", "value": "overall"},
                                {"label": "Within sector", "value": "sector"},
                                {"label": "Within country", "value": "country"},
                                {"label": "Within sector and country", "value": "sector_country"},
                            ],
                            value="overall",
                            clearable=False,
                        ),
                    ),
                ],
                open_by_default=True,
            ),
            settings_section(
                "Evaluation window",
                "Use a chronological holdout to separate signal design from evaluation.",
                [
                    html.Div(
                        [
                            field(
                                "Start date",
                                dcc.Input(
                                    id="cs-start",
                                    value=str(TODAY - timedelta(days=365 * 6)),
                                    type="text",
                                ),
                            ),
                            field(
                                "End date",
                                dcc.Input(
                                    id="cs-end",
                                    value=str(TODAY - timedelta(days=1)),
                                    type="text",
                                ),
                            ),
                        ],
                        className="two-column-fields",
                    ),
                    field(
                        "Holdout start · optional",
                        dcc.Input(
                            id="cs-holdout",
                            value=str(TODAY - timedelta(days=365 * 2)),
                            type="text",
                        ),
                    ),
                ],
                open_by_default=True,
            ),
            settings_section(
                "Portfolio construction",
                "Control portfolio breadth, group eligibility and illustrative trading cost.",
                [
                    field("Quantiles", dcc.Input(id="cs-quantiles", value=5, type="number")),
                    field(
                        "Minimum total assets",
                        dcc.Input(id="cs-minimum-assets", value=10, type="number"),
                    ),
                    field(
                        "Minimum assets per group",
                        dcc.Input(id="cs-minimum-group", value=5, type="number"),
                    ),
                    field(
                        "One-way cost · bps per unit turnover",
                        dcc.Input(id="cs-cost", value=10, type="number"),
                    ),
                    help_text(
                        "Grouped runs need enough eligible stocks in each group to form every quantile."
                    ),
                ],
            ),
            html.Button(
                "Run cross-sectional analysis",
                id="cs-run",
                n_clicks=0,
                className="primary",
            ),
            html.P("Results refresh only when you run the study.", className="run-note"),
        ],
        className="control-panel",
    )
    results = html.Main(
        dcc.Loading(
            html.Div(
                id="cs-result",
                children=empty_state(
                    "Test whether a signal ranks future returns",
                    "Evaluate 12–1 momentum with rank IC, quantile returns, turnover, costs and a chronological holdout.",
                    [
                        (
                            "Define",
                            "Enter an investable universe with sector and country metadata.",
                        ),
                        ("Rank", "Choose whole-universe or group-neutral ranking."),
                        ("Validate", "Compare development and holdout behavior after costs."),
                    ],
                ),
            ),
            type="circle",
        ),
        className="results-panel",
    )
    return html.Div(
        [
            workspace_header(
                "SIGNAL VALIDATION",
                "Cross-sectional analysis",
                "Test whether a ranking signal separates subsequent returns across an editable stock universe.",
                ["Rank IC", "Quantiles", "Holdout"],
            ),
            html.Div([controls, results], className="workspace-grid"),
        ],
        className="workspace",
    )


logging.getLogger("werkzeug").setLevel(logging.ERROR)
app = Dash(__name__, title="Factor Playground")
app.layout = html.Div(
    [
        html.Header(
            [
                html.Div(
                    [
                        html.Div("TRADE RESEARCH", className="eyebrow"),
                        html.H1("Factor Playground"),
                        html.P(
                            "A local research console for transparent factor attribution and signal validation."
                        ),
                    ]
                ),
                html.Div("LOCAL · ANALYTICS ONLY", className="lab"),
            ]
        ),
        dcc.Tabs(
            id="workspace-tabs",
            value="single-stock",
            className="workspace-tabs",
            parent_className="workspace-tabs-wrap",
            children=[
                dcc.Tab(
                    label="Single-stock attribution",
                    value="single-stock",
                    className="workspace-tab",
                    selected_className="workspace-tab--selected",
                    children=single_stock_workspace(),
                ),
                dcc.Tab(
                    label="Cross-sectional signals",
                    value="cross-sectional",
                    className="workspace-tab",
                    selected_className="workspace-tab--selected",
                    children=cross_section_workspace(),
                ),
            ],
        ),
        html.Footer(
            "Local research interface · calculations and provider normalization run in TradeAgent"
        ),
    ],
    className="shell",
)

CONTROL_IDS = (
    "stock",
    "preset",
    "selected",
    "extra",
    "advanced",
    "frequency",
    "region",
    "start",
    "end",
    "minimum",
    "rolling",
    "lags",
    "comparisons",
    "attribution-mode",
    "residual-targets",
    "residual-controls",
    "sequential-order",
)


@app.callback(Output("selected", "options"), Output("selected", "value"), Input("preset", "value"))
def select_preset(preset):
    factors = available_factors(preset)
    return (
        [{"label": f"{f.label} [{f.id}]", "value": f.id} for f in factors],
        [f.id for f in factors],
    )


@app.callback(
    Output("residual-targets", "options"),
    Output("residual-controls", "options"),
    Output("sequential-order", "options"),
    Output("residual-targets", "value"),
    Output("residual-controls", "value"),
    Output("sequential-order", "value"),
    Input("preset", "value"),
    Input("selected", "value"),
    Input("extra", "value"),
    Input("advanced", "value"),
    State("residual-targets", "value"),
    State("residual-controls", "value"),
    State("sequential-order", "value"),
)
def attribution_choices(preset, selected, extra, advanced, targets, controls, order):
    try:
        factors = editor_factors(preset, selected, extra, advanced)
        # Validate editable definitions before using their IDs in the selectors.
        factors = [FactorSpec.model_validate(f) for f in factors]
        ids = list(dict.fromkeys(f.id for f in factors))
        options = [{"label": f"{f.label} [{f.id}]", "value": f.id} for f in factors]
    except (ValueError, TypeError):
        ids, options = [], []
    kept = list(dict.fromkeys(v for v in (order or []) if v in ids))
    return (
        options,
        options,
        options,
        [v for v in (targets or []) if v in ids],
        [v for v in (controls or []) if v in ids],
        kept + [v for v in ids if v not in kept],
    )


@app.callback(
    Output("control-attribution-settings", "style"),
    Output("sequential-attribution-settings", "style"),
    Input("attribution-mode", "value"),
)
def show_attribution_settings(mode):
    return (
        {"display": "block" if mode == "controls" else "none"},
        {"display": "block" if mode == "sequential" else "none"},
    )


@app.callback(Output("sequential-order-preview", "children"), Input("sequential-order", "value"))
def show_order(order):
    return " → ".join(f"{i + 1}. {key}" for i, key in enumerate(order or []))


@app.callback(Output("source-guidance", "children"), Input("source", "value"))
def explain_source(source):
    return data_source_guidance(source)


@app.callback(Output("cs-source-guidance", "children"), Input("cs-source", "value"))
def explain_cross_section_source(source):
    guidance = data_source_guidance(source)
    if source == "yahoo":
        return guidance.replace(
            " The Fama–French preset separately downloads official research files.", ""
        )
    return guidance


@app.callback(Output("pack-summary", "children"), Input("preset", "value"))
def describe_pack(preset):
    pack = pack_for(preset)
    if not pack:
        return "YAML packs are loaded from the local factor_packs folder at startup."
    items = [html.P(pack.description), html.P("Sources: " + ", ".join(pack.sources))]
    if pack.level_factors:
        items.append(html.P(f"Study currency: {pack.currency} · Calendar: {pack.calendar}"))
        items.append(
            html.Ul(
                [
                    html.Li(
                        f"{f.label}: {f.transform} of "
                        + " + ".join(f"{weight:g} × {key}" for key, weight in f.legs.items())
                    )
                    for f in pack.level_factors
                ]
            )
        )
        items.append(
            html.P(
                "The stock remains in simple total returns. Factor transformations apply "
                "at the selected frequency. Bloomberg mappings must be verified locally."
            )
        )
    return items


@app.callback(
    Output("minimum", "value"),
    Output("rolling", "value"),
    Output("lags", "value"),
    Input("frequency", "value"),
)
def frequency_defaults(frequency):
    params = FactorRegressionParameters(frequency=frequency)
    return params.minimum_observations, params.rolling_window, params.hac_lags


@app.callback(
    Output("result", "children"),
    Input("run", "n_clicks"),
    State("source", "value"),
    *[State(k, "value") for k in CONTROL_IDS],
    prevent_initial_call=True,
    running=[(Output("run", "disabled"), True, False)],
)
def update_result(_clicks, source, *controls):
    try:
        pack = pack_for(controls[1])
        if pack and (
            not pack.enabled
            or source not in pack.sources
            or (pack.level_factors and source != "configured")
        ):
            return html.P(
                "This YAML pack requires an enabled configuration and source: "
                + ", ".join(pack.sources)
            )
        if pack and (controls[4] or "").strip():
            return html.P("Clear the JSON override to run a YAML pack, or choose Custom factors.")
        request = build_request(*controls)
    except (ValueError, ValidationError):
        return html.P(
            "Invalid settings. Check factor syntax, dates, unique IDs and sample size; "
            "minimum and rolling windows must exceed the factor count plus HAC lags plus one. "
            "Comparison IDs must be selected factors; controls cannot be targets. Sequential order "
            "must include every selected factor exactly once."
        )
    try:
        # Validation is intentionally separate from provider execution.
        del request
        return render_result(run_study(source, *controls), source)
    except Exception as error:
        reference = uuid.uuid4().hex
        logging.getLogger(__name__).error(
            "factor study stage=execution code=%s reference=%s", type(error).__name__, reference
        )
        return html.P(
            f"Study failed. Stage: execution · Code: {type(error).__name__} · "
            f"Reference: {reference}. Check provider configuration. Raw payloads are not logged."
        )


CS_CONTROL_IDS = (
    "cs-universe",
    "cs-universe-mode",
    "cs-grouping",
    "cs-start",
    "cs-end",
    "cs-quantiles",
    "cs-minimum-assets",
    "cs-minimum-group",
    "cs-cost",
    "cs-holdout",
)


@app.callback(
    Output("cs-result", "children"),
    Input("cs-run", "n_clicks"),
    State("cs-source", "value"),
    *[State(control, "value") for control in CS_CONTROL_IDS],
    prevent_initial_call=True,
    running=[(Output("cs-run", "disabled"), True, False)],
)
def update_cross_section_result(_clicks, source, *controls):
    try:
        request = build_cross_section_request(*controls)
    except (ValueError, ValidationError):
        return html.P(
            "Invalid settings. Check universe rows, dates, group metadata, quantiles and "
            "minimum asset counts. Point-in-time mode requires membership-from dates."
        )
    try:
        del request
        return render_cross_section_result(run_cross_section_study(source, *controls), source)
    except Exception as error:
        reference = uuid.uuid4().hex
        logging.getLogger(__name__).error(
            "cross-sectional stage=execution code=%s reference=%s",
            type(error).__name__,
            reference,
        )
        return html.P(
            f"Study failed. Stage: execution · Code: {type(error).__name__} · "
            f"Reference: {reference}. Check provider configuration. Raw payloads are not logged."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    if args.open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
