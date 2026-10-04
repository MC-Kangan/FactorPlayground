"""Deterministic synthetic observations; never used for real investment conclusions."""

import hashlib
from datetime import UTC, datetime, timedelta

import numpy as np

from trade_research.domain.factors import FRENCH_DEFINITIONS
from trade_research.domain.models import (
    FactorReturnPoint,
    FactorReturnSeries,
    ResearchFactorPanel,
    ResearchFactorPoint,
)
from trade_research.skills.factor_data import month_end, session_dates
from trade_research.skills.cross_sectional_signal import CrossSectionalSignalParameters
from trade_research.skills.factor_regression import FactorRegressionParameters


def innovations(key, labels, frequency):
    """Stable per-series, per-period draws, independent of requested model or date range."""
    return np.array(
        [
            np.random.default_rng(
                int.from_bytes(
                    hashlib.sha256(f"{frequency}|{key}|{day.isoformat()}".encode()).digest()[:8],
                    "big",
                )
            ).standard_normal()
            for day in labels
        ]
    )


def demo_request(request):
    params = FactorRegressionParameters.model_validate(
        request.skill_parameters["factor-regression"]
    )
    start, end = params.start_date, params.end_date
    dates = session_dates(
        params.stock_calendar or request.instrument.market, start - timedelta(days=40), end
    )
    if params.frequency == "monthly":
        closes = {month_end(d): d for d in dates if month_end(d) <= end}
        values = list(closes.items())
        intervals = [
            (label, values[i - 1][1], close)
            for i, (label, close) in enumerate(values)
            if i and start <= label <= end
        ]
    else:
        intervals = [
            (b, a, b) for a, b in zip(dates[:-1], dates[1:], strict=False) if start <= b <= end
        ]
    labels = [label for label, _, _ in intervals]
    scale = 0.01 if params.frequency == "daily" else 0.04
    cash = 0.0001 if params.frequency == "daily" else 0.002
    # A fixed synthetic economy: model selection only chooses which inputs to expose.
    research = {
        d.id: scale * innovations("research:" + d.id, labels, params.frequency)
        for d in FRENCH_DEFINITIONS
    }
    research_keys = list(
        dict.fromkeys(f.research_key for f in params.selected_factors() if f.kind == "research")
    )
    if request.instrument in params.benchmarks():
        raise ValueError("Target must differ from demo benchmarks.")
    histories = {
        asset: cash
        + 1.2 * research["market_excess"]
        + 0.4 * research["hml"]
        + 0.2 * research["momentum"]
        + 0.4
        * scale
        * innovations(f"asset:{asset.market}:{asset.symbol}", labels, params.frequency)
        for asset in (request.instrument, *params.benchmarks())
    }
    series = tuple(
        FactorReturnSeries(
            instrument=asset,
            frequency=params.frequency,
            calendar=params.stock_calendar or request.instrument.market,
            currency="USD",
            source="fixture",
            return_basis="gross_total_return",
            vendor_field="TOTAL_RETURN",
            retrieved_at=datetime.now(UTC),
            points=tuple(
                FactorReturnPoint(start_date=a, end_date=b, value=float(v))
                for (_, a, b), v in zip(intervals, row, strict=True)
            ),
        )
        for asset, row in histories.items()
    )
    update = {"factor_series": series}
    if research_keys or params.return_mode == "excess_return":
        definitions = {d.id: d for d in FRENCH_DEFINITIONS}
        # Unknown custom research factors require actual provider metadata, not invented semantics.
        if any(key not in definitions for key in research_keys):
            raise ValueError("Demo research keys must come from the French catalog.")
        if not research_keys:
            research_keys = ["market_excess"]
        update["research_factors"] = ResearchFactorPanel(
            region=params.region,
            frequency=params.frequency,
            currency="USD",
            calendar=params.stock_calendar or request.instrument.market,
            source="fixture",
            definitions=tuple(definitions[key] for key in research_keys),
            retrieved_at=datetime.now(UTC),
            reference="sha256:" + "0" * 64,
            points=tuple(
                ResearchFactorPoint(
                    date=label,
                    risk_free=cash,
                    values={key: float(research[key][i]) for key in research_keys},
                )
                for i, (label, _, _) in enumerate(intervals)
            ),
        )
    return request.model_copy(update=update)


def cross_section_demo_request(request):
    """Attach stable daily total returns for a cross-sectional learning run."""
    params = CrossSectionalSignalParameters.model_validate(
        request.skill_parameters["cross-sectional-signal"]
    )
    start, end = params.start_date, params.end_date
    assert start is not None and end is not None
    dates = session_dates("US", start - timedelta(days=400), end)
    series = []
    count = len(request.portfolio_instruments)
    for index, asset in enumerate(request.portfolio_instruments):
        rank_effect = (index - (count - 1) / 2) * 0.000035
        noise = 0.0008 * innovations(
            f"cross-section:{asset.market}:{asset.symbol}", dates[1:], "daily"
        )
        market = 0.00015 + 0.0005 * innovations("cross-section:market", dates[1:], "daily")
        returns = np.clip(market + rank_effect + noise, -0.2, 0.2)
        series.append(
            FactorReturnSeries(
                instrument=asset,
                frequency="daily",
                calendar="US",
                currency="USD",
                source="fixture",
                return_basis="gross_total_return",
                vendor_field="TOTAL_RETURN",
                retrieved_at=datetime.now(UTC),
                points=tuple(
                    FactorReturnPoint(start_date=a, end_date=b, value=float(value))
                    for a, b, value in zip(dates[:-1], dates[1:], returns, strict=True)
                ),
            )
        )
    return request.model_copy(update={"factor_series": tuple(series)})
