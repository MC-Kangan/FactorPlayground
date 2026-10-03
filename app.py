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

import plotly.graph_objects as go
from dash import Dash, Input, Output, ctx, dcc, html
from demo_data import demo_request
from pydantic import ValidationError

from trade_research.domain import AnalysisRequest, InstrumentId
from trade_research.domain.factors import preset_factors
from trade_research.domain.models import MAX_FACTOR_COUNT
from trade_research.engine import ResearchEngine
from trade_research.settings import Settings
from trade_research.skills.factor_regression import FactorRegressionParameters

WARNINGS = {
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
    residualizations,
):
    factors = [
        f.model_dump(mode="json") for f in preset_factors(preset) if f.id in (selected or [])
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
        factors.append(
            dict(
                id=f"extra_{i}",
                label=parts[0],
                kind="spread" if len(parts) == 3 else "asset_return",
                instrument=instrument(parts[1]).model_dump(),
                short_instrument=instrument(parts[2]).model_dump() if len(parts) == 3 else None,
            )
        )
    if (advanced or "").strip():
        factors = json.loads(advanced)
    params = FactorRegressionParameters.model_validate(
        dict(
            preset=preset,
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
                {"factor_id": target, "against": ids}
                for target, ids in parse_lines(residualizations)
            ],
        )
    ).resolve_dates(TODAY)
    return AnalysisRequest(
        instrument=instrument(stock),
        analysts=("factor-regression",),
        skill_parameters={"factor-regression": params.model_dump(mode="json")},
    )


def run_study(source, *controls):
    request = build_request(*controls)
    if source == "demo":
        request = demo_request(request)
        settings = Settings()
    elif source == "yahoo":
        settings = Settings(price_provider="yahoo")
    elif source == "configured":
        settings = Settings.from_environment(os.environ)
    else:
        raise ValueError("Choose a data source.")
    return asyncio.run(ResearchEngine.from_settings(settings).analyze(request)).model_dump(
        mode="json"
    )


def table(headings, rows):
    return html.Div(
        html.Table(
            [
                html.Thead(html.Tr([html.Th(v) for v in headings])),
                html.Tbody([html.Tr([html.Td(v) for v in row]) for row in rows]),
            ]
        ),
        style={"overflowX": "auto"},
    )


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
            f"R²: {p['r_squared']:.1%} · Adjusted R²: {p['adjusted_r_squared']:.1%} · "
            f"Covariance: {p['covariance']}"
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
    chart.update_layout(
        title="Standardized effects (comparable across units)", template="plotly_white"
    )
    details.append(dcc.Graph(figure=chart))
    if p.get("rolling"):
        rolling = go.Figure()
        for i, c in enumerate(coefficients[1:], 1):
            rolling.add_scatter(
                x=[v["end_date"] for v in p["rolling"]],
                y=[v["estimates"][i] for v in p["rolling"]],
                name=c["label"],
            )
        rolling.update_layout(title="Rolling exposures (original units)", template="plotly_white")
        details.append(dcc.Graph(figure=rolling))
    if p.get("comparisons"):
        details.extend(
            [
                html.H3("Model comparison"),
                html.P(
                    "All models below use original factors and exactly the same observations. "
                    "Higher in-sample R² does not establish forecasting value."
                ),
                table(
                    ["Model", "N", "R²", "Adjusted R²", "Residual volatility", "Condition number"],
                    [
                        [
                            m["name"],
                            m["sample_count"],
                            f"{m['r_squared']:.1%}",
                            f"{m['adjusted_r_squared']:.1%}",
                            f"{m['residual_volatility']:.4f}",
                            f"{m['condition_number']:.1f}",
                        ]
                        for m in p["comparisons"]
                    ],
                ),
                html.Details(
                    [
                        html.Summary("How coefficients change across models"),
                        table(
                            ["Model", "Factor", "Coefficient"],
                            [
                                [m["name"], c["label"], f"{c['estimate']:.5f}"]
                                for m in p["comparisons"]
                                for c in m["coefficients"]
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
                ["Factor", "Windows", "Minimum", "Median", "Maximum", "Latest", "Positive share"],
                [
                    [
                        v["label"],
                        v["window_count"],
                        f"{v['minimum']:.4f}",
                        f"{v['median']:.4f}",
                        f"{v['maximum']:.4f}",
                        f"{v['latest']:.4f}",
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


logging.getLogger("werkzeug").setLevel(logging.ERROR)
app = Dash(__name__, title="Factor Playground")
app.layout = html.Div(
    [
        html.H1("Factor Playground"),
        html.P(
            "Choose a stock, select factors and run a historical explanatory study. "
            "Start with the synthetic demo; monthly data is a useful starting point for Europe."
        ),
        html.Div(
            [
                field(
                    "Data source",
                    dcc.Dropdown(
                        id="source",
                        options=[
                            {"label": "Synthetic demo (offline)", "value": "demo"},
                            {"label": "Yahoo + French research factors", "value": "yahoo"},
                            {
                                "label": "Configured backend providers (including Bloomberg)",
                                "value": "configured",
                            },
                        ],
                        value="demo",
                        clearable=False,
                    ),
                ),
                field(
                    "Stock: MARKET:SYMBOL",
                    dcc.Input(id="stock", value="US:AAPL", style={"width": "100%"}),
                ),
                field(
                    "Starting factor set",
                    dcc.Dropdown(
                        id="preset",
                        options=[
                            {"label": "Fama–French 5 + momentum", "value": "french"},
                            {"label": "US ETF proxies", "value": "us_etf"},
                            {"label": "Custom factors", "value": "custom"},
                        ],
                        value="french",
                        clearable=False,
                    ),
                ),
                field("Selected factors (remove any)", dcc.Dropdown(id="selected", multi=True)),
                field(
                    "Additional factors: one per line",
                    dcc.Textarea(
                        id="extra",
                        value="",
                        placeholder="Pharma | US:IHE\nEnergy spread | US:XLE | US:IWB",
                        style={"width": "100%", "height": "90px"},
                    ),
                ),
                html.P(
                    "Format: Label | long benchmark | optional short benchmark. "
                    "Use total-return instruments. "
                    "ETF sector exposure is not a pure industry shock."
                ),
                field(
                    "Frequency",
                    dcc.Dropdown(
                        id="frequency",
                        options=["daily", "monthly"],
                        value="monthly",
                        clearable=False,
                    ),
                ),
                field(
                    "Research factor region",
                    dcc.Dropdown(
                        id="region", options=["US", "Europe"], value="US", clearable=False
                    ),
                ),
                field(
                    "Start date",
                    dcc.Input(id="start", value=str(TODAY - timedelta(days=3652)), type="text"),
                ),
                field(
                    "End date (completed history)",
                    dcc.Input(id="end", value=str(TODAY - timedelta(days=1)), type="text"),
                ),
                field("Minimum observations", dcc.Input(id="minimum", type="number", value=60)),
                field(
                    "Rolling window (observations)",
                    dcc.Input(id="rolling", type="number", value=60),
                ),
                field("HAC lags (observations)", dcc.Input(id="lags", type="number", value=3)),
                html.Details(
                    [
                        html.Summary("Advanced factor definitions"),
                        html.P(
                            "Optional JSON replaces the selections above. "
                            "Supports asset_return, spread and "
                            "research kinds, per-leg calendars, labels and IDs. "
                            f"Up to {MAX_FACTOR_COUNT} factors, subject to sample size. "
                            "Custom research inputs need a provider "
                            "implementing the normalized panel contract."
                        ),
                        dcc.Textarea(
                            id="advanced", value="", style={"width": "100%", "height": "140px"}
                        ),
                    ]
                ),
                html.H3("Compare models"),
                html.P(
                    "One model per line: name | factor IDs. Select proper subsets of the "
                    "full model. Preset IDs appear in brackets; additional factors use "
                    "extra_0, extra_1, etc. based on their line position."
                ),
                field(
                    "Named factor subsets",
                    dcc.Textarea(
                        id="comparisons",
                        value="",
                        placeholder="Market | market_excess",
                        style={"width": "100%", "height": "85px"},
                    ),
                ),
                html.Details(
                    [
                        html.Summary("Optional residualization"),
                        html.P(
                            "One factor per line: target ID | control IDs. Controls must remain "
                            "in the full model and cannot themselves be residualized. "
                            "This does not make all factors mutually independent."
                        ),
                        field(
                            "Remove selected benchmark exposures",
                            dcc.Textarea(
                                id="residualizations",
                                value="",
                                placeholder="extra_0 | market_excess,hml",
                                style={"width": "100%", "height": "85px"},
                            ),
                        ),
                    ]
                ),
                html.Button("Run factor analysis", id="run", n_clicks=0, className="primary"),
            ],
            style={"padding": "24px", "background": "#f5f7fa", "borderRadius": "12px"},
        ),
        dcc.Loading(html.Div(id="result", style={"marginTop": "24px"})),
    ],
    style={
        "maxWidth": "1100px",
        "margin": "30px auto",
        "padding": "24px",
        "fontFamily": "system-ui, sans-serif",
        "color": "#142a43",
    },
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
    "residualizations",
)


@app.callback(Output("selected", "options"), Output("selected", "value"), Input("preset", "value"))
def select_preset(preset):
    factors = preset_factors(preset)
    return (
        [{"label": f"{f.label} [{f.id}]", "value": f.id} for f in factors],
        [f.id for f in factors],
    )


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
    Input("source", "value"),
    *[Input(k, "value") for k in CONTROL_IDS],
    prevent_initial_call=True,
    running=[(Output("run", "disabled"), True, False)],
)
def update_result(_clicks, source, *controls):
    if ctx.triggered_id != "run":
        return html.P("Settings changed. Click Run factor analysis to refresh the results.")
    try:
        request = build_request(*controls)
    except (ValueError, ValidationError):
        return html.P(
            "Invalid settings. Check factor syntax, dates, unique IDs and sample size; "
            "minimum and rolling windows must exceed the factor count plus HAC lags plus one. "
            "Comparison IDs must be selected factors; residualization controls cannot be targets."
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    if args.open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
