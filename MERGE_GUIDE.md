# Merge the factor research release into company code

This guide is for a company checkout that already contains local Bloomberg adapters,
factor definitions or UI changes. The maintained source is published as two independent
GitHub repositories:

- `https://github.com/MC-Kangan/TradeAgent`
- `https://github.com/MC-Kangan/FactorPlayground`

Clone or merge both repositories and keep the working directories as siblings:

```text
C:\Research\TradeAgent
C:\Research\FactorPlayground
```

Record the exact commit ID from both repositories before changing anything. Do not copy
`.venv`, caches, credentials, Bloomberg responses or licensed observations between machines.

## 1. Protect the working company version

In each existing company repository, commit or stash intentional work and create a merge
branch. If the company repository was cloned from the corresponding public repository,
add it as `upstream` once, fetch it, and merge the required branch:

```powershell
git status
git switch -c integrate-factor-research-release
git remote add upstream https://github.com/MC-Kangan/TradeAgent.git
git fetch upstream
git merge upstream/feature/local-research-framework
```

For FactorPlayground, use its repository URL and merge `upstream/main`. If an `upstream`
remote already exists, verify it with `git remote -v` instead of adding it again.

If the company code does not share Git history with these repositories, do not force an
unrelated-history merge. Clone the public repositories into separate review directories,
compare them with the firm's approved diff tool, and port the reviewed backend contracts
before the UI. Resolve each difference deliberately; avoid adding fallback paths merely to
keep obsolete implementations alive.

## 2. Preserve the ownership boundary

The company version should continue to own:

- Bloomberg Desktop/BPIPE session creation, authentication and entitlement handling.
- Firm-specific Bloomberg identifiers and approved ticker-resolution tables.
- Secret locations, proxy settings, JFrog configuration and internal deployment files.
- Licensed raw data and internal factor definitions that cannot be committed.

The delivered code should own the normalized analytics contract:

- stock and funded-index returns;
- Fama–French research returns and cash series;
- price-change factors such as oil, gas, diesel and crack spreads;
- frequency conversion, FX conversion, common-interval alignment and missingness;
- regression, correlation, collinearity, attribution and report presentation.

Keep source-specific identifiers and vendor fields inside provider configuration or YAML.
Do not put Bloomberg authentication or raw responses into the Dash app.

## 3. Merge the backend first

Treat the factor backend as one coherent change. Merge these areas together rather than
copying only the regression file:

```text
TradeAgent/src/trade_research/domain/factors.py
TradeAgent/src/trade_research/domain/models.py
TradeAgent/src/trade_research/factor_packs.py
TradeAgent/src/trade_research/factor_monitor.py
TradeAgent/src/trade_research/providers/factor_composition.py
TradeAgent/src/trade_research/providers/factor_fx.py
TradeAgent/src/trade_research/providers/factor_returns.py
TradeAgent/src/trade_research/providers/french.py
TradeAgent/src/trade_research/providers/pack_factors.py
TradeAgent/src/trade_research/price_inspection.py
TradeAgent/src/trade_research/skills/factor_*.py
TradeAgent/src/trade_research/reporting.py
TradeAgent/src/trade_research/settings.py
```

Also merge their corresponding `tests/test_factor_*.py` tests and
`tests/test_price_inspection.py`. If the company Bloomberg
adapter differs, adapt it to the delivered typed provider contracts. Keep the company
connection implementation; do not change the analytics layer to understand BLPAPI
sessions or firm-specific response objects.

The important end-to-end contract is:

```text
vendor observations
  -> typed levels/returns with source, field, currency, unit and dates
  -> one normalized factor panel
  -> regression and diagnostics
  -> presentation schema used by FactorPlayground
```

Run the backend checks before merging the UI:

```powershell
cd C:\Research\TradeAgent
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe
.\.venv\Scripts\python.exe -m build
```

If the company environment does not contain development packages, at minimum run the
factor tests and record which quality gates were unavailable. Do not relax package pins
silently; follow `TradeAgent\docs\corporate-installation.md` for JFrog installation.

## 4. Merge the standalone UI

After the backend tests pass, merge these FactorPlayground files:

```text
FactorPlayground/app.py
FactorPlayground/watchlist_ui.py
FactorPlayground/assets/style.css
FactorPlayground/factor_packs/*.yaml
FactorPlayground/requirements.txt
FactorPlayground/requirements-ui.lock
FactorPlayground/test_app.py
FactorPlayground/test_watchlist.py
```

Prefer the delivered `app.py` structure unless the company file contains required
workflow changes. It contains the multi-pack selector, aligned-input inspector,
single-stock view, watchlist view and cross-sectional view as one coordinated callback
graph. Copying isolated callback fragments is likely to leave missing component IDs or
stale callback state.

The simplified single-stock screen has not removed analytical outputs. **Data inspector**
shows native vendor price levels first. **Model inputs and alignment** separately loads the
normalized regression inputs, source definitions, missingness and exact rows. The main view
shows stock-to-factor correlations; the complete factor matrix, VIF, coefficients, residual
tests, model comparisons, orthogonalization and metadata remain under **Technical diagnostics**.

## 5. Reconcile factor YAML files

Do not overwrite verified company tickers with sample placeholders. For each company
pack, compare and retain:

- unique factor ID and human label;
- exact Bloomberg security and field;
- level, return or spread definition;
- log-return or difference transform;
- currency, economic unit and scale;
- enabled state and source requirements.

When combining Fama–French, MSCI and energy packs, factor IDs must be unique. Funded
index returns used with the French model must be in the study currency. Commodity price
changes can retain declared economic units. Never label a series as converted merely by
changing its YAML currency field.

## 6. Verify the integrated product

Install both sibling projects into the same Windows environment as described in the
corporate installation guide, then run:

```powershell
cd C:\Research\FactorPlayground
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe app.py
```

Verify these workflows in order:

1. Synthetic single-stock study and the aligned-input chart.
2. Synthetic watchlist refresh and cross-sectional study.
3. One Bloomberg stock with one Bloomberg factor pack.
4. Fama–French plus one Bloomberg factor pack at monthly frequency.
5. The same mixed study at daily frequency if the official French download is permitted.
6. A local-currency European stock requiring Bloomberg FX conversion.
7. Correlated energy factors, confirming the correlation/VIF warnings and optional
   attribution views.

For every Bloomberg check, compare several retrieved dates and values with the Terminal.
Confirm that the last aligned date, adjustment basis, currency and transform are correct.
Do not accept a successful regression as proof that the inputs are economically correct.

## 7. Complete the merge

Review the final diff for secrets, licensed data and unintended generated files. Commit
the backend and UI repositories separately so each remains independently reviewable.
Record the resulting company commit IDs beside the two upstream commit IDs. Treat that
four-commit record as the reproducible release reference. When pulling future updates,
merge TradeAgent first, run its tests, then merge FactorPlayground and run its tests.

If a test fails, identify whether the mismatch is in the provider boundary, normalized
panel or UI presentation before changing code. Avoid solving a provider mismatch with a
UI workaround or maintaining both old and new analytics paths.
