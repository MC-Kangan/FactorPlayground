# Factor Playground

A standalone local Dash UI for TradeAgent's research analysts. It contains controls,
presentation and synthetic input generation; all real alignment, currency conversion
and estimation run in the backend. The interface is split into two tabbed workspaces:
single-stock attribution and cross-sectional signal validation.

The same app includes a **Cross-sectional Signal Lab** for TradeAgent's
`cross-sectional-signal` analyst. It evaluates 12–1 momentum across an editable stock
universe using monthly formation dates and next-session entry.

## Install on a company Windows laptop

Keep the two source repositories side by side. Do not copy an existing virtual environment:

```text
C:\Research\TradeAgent\
C:\Research\FactorPlayground\
```

Use firm-approved **64-bit Python 3.12** and your managed JFrog pip configuration.
From PowerShell, without activating a virtual environment:

```powershell
cd C:\Research\FactorPlayground
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r ..\TradeAgent\requirements.lock -r requirements-ui.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e ..\TradeAgent
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe app.py --open-browser
```

These commands install the backend into the app environment; a second backend environment,
HTTP server, Docker, Node.js and an LLM subscription are unnecessary. `requirements.txt`
is a convenience installation combining the same backend and UI pins. The UI lock contains
only additional packages, so backend versions have one authoritative definition.

Read the [Windows/JFrog and offline installation guide](../TradeAgent/docs/corporate-installation.md)
before transferring the project. The [dependency audit](../TradeAgent/docs/dependency-audit.md)
identifies packages to check with IT, including Bloomberg `blpapi`, platform-specific wheels
and the `backtesting` license. Windows and live corporate Bloomberg access still require
validation on your firm machine; local validation was on macOS.

If your company already has a modified version, follow the
[company merge guide](MERGE_GUIDE.md). It explains which Bloomberg-specific code to preserve,
which backend modules must move together and how to verify the combined implementation.

## Start on macOS / Linux

```sh
cd ~/Documents/FactorPlayground
python3.12 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: -r ../TradeAgent/requirements.lock -r requirements-ui.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e ../TradeAgent
.venv/bin/python app.py --open-browser
```

For subsequent runs use the last command, or the macOS **Start Factor Playground.command**
launcher. Open http://127.0.0.1:8050. Use `--port 8051` if that port is occupied.
The app binds only to localhost and stores neither raw observations nor reports.
Stop it with Ctrl+C. Restart after changing Python, CSS, YAML packs or provider settings.

## Configure the backend and packs

Select **Configured backend** to use `TRADE_RESEARCH_CONFIG`. Set environment variables
in the same PowerShell session that starts the app:

```powershell
$env:TRADE_RESEARCH_CONFIG = "C:\Research\private-config\research.json"
$env:FACTOR_PLAYGROUND_PACK_DIR = "C:\Research\private-config\factor_packs"
.\.venv\Scripts\python.exe app.py --open-browser
```

The pack-directory variable is optional; omit it to use the bundled `factor_packs` directory.
The app reads process environment directly and does **not** automatically load `.env`.
The CLI's `.env` behavior is separate. Keep credentials outside packs and source control.
For Bloomberg mappings, units, currencies and BPIPE limitations, follow the
[corporate configuration example](../TradeAgent/docs/corporate-installation.md#bloomberg-configuration).

## Use

1. Choose **Single-stock attribution** or **Cross-sectional signals** from the top
   navigation. Each workspace keeps core settings open and groups less common controls
   into expandable sections.
2. When `TRADE_RESEARCH_CONFIG` selects Bloomberg, the configured Bloomberg source is
   selected by default. Otherwise start with Synthetic demo for an offline UI check.
3. Enter the Bloomberg equity identifier, such as `AAPL US Equity`, `REP SM Equity`
   or `SAP GY Equity`. The app translates the exchange code for Yahoo when Bloomberg
   is unavailable and keeps the exact identifier for Bloomberg.
4. Select one or more factor packs, for example MSCI plus Energy. Then remove any
   individual factors you do not want in this run.
5. Add as many extra benchmarks as the sample can support, one per line:
   `Pharma | IHE US Equity`
   or `Growth minus value | IWF US Equity | IWD US Equity`.
   French selections subtract cash from funded asset benchmarks once.
6. Choose daily/monthly, research region, dates, observation minimum, rolling
   observations and HAC lags. Then click Run.
7. Check coverage before coefficients. Internal gaps withhold HAC intervals.
   R² measures historical explanation, not predictability. The standardized-effect
   chart helps compare factors with different units. Plotly charts support hover,
   zoom, pan and image export from their graph toolbar.
8. Open **Data inspector** and click **Load current data** to issue a fresh provider
   query. The single chart plots every aligned model input on a standardized scale;
   hover a date to see its exact model value, unit and currency. The preview stays in
   memory and is not written into the research report.

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
  When Bloomberg is configured it is the default single-stock source. Equity histories
  use `PX_LAST` with explicit normal-dividend, abnormal-dividend and split adjustments;
  Bloomberg `PX_LAST` currency pairs supply required FX. BLPAPI/BPIPE authentication,
  mappings and entitlements remain firm-specific, with no Yahoo fallback.
- **Synthetic demo:** offline controls and numerical integration check only.

Synthetic data remains the safe default only when Bloomberg is not configured, so the app
can open without market-data credentials. It is never mixed into a Yahoo or configured-provider result. For a public
Repsol example, choose **Yahoo + French**, enter `REP SM Equity`, select the French preset and
Europe, then choose daily or monthly frequency. The adapter resolves that canonical ID to
Yahoo's `REP.MC`; the French adapter downloads the corresponding native-frequency files
from the official Kenneth French library when the study runs.

The **MSCI Europe** preset is for the configured provider. Its canonical index identities
must be mapped to the firm's exact entitled Bloomberg identifiers, fields, currency and
return basis in `TRADE_RESEARCH_CONFIG`; the app does not scrape or guess
licensed MSCI series. The energy YAML pack for Yahoo uses USO, BNO, UNG and UGA as
tradable ETP return proxies, whose returns include product structure, collateral, fees and
futures roll effects. It does not claim to represent spot crude, spot gas or diesel. The Bloomberg YAML templates provide editable price-series mappings and transforms;
replace placeholders with verified entitled securities before enabling them.

The stock-entry translator supports Bloomberg U.S. composite/venue codes (`US`, `UA`,
`UN`, `UQ`, `UW`) and the major European codes `LN`, `GY`, `SM`, `IM`, `SW`, `FP`,
`NA`, `BB`, `PL`, `DC`, `FH`, `ID`, `NO`, `SS` and `AV`. Unsupported codes fail
instead of being assigned a guessed Yahoo market. Some share-class roots differ between
vendors; verify the resolved series in **Data inspector** before using it.

See ../TradeAgent/docs/factor-mvp2-implementation.md for provider contracts, cash
semantics, native monthly inputs, coverage and assumptions. Custom providers can be
registered in ResearchEngine without changing the estimator; this UI does not attempt
to load arbitrary provider code or credentials.

A failed run shows a safe code, stage and reference. Unexpected failures log those
metadata only, not exception payloads or licensed data. Changed controls clear stale
results until Run is clicked again.

## Development checks

Install development tools into the same environment as Dash, through the approved index:

```powershell
.\.venv\Scripts\python.exe -m pip install -r ..\TradeAgent\requirements-dev.lock
.\.venv\Scripts\python.exe -m pytest test_app.py
```

On macOS/Linux replace `.\.venv\Scripts\python.exe` with `.venv/bin/python`.
Tests use synthetic data and mocked providers; they do not prove Bloomberg entitlements.
Backend quality checks are documented in the TradeAgent README.

## Model comparison and stability

The factor engine stays sector-independent. Under **Model diagnostics**, select:

- **Original factors**: reference joint regression.
- **Remove selected exposures**: choose factor targets and retained control factors
  using dropdowns. The targets and controls must not overlap.
- **Sequential attribution**: select every factor in priority order. Clear and
  reselect to reorder; the displayed numbered sequence is the applied order.

Earlier sequential factors receive shared variation. These full-basis transformations
preserve fitted returns/R², but change the interpretation of exposures. Rolling
transformations are fitted inside each window. Exact redundant definitions still fail;
orthogonalization cannot identify independent oil, diesel and crack-change exposures
when one is exactly the difference of the others.

Open **Correlation and collinearity checks · review first** in the results for the
stock-plus-factor correlation matrix, stock–factor Pearson/Spearman
correlations, partial correlations controlling for all other factors, same-sample
incremental R², before/after factor heatmaps, VIF and rolling correlations. Rolling
correlations use the existing rolling-window setting; Spearman traces can be enabled
in the legend. Partial correlation is blank if the reduced stock regression leaves no
meaningful residual variation. These are descriptive measures, not selection rules,
causal effects or predictive tests. Original-model coefficients remain in Model comparison.

## Regression diagnostics and scenarios

The results distinguish **univariate beta** (one separate regression per original
factor) from the **original joint beta** and selected-basis coefficient. Large gaps
between univariate and joint estimates reveal specification sensitivity, often from
overlapping factors; they do not choose the preferred model automatically. The existing
standardized-effect chart is the standardized beta, comparable across factor units.

The summary includes adjusted R² and a HAC joint-factor F test. Open **Residual
diagnostics** for Durbin-Watson, Ljung-Box, Jarque-Bera and ARCH LM results. Small
p-values are review flags, not an automatic factor-deletion rule. Lag-based tests are
withheld when retained dates are irregular.

Open **Factor-move scenario** and enter hypothetical moves in each original economic
factor's displayed unit. For decimal returns, `0.01` means 1%; commodity price changes
use the pack's declared price unit. The app always uses the original joint-model betas,
even when the attribution view is orthogonalized. It shows `beta × move` in basis points
and excludes the intercept and unexplained residual. It is a linear sensitivity, not a
forecast or reconciled P&L attribution. See the backend
[interpretation guide](../TradeAgent/docs/factor-diagnostics-scenarios.md).

Named model subsets still use `Model name | factor_id,other_id`. Factor IDs appear
in selection labels; manually entered extra factors use `extra_0`, `extra_1`, etc.

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
XOM US Equity | Energy | US
CVX US Equity | Energy | US
LLY US Equity | Health Care | US
```

Point-in-time mode adds membership dates:

```text
XOM US Equity | Energy | US | 2015-01-01 |
OLDCO US Equity | Energy | US | 2015-01-01 | 2021-06-30
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
return provider, including administrator-mapped Bloomberg data. One study
must use a common currency and return basis. The synthetic universe is generated only
for learning and does not represent historical securities.


## YAML factor packs

Place reusable definitions in `factor_packs/*.yaml` and restart the app. They appear
in **Factor packs** with a `YAML pack ·` prefix. Select multiple packs and their factors
load into **Select factors from these packs**, where individual factors can be included
or removed. Duplicate factor IDs are rejected instead of silently overwriting a series.
**Model comparisons only** never creates factors—it only compares
subsets of the factors already selected. All
selectors now show their choices without a search input. Set
`FACTOR_PLAYGROUND_PACK_DIR` to use another local directory, such as a firm repo.

The public **Energy · Yahoo ETF proxies** pack runs with Yahoo or the offline demo.
The Bloomberg MSCI and energy templates are disabled until you replace the placeholder
tickers, verify units/scales/calendar and set `enabled: true`. The energy template
shows how to form diesel minus Brent from compatible price levels before applying a
first difference. Simple returns and log returns are also supported at daily/monthly
frequency. Bloomberg stock returns use corporate-action-adjusted `PX_LAST`; Yahoo stock
returns use adjusted close.

For the schema, Repsol currency considerations and backend configuration, see
[the YAML pack guide](../TradeAgent/docs/factor-yaml-packs.md). Pack definitions are
local configuration; no raw licensed observations or credentials belong in them.

## Troubleshooting

- **Install cannot find a version:** give IT both lockfiles and the failing package/version.
  A package on public PyPI may be absent or blocked in JFrog. Do not silently relax pins.
- **Missing DLL or no matching wheel:** check Python version, Windows architecture and the
  approved Bloomberg runtime. Do not copy macOS wheels or a macOS virtual environment.
- **No packs:** check the configured directory, file validation and `enabled` flags; restart.
- **No data:** distinguish network access, Bloomberg entitlement, identifier mapping and
  insufficient overlapping history. Synthetic demo checks the UI independently.
- **Currency error:** provide a normalized same-currency panel or an authorized FX adapter.
  Changing a label in YAML does not convert a series.
- **Redundant factors:** remove an exact dependency (for example oil, diesel and their
  difference together). Orthogonalization cannot restore missing rank.
- **Old layout or growing plots:** stop older app processes, restart from the current
  checkout and refresh the browser. Charts have bounded heights in the current CSS.
- **Stale results:** changing inputs intentionally clears results; click Run again.


## Fama–French plus Bloomberg factors

With **Configured providers · Bloomberg**, select **Fama–French** and an enabled
Bloomberg energy or MSCI YAML pack together, then deselect unwanted factors. The
backend combines the selected columns before estimation and the data inspector uses
the identical alignment pipeline.

The combined regression is conceptually `stock excess return = Fama–French factors +
index-proxy returns + commodity changes + residual`. French models use USD stock excess
returns, so a local-currency stock is first converted with Bloomberg FX and the French
cash return is then subtracted once. A funded MSCI or sector index is treated as another
USD return factor. Oil, gas, diesel and crack-spread series can instead remain declared
price changes, such as USD/barrel or EUR/MWh; their coefficients retain those economic
units and standardized betas make their relative influence comparable.

All inputs must describe the same daily or monthly interval. Daily data must match both
period endpoints; monthly inputs must represent complete months. Gaps are dropped and
never filled. The app does not rename an index proxy as a Fama–French factor: each series
keeps a unique ID, source, transform, currency and unit. Because style indices often
overlap with Fama–French factors, inspect correlation and VIF before interpreting their
joint betas. Keep the original joint model as the reference and use residualization only
as a secondary attribution view. Use the inspector chart for visual alignment checks and
open **Technical diagnostics** for source-specific coverage losses.

French files still require web access to the official Kenneth French library. Pack-only
studies do not fetch French files. See the backend
[composition guide](../TradeAgent/docs/factor-panel-composition.md) for details.


## Watchlist attribution

Use the **Watchlist attribution** tab for up to 20 Bloomberg equity identifiers,
one per line. Select a source, one or more enabled factor packs, individual factors,
research region, training start, minimum sample and HAC lags. Click **Refresh data
and refit**. Optional 5-minute, 15-minute or hourly refresh runs only while this tab
is active. Every refresh uses the settings currently shown and rebuilds the snapshot;
failed stocks show an unavailable status instead of keeping old numbers.

This first version monitors completed daily observations, not streaming intraday
prices. It excludes the current UTC date. The latest fully aligned day is held out
of estimation. Betas are fitted using the preceding history and the same normalized
factor definitions used for that day’s attribution. Every refresh refits; there is no
persistent model cache and no additional dependency. The browser holds only an
in-memory snapshot. Use the source configuration and entitlements documented above.

Read **As of** before comparing rows: French publication delays and missing vendor
observations can make the aligned date older. `Lagged data` means the row does not
reach the last expected stock session. Synthetic data is explicitly labelled Demo.
Contributions and the intercept are shown in basis points (100 bp = 1%). Actual
may mean an excess return in the study currency, not the stock’s local price move.
Explained equals the intercept plus factor contributions; unexplained is actual
minus explained. Missing values are blank, never zero.

Select a row for its contribution chart, exact transformed factor moves, betas and
model warnings. Adjusted R² and VIF describe the training sample, not daily prediction
accuracy. Strong collinearity can make individual contributions unstable. This tab
uses the original factor basis; use single-stock diagnostics for controlled or
sequential residualization. Do not apply transformed betas to raw factor moves.

This is a contemporaneous model estimate, not causal attribution or a point-in-time
backtest. Revised input history, asynchronous closing times and omitted drivers can
all affect the unexplained return. No ETF proxy is substituted for a French factor.
The MSCI YAML template uses `msci_momentum` so it can coexist with French `momentum`.
