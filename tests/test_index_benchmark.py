"""Buy-and-hold index benchmark: same capital, same start date, never invented."""
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import date, datetime

import pytest

from benchmark import index_benchmark as ib


@pytest.fixture()
def ledger(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    import ledger.db as db
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("TOTAL_CAPITAL_INR", "1000000")
    monkeypatch.delenv("BENCHMARK_START_DATE", raising=False)
    with closing(sqlite3.connect(path)) as conn:
        with open(os.path.join(os.path.dirname(db.__file__), "schema.sql")) as f:
            conn.executescript(f.read())
    yield db
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def _trade(db, ticker, qty, entry, entry_time, exit_price=None, exit_time=None,
           pnl=None, mode="PAPER", fill="CONFIRMED"):
    with closing(sqlite3.connect(db.DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO trades (ticker, direction, quantity, entry_price, entry_time, "
            "exit_price, exit_time, pnl, mode, exchange, fill_status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ticker, "BUY", qty, entry, entry_time, exit_price, exit_time, pnl,
             mode, "NSE", fill),
        )
        conn.commit()


class FakeKite:
    def __init__(self, closes, has_index=True):
        self.closes = closes          # list of (date, close)
        self.has_index = has_index
        self.calls = 0

    def instruments(self, exch):
        if not self.has_index:
            return []
        return [{"tradingsymbol": "NIFTY SMLCAP 250", "segment": "INDICES",
                 "instrument_type": "EQ", "instrument_token": 999}]

    def historical_data(self, token, f, t, interval, continuous=False, oi=False):
        self.calls += 1
        assert token == 999
        lo = datetime.strptime(f, "%Y-%m-%d").date()
        hi = datetime.strptime(t, "%Y-%m-%d").date()
        return [{"date": datetime(d.year, d.month, d.day), "close": c}
                for d, c in self.closes if lo <= d <= hi]


CLOSES = [(date(2026, 10, 1), 15000.0), (date(2026, 10, 5), 15300.0),
          (date(2026, 10, 12), 14700.0), (date(2026, 10, 22), 16500.0)]


def test_no_trades_yet(ledger):
    r = ib.compute_benchmark("PAPER", kite=FakeKite(CLOSES), today=date(2026, 10, 22))
    assert r == {"status": "NO_TRADES"}
    assert "no trades yet" in ib.format_benchmark_block(r)


def test_book_behind_index(ledger):
    # First entry on 1 Oct; two closed losers worth -23,427 in total.
    _trade(ledger, "SONACOMS", 1000, 828.67, "2026-10-01 10:15:00",
           765.90, "2026-10-21 15:35:00", -11676.0)
    _trade(ledger, "GREAVESCOT", 1000, 211.0, "2026-10-05 10:15:00",
           193.70, "2026-10-21 15:35:00", -11751.0)
    r = ib.compute_benchmark("PAPER", kite=FakeKite(CLOSES), ltp_fn=lambda t, e: 0,
                             today=date(2026, 10, 22))
    assert r["status"] == "OK"
    assert r["start_date"] == "2026-10-01" and r["base_close"] == 15000.0
    assert r["book_value"] == pytest.approx(976573.0)
    assert r["bench_value"] == pytest.approx(1_100_000.0)   # 16500/15000
    assert r["bench_return_pct"] == pytest.approx(10.0)
    assert r["edge_inr"] == pytest.approx(976573.0 - 1_100_000.0)
    assert r["bench_max_drawdown_pct"] == pytest.approx((14700 - 15300) / 15300 * 100)
    block = ib.format_benchmark_block(r)
    assert "BEHIND" in block and "NIFTY SMLCAP 250" in block


def test_open_position_marked_to_market(ledger):
    _trade(ledger, "ABC", 100, 500.0, "2026-10-01 10:15:00")
    r = ib.compute_benchmark("PAPER", kite=FakeKite(CLOSES), ltp_fn=lambda t, e: 600.0,
                             today=date(2026, 10, 22))
    assert r["book_value"] == pytest.approx(1_010_000.0)
    assert r["unpriced_positions"] == 0


def test_unpriced_position_carried_at_cost_and_flagged(ledger):
    _trade(ledger, "ABC", 100, 500.0, "2026-10-01 10:15:00")
    def boom(t, e):
        raise RuntimeError("no token")
    r = ib.compute_benchmark("PAPER", kite=FakeKite(CLOSES), ltp_fn=boom,
                             today=date(2026, 10, 22))
    assert r["book_value"] == pytest.approx(1_000_000.0)
    assert r["unpriced_positions"] == 1
    assert "carried at cost" in ib.format_benchmark_block(r)


def test_not_executed_trades_do_not_set_start(ledger):
    _trade(ledger, "DEAD", 10, 100.0, "2026-09-01 10:00:00", fill="NOT_EXECUTED")
    _trade(ledger, "ABC", 10, 100.0, "2026-10-05 10:00:00")
    assert ib.get_book_start_date("PAPER") == date(2026, 10, 5)


def test_live_trades_do_not_leak_into_paper(ledger):
    _trade(ledger, "LIVE1", 10, 100.0, "2026-09-01 10:00:00", mode="LIVE")
    assert ib.get_book_start_date("PAPER") is None


def test_baseline_is_frozen(ledger):
    _trade(ledger, "ABC", 10, 100.0, "2026-10-01 10:00:00",
           110.0, "2026-10-02 10:00:00", 100.0)
    ib.compute_benchmark("PAPER", kite=FakeKite(CLOSES), ltp_fn=lambda t, e: 0,
                         today=date(2026, 10, 22))
    # History later revised; the frozen baseline must not move.
    revised = [(date(2026, 10, 1), 99999.0)] + CLOSES[1:]
    r = ib.compute_benchmark("PAPER", kite=FakeKite(revised), ltp_fn=lambda t, e: 0,
                             today=date(2026, 10, 22))
    assert r["base_close"] == 15000.0
    stored = json.loads(ledger.kv_get(ib._baseline_key("PAPER"))[0])
    assert stored["base_date"] == "2026-10-01"


def test_start_on_holiday_uses_next_trading_close(ledger):
    _trade(ledger, "ABC", 10, 100.0, "2026-10-03 10:00:00")   # no bar on 3 Oct
    r = ib.compute_benchmark("PAPER", kite=FakeKite(CLOSES), ltp_fn=lambda t, e: 100.0,
                             today=date(2026, 10, 22))
    assert r["base_date"] == "2026-10-05" and r["base_close"] == 15300.0


def test_missing_index_shows_no_number(ledger):
    _trade(ledger, "ABC", 10, 100.0, "2026-10-01 10:00:00")
    r = ib.compute_benchmark("PAPER", kite=FakeKite(CLOSES, has_index=False),
                             ltp_fn=lambda t, e: 100.0, today=date(2026, 10, 22))
    assert r["status"] == "NO_INDEX"
    block = ib.format_benchmark_block(r)
    assert "Unavailable" in block and "Rs" not in block


def test_kite_failure_never_raises(ledger):
    _trade(ledger, "ABC", 10, 100.0, "2026-10-01 10:00:00")
    class Broken(FakeKite):
        def historical_data(self, *a, **k):
            raise RuntimeError("Incorrect `api_key` or `access_token`.")
    r = ib.compute_benchmark("PAPER", kite=Broken(CLOSES), ltp_fn=lambda t, e: 100.0,
                             today=date(2026, 10, 22))
    assert r["status"] == "ERROR" and "Unavailable" in ib.format_benchmark_block(r)


def test_daily_snapshot_saved_once_per_day(ledger):
    _trade(ledger, "ABC", 10, 100.0, "2026-10-01 10:00:00")
    for _ in range(2):
        ib.compute_benchmark("PAPER", kite=FakeKite(CLOSES), ltp_fn=lambda t, e: 100.0,
                             today=date(2026, 10, 22))
    with closing(sqlite3.connect(ledger.DB_PATH)) as conn:
        rows = conn.execute("SELECT day, bench_value FROM benchmark_daily").fetchall()
    assert rows == [("2026-10-22", pytest.approx(1_100_000.0))]


def test_start_date_override(ledger, monkeypatch):
    monkeypatch.setenv("BENCHMARK_START_DATE", "2026-10-12")
    assert ib.get_book_start_date("PAPER") == date(2026, 10, 12)


def test_daily_summary_includes_benchmark_and_survives_its_failure(ledger, monkeypatch):
    import alerts.gmail_alert as ga
    sent = []
    monkeypatch.setattr(ga, "send_plain_email", lambda subject, body: sent.append(body))
    monkeypatch.setattr(ib, "compute_benchmark", lambda mode: {
        "status": "OK", "mode": mode, "symbol": "NIFTY SMLCAP 250",
        "start_date": "2026-10-01", "base_date": "2026-10-01", "base_close": 15000.0,
        "index_date": "2026-10-22", "index_close": 16500.0, "capital": 1e6,
        "book_value": 976573.0, "book_return_pct": -2.3427, "book_drawdown_pct": -3.2,
        "bench_value": 1.1e6, "bench_return_pct": 10.0, "bench_max_drawdown_pct": -3.9,
        "unpriced_positions": 0, "edge_pct_points": -12.3427, "edge_inr": -123427.0})
    ga.send_daily_cycle_summary()
    assert "buy-and-hold NIFTY SMLCAP 250" in sent[-1] and "BEHIND" in sent[-1]

    def boom(mode):
        raise RuntimeError("x")
    monkeypatch.setattr(ib, "compute_benchmark", boom)
    ga.send_daily_cycle_summary()
    assert len(sent) == 2   # summary still sent
