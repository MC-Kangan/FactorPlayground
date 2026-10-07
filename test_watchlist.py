from datetime import date

import pytest
from app import study_context
from watchlist_ui import fetch_watchlist, parse_watchlist


def test_demo_watchlist_and_isolated_failure():
    rows = fetch_watchlist(
        study_context,
        "demo",
        "AAPL US Equity\nMSFT US Equity",
        ["us_etf"],
        ["market", "growth_minus_value", "momentum_minus_market"],
        "US",
        "2022-01-01",
        60,
        3,
        today=date(2025, 1, 10),
    )
    assert len(rows) == 2
    assert all(row["snapshot"] for row in rows)
    for row in rows:
        snap = row["snapshot"]
        assert snap["period_end"] <= "2025-01-09"
        assert snap["model"]["actual_end"] < snap["period_end"]


def test_watchlist_validation():
    with pytest.raises(ValueError):
        parse_watchlist("")
    with pytest.raises(ValueError):
        parse_watchlist("AAPL US Equity\nAAPL US Equity")
    with pytest.raises(ValueError):
        parse_watchlist("\n".join(f"T{i} US Equity" for i in range(21)))


def test_one_failed_stock_does_not_hide_successful_rows():
    def context(source, *controls):
        if controls[0].startswith("BAD"):
            raise RuntimeError("private provider payload")
        return study_context(source, *controls)

    rows = fetch_watchlist(
        context,
        "demo",
        "AAPL US Equity\nBAD US Equity",
        ["us_etf"],
        ["market", "growth_minus_value", "momentum_minus_market"],
        "US",
        "2022-01-01",
        60,
        3,
        today=date(2025, 1, 10),
    )
    assert rows[0]["snapshot"]
    assert rows[1]["status"] == "Unavailable · RuntimeError"
    assert rows[1]["snapshot"] is None
    assert "private provider payload" not in str(rows)


def test_entered_ticker_preserves_explicit_bloomberg_mapping():
    from app import add_entered_bloomberg_mappings

    from trade_research.providers import BloombergReturnMapping, parse_bloomberg_equity_identifier
    from trade_research.settings import Settings

    identity = parse_bloomberg_equity_identifier("TEST LN Equity")
    mapping = BloombergReturnMapping(
        instrument=identity.instrument,
        security=identity.security,
        field="PX_LAST",
        currency="USD",
        return_basis="adjusted_close_return",
    )
    settings = Settings(price_provider="bloomberg", bloomberg_return_mappings=(mapping,))
    updated = add_entered_bloomberg_mappings(settings, (identity,))
    assert updated.bloomberg_return_mappings == (mapping,)


def test_refresh_callback_and_initial_table():
    from app import app

    client = app.server.test_client()
    assert b"wl-table" in client.get("/_dash-layout").data
    key = next(k for k in app.callback_map if "wl-status.children" in k)
    definition = app.callback_map[key]
    values = [
        "watchlist",
        "demo",
        "REP SM Equity\nTTE FP Equity",
        ["french"],
        ["market_excess", "hml"],
        "Europe",
        "2022-01-01",
        60,
        3,
    ]
    response = client.post(
        "/_dash-update-component",
        json={
            "output": key,
            "outputs": [
                {"id": "wl-rows", "property": "data"},
                {"id": "wl-results", "property": "children"},
                {"id": "wl-status", "property": "children"},
            ],
            "inputs": [
                dict(definition["inputs"][0], value=1),
                dict(definition["inputs"][1], value=0),
            ],
            "state": [
                dict(field, value=value)
                for field, value in zip(definition["state"], values, strict=False)
            ],
            "changedPropIds": ["wl-refresh.n_clicks"],
        },
    )
    assert response.status_code == 200
    result = response.get_json()["response"]
    assert "2/2 models available" in result["wl-status"]["children"]
    assert all(row["snapshot"] for row in result["wl-rows"]["data"])


def test_auto_refresh_pauses_when_tab_is_hidden():
    from app import app

    callback = next(
        v["callback"].__wrapped__ for k, v in app.callback_map.items() if "wl-timer.disabled" in k
    )
    assert callback(300000, "watchlist") == (False, 300000)
    assert callback(300000, "single-stock") == (True, 300000)
    assert callback(0, "watchlist") == (True, 300000)
