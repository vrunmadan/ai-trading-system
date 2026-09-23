"""Weekly discovery list (universe/discovery.py) + its merge into the scan."""

import json
import os
import sqlite3
import tempfile

import pytest

import universe.discovery as disc
from universe.loader import UniverseEntry


class FakeKite:
    """Five candidates outside the core universe, each failing a different stage."""

    INSTR = [
        {"tradingsymbol": "GOODCO", "instrument_token": 1, "instrument_type": "EQ", "segment": "NSE", "name": "GOOD CO"},
        {"tradingsymbol": "BAND5", "instrument_token": 2, "instrument_type": "EQ", "segment": "NSE", "name": "B"},
        {"tradingsymbol": "THIN", "instrument_token": 3, "instrument_type": "EQ", "segment": "NSE", "name": "T"},
        {"tradingsymbol": "PLEDGED", "instrument_token": 4, "instrument_type": "EQ", "segment": "NSE", "name": "P"},
        {"tradingsymbol": "NODATA", "instrument_token": 5, "instrument_type": "EQ", "segment": "NSE", "name": "N"},
        {"tradingsymbol": "CORE1", "instrument_token": 6, "instrument_type": "EQ", "segment": "NSE", "name": "C"},
        {"tradingsymbol": "SMEX-SM", "instrument_token": 7, "instrument_type": "EQ", "segment": "NSE", "name": "S"},
        {"tradingsymbol": "RELIANCE", "instrument_token": 8, "instrument_type": "EQ", "segment": "NSE", "name": "R"},
    ]

    def instruments(self, exch):
        return self.INSTR

    def quote(self, keys):
        out = {}
        for k in keys:
            sym = k.split(":")[1]
            band = 0.05 if sym == "BAND5" else 0.10
            vol = 100 if sym == "THIN" else 1_000_000
            out[k] = {"last_price": 100.0, "volume": vol, "ohlc": {"close": 100.0},
                      "upper_circuit_limit": 100 * (1 + band), "lower_circuit_limit": 100 * (1 - band)}
        return out

    def historical_data(self, token, f, t, interval):
        return [{"volume": 1_000_000, "close": 100.0}] * 20   # ₹10 Cr/day


FUND = {
    "GOODCO": {"market_cap_cr": 2000, "roce": 28.0, "sales_growth_3yr": 30.0,
               "promoter_holding": 60.0, "promoter_pledge_pct": 0.0, "broad_sector": "Capital Goods"},
    "PLEDGED": {"market_cap_cr": 2000, "roce": 28.0, "sales_growth_3yr": 30.0,
                "promoter_holding": 60.0, "promoter_pledge_pct": 45.0, "broad_sector": "Services"},
    "NODATA": None,
}


@pytest.fixture()
def env(monkeypatch):
    import universe.loader as ul
    monkeypatch.setattr(ul, "load_universe", lambda: [
        UniverseEntry("CORE1", "Core", "IT", 0, "NSE", "")])
    monkeypatch.setattr(disc, "_load_largecaps", lambda: {"RELIANCE"})


def _run():
    return disc.build_discovery_list(FakeKite(), fetch_fundamentals=lambda s: FUND.get(s),
                                     sleep=lambda s: None, min_turnover_inr=3e7)


def test_only_the_clean_name_survives(env):
    r = _run()
    assert [e["ticker"] for e in r["entries"]] == ["GOODCO"]
    e = r["entries"][0]
    assert e["sector"] == "CAPITAL GOODS" and e["notes"] == "Discovery"


def test_funnel_counts_explain_every_drop(env):
    st = _run()["stats"]
    assert st["listed_outside_core"] == 5        # core, large cap and SME suffix excluded
    assert st["circuit_band_ok"] == 4            # 5% band dropped
    assert st["turnover_prescreen_ok"] == 3      # THIN dropped
    assert st["fundamentals_ok"] == 1            # PLEDGED + NODATA dropped
    assert st["rejections"]["no fundamentals data"] == 1


def test_quality_bar_is_fail_closed():
    assert disc._quality_ok({"market_cap_cr": 2000, "sales_growth_3yr": 20}, "IT")[0] is False
    assert disc._quality_ok({"market_cap_cr": 2000, "roce": 30}, "IT")[0] is False


def test_quality_bar_uses_roe_for_financials():
    f = {"market_cap_cr": 2000, "roce": 6.0, "roe": 16.0, "sales_growth_3yr": 20}
    assert disc._quality_ok(f, "FINANCIAL SERVICES")[0] is True
    assert disc._quality_ok(f, "IT")[0] is False


def test_market_cap_window():
    base = {"roce": 30, "sales_growth_3yr": 20}
    assert disc._quality_ok({**base, "market_cap_cr": 100}, "IT")[0] is False
    assert disc._quality_ok({**base, "market_cap_cr": 90000}, "IT")[0] is False


def test_circuit_band():
    q = {"upper_circuit_limit": 110, "lower_circuit_limit": 90, "last_price": 100, "ohlc": {"close": 100}}
    assert round(disc._circuit_band_pct(q)) == 10
    assert disc._circuit_band_pct({}) is None


# ---------------------------------------------------------- scan merge ----

@pytest.fixture()
def ledger(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    import ledger.db as db
    monkeypatch.setattr(db, "DB_PATH", path)
    with sqlite3.connect(path) as conn, open(os.path.join(os.path.dirname(db.__file__), "schema.sql")) as f:
        conn.executescript(f.read())
    yield db
    os.unlink(path)


def test_scan_universe_merges_discovery_after_core(ledger, monkeypatch):
    import universe.loader as ul
    monkeypatch.setattr(ul, "load_universe", lambda: [UniverseEntry("CORE1", "C", "IT", 0, "NSE", "")])
    ledger.kv_set(disc.KV_KEY, json.dumps({"entries": [
        {"ticker": "GOODCO", "company": "Good", "sector": "CAPITAL GOODS", "market_cap_cr": 2000},
        {"ticker": "CORE1", "sector": "X"},          # duplicate of core -> ignored
    ]}))
    scan = ul.load_scan_universe()
    assert [e.ticker for e in scan] == ["CORE1", "GOODCO"]
    assert scan[1].sector == "CAPITAL GOODS" and scan[1].notes == "Discovery"


def test_scan_universe_can_be_disabled(ledger, monkeypatch):
    import universe.loader as ul
    monkeypatch.setattr(ul, "load_universe", lambda: [UniverseEntry("CORE1", "C", "IT", 0, "NSE", "")])
    ledger.kv_set(disc.KV_KEY, json.dumps({"entries": [{"ticker": "GOODCO"}]}))
    monkeypatch.setenv("DISCOVERY_ENABLED", "false")
    assert [e.ticker for e in ul.load_scan_universe()] == ["CORE1"]


def test_scan_universe_without_a_list_is_core_only(ledger, monkeypatch):
    import universe.loader as ul
    monkeypatch.setattr(ul, "load_universe", lambda: [UniverseEntry("CORE1", "C", "IT", 0, "NSE", "")])
    assert [e.ticker for e in ul.load_scan_universe()] == ["CORE1"]


def test_run_and_email_keeps_last_list_on_empty_result(ledger, monkeypatch):
    ledger.kv_set(disc.KV_KEY, json.dumps({"entries": [{"ticker": "OLDCO"}]}))
    monkeypatch.setattr(disc, "build_discovery_list", lambda k: {"entries": [], "stats": {}})
    import trader.kite_client as kc
    import alerts.gmail_alert as ga
    monkeypatch.setattr(kc, "get_kite_client", lambda: object())
    sent = []
    monkeypatch.setattr(ga, "send_plain_email", lambda s, b: sent.append(s))
    disc.run_and_email()
    assert [e["ticker"] for e in disc.load_discovery_entries()] == ["OLDCO"]
    assert "kept last week" in sent[0]
