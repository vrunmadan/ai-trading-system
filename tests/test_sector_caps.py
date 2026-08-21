"""
The two sector limits, and why the tighter one has to win.

  MAX_SECTOR_PCT            portfolio circuit breaker (default 30%). Breaching
                            it halts the ENTIRE cycle, unrelated sectors too.
  MAX_SECTOR_PCT_OF_CAPITAL per-trade ceiling in the Risk Sizer (default 35%).

Configured 35 > 30, the per-trade ceiling was looser than the halt threshold,
which opened a band where the sizer could build a position that halted the
next cycle. The sizer would be manufacturing the outage.
"""

import importlib

import pytest


def _sizer(monkeypatch, per_trade=None, gate=None):
    if per_trade is not None:
        monkeypatch.setenv("MAX_SECTOR_PCT_OF_CAPITAL", str(per_trade))
    if gate is not None:
        monkeypatch.setenv("MAX_SECTOR_PCT", str(gate))
    import risk_sizer.sizer as sz
    return importlib.reload(sz)


def test_effective_cap_is_the_tighter_of_the_two(monkeypatch):
    sz = _sizer(monkeypatch, per_trade=35, gate=30)
    assert sz.EFFECTIVE_SECTOR_PCT == 30.0


def test_a_tighter_per_trade_ceiling_is_respected(monkeypatch):
    """The sizer may be stricter than the gate; that creates no trap."""
    sz = _sizer(monkeypatch, per_trade=20, gate=30)
    assert sz.EFFECTIVE_SECTOR_PCT == 20.0


def test_matching_values_are_used_as_is(monkeypatch):
    sz = _sizer(monkeypatch, per_trade=30, gate=30)
    assert sz.EFFECTIVE_SECTOR_PCT == 30.0


def test_mismatch_is_logged_not_silently_reconciled(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.WARNING)
    _sizer(monkeypatch, per_trade=35, gate=30)
    assert any("Sector cap mismatch" in r.message or "Sector cap mismatch" in r.getMessage()
               for r in caplog.records)


def test_matching_values_log_nothing(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.WARNING)
    _sizer(monkeypatch, per_trade=30, gate=30)
    assert not any("Sector cap mismatch" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# The behaviour that matters
# ---------------------------------------------------------------------------

def _position(sz, sector, amount):
    return sz.OpenPosition(ticker="X", sector=sector, capital_deployed=amount)


class _Signal:
    ticker = "NEWCO"
    sector = "IT"
    confidence_score = 90.0
    direction = "BUY"


def test_sizer_cannot_push_a_sector_past_the_halt_threshold(monkeypatch):
    """
    The core guarantee. With the sector at 29% of capital and a gate at 30%,
    the sizer may add at most 1% — not the 6% the old 35% ceiling allowed.
    """
    sz = _sizer(monkeypatch, per_trade=35, gate=30)
    capital = 1_000_000.0

    decision = sz.size_position(
        signal=_Signal(),
        open_positions=[_position(sz, "IT", 290_000.0)],
        weekly_pnl=0.0,
        capital=capital,
    )

    if decision.approved:
        total_after = 290_000.0 + decision.capital_to_deploy
        assert total_after <= capital * 0.30 + 0.01, (
            f"sizer allowed the IT sector to reach Rs {total_after:,.0f}, past "
            f"the Rs {capital * 0.30:,.0f} point where the portfolio gate halts "
            f"every trade next cycle"
        )


@pytest.mark.parametrize("already_deployed_pct", [25, 28, 29, 30, 32, 34])
def test_no_deployment_level_lets_the_sizer_create_a_halt(
    monkeypatch, already_deployed_pct
):
    """Swept across the old 30-35% trap band."""
    sz = _sizer(monkeypatch, per_trade=35, gate=30)
    capital = 1_000_000.0
    deployed = capital * already_deployed_pct / 100

    decision = sz.size_position(
        signal=_Signal(),
        open_positions=[_position(sz, "IT", deployed)],
        weekly_pnl=0.0,
        capital=capital,
    )

    total_after = deployed + (decision.capital_to_deploy if decision.approved else 0)
    assert total_after <= max(deployed, capital * 0.30) + 0.01


def test_sector_already_over_the_cap_is_rejected(monkeypatch):
    sz = _sizer(monkeypatch, per_trade=35, gate=30)
    decision = sz.size_position(
        signal=_Signal(),
        open_positions=[_position(sz, "IT", 320_000.0)],
        weekly_pnl=0.0,
        capital=1_000_000.0,
    )
    assert decision.approved is False
    assert "sector" in decision.notes.lower()


def test_an_unrelated_sector_is_unaffected(monkeypatch):
    """The clamp is per-sector, not a blanket reduction."""
    sz = _sizer(monkeypatch, per_trade=35, gate=30)
    decision = sz.size_position(
        signal=_Signal(),
        open_positions=[_position(sz, "PHARMA", 290_000.0)],
        weekly_pnl=0.0,
        capital=1_000_000.0,
    )
    assert decision.approved is True
    assert decision.capital_to_deploy > 100_000


@pytest.fixture(autouse=True)
def _restore():
    yield
    import risk_sizer.sizer as sz
    importlib.reload(sz)
