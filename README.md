# Factor Playground

A standalone local Dash UI for TradeAgent's research analysts. It contains controls,
presentation and synthetic input generation; all real alignment, currency conversion
and estimation run in the backend. The interface is split into two tabbed workspaces:
single-stock attribution and cross-sectional signal validation.

The same app includes a **Cross-sectional Signal Lab** for TradeAgent's
`cross-sectional-signal` analyst. It evaluates 12–1 momentum across an editable stock
universe using monthly formation dates and next-session entry.

## Start

Double-click **Start Factor Playground.command**, or:

```sh
cd ~/Documents/FactorPlayground
.venv/bin/python app.py --open-browser
```

Open http://127.0.0.1:8050. The server binds only to localhost. Do not expose it
publicly. It stores neither raw observations nor reports.

For a fresh environment, install requirements.txt. It pins the UI environment and
installs the sibling ../TradeAgent checkout editable.

## Use

1. Choose **Single-stock attribution** or **Cross-sectional signals** from the top
   navigation. Each workspace keeps core settings open and groups less common controls
   into expandable sections.
2. Start with Synthetic demo. It creates deterministic USD observations on the
   target calendar, even for a European stock; it does not imitate real FX or market history.
3. Enter a stock such as US:AAPL or XETRA:SAP.
4. Choose French, US ETF or Custom. Remove any unwanted preset factors.
5. Add as many extra benchmarks as the sample can support, one per line:
   `Pharma | US:IHE`
   or `Growth minus value | US:IWF | US:IWD`.
   French selections subtract cash from funded asset benchmarks once.
6. Choose daily/monthly, research region, dates, observation minimum, rolling
   observations and HAC lags. Then click Run.
7. Check coverage before coefficients. Internal gaps withhold HAC intervals.
   R² measures historical explanation, not predictability. The standardized-effect
   chart helps compare factors with different units. Plotly charts support hover,
   zoom, pan and image export from their graph toolbar.

Monthly is a useful starting point for cross-market research. Only daily/monthly
are currently implemented. More factors are not automatically better: correlated
benchmarks destabilize coefficients and require more observations.

The backend accepts up to 64 explicitly selected factors. The UI uses that safety
bound; the observation/window constraints also apply. There is no fixed three- or
six-factor model.

## Advanced factor list

An optional JSON list replaces all simple selections above. For example:

```json
[
  {"id": "market", "label": "Market", "kind": "research", "research_key": "market_excess"},
  {"id": "pharma", "label": "Pharma", "kind": "asset_return",
   "instrument": {"market": "US", "symbol": "IHE"}, "calendar": "XNYS"}
]
```

A spread has a short_instrument and optional short_calendar. Use the named-model editor to compare multiple strict subsets on identical observations.
Additional factor IDs from the simple editor are extra_0, extra_1, etc., corresponding
to line positions. Custom research columns need an injected normalized provider;
the demo supports the French research catalog only.

## Data sources

- **Yahoo + French:** adjusted equity/ETF closes, official French files and supported
  EUR/GBP/CHF-to-USD FX. Network availability and Yahoo rate limits can affect results.
- **Configured backend:** reads existing TRADE_RESEARCH_CONFIG/environment settings.
  Bloomberg total-return mappings must be explicitly verified. B-PIPE authentication
  and entitlements remain firm-specific. There is no implicit Yahoo fallback for
  Bloomberg FX. Use a firm FX adapter or normalized USD inputs.
- **Synthetic demo:** offline controls and numerical integration check only.

See ../TradeAgent/docs/factor-mvp2-implementation.md for provider contracts, cash
semantics, native monthly inputs, coverage and assumptions. Custom providers can be
registered in ResearchEngine without changing the estimator; this UI does not attempt
to load arbitrary provider code or credentials.

A failed run shows a safe code, stage and reference. Unexpected failures log those
metadata only, not exception payloads or licensed data. Changed controls clear stale
results until Run is clicked again.

## Development checks

The backend development environment supplies pytest; the app environment supplies Dash:

```sh
PYTHONPATH="$PWD:$PWD/.venv/lib/python3.13/site-packages" ../TradeAgent/.venv/bin/python -m pytest test_app.py
```

Adjust the Python minor-version directory to match the installed environments.
Tests are offline: daily/monthly, both presets, subset plus multiple extra factors,
ten-factor custom model, safe errors and Dash routes.


## Model comparison and stability

The factor engine stays sector-independent. The new controls accept:
- Named models, one per line: `Market and sector | market_excess,extra_0`.
- Optional residualization, one per line: `extra_0 | market_excess,hml`.

Preset factor IDs appear in brackets in the selector. Additional factors use their
zero-based line positions (extra_0, extra_1). Comparison subsets must be part of the
selected full model. Controls must remain selected and cannot themselves be targets.

The comparison table always uses original factors. Residualization changes the main
coefficient table and rolling plot; it preserves the full fitted returns and shows
the remaining variance of each transformed factor. This is attribution, not causality.

Rolling minimum/median/maximum and sign shares are descriptive summaries of overlapping
windows, not confidence intervals. Change the rolling-window control to assess horizon
sensitivity. See ../TradeAgent/docs/factor-model-comparison.md for assumptions and the
synthetic European energy example. No PCA, ridge or prediction claims are introduced.


## Review fixes

Synthetic observations are now keyed by series identity, frequency and period date.
Selecting, removing or reordering factors does not regenerate the stock or existing
benchmarks. Extending a date range preserves overlapping observations. The fixed demo
economy has shared market/value/momentum drivers plus asset-specific noise. Monthly
and daily simulations remain separate synthetic datasets, not a resampling identity.

Rolling summaries show the last valid fit date and mark stale estimates. Skipped window
dates are listed, and charts leave gaps at failed fits. Original-model condition numbers,
VIF and collinearity flags remain visible alongside residualized results.

## Cross-sectional Signal Lab

Enter one stock per line:

```text
US:XOM | Energy | US
US:CVX | Energy | US
US:LLY | Health Care | US
```

Point-in-time mode adds membership dates:

```text
US:XOM | Energy | US | 2015-01-01 |
US:OLDCO | Energy | US | 2015-01-01 | 2021-06-30
```

The controls select current-watchlist or point-in-time membership, whole-universe or
within-sector/country ranking, quantile count, minimum group size, illustrative cost and
the chronological holdout start. The output shows:

- monthly Spearman rank IC and its date-block-bootstrap interval;
- IC consistency, including median, information ratio and positive-month share;
- average return by score quantile;
- top-quantile net performance versus the eligible equal-weight universe;
- turnover, drawdown and development-versus-holdout summaries.

Yahoo runs use adjusted-close returns. Configured runs use TradeAgent's normalized
return provider, including administrator-mapped Bloomberg total-return data. One study
must use a common currency and return basis. The synthetic universe is generated only
for learning and does not represent historical securities.
