"""Deterministic synthetic observations; never used for real investment conclusions."""

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
from trade_research.skills.factor_regression import FactorRegressionParameters


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
    rng = np.random.default_rng(73)
    n = len(intervals)
    scale = 0.01 if params.frequency == "daily" else 0.04
    cash = 0.0001 if params.frequency == "daily" else 0.002
    histories = {asset: rng.normal(0, scale, n) for asset in params.benchmarks()}
    research_keys = list(
        dict.fromkeys(f.research_key for f in params.selected_factors() if f.kind == "research")
    )
    research = {key: rng.normal(0, scale, n) for key in research_keys}
    target = rng.normal(0.0002, scale / 5, n)
    for i, f in enumerate(params.selected_factors()):
        if f.kind == "research":
            column = research[f.research_key]
        elif f.kind == "spread":
            column = histories[f.instrument] - histories[f.short_instrument]
        else:
            column = histories[f.instrument] - (
                cash if params.return_mode == "excess_return" else 0
            )
        target += (1.2 / (i + 1)) * column
    if request.instrument in histories:
        raise ValueError("Target must differ from demo benchmarks.")
    histories[request.instrument] = target + (cash if params.return_mode == "excess_return" else 0)
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
            research = {"market_excess": rng.normal(0, scale, n)}
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
