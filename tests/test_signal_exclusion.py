"""
One slot per cycle must not be monopolised by a ticker that already fired.

2026-09-23: indicators are built on COMPLETED daily bars (since 2026-09-22),
so GABRIEL saw identical inputs every hour, won the single per-cycle slot 7
times, and IKS (69-72%) / SYRMA (75%) cleared the bar but were never sent.
Tickers already signalled today, or already held, are now excluded from
competing and logged to cycle_log instead.
"""

import os
import sqlite3
import tempfile

import pytest


@pytest.fixture()
def ledger(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    import ledger.db as db
    monkeypatch.setattr(db, "DB_PATH", path)
    schema = os.path.join(os.path.dirname(db.__file__), "schema.sql")
    with sqlite3.connect(path) as conn:
        with open(schema) as f:
            conn.executescript(f.read())
    yield db
    try:
        os.unlink(path)
    except OSError:
        pass


def _signal(ticker="GABRIEL"):
    from researcher.regime_classifier import Regime
    from researcher.signal_generator import TradeSignal
    return TradeSignal(
        ticker=ticker, sector="AUTOMOBILE", exchange="NSE", regime=Regime.SIDEWAYS,
        strategy_bucket="bb_squeeze_break", direction="BUY", technical_score=82,
        fundamental_score=74, confidence_score=79, rationale="x",
    )


def _sizing():
    from risk_sizer.sizer import SizingDecision
    return SizingDecision(approved=True, capital_to_deploy=100_000.0, quantity=10, notes="")


# ---------------------------------------------------------------- ledger ----

def test_signalled_today_includes_any_status(ledger):
    ledger.log_signal(_signal("GABRIEL"), _sizing(), status="PENDING")
    ledger.log_signal(_signal("IKS"), _sizing(), status="QC_BLOCKED")
    assert ledger.get_tickers_signalled_today() == {"GABRIEL", "IKS"}


def test_signalled_today_ignores_earlier_days(ledger):
    sid = ledger.log_signal(_signal("TBOTEK"), _sizing(), status="NO_RESPONSE")
    with ledger.get_db() as conn:
        conn.execute("UPDATE signals SET created_at='2026-09-22 14:19:05' WHERE id=?", (sid,))
    assert "TBOTEK" not in ledger.get_tickers_signalled_today()


# ------------------------------------------------------- generate_signal ----

@pytest.fixture()
def scan_env(monkeypatch):
    """Two tickers that both pass every gate; Claude scores GABRIEL higher."""
    import ledger.db as db
    import researcher.signal_generator as sg
    import trader.kite_client as kc
    import fundamentals.screener_public as sp
    from universe.loader import UniverseEntry

    class FakeKite:
        def instruments(self, exch):
            if exch != "NSE":
                return []
            return [{"tradingsymbol": t, "instrument_token": i, "instrument_type": "EQ"}
                    for i, t in enumerate(["GABRIEL", "IKS"], start=1)]

        def historical_data(self, *a, **k):
            return [{}] * 40

    monkeypatch.setattr(kc, "get_kite_client", lambda: FakeKite())
    monkeypatch.setattr(kc, "drop_incomplete_today_bar", lambda h, **k: h)
    monkeypatch.setattr(sg, "load_universe", lambda: [
        UniverseEntry("GABRIEL", "Gabriel", "AUTOMOBILE", 0, "NSE", ""),
        UniverseEntry("IKS", "IKS", "HEALTHCARE SERVICES", 0, "NSE", ""),
    ])
    monkeypatch.setattr(sg, "_compute_indicators", lambda h, t: {"ticker": t})
    monkeypatch.setattr(sg, "_passes_prefilter", lambda name, ind: True)
    monkeypatch.setattr(sp, "get_fundamentals_cached", lambda t: None)
    monkeypatch.setattr(sg, "_fetch_qualitative_context", lambda **k: "")
    scores = {"GABRIEL": 80, "IKS": 72}
    claude_calls = []

    def fake_claude(regime, strategy, ind, **k):
        claude_calls.append(ind["ticker"])
        return {"verdict": "TRADE", "confidence_score": scores[ind["ticker"]],
                "technical_score": 80, "fundamental_score": 70, "rationale": "ok"}

    monkeypatch.setattr(sg, "_call_claude", fake_claude)
    logged = []
    monkeypatch.setattr(db, "log_cycle_evaluation", lambda **k: logged.append(k))
    monkeypatch.setattr(sg, "MIN_CONFIDENCE", 65.0)
    return sg, claude_calls, logged


def _reading():
    from researcher.regime_classifier import Regime, RegimeReading
    import dataclasses
    fields = {f.name for f in dataclasses.fields(RegimeReading)}
    kw = {"regime": Regime.SIDEWAYS, "confidence": 52.0}
    for f in fields - set(kw):
        kw[f] = ""  # rationale etc.
    return RegimeReading(**kw)


def test_without_exclusions_the_top_scorer_wins(scan_env):
    sg, _, _ = scan_env
    assert sg.generate_signal(_reading()).ticker == "GABRIEL"


def test_already_signalled_ticker_yields_slot_to_next_best(scan_env):
    sg, claude_calls, logged = scan_env
    sig = sg.generate_signal(_reading(), exclude_tickers={"GABRIEL": "SKIP_SIGNALLED_TODAY"})
    assert sig is not None and sig.ticker == "IKS"
    assert "GABRIEL" not in claude_calls          # no wasted LLM call
    skips = [r for r in logged if r["ticker"] == "GABRIEL"]
    assert skips and all(r["verdict"] == "SKIP_SIGNALLED_TODAY" for r in skips)


def test_all_excluded_means_no_signal(scan_env):
    sg, _, _ = scan_env
    ex = {"GABRIEL": "SKIP_SIGNALLED_TODAY", "IKS": "SKIP_ALREADY_HELD"}
    assert sg.generate_signal(_reading(), exclude_tickers=ex) is None


# -------------------------------------------------------------- run_cycle ----

def test_run_cycle_passes_signalled_and_held_tickers(ledger, monkeypatch):
    import main
    import risk_manager.portfolio_risk as pr
    import researcher.regime_classifier as rc
    import researcher.signal_generator as sg
    from researcher.regime_classifier import Regime

    class Status:
        approved = True
        halt_reason = ""
        advisory_flags = []

    class Reading:
        regime = Regime.SIDEWAYS
        confidence = 80.0
        rationale = "x"

    monkeypatch.setattr(pr, "check_portfolio_risk", lambda *a, **k: Status())
    monkeypatch.setattr(rc, "classify_regime", lambda *a, **k: Reading())
    monkeypatch.setattr(ledger, "get_open_positions",
                        lambda mode=None: [{"ticker": "RBLBANK", "entry_price": 400,
                                            "quantity": 10, "exchange": "NSE"}])
    monkeypatch.setattr(main, "_mark_to_market_value", lambda *a, **k: None)
    ledger.log_signal(_signal("GABRIEL"), _sizing(), status="NO_RESPONSE")

    seen = {}

    def fake_generate(reading, exclude_tickers=None):
        seen.update(exclude_tickers or {})
        return None

    monkeypatch.setattr(sg, "generate_signal", fake_generate)
    main.run_cycle()
    assert seen == {"GABRIEL": "SKIP_SIGNALLED_TODAY", "RBLBANK": "SKIP_ALREADY_HELD"}
