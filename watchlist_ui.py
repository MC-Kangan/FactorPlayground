"""Watchlist controls and presentation; numerical work stays in TradeAgent."""

from datetime import UTC, datetime, timedelta
from threading import Lock

import plotly.graph_objects as go
from dash import Input, Output, State, dash_table, dcc, html
from dash.exceptions import PreventUpdate

from trade_research.engine import ResearchEngine
from trade_research.providers import parse_bloomberg_equity_identifier
from trade_research.skills.factor_data import session_dates
from trade_research.skills.factor_study import StudyError

MAX_STOCKS = 20
_REFRESH_LOCK = Lock()


def parse_watchlist(text):
    tickers = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not 1 <= len(tickers) <= MAX_STOCKS:
        raise ValueError("Enter 1–20 Bloomberg equity identifiers, one per line.")
    identities = [parse_bloomberg_equity_identifier(ticker) for ticker in tickers]
    if len({item.instrument for item in identities}) != len(identities):
        raise ValueError("Remove duplicate stocks from the watchlist.")
    return [item.security for item in identities]


def fetch_watchlist(
    context, source, text, packs, selected, region, start, minimum, lags, *, today=None
):
    tickers = parse_watchlist(text)
    today = today or datetime.now(UTC).date()
    end = today - timedelta(days=1)
    rows = []
    for ticker in tickers:
        row = {"ticker": ticker, "snapshot": None, "status": "Unavailable"}
        try:
            controls = (
                ticker,
                packs,
                selected,
                "",
                "",
                "daily",
                region,
                start,
                end.isoformat(),
                minimum,
                minimum,
                lags,
                "",
                "original",
                [],
                [],
                [],
            )
            request, settings = context(source, *controls)
            snapshot = ResearchEngine.from_settings(settings).factor_monitor_snapshot(request)
            sessions = session_dates(request.instrument.market, end - timedelta(days=14), end)
            row.update(
                snapshot=snapshot.model_dump(mode="json"),
                status=(
                    "Demo"
                    if source == "demo"
                    else "Aligned close"
                    if sessions and snapshot.period_end == sessions[-1]
                    else "Lagged data"
                ),
            )
        except StudyError as error:
            row["status"] = "Unavailable · " + error.diagnostic
        except Exception as error:
            # Never return raw provider errors or licensed payloads to the browser/logs.
            row["status"] = "Unavailable · " + type(error).__name__
        rows.append(row)
    return rows


def workspace(pack_options, default_source, default_packs):
    def field(label, component):
        return html.Div([html.Label(label), component], className="field")

    return html.Div(
        [
            html.H2("Watchlist attribution"),
            html.P(
                "Daily closing-data monitor · original-factor OLS/HAC · contributions in basis "
                "points."
            ),
            html.P(
                "Refresh fetches data and refits each model using observations before the "
                "displayed day. "
                "Today’s partial session is excluded. This is an estimate of contemporaneous "
                "exposure, "
                "not a causal explanation or trading signal."
            ),
            html.Details(
                [
                    html.Summary("Configure watchlist and factors"),
                    field(
                        "Data source",
                        dcc.Dropdown(
                            id="wl-source",
                            options=[
                                {"label": "Synthetic demo · offline", "value": "demo"},
                                {"label": "Yahoo + published research factors", "value": "yahoo"},
                                {
                                    "label": "Configured providers · Bloomberg",
                                    "value": "configured",
                                },
                            ],
                            value=default_source,
                            clearable=False,
                            searchable=False,
                        ),
                    ),
                    field(
                        "Bloomberg identifiers · one per line · maximum 20",
                        dcc.Textarea(
                            id="wl-tickers",
                            value="REP SM Equity\nSHEL LN Equity\nTTE FP Equity",
                            style={"width": "100%", "height": "110px"},
                        ),
                    ),
                    field(
                        "Factor packs",
                        dcc.Dropdown(
                            id="wl-packs",
                            options=pack_options,
                            value=default_packs,
                            multi=True,
                            searchable=False,
                        ),
                    ),
                    field(
                        "Selected factors",
                        dcc.Dropdown(id="wl-factors", multi=True, searchable=False),
                    ),
                    field(
                        "Research region",
                        dcc.Dropdown(
                            id="wl-region",
                            options=["Europe", "US"],
                            value="Europe",
                            clearable=False,
                            searchable=False,
                        ),
                    ),
                    field(
                        "Training history starts",
                        dcc.Input(
                            id="wl-start",
                            type="date",
                            value=(datetime.now(UTC).date() - timedelta(days=3 * 365)).isoformat(),
                        ),
                    ),
                    field(
                        "Minimum training observations",
                        dcc.Input(
                            id="wl-minimum", type="number", min=60, max=2520, step=1, value=252
                        ),
                    ),
                    field(
                        "HAC lags",
                        dcc.Input(id="wl-lags", type="number", min=0, max=60, step=1, value=5),
                    ),
                    html.P(
                        "YAML packs use the same definitions as single-stock analysis. French "
                        "factors may "
                        "lag and are never replaced with ETF proxies. Stock returns may be "
                        "excess returns "
                        "in USD: check each row’s currency and basis. Use single-stock "
                        "diagnostics to "
                        "investigate collinearity; this monitor keeps the original factor basis."
                    ),
                ],
                open=True,
            ),
            html.Button("Refresh data and refit", id="wl-refresh", n_clicks=0),
            field(
                "Automatic refresh · active only on this tab",
                dcc.Dropdown(
                    id="wl-auto",
                    options=[
                        {"label": "Off", "value": 0},
                        {"label": "Every 5 minutes", "value": 300000},
                        {"label": "Every 15 minutes", "value": 900000},
                        {"label": "Every hour", "value": 3600000},
                    ],
                    value=0,
                    clearable=False,
                    searchable=False,
                ),
            ),
            html.P(
                "Changing settings applies on the next refresh. Auto-refresh refits the whole "
                "watchlist."
            ),
            dcc.Interval(id="wl-timer", interval=300000, n_intervals=0, disabled=True),
            dcc.Store(id="wl-rows", storage_type="memory"),
            html.Div(id="wl-status", role="status"),
            dcc.Loading(html.Div(render_rows([]), id="wl-results")),
            html.Div(id="wl-detail"),
        ],
        className="result-card",
        style={"padding": "24px"},
    )


def render_rows(rows):
    factors = {}
    for row in rows:
        if row["snapshot"]:
            for item in row["snapshot"]["contributions"]:
                factors[item["factor_id"]] = item["label"]
    records = []
    for row in rows:
        item = {"Ticker": row["ticker"], "Status": row["status"]}
        snapshot = row["snapshot"]
        if snapshot:
            model = snapshot["model"]
            item.update(
                {
                    "As of": snapshot["period_end"],
                    "Currency": model["study_currency"],
                    "Basis": "Excess return"
                    if model["return_mode"] == "excess_return"
                    else "Adjusted return",
                    "Training through": model["actual_end"],
                    "N": model["sample_count"],
                    "Adj R²": model["adjusted_r_squared"],
                    "Max VIF": max(model["variance_inflation_factors"], default=None),
                    "Actual bp": snapshot["actual"] * 10000,
                    "Explained bp": snapshot["explained"] * 10000,
                    "Unexplained bp": snapshot["unexplained"] * 10000,
                    "Intercept bp": snapshot["intercept"] * 10000,
                    "Warnings": ", ".join(model["diagnostics"]),
                }
            )
            item.update(
                {
                    "factor:" + c["factor_id"]: c["contribution"] * 10000
                    for c in snapshot["contributions"]
                }
            )
        records.append(item)
    text_cols = ["Ticker", "Status", "As of", "Currency", "Basis", "Training through", "Warnings"]
    columns = [{"name": key, "id": key} for key in text_cols[:5]]
    numeric = ["Actual bp", "Explained bp", "Unexplained bp", "Intercept bp"]
    numeric += ["factor:" + key for key in factors]
    numeric += ["N", "Adj R²", "Max VIF"]
    columns += [
        {
            "name": factors.get(key.removeprefix("factor:"), key)
            + (" bp" if key.startswith("factor:") else ""),
            "id": key,
            "type": "numeric",
            "format": {"specifier": ".2f"},
        }
        for key in numeric
    ]
    columns += [{"name": key, "id": key} for key in text_cols[5:]]
    return html.Div(
        [
            html.P(
                "Select a row for factor moves and model diagnostics. Blank cells mean "
                "unavailable, "
                "not zero. Rows can have different observation dates. R² describes the "
                "training sample."
            ),
            dash_table.DataTable(
                id="wl-table",
                columns=columns,
                data=records,
                row_selectable="single",
                selected_rows=[],
                sort_action="native",
                filter_action="native",
                page_size=20,
                style_table={"overflowX": "auto"},
                style_cell={
                    "minWidth": "100px",
                    "maxWidth": "240px",
                    "whiteSpace": "normal",
                    "padding": "8px",
                },
                style_data_conditional=[
                    {
                        "if": {"filter_query": '{Status} = "Lagged data"'},
                        "backgroundColor": "#fff1cc",
                    },
                    {"if": {"column_id": "Unexplained bp"}, "fontWeight": "bold"},
                ],
            ),
        ]
    )


def register_callbacks(app, context, select_preset, warning_labels):
    @app.callback(
        Output("wl-factors", "options"), Output("wl-factors", "value"), Input("wl-packs", "value")
    )
    def select_factors(packs):
        return select_preset(packs)

    @app.callback(
        Output("wl-timer", "disabled"),
        Output("wl-timer", "interval"),
        Input("wl-auto", "value"),
        Input("workspace-tabs", "value"),
    )
    def timer(minutes, tab):
        return not minutes or tab != "watchlist", minutes or 300000

    @app.callback(
        Output("wl-rows", "data"),
        Output("wl-results", "children"),
        Output("wl-status", "children"),
        Input("wl-refresh", "n_clicks"),
        Input("wl-timer", "n_intervals"),
        State("workspace-tabs", "value"),
        *[
            State("wl-" + key, "value")
            for key in (
                "source",
                "tickers",
                "packs",
                "factors",
                "region",
                "start",
                "minimum",
                "lags",
            )
        ],
        prevent_initial_call=True,
        running=[(Output("wl-refresh", "disabled"), True, False)],
    )
    def refresh(_clicks, _intervals, tab, *controls):
        if tab != "watchlist" or not _REFRESH_LOCK.acquire(blocking=False):
            raise PreventUpdate
        try:
            rows = fetch_watchlist(context, *controls)
            count = sum(row["snapshot"] is not None for row in rows)
            return (
                rows,
                render_rows(rows),
                (
                    f"Checked {datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC · "
                    f"{count}/{len(rows)} models available. "
                    "Data dates are shown per row; this is not an intraday feed."
                ),
            )
        except ValueError:
            return (
                [],
                html.P(
                    "Enter 1–20 distinct Bloomberg equity identifiers and valid model settings."
                ),
                "Refresh failed",
            )
        finally:
            _REFRESH_LOCK.release()

    @app.callback(
        Output("wl-detail", "children"),
        Input("wl-table", "selected_rows", allow_optional=True),
        Input("wl-rows", "data"),
    )
    def detail(selected, rows):
        if not rows or not selected or selected[0] >= len(rows):
            return []
        row = rows[selected[0]]
        snapshot = row["snapshot"]
        if not snapshot:
            return html.P(row["status"])
        model = snapshot["model"]
        contributions = snapshot["contributions"]
        figure = go.Figure(
            go.Bar(
                x=[c["label"] for c in contributions] + ["Intercept"],
                y=[c["contribution"] * 10000 for c in contributions]
                + [snapshot["intercept"] * 10000],
            )
        )
        figure.update_layout(
            title=row["ticker"] + " · " + snapshot["period_end"],
            height=360,
            yaxis_title="Contribution (bp)",
            margin={"t": 55, "b": 80},
        )
        return html.Div(
            [
                dcc.Graph(
                    figure=figure,
                    responsive=False,
                    style={"height": "360px"},
                    config={"responsive": True},
                ),
                html.P(
                    "Unexplained includes omitted drivers and estimation error; it is not pure "
                    "stock-specific news."
                ),
                html.Details(
                    [
                        html.Summary("Factor inputs and model diagnostics"),
                        html.P(
                            f"Training: {model['actual_start']} to {model['actual_end']} · "
                            f"N={model['sample_count']} · "
                            f"Covariance: {model['covariance']}"
                        ),
                        html.Ul([html.Li(warning_labels.get(w, w)) for w in model["diagnostics"]]),
                        dash_table.DataTable(
                            columns=[
                                {"name": c, "id": c} for c in ("Factor", "Unit", "Move", "Beta")
                            ],
                            data=[
                                {
                                    "Factor": c["label"],
                                    "Unit": c["unit"],
                                    "Move": c["move"],
                                    "Beta": c["beta"],
                                }
                                for c in contributions
                            ],
                            style_table={"overflowX": "auto"},
                        ),
                    ]
                ),
            ]
        )
