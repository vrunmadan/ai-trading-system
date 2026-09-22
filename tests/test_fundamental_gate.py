"""
Tests for the real-data fundamental quality gate in researcher/signal_generator.py
(_passes_fundamental_gate, _best_available_profit_growth, _format_fundamentals_block).

Design being tested (2026-09-22): this gate is deliberately SEPARATE from both
the technical prefilter (_passes_prefilter) and Claude's own fundamental_score
-- it runs on real Screener.in ratios (ROCE, debt/equity, growth, promoter
holding), before Claude is even called, and blocks a ticker outright on real
numbers rather than blending them into one composite score.

Thresholds are monkeypatched directly onto the module's FUND_* constants in
each test, rather than relying on whatever the current defaults are, so
these tests stay correct if the defaults are ever retuned.
"""

import researcher.signal_generator as sg


class TestBestAvailableProfitGrowth:
    def test_prefers_3yr_over_5yr(self):
        result = sg._best_available_profit_growth(
            {"profit_growth_3yr": 12.0, "profit_growth_5yr": 30.0}
        )
        assert result == 12.0

    def test_falls_back_to_5yr_when_3yr_missing(self):
        result = sg._best_available_profit_growth({"profit_growth_5yr": 30.0})
        assert result == 30.0

    def test_none_when_neither_present(self):
        assert sg._best_available_profit_growth({}) is None

    def test_none_when_3yr_explicitly_none(self):
        # parse_fundamentals_page() never stores explicit None values (it
        # drops them), but this guards the helper's own contract anyway.
        result = sg._best_available_profit_growth(
            {"profit_growth_3yr": None, "profit_growth_5yr": 8.0}
        )
        assert result == 8.0


class TestPassesFundamentalGate:
    def setup_method(self):
        # Fix thresholds explicitly so this test doesn't silently change
        # behaviour if the real defaults are retuned later.
        self._orig = (
            sg.FUND_MIN_ROCE,
            sg.FUND_MAX_DEBT_EQUITY,
            sg.FUND_MIN_PROFIT_GROWTH,
            sg.FUND_MIN_PROMOTER_HOLDING,
        )
        sg.FUND_MIN_ROCE = 10.0
        sg.FUND_MAX_DEBT_EQUITY = 1.5
        sg.FUND_MIN_PROFIT_GROWTH = -15.0
        sg.FUND_MIN_PROMOTER_HOLDING = 25.0

    def teardown_method(self):
        (
            sg.FUND_MIN_ROCE,
            sg.FUND_MAX_DEBT_EQUITY,
            sg.FUND_MIN_PROFIT_GROWTH,
            sg.FUND_MIN_PROMOTER_HOLDING,
        ) = self._orig

    def _good_fundamentals(self, **overrides):
        base = {
            "roce": 18.0,
            "debt_to_equity": 0.4,
            "profit_growth_3yr": 15.0,
            "promoter_holding": 55.0,
        }
        base.update(overrides)
        return base

    def test_none_fundamentals_fails_open(self):
        ok, reason = sg._passes_fundamental_gate(None)
        assert ok is True
        assert "skipped" in reason

    def test_empty_dict_fails_open(self):
        ok, reason = sg._passes_fundamental_gate({})
        assert ok is True
        assert "skipped" in reason

    def test_all_metrics_passing(self):
        ok, reason = sg._passes_fundamental_gate(self._good_fundamentals())
        assert ok is True
        assert reason == "passed"

    def test_low_roce_rejects(self):
        ok, reason = sg._passes_fundamental_gate(self._good_fundamentals(roce=4.0))
        assert ok is False
        assert "ROCE" in reason

    def test_high_debt_equity_rejects(self):
        ok, reason = sg._passes_fundamental_gate(
            self._good_fundamentals(debt_to_equity=3.2)
        )
        assert ok is False
        assert "debt/equity" in reason

    def test_deeply_negative_growth_rejects(self):
        ok, reason = sg._passes_fundamental_gate(
            self._good_fundamentals(profit_growth_3yr=-40.0)
        )
        assert ok is False
        assert "profit growth" in reason

    def test_low_promoter_holding_rejects(self):
        ok, reason = sg._passes_fundamental_gate(
            self._good_fundamentals(promoter_holding=8.0)
        )
        assert ok is False
        assert "promoter holding" in reason

    def test_multiple_failures_all_listed(self):
        ok, reason = sg._passes_fundamental_gate(
            self._good_fundamentals(roce=2.0, debt_to_equity=5.0)
        )
        assert ok is False
        assert "ROCE" in reason
        assert "debt/equity" in reason
        assert reason.count(";") == 1  # exactly two reasons joined

    def test_missing_individual_field_is_not_a_failure(self):
        # Screener doesn't expose every field for every company (e.g. some
        # thinly-covered names have no growth table). A field simply absent
        # from the dict must never count as a violation.
        data = {"roce": 18.0}  # debt_to_equity, growth, promoter all absent
        ok, reason = sg._passes_fundamental_gate(data)
        assert ok is True
        assert reason == "passed"

    def test_boundary_values_pass(self):
        # Exactly at the floor/ceiling should pass (checks use strict < / >).
        ok, reason = sg._passes_fundamental_gate(
            self._good_fundamentals(
                roce=10.0, debt_to_equity=1.5,
                profit_growth_3yr=-15.0, promoter_holding=25.0,
            )
        )
        assert ok is True


class TestFormatFundamentalsBlock:
    def test_none_returns_empty_string(self):
        assert sg._format_fundamentals_block(None) == ""

    def test_empty_dict_returns_empty_string(self):
        assert sg._format_fundamentals_block({}) == ""

    def test_full_data_includes_all_sections(self):
        block = sg._format_fundamentals_block({
            "roce": 18.5,
            "roe": 16.2,
            "pe": 22.1,
            "debt_to_equity": 0.42,
            "sales_growth_3yr": 12.0,
            "profit_growth_3yr": 15.5,
            "promoter_holding": 55.3,
            "promoter_holding_change_yoy": -1.2,
        })
        assert "FUNDAMENTAL DATA" in block
        assert "18.5" in block  # ROCE
        assert "16.2" in block  # ROE
        assert "0.42" in block  # debt/equity
        assert "55.3" in block  # promoter holding
        assert "-1.2" in block  # YoY change

    def test_partial_data_does_not_crash(self):
        # Only one field available -- must still produce a valid, non-empty
        # block rather than raising on the missing keys.
        block = sg._format_fundamentals_block({"debt_to_equity": 0.9})
        assert "FUNDAMENTAL DATA" in block
        assert "0.9" in block
        assert "n/a" not in block or "ROCE" not in block  # no dangling ROCE line printed with n/a-only content

    def test_promoter_holding_without_yoy_omits_yoy_note(self):
        block = sg._format_fundamentals_block({"promoter_holding": 60.0})
        assert "60.0" in block
        assert "pp YoY" not in block

    def test_no_recognized_fields_returns_empty_string(self):
        # A dict with only unrecognized keys should yield no lines, hence "".
        block = sg._format_fundamentals_block({"market_cap_cr": 500000.0})
        assert block == ""
