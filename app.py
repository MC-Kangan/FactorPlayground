"""Local factor playground; all statistical calculations belong to TradeAgent."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import threading
import uuid
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path

import plotly.graph_objects as go
import numpy as np
from dash import ALL, Dash, Input, Output, State, dcc, html
from demo_data import cross_section_demo_request, demo_request
from watchlist_ui import workspace as watchlist_workspace, register_callbacks as register_watchlist
from pydantic import ValidationError

from trade_research.domain import AnalysisRequest, InstrumentId
from trade_research.domain.factors import preset_factors
from trade_research.domain.models import MAX_FACTOR_COUNT, FactorSpec
from trade_research.engine import ResearchEngine
from trade_research.factor_packs import load_factor_packs, merge_factor_packs
from trade_research.providers import (
    BloombergReturnMapping,
    parse_bloomberg_equity_identifier,
)
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
        "Uses the provider configured for TradeAgent. With Bloomberg, equities use adjusted "
        "PX_LAST and FX uses Bloomberg currency pairs. MSCI and commodity indexes require "
        "verified identifiers, fields, units and entitlements; there is no public-data fallback."
    ),
}
PACK_DIRECTORY = Path(
    os.environ.get("FACTOR_PLAYGROUND_PACK_DIR", str(Path(__file__).parent / "factor_packs"))
)
FACTOR_PACKS = load_factor_packs(PACK_DIRECTORY)


def selected_pack_values(presets):
    if not isinstance(presets, list) or not presets:
        raise ValueError("Choose at least one factor pack")
    return presets


def available_factors(presets):
    factors = []
    for preset in selected_pack_values(presets):
        pack = pack_for(preset)
        factors.extend(pack.selected_factors() if pack else preset_factors(preset))
    ids = [factor.id for factor in factors]
    if len(ids) != len(set(ids)):
        raise ValueError("Selected packs contain duplicate factor IDs; deselect one duplicate")
    return tuple(factors)


def pack_for(preset):
    if not preset.startswith("pack:"):
        return None
    if preset[5:] not in FACTOR_PACKS:
        raise ValueError("Unknown YAML factor pack")
    return FACTOR_PACKS[preset[5:]]


def packs_for(presets):
    return tuple(
        pack for value in selected_pack_values(presets) if (pack := pack_for(value)) is not None
    )


def bloomberg_is_configured():
    try:
        return Settings.from_environment(os.environ).price_provider == "bloomberg"
    except Exception:
        return False


BLOOMBERG_DEFAULT = bloomberg_is_configured()
DEFAULT_SOURCE = "configured" if BLOOMBERG_DEFAULT else "demo"
DEFAULT_PACKS = [
    "pack:" + pack.id
    for pack in FACTOR_PACKS.values()
    if pack.enabled and "configured" in pack.sources
][:1] or (["msci_europe"] if BLOOMBERG_DEFAULT else ["french"])


def data_source_guidance(source):
    return SOURCE_GUIDANCE.get(source, "Choose a supported data source.")


def instrument(text):
    return parse_bloomberg_equity_identifier(text or "").instrument


def entered_bloomberg_identities(stock, extra=""):
    values = [stock]
    for line in (extra or "").splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) not in (2, 3):
            raise ValueError(
                "Extra factors use Label | Bloomberg equity | optional short Bloomberg equity."
            )
        values.extend(parts[1:])
    identities = [parse_bloomberg_equity_identifier(value or "") for value in values]
    by_instrument = {}
    for identity in identities:
        previous = by_instrument.get(identity.instrument)
        if previous is not None and previous.security != identity.security:
            raise ValueError("One instrument cannot use two Bloomberg identifiers in one study")
        by_instrument[identity.instrument] = identity
    return tuple(by_instrument.values())


def add_entered_bloomberg_mappings(settings, identities):
    entered = {identity.instrument: identity for identity in identities}
    mappings = list(settings.bloomberg_return_mappings)
    configured = {mapping.instrument for mapping in mappings}
    mappings.extend(
        BloombergReturnMapping(
            instrument=identity.instrument,
            security=identity.security,
            field="PX_LAST",
            currency=identity.currency,
            return_basis="adjusted_close_return",
        )
        for identity in entered.values()
        if identity.instrument not in configured
    )
    return Settings.model_validate(settings.model_dump() | {"bloomberg_return_mappings": mappings})


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


def editor_factors(presets, selected, extra, advanced):
    factors = [
        f.model_dump(mode="json") for f in available_factors(presets) if f.id in (selected or [])
    ]
    # Each line adds a funded benchmark; two legs make a self-financing spread.
    for i, line in enumerate((extra or "").splitlines()):
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) not in (2, 3):
            raise ValueError(
                "Extra factors use Label | Bloomberg equity | optional short Bloomberg equity."
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
    presets,
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
    factors = editor_factors(presets, selected, extra, advanced)
    if attribution_mode not in {"original", "controls", "sequential"}:
        raise ValueError("Choose a supported attribution mode")
    if attribution_mode == "controls" and (not residual_targets or not residual_controls):
        raise ValueError("Choose targets and controls for residualization")
    if attribution_mode == "sequential" and not sequential_order:
        raise ValueError("Choose an explicit factor order")
    params = FactorRegressionParameters.model_validate(
        dict(
            preset=(
                presets[0]
                if len(selected_pack_values(presets)) == 1 and not pack_for(presets[0])
                else "custom"
            ),
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


def study_context(source, *controls):
    request = build_request(*controls)
    packs = packs_for(controls[1])
    selected = {f["id"] for f in request.skill_parameters["factor-regression"]["factors"]}
    selected_packs = []
    for pack in packs:
        if not pack.enabled or source not in pack.sources:
            raise ValueError("Choose a supported source and a configured, enabled factor pack.")
        if (controls[4] or "").strip():
            raise ValueError("Use Custom factors for JSON overrides, not a YAML pack.")
        pack_ids = {factor.id for factor in pack.selected_factors()}
        kept = selected & pack_ids
        if kept:
            selected_packs.append(pack.subset(kept))
        if any(factor.id in selected for factor in pack.level_factors) and source != "configured":
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
        if settings.price_provider == "bloomberg":
            identities = entered_bloomberg_identities(
                controls[0], "" if (controls[4] or "").strip() else controls[3]
            )
            settings = add_entered_bloomberg_mappings(settings, identities)
        combined = None
        if selected_packs:
            mapping = next(
                (
                    item
                    for item in settings.bloomberg_return_mappings
                    if item.instrument == request.instrument
                ),
                None,
            )
            fallback_currency = next(
                (pack.currency for pack in selected_packs if pack.currency is not None), "USD"
            )
            combined = merge_factor_packs(
                tuple(selected_packs),
                study_currency=mapping.currency if mapping is not None else fallback_currency,
            )
        settings = Settings.model_validate(
            settings.model_dump()
            | {
                "bloomberg_factor_pack": (
                    combined if combined is not None and combined.level_factors else None
                )
            }
        )
    return request, settings


def run_study(source, *controls):
    request, settings = study_context(source, *controls)
    return asyncio.run(ResearchEngine.from_settings(settings).analyze(request)).model_dump(
        mode="json"
    )


def run_preview(source, *controls):
    request, settings = study_context(source, *controls)
    preview = (
        ResearchEngine.from_settings(settings).factor_study_preview(request).model_dump(mode="json")
    )
    requested_instruments = {
        (item["instrument"]["market"], item["instrument"]["symbol"]) for item in preview["inputs"]
    }
    preview["configured_mappings"] = [
        mapping.model_dump(mode="json")
        for mapping in settings.bloomberg_return_mappings
        if (mapping.instrument.market, mapping.instrument.symbol) in requested_instruments
    ]
    preview["pack_inputs"] = (
        {
            key: item.model_dump(mode="json")
            for key, item in settings.bloomberg_factor_pack.inputs.items()
        }
        if settings.bloomberg_factor_pack is not None
        else {}
    )
    return preview


def parse_universe(text):
    """Parse Bloomberg equity | sector | country | optional from | optional to."""
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
        if settings.price_provider == "bloomberg":
            identities = tuple(
                parse_bloomberg_equity_identifier(line.split("|", 1)[0].strip())
                for line in controls[0].splitlines()
                if line.strip()
            )
            settings = add_entered_bloomberg_mappings(settings, identities)
    else:
        raise ValueError("Choose a data source")
    return asyncio.run(ResearchEngine.from_settings(settings).analyze(request)).model_dump(
        mode="json"
    )


def _percent(value):
    return "—" if value is None else f"{100 * value:.2f}%"


def _number(value):
    return "—" if value is None else f"{value:.3f}"


def _p_value(value):
    if value is None:
        return "—"
    return "<0.001" if value < 0.001 else f"{value:.3f}"


def scenario_impact(moves, identities):
    if len(moves or []) != len(identities or []):
        raise ValueError("Scenario inputs do not match the fitted factors")
    rows = []
    total_bps = 0.0
    for raw_move, identity in zip(moves, identities, strict=True):
        move = 0.0 if raw_move in (None, "") else float(raw_move)
        beta = float(identity["beta"])
        if not math.isfinite(move) or not math.isfinite(beta):
            raise ValueError("Scenario values must be finite")
        contribution_bps = beta * move * 10_000
        total_bps += contribution_bps
        rows.append(
            {
                "term": identity["term"],
                "label": identity["label"],
                "move": move,
                "beta": beta,
                "contribution_bps": contribution_bps,
            }
        )
    return rows, total_bps


def scenario_inputs(presentation):
    units = {
        coefficient["term"]: coefficient["unit"] for coefficient in presentation["coefficients"][1:]
    }
    return [
        {
            "term": coefficient["term"],
            "label": coefficient["label"],
            "beta": coefficient["estimate"],
            "unit": units[coefficient["term"]],
        }
        for coefficient in presentation["comparisons"][0]["coefficients"][1:]
    ]


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


def coverage_table(coverage):
    return table(
        ["Input", "Expected", "Available", "Missing/invalid", "FX losses", "Alignment losses"],
        [
            [
                item[key]
                for key in (
                    "role",
                    "expected_periods",
                    "available_periods",
                    "invalid_or_missing_periods",
                    "fx_endpoint_losses",
                    "alignment_losses",
                )
            ]
            for item in coverage
        ],
    )


def render_preview(preview):
    columns = preview["columns"]
    rows = preview["rows"]
    if not rows:
        return html.P("No aligned observations are available for inspection.")
    values = np.asarray([row["values"] for row in rows], dtype=float)
    means = values.mean(axis=0)
    scales = values.std(axis=0, ddof=1)
    standardized = np.divide(
        values - means,
        scales,
        out=np.zeros_like(values),
        where=scales > 1e-14,
    )
    figure = go.Figure()
    for index, column in enumerate(columns):
        unit = column["unit"]
        currency = column.get("currency") or "study currency"
        figure.add_scatter(
            x=[row["end_date"] for row in rows],
            y=standardized[:, index],
            name=column["label"],
            mode="lines",
            customdata=values[:, index],
            hovertemplate=(
                "%{fullData.name}<br>Date: %{x}<br>Model value: %{customdata:.6g}<br>"
                f"Unit: {unit}<br>Currency: {currency}<extra></extra>"
            ),
        )
    finish_figure(
        figure,
        "Aligned model inputs · standardized for visual comparison",
        yaxis_title="Standard deviations",
    )
    drill_down = [
        html.Summary("Input definitions and exact rows"),
        html.P(
            "Use this only when a visual alignment check is not enough. It shows normalized "
            "model inputs and source metadata; licensed vendor payloads are not persisted."
        ),
        html.H4("Input definitions"),
        table(
            ["Role", "Instrument", "Source", "Vendor field", "Return basis", "Currency", "Intervals"],
            [
                [
                    item["role"],
                    f"{item['instrument']['market']}:{item['instrument']['symbol']}",
                    item["source"],
                    item["vendor_field"],
                    item["return_basis"],
                    item["currency"],
                    item["interval_count"],
                ]
                for item in preview["inputs"]
            ],
        ),
        html.H4("Research sources and alignment"),
        table(
            ["Source", "Currency convention", "Observations", "Reference"],
            [
                [d["source"], d["currency"], d["observation_count"], d["reference"]]
                for d in preview["datasets"]
            ],
        ),
        table(
            ["Input", "Available", "Missing", "Alignment losses"],
            [
                [
                    c["role"],
                    c["available_periods"],
                    c["invalid_or_missing_periods"],
                    c["alignment_losses"],
                ]
                for c in preview["coverage"]
            ],
        ),
    ]
    configured_mappings = preview.get("configured_mappings", [])
    if configured_mappings:
        drill_down.extend(
            [
                html.H4("Configured Bloomberg return mappings"),
                table(
                    ["Instrument", "Bloomberg security", "Field", "Currency", "Basis"],
                    [
                        [
                            f"{item['instrument']['market']}:{item['instrument']['symbol']}",
                            item["security"],
                            item["field"],
                            item["currency"],
                            item["return_basis"],
                        ]
                        for item in configured_mappings
                    ],
                ),
            ]
        )
    pack_inputs = preview.get("pack_inputs", {})
    if pack_inputs:
        drill_down.extend(
            [
                html.H4("Selected Bloomberg pack inputs"),
                table(
                    ["Input", "Bloomberg security", "Field", "Currency", "Unit", "Scale"],
                    [
                        [key, item["security"], item["field"], item["currency"], item["unit"], item["scale"]]
                        for key, item in pack_inputs.items()
                    ],
                ),
            ]
        )
    drill_down.extend(
        [
            html.H4("Exact aligned model rows"),
            html.P(f"Showing the latest {min(len(rows), 120)} of {len(rows)} observations."),
            table(
                ["Start", "End", *(f"{c['label']} · {c['unit']}" for c in columns)],
                [
                    [row["start_date"], row["end_date"], *(f"{value:.8g}" for value in row["values"])]
                    for row in rows[-120:]
                ],
            ),
        ]
    )
    return html.Div(
        [
            html.H3("Aligned model inputs"),
            html.P(
                f"{len(rows)} shared observations from {rows[0]['start_date']} to "
                f"{rows[-1]['end_date']}. Hover to inspect the exact model value, unit and "
                "currency for every series on the same date. Lines are standardized only so "
                "differently scaled inputs can be compared on one chart."
            ),
            graph(figure),
            html.Details(drill_down),
        ]
    )


def combined_correlation_matrix(presentation):
    relationships = presentation["relationships"]
    labels = ["Stock", *(row["label"] for row in relationships)]
    stock = [float("nan") if row["pearson"] is None else row["pearson"] for row in relationships]
    factors = presentation["original_factor_correlations"]
    matrix = [[1.0, *stock]]
    matrix.extend([[stock[index], *row] for index, row in enumerate(factors)])
    return labels, matrix


def collinearity_recommendations(presentation, *, correlation_limit=0.8, vif_limit=10.0):
    relationships = {row["term"]: row for row in presentation["relationships"]}
    full = presentation["comparisons"][0]
    factor_ids = full["factor_ids"]
    labels = [relationships[term]["label"] for term in factor_ids]
    matrix = presentation["original_factor_correlations"]
    vifs = full["variance_inflation_factors"]
    suggestions = []
    for left in range(len(factor_ids)):
        for right in range(left + 1, len(factor_ids)):
            correlation_value = matrix[left][right]
            if (
                abs(correlation_value) < correlation_limit
                or max(vifs[left], vifs[right]) < vif_limit
            ):
                continue
            left_increment = relationships[factor_ids[left]]["incremental_r_squared"]
            right_increment = relationships[factor_ids[right]]["incremental_r_squared"]
            remove, keep = (right, left) if right_increment <= left_increment else (left, right)
            suggestions.append(
                f"Review removing {labels[remove]} before {labels[keep]}: pair correlation "
                f"is {correlation_value:.2f}, VIFs are {vifs[left]:.1f}/{vifs[right]:.1f}, and "
                "the suggested removal contributes less incremental R². Confirm the economic "
                "role and rerun the named-model comparison before excluding it."
            )
    return suggestions


def render_relationships(p):
    rows = p.get("relationships", [])
    if not rows:
        return []
    labels = [r["label"] for r in rows]
    original_joint = {
        coefficient["term"]: coefficient["estimate"]
        for coefficient in p["comparisons"][0]["coefficients"][1:]
    }
    children = [
        html.H3("Factor relationships · original inputs"),
        html.P(
            "All diagnostics use the regression's aligned sample and configured returns or "
            "changes, "
            "not price levels. Partial correlation removes all other selected factors from both "
            "the stock and the factor. Incremental R² removes one factor from the full model; "
            "contributions do not add up when factors share information. These are historical "
            "associations, not significance rankings or forecasts."
        ),
    ]
    combined_labels, combined_matrix = combined_correlation_matrix(p)
    combined_heatmap = go.Figure(
        go.Heatmap(
            z=combined_matrix,
            x=combined_labels,
            y=combined_labels,
            zmin=-1,
            zmax=1,
            colorscale="RdBu",
            reversescale=True,
        )
    )
    finish_figure(combined_heatmap, "Stock and factor correlations · original inputs")
    combined_heatmap.update_layout(hovermode="closest")
    children.append(graph(combined_heatmap))
    suggestions = collinearity_recommendations(p)
    children.extend(
        [
            html.H4("Collinearity review"),
            html.Ul([html.Li(item) for item in suggestions])
            if suggestions
            else html.P(
                "No factor pair crosses both the 0.80 correlation and 10.0 VIF review thresholds."
            ),
            html.P(
                "These are transparent review suggestions, not automatic deletion rules. "
                "Economic meaning and out-of-sample stability take priority."
            ),
        ]
    )
    children.append(
        table(
            [
                "Factor",
                "Univariate beta",
                "Original joint beta",
                "Univariate R²",
                "Pearson",
                "Spearman",
                "Partial correlation",
                "Incremental R² · pp",
            ],
            [
                [
                    r["label"],
                    _number(r["univariate_beta"]),
                    _number(original_joint[r["term"]]),
                    f"{r['univariate_r_squared']:.1%}",
                    _number(r["pearson"]),
                    _number(r["spearman"]),
                    _number(r["partial_correlation"]),
                    f"{100 * r['incremental_r_squared']:.2f}",
                ]
                for r in rows
            ],
        )
    )
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
                    f"Window: {p['rolling_window']} aligned observations, sampled at each "
                    "month's last "
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
    details = [
        html.H2("Single-stock factor view"),
        html.P(
            f"{p['frequency']} · {p['study_currency']} · {p['sample_count']} aligned observations · "
            f"{p['return_mode'].replace('_', ' ')}"
        ),
    ]
    if source == "demo":
        details.append(
            html.P(
                "Synthetic data for learning only; these are not historical market results.",
                className="result-note",
            )
        )
    if p.get("error_code"):
        details.append(
            html.P(
                f"Stage: {p['error_stage']} · Code: {p['error_code']} · Reference: {p['error_id']}"
            )
        )
    coefficients = p.get("coefficients", [])
    if not coefficients:
        details.append(
            html.P("No fit is shown. Review coverage, factor definitions and minimum observations.")
        )
        if p.get("coverage"):
            details.append(
                html.Details(
                    [html.Summary("Data coverage"), coverage_table(p["coverage"])],
                    open=True,
                )
            )
        return html.Div(details)
    factor_coefficients = coefficients[1:]
    dominant = max(factor_coefficients, key=lambda c: abs(c["standardized_effect"]))
    model_p = p["joint_factor_p_value"]
    model_signal = "Significant" if model_p is not None and model_p < 0.05 else "Review"
    review_count = len(p["diagnostics"])
    details.extend(
        [
            html.H3("At a glance"),
            html.Div(
                [
                    html.Div(
                        [html.Span("Explained variation"), html.Strong(f"{p['adjusted_r_squared']:.1%}"), html.Small("Adjusted R²")],
                        className="metric-card",
                    ),
                    html.Div(
                        [html.Span("Strongest exposure"), html.Strong(dominant["label"]), html.Small(f"Standardized beta {_number(dominant['standardized_effect'])}")],
                        className="metric-card",
                    ),
                    html.Div(
                        [html.Span("Joint model"), html.Strong(model_signal), html.Small(f"p = {_p_value(model_p)}")],
                        className="metric-card",
                    ),
                    html.Div(
                        [html.Span("Review flags"), html.Strong(str(review_count)), html.Small("Open diagnostics below")],
                        className="metric-card",
                    ),
                ],
                className="metric-grid",
            ),
            html.P(
                f"The model explains {p['adjusted_r_squared']:.0%} of historical return variation "
                f"after allowing for model size. {dominant['label']} has the largest exposure on "
                "a comparable standardized basis. Treat this as historical co-movement, not "
                "causality or a return forecast.",
                className="key-message",
            ),
        ]
    )
    scenario_coefficients = scenario_inputs(p)
    chart = go.Figure(
        go.Bar(
            x=[c["label"] for c in factor_coefficients],
            y=[c["standardized_effect"] for c in factor_coefficients],
        )
    )
    finish_figure(
        chart,
        "Standardized effects (comparable across units)",
        yaxis_title="Standardized coefficient",
    )
    details.extend(
        [
            html.H3("What drives the stock?"),
            html.P(
                "Positive bars move with the stock; negative bars move against it. Standardizing "
                "puts returns and price-change factors on a comparable scale."
            ),
            graph(chart),
        ]
    )
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
        details.extend(
            [
                html.H3("Have the exposures been stable?"),
                html.P(
                    "A stable line supports a persistent relationship. Large swings or sign "
                    "changes suggest the full-sample beta hides different regimes."
                ),
                graph(rolling),
            ]
        )

    diagnostic = p.get("residual_diagnostics")
    technical = [
        html.P(
            "Use this section to challenge the headline result. It contains data coverage, "
            "correlation, regression assumptions, alternative models and attribution details."
        ),
        html.H3("Review flags"),
        html.Ul(
            [html.Li(WARNINGS.get(w, w.replace("_", " "))) for w in p["diagnostics"]]
        )
        if p["diagnostics"]
        else html.P("No automated diagnostic warning was raised."),
    ]
    if p.get("coverage"):
        technical.extend(
            [
                html.H3("Data coverage"),
                coverage_table(p["coverage"]),
            ]
        )
    technical.extend(
        [
            html.Details(
                [html.Summary("Correlation and collinearity checks"), *render_relationships(p)]
            ),
            html.H3("Exposure estimates"),
            html.P(
                f"R² {p['r_squared']:.1%} · adjusted R² {p['adjusted_r_squared']:.1%} · "
                f"HAC joint F {_number(p['joint_factor_f_statistic'])} "
                f"(p={_p_value(model_p)}) · covariance {p['covariance']} · "
                f"condition number {p['condition_number']:.2f}. Attribution: "
                + p["attribution_mode"]
                + (" · " + " → ".join(p["sequential_order"]) if p["sequential_order"] else "")
            ),
            table(
                ["Factor", "Coefficient", "Standardized beta", "95% interval", "Input unit"],
                [
                    [
                        c["label"],
                        f"{c['estimate']:.5f}",
                        _number(c["standardized_effect"]),
                        "Withheld: irregular dates" if c["lower_95"] is None else f"[{c['lower_95']:.5f}, {c['upper_95']:.5f}]",
                        c["unit"],
                    ]
                    for c in coefficients
                ],
            ),
        ]
    )
    if diagnostic:
        technical.extend(
            [
                html.H3("Residual diagnostics"),
                html.P(
                    "Small p-values flag assumptions for review. Lag-based tests are withheld "
                    "when retained dates are irregular."
                ),
                table(
                    ["Test", "Statistic", "p-value", "Lag"],
                    [
                        ["Durbin–Watson", _number(diagnostic["durbin_watson"]), "—", 1],
                        ["Ljung–Box", _number(diagnostic["ljung_box_statistic"]), _p_value(diagnostic["ljung_box_p_value"]), diagnostic["lag"]],
                        ["Jarque–Bera", _number(diagnostic["jarque_bera_statistic"]), _p_value(diagnostic["jarque_bera_p_value"]), "—"],
                        ["ARCH LM", _number(diagnostic["arch_lm_statistic"]), _p_value(diagnostic["arch_lm_p_value"]), diagnostic["lag"]],
                    ],
                ),
            ]
        )
    if p.get("comparisons"):
        technical.extend(
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
        technical.extend(
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
    technical.append(html.H3("Rolling stability"))
    technical.append(
        html.P(
            f"Rolling window: {p['rolling_window']} observations; "
            f"successful fits: {len(p['rolling'])}; "
            f"skipped fits: {len(p['rolling_skipped_end_dates'])}."
        )
    )
    if p["rolling_skipped_end_dates"]:
        technical.append(
            html.P("Unavailable window end dates: " + ", ".join(p["rolling_skipped_end_dates"]))
        )
    technical.append(
        html.P(
            "Ranges and sign shares describe overlapping rolling windows, not confidence intervals "
            "or independent statistical tests. "
            "Small coefficients changing sign may be economically "
            "unimportant. Change the rolling window to assess sensitivity. Residualized control "
            "coefficients use a different attribution basis in each window."
        )
    )
    if p.get("stability"):
        technical.append(
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
        technical.append(
            html.P("No valid rolling fits. Extend history or adjust the rolling window.")
        )
    technical.append(
        html.Details(
            [
                html.Summary("Full diagnostics and metadata"),
                html.Pre(json.dumps(p, indent=2), style={"overflowX": "auto"}),
            ]
        )
    )
    details.extend(
        [
            html.Details(
                [
                    html.Summary("Test a factor scenario"),
                    html.H3("Factor-move scenario · original economic factors"),
                    html.P(
                        "Enter moves in each factor's stated unit. Contributions use the original "
                        "joint-model beta and exclude the intercept and residual. This is a linear "
                        "sensitivity, not a forecast."
                    ),
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Label(f"{coefficient['label']} · {coefficient['unit']}"),
                                    dcc.Input(
                                        id={"type": "factor-scenario-move", "term": coefficient["term"], "label": coefficient["label"], "beta": coefficient["beta"]},
                                        type="number",
                                        value=0,
                                        step="any",
                                        debounce=True,
                                    ),
                                ],
                                className="field",
                            )
                            for coefficient in scenario_coefficients
                        ],
                        className="scenario-grid",
                    ),
                    html.Div(id="scenario-output"),
                ]
            ),
            html.Details(
                [html.Summary("Technical diagnostics"), *technical],
                className="technical-diagnostics",
            ),
        ]
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


def factor_pack_options():
    return [
        {
            "label": "Built-in · Fama–French 5 + momentum",
            "value": "french",
        },
        {"label": "Built-in · US ETF proxies", "value": "us_etf"},
        {
            "label": "Built-in · MSCI Europe · configured provider",
            "value": "msci_europe",
        },
        *[
            {
                "label": "YAML pack · "
                + pack.label.replace("\\xB7", "·")
                + (" · configure YAML first" if not pack.enabled else ""),
                "value": "pack:" + pack.id,
                "disabled": not pack.enabled,
            }
            for pack in FACTOR_PACKS.values()
        ],
        {
            "label": "Blank model · add benchmarks manually below",
            "value": "custom",
        },
    ]


def single_stock_workspace():
    controls = html.Aside(
        [
            html.Div(
                [
                    html.Div("STUDY SETTINGS", className="eyebrow"),
                    html.H3("Configure the regression"),
                    help_text(
                        "Choose the data source, stock and factor pack in Core setup. "
                        "Manual factor entry is optional."
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
                                    "label": "Configured providers · Bloomberg",
                                    "value": "configured",
                                },
                            ],
                            value=DEFAULT_SOURCE,
                            clearable=False,
                        ),
                    ),
                    html.P(
                        data_source_guidance(DEFAULT_SOURCE),
                        id="source-guidance",
                        className="source-guidance",
                    ),
                    field(
                        "Bloomberg equity identifier",
                        dcc.Input(
                            id="stock",
                            value="AAPL US Equity",
                            placeholder="TICKER EXCHANGE Equity",
                        ),
                    ),
                    help_text(
                        "Examples: REP SM Equity for Repsol, AAPL US Equity, SAP GY Equity. "
                        "The same identifier maps to Bloomberg or Yahoo according to the source."
                    ),
                    field(
                        "Factor packs",
                        dcc.Dropdown(
                            searchable=False,
                            id="preset",
                            multi=True,
                            options=factor_pack_options(),
                            value=DEFAULT_PACKS,
                            clearable=False,
                        ),
                    ),
                    html.Div(id="pack-summary", className="help-text"),
                    field(
                        "Select factors from these packs",
                        dcc.Dropdown(searchable=False, id="selected", multi=True),
                    ),
                    help_text(
                        "Combine Fama–French and Bloomberg packs in one study. French models use USD "
                        "stock excess returns; price-change factors retain their declared units. "
                        "Select or remove individual factors "
                        "before running. Duplicate factor IDs across packs are rejected. Disabled "
                        "YAML packs must be configured "
                        "and enabled in "
                        "their files, then the app must be restarted."
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
            ),
            settings_section(
                "Manual factor entry · optional",
                "Use this only for one-off funded benchmarks or spreads. "
                "Prefer a YAML pack for reusable factors.",
                [
                    field(
                        "One factor per line",
                        dcc.Textarea(
                            id="extra",
                            value="",
                            placeholder=(
                                "Pharma | IHE US Equity\n"
                                "Energy spread | XLE US Equity | IWB US Equity"
                            ),
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
                        f"JSON replaces the selections above. Maximum {MAX_FACTOR_COUNT} factors, "
                        "subject to sample size."
                    ),
                ],
            ),
            settings_section(
                "Model diagnostics",
                "Compare nested models or remove selected benchmark exposure for attribution.",
                [
                    field(
                        "Model comparisons only · does not add factors",
                        dcc.Textarea(
                            id="comparisons",
                            value="",
                            placeholder="Market | market_excess",
                            className="code-input short",
                        ),
                    ),
                    help_text(
                        "Optional: compare subsets of factors already selected above. "
                        "Format: model name | comma-separated factor IDs."
                    ),
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
        [
            html.Details(
                [
                    html.Summary("Data inspector · query and verify aligned inputs"),
                    html.P(
                        "Load this only when you want to inspect the current settings. It performs "
                        "a fresh provider query and keeps normalized values in memory only."
                    ),
                    html.Button(
                        "Load current data",
                        id="load-preview",
                        n_clicks=0,
                        className="secondary",
                    ),
                    dcc.Loading(html.Div(id="preview-output"), type="circle"),
                ],
                className="data-inspector",
            ),
            dcc.Loading(
                html.Div(
                    id="result",
                    children=empty_state(
                        "Explain a stock's historical returns",
                        "Estimate factor exposures, inspect collinearity and see how coefficients "
                        "change through time.",
                        [
                            ("Choose", "Set a stock, data source and factor family."),
                            ("Refine", "Remove factors or add benchmarks relevant to your thesis."),
                            ("Interpret", "Review coverage first, then exposures and stability."),
                        ],
                    ),
                ),
                type="circle",
            ),
        ],
        className="results-panel",
    )
    return html.Div(
        [
            workspace_header(
                "TIME-SERIES ATTRIBUTION",
                "Single-stock analysis",
                "Explain one stock's historical returns with research factors, traded benchmarks "
                "and custom spreads.",
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
                                "XOM US Equity | Energy | US\nCVX US Equity | Energy | US\n"
                                "COP US Equity | Energy | US\nSLB US Equity | Energy | US\n"
                                "EOG US Equity | Energy | US\n"
                                "LLY US Equity | Health Care | US\n"
                                "JNJ US Equity | Health Care | US\n"
                                "MRK US Equity | Health Care | US\n"
                                "ABBV US Equity | Health Care | US\n"
                                "AMGN US Equity | Health Care | US"
                            ),
                            className="code-input universe-input",
                        ),
                    ),
                    help_text(
                        "Bloomberg equity identifier | sector | country | optional member-from | "
                        "optional member-to"
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
                        "Grouped runs need enough eligible stocks in each group to form every "
                        "quantile."
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
                    "Evaluate 12–1 momentum with rank IC, quantile returns, turnover, costs and a "
                    "chronological holdout.",
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
                "Test whether a ranking signal separates subsequent returns across an editable "
                "stock universe.",
                ["Rank IC", "Quantiles", "Holdout"],
            ),
            html.Div([controls, results], className="workspace-grid"),
        ],
        className="workspace",
    )


logging.getLogger("werkzeug").setLevel(logging.ERROR)
app = Dash(__name__, title="Factor Playground", suppress_callback_exceptions=True)
app.layout = html.Div(
    [
        html.Header(
            [
                html.Div(
                    [
                        html.Div("TRADE RESEARCH", className="eyebrow"),
                        html.H1("Factor Playground"),
                        html.P(
                            "A local research console for transparent factor attribution and "
                            "signal validation."
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
                    label="Watchlist attribution",
                    value="watchlist",
                    className="workspace-tab",
                    selected_className="workspace-tab--selected",
                    children=watchlist_workspace(
                        factor_pack_options(), DEFAULT_SOURCE, DEFAULT_PACKS
                    ),
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
def select_preset(presets):
    if not presets:
        return [], []
    try:
        factors = available_factors(presets)
    except ValueError:
        return [], []
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
def attribution_choices(presets, selected, extra, advanced, targets, controls, order):
    try:
        factors = editor_factors(presets, selected, extra, advanced)
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
def describe_pack(presets):
    if not presets:
        return "Choose one or more factor packs."
    try:
        available_factors(presets)
    except ValueError as error:
        return html.P(str(error), className="source-guidance")
    packs = packs_for(presets)
    if not packs:
        return "YAML packs are loaded from the local factor_packs folder at startup."
    items = []
    for pack in packs:
        items.extend(
            [
                html.H4(pack.label.replace("\\xB7", "·")),
                html.P(pack.description),
                html.P("Sources: " + ", ".join(pack.sources)),
            ]
        )
        if pack.level_factors:
            items.append(html.P(f"Pack currency: {pack.currency} · Calendar: {pack.calendar}"))
            items.append(
                html.Ul(
                    [
                        html.Li(
                            f"{key}: {item.security} · {item.field} · "
                            f"{item.currency} · {item.unit} · scale {item.scale:g}"
                        )
                        for key, item in pack.inputs.items()
                    ]
                )
            )
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
            "The stock remains in simple adjusted returns. Factor transformations apply at "
            "the selected frequency. Bloomberg identifiers and units must be verified locally."
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
        packs = packs_for(controls[1])
        unavailable = [
            pack
            for pack in packs
            if not pack.enabled
            or source not in pack.sources
            or (pack.level_factors and source != "configured")
        ]
        if unavailable:
            return html.P("One or more selected YAML packs do not support this data source.")
        if packs and (controls[4] or "").strip():
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


@app.callback(
    Output("preview-output", "children"),
    Input("load-preview", "n_clicks"),
    State("source", "value"),
    *[State(k, "value") for k in CONTROL_IDS],
    prevent_initial_call=True,
    running=[(Output("load-preview", "disabled"), True, False)],
)
def update_preview(_clicks, source, *controls):
    try:
        return render_preview(run_preview(source, *controls))
    except (ValueError, ValidationError):
        return html.P("Invalid settings. Resolve factor-pack, date or model-input errors first.")
    except Exception as error:
        reference = uuid.uuid4().hex
        logging.getLogger(__name__).error(
            "factor preview stage=execution code=%s reference=%s", type(error).__name__, reference
        )
        return html.P(
            f"Data preview failed · Code: {type(error).__name__} · Reference: {reference}. "
            "Check Bloomberg connectivity, entitlements and mappings. Raw payloads are not logged."
        )


@app.callback(
    Output("scenario-output", "children"),
    Input(
        {
            "type": "factor-scenario-move",
            "term": ALL,
            "label": ALL,
            "beta": ALL,
        },
        "value",
    ),
    State(
        {
            "type": "factor-scenario-move",
            "term": ALL,
            "label": ALL,
            "beta": ALL,
        },
        "id",
    ),
)
def update_scenario(moves, identities):
    try:
        rows, total_bps = scenario_impact(moves, identities)
    except (TypeError, ValueError, KeyError):
        return html.P("Enter finite numerical factor moves.")
    if not rows:
        return html.P("Run a factor study before entering a scenario.")
    return html.Div(
        [
            table(
                ["Factor", "Move", "Beta", "Contribution · bp"],
                [
                    [
                        row["label"],
                        f"{row['move']:.5g}",
                        f"{row['beta']:.5g}",
                        f"{row['contribution_bps']:.1f}",
                    ]
                    for row in rows
                ],
            ),
            html.P(f"Total modelled factor contribution: {total_bps:.1f} bp."),
        ]
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


register_watchlist(app, study_context, select_preset, WARNINGS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    if args.open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
