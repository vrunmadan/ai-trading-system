"""
Tests for fundamentals/screener_public.py.

The HTML fixture below mirrors the REAL structure verified live against
screener.in/company/RELIANCE/ and screener.in/company/TATASTEEL/ on
2026-09-22 (via an actual browser session, reading real DOM — see that
module's docstring for the exact selectors this pins down): ul#top-ratios
with span.name/span.number, table.ranges-table for the compounded-growth
panels, #balance-sheet's data-table, and #shareholding's data-table. This
is not a guess at Screener's markup — it's a reduced, offline copy of what
was actually observed, so these tests catch a real regression in the
parser without hitting the network.
"""

import pytest

from fundamentals.screener_public import (
    parse_fundamentals_page,
    fetch_fundamentals,
    get_fundamentals_cached,
    _clean_number,
)


FIXTURE_HTML = """
<html><body>
<ul id="top-ratios">
  <li class="flex flex-space-between" data-source="default">
    <span class="name">Market Cap</span>
    <span class="nowrap value">₹ <span class="number">16,78,172</span> Cr.</span>
  </li>
  <li class="flex flex-space-between" data-source="default">
    <span class="name">Current Price</span>
    <span class="nowrap value">₹ <span class="number">1,240</span></span>
  </li>
  <li class="flex flex-space-between" data-source="default">
    <span class="name">High / Low</span>
    <span class="nowrap value">₹ <span class="number">1,612</span> / <span class="number">1,226</span></span>
  </li>
  <li class="flex flex-space-between" data-source="default">
    <span class="name">Stock P/E</span>
    <span class="nowrap value"><span class="number">42.8</span></span>
  </li>
  <li class="flex flex-space-between" data-source="default">
    <span class="name">Book Value</span>
    <span class="nowrap value">₹ <span class="number">418</span></span>
  </li>
  <li class="flex flex-space-between" data-source="default">
    <span class="name">Dividend Yield</span>
    <span class="nowrap value"><span class="number">0.48</span> %</span>
  </li>
  <li class="flex flex-space-between" data-source="default">
    <span class="name">ROCE</span>
    <span class="nowrap value"><span class="number">7.78</span> %</span>
  </li>
  <li class="flex flex-space-between" data-source="default">
    <span class="name">ROE</span>
    <span class="nowrap value"><span class="number">7.71</span> %</span>
  </li>
  <li class="flex flex-space-between" data-source="default">
    <span class="name">Face Value</span>
    <span class="nowrap value">₹ <span class="number">10.0</span></span>
  </li>
</ul>

<div id="analysis">
  <table class="ranges-table">
    <tr><td colspan="2">Compounded Sales Growth</td></tr>
    <tr><td>10 Years:</td><td>8%</td></tr>
    <tr><td>5 Years:</td><td>16%</td></tr>
    <tr><td>3 Years:</td><td>-2%</td></tr>
    <tr><td>TTM:</td><td>10%</td></tr>
  </table>
  <table class="ranges-table">
    <tr><td colspan="2">Compounded Profit Growth</td></tr>
    <tr><td>10 Years:</td><td>5%</td></tr>
    <tr><td>5 Years:</td><td>9%</td></tr>
    <tr><td>3 Years:</td><td>-2%</td></tr>
    <tr><td>TTM:</td><td>4%</td></tr>
  </table>
  <table class="ranges-table">
    <tr><td colspan="2">Stock Price CAGR</td></tr>
    <tr><td>10 Years:</td><td>17%</td></tr>
    <tr><td>1 Year:</td><td>-11%</td></tr>
  </table>
  <table class="ranges-table">
    <tr><td colspan="2">Return on Equity</td></tr>
    <tr><td>10 Years:</td><td>8%</td></tr>
    <tr><td>Last Year:</td><td>8%</td></tr>
  </table>
</div>

<section id="balance-sheet">
  <table class="data-table responsive-text-nowrap">
    <tr><td></td><td>Mar 2025</td><td>Mar 2026</td></tr>
    <tr><td>Equity Capital</td><td>13,532</td><td>13,532</td></tr>
    <tr><td>Reserves</td><td>529,555</td><td>552,703</td></tr>
    <tr><td>Borrowings +</td><td>201,505</td><td>234,008</td></tr>
  </table>
</section>

<section id="shareholding">
  <table class="data-table">
    <tr><td></td><td>Sep 2024</td><td>Dec 2024</td><td>Mar 2025</td><td>Jun 2025</td><td>Sep 2025</td><td>Dec 2025</td><td>Mar 2026</td><td>Jun 2026</td></tr>
    <tr><td>Promoters +</td><td>50.24%</td><td>50.13%</td><td>50.10%</td><td>50.07%</td><td>50.01%</td><td>50.00%</td><td>50.00%</td><td>50.48%</td></tr>
    <tr><td>FIIs +</td><td>21.30%</td><td>19.16%</td><td>19.07%</td><td>19.21%</td><td>18.65%</td><td>19.09%</td><td>18.67%</td><td>17.19%</td></tr>
  </table>
</section>
</body></html>
"""


class TestCleanNumber:
    def test_parses_plain_number(self):
        assert _clean_number("42.8") == 42.8

    def test_strips_currency_and_commas(self):
        assert _clean_number("₹ 16,78,172") == 1678172.0

    def test_strips_percent(self):
        assert _clean_number("7.78%") == 7.78

    def test_handles_negative(self):
        assert _clean_number("-2%") == -2.0

    def test_none_on_dash(self):
        assert _clean_number("-") is None

    def test_none_on_none(self):
        assert _clean_number(None) is None


class TestParseFundamentalsPage:
    def test_top_ratios(self):
        d = parse_fundamentals_page(FIXTURE_HTML)
        assert d["market_cap_cr"] == 1678172.0
        assert d["current_price"] == 1240.0
        assert d["pe"] == 42.8
        assert d["book_value"] == 418.0
        assert d["dividend_yield"] == 0.48
        assert d["roce"] == 7.78
        assert d["roe"] == 7.71

    def test_growth_metrics(self):
        d = parse_fundamentals_page(FIXTURE_HTML)
        assert d["sales_growth_3yr"] == -2.0
        assert d["sales_growth_5yr"] == 16.0
        assert d["sales_growth_10yr"] == 8.0
        assert d["sales_growth_ttm"] == 10.0
        assert d["profit_growth_3yr"] == -2.0
        assert d["profit_growth_5yr"] == 9.0

    def test_debt_to_equity_computed_from_balance_sheet(self):
        d = parse_fundamentals_page(FIXTURE_HTML)
        # latest column: Borrowings 234,008 / (Equity Capital 13,532 + Reserves 552,703)
        expected = 234008 / (13532 + 552703)
        assert d["debt_to_equity"] == pytest.approx(expected, abs=0.001)

    def test_promoter_holding_and_trend(self):
        d = parse_fundamentals_page(FIXTURE_HTML)
        assert d["promoter_holding"] == 50.48
        assert d["promoter_holding_1q_ago"] == 50.00
        # 4 quarters back from Jun 2026 is Jun 2025 (Jun26->Mar26->Dec25->Sep25->Jun25)
        assert d["promoter_holding_4q_ago"] == 50.07
        assert d["promoter_holding_change_yoy"] == pytest.approx(50.48 - 50.07, abs=0.001)

    def test_no_none_values_leak_into_result(self):
        d = parse_fundamentals_page(FIXTURE_HTML)
        assert all(v is not None for v in d.values())

    def test_empty_html_returns_empty_dict(self):
        assert parse_fundamentals_page("<html><body></body></html>") == {}

    def test_missing_balance_sheet_section_omits_debt_to_equity(self):
        html = FIXTURE_HTML.replace('id="balance-sheet"', 'id="renamed-section"')
        d = parse_fundamentals_page(html)
        assert "debt_to_equity" not in d
        # everything else should still parse fine
        assert d["roce"] == 7.78

    def test_missing_shareholding_section_omits_promoter_fields(self):
        html = FIXTURE_HTML.replace('id="shareholding"', 'id="renamed-section"')
        d = parse_fundamentals_page(html)
        assert "promoter_holding" not in d
        assert d["roce"] == 7.78

    def test_garbled_html_does_not_raise(self):
        # Truncated/malformed markup should degrade gracefully, not crash.
        parse_fundamentals_page("<ul id='top-ratios'><li><span class='name'>Broken")


class TestFetchFundamentals:
    def test_404_returns_none(self, monkeypatch):
        class _Resp:
            status_code = 404
            text = ""

            def raise_for_status(self):
                pass

        class _Session:
            headers = {}

            def update(self, *a, **k):
                pass

            def get(self, *a, **k):
                return _Resp()

        s = _Session()
        s.headers = type("H", (), {"update": lambda self, *a, **k: None})()
        assert fetch_fundamentals("NOSUCHTICKER", session=s) is None

    def test_network_error_returns_none(self, monkeypatch):
        class _Session:
            headers = type("H", (), {"update": lambda self, *a, **k: None})()

            def get(self, *a, **k):
                raise ConnectionError("simulated network failure")

        assert fetch_fundamentals("RELIANCE", session=_Session()) is None

    def test_successful_fetch_returns_parsed_data(self, monkeypatch):
        class _Resp:
            status_code = 200
            text = FIXTURE_HTML

            def raise_for_status(self):
                pass

        class _Session:
            headers = type("H", (), {"update": lambda self, *a, **k: None})()

            def get(self, *a, **k):
                return _Resp()

        d = fetch_fundamentals("RELIANCE", session=_Session())
        assert d is not None
        assert d["roce"] == 7.78
        assert d["promoter_holding"] == 50.48

class TestGetFundamentalsCached:
    """
    get_fundamentals_cached() wraps fetch_fundamentals() with ledger.db's
    kv_store cache. These tests patch ledger.db's functions directly
    (rather than a real DB file) and fundamentals.screener_public's own
    fetch_fundamentals, so they exercise only the cache-vs-refetch
    decision logic -- HTML parsing is covered separately above, and the
    real kv_store round-trip is covered by ledger/db.py's own tests.

    A fixed FIXED_NOW is patched in for ledger.db.now_ist() so "fresh"
    vs. "stale" is deterministic rather than depending on wall-clock time
    at test-run time.
    """

    FIXED_NOW = "2026-09-22 10:00:00"

    @pytest.fixture(autouse=True)
    def _fixed_clock(self, monkeypatch):
        monkeypatch.setattr("ledger.db.now_ist", lambda: self.FIXED_NOW)

    @staticmethod
    def _ts_days_before_now(days):
        from datetime import datetime, timedelta

        dt = datetime.strptime(
            TestGetFundamentalsCached.FIXED_NOW, "%Y-%m-%d %H:%M:%S"
        ) - timedelta(days=days)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def test_fresh_cache_hit_skips_live_fetch(self, monkeypatch):
        cached_data = {"roce": 12.3}
        fresh_ts = self._ts_days_before_now(1)  # well within the 7-day default
        monkeypatch.setattr(
            "ledger.db.get_cached_fundamentals", lambda t: (cached_data, fresh_ts)
        )
        fetch_calls = []
        monkeypatch.setattr(
            "fundamentals.screener_public.fetch_fundamentals",
            lambda *a, **k: fetch_calls.append(1),
        )

        result = get_fundamentals_cached("RELIANCE")

        assert result == cached_data
        assert fetch_calls == []  # cache hit -- no network call made

    def test_stale_cache_triggers_refetch_and_resave(self, monkeypatch):
        stale_data = {"roce": 1.0}
        stale_ts = self._ts_days_before_now(30)
        fresh_data = {"roce": 99.0}
        monkeypatch.setattr(
            "ledger.db.get_cached_fundamentals", lambda t: (stale_data, stale_ts)
        )
        saved = {}
        monkeypatch.setattr(
            "ledger.db.save_fundamentals_cache",
            lambda t, d: saved.__setitem__(t, d),
        )
        monkeypatch.setattr(
            "fundamentals.screener_public.fetch_fundamentals",
            lambda *a, **k: fresh_data,
        )

        result = get_fundamentals_cached("RELIANCE")

        assert result == fresh_data
        assert saved == {"RELIANCE": fresh_data}

    def test_no_cache_fetches_and_saves(self, monkeypatch):
        monkeypatch.setattr("ledger.db.get_cached_fundamentals", lambda t: None)
        saved = {}
        monkeypatch.setattr(
            "ledger.db.save_fundamentals_cache",
            lambda t, d: saved.__setitem__(t, d),
        )
        monkeypatch.setattr(
            "fundamentals.screener_public.fetch_fundamentals",
            lambda *a, **k: {"roce": 5.0},
        )

        result = get_fundamentals_cached("TATASTEEL")

        assert result == {"roce": 5.0}
        assert saved == {"TATASTEEL": {"roce": 5.0}}

    def test_no_cache_and_fetch_fails_returns_none(self, monkeypatch):
        monkeypatch.setattr("ledger.db.get_cached_fundamentals", lambda t: None)
        monkeypatch.setattr(
            "fundamentals.screener_public.fetch_fundamentals", lambda *a, **k: None
        )

        assert get_fundamentals_cached("NOSUCHTICKER") is None

    def test_stale_cache_and_fetch_fails_falls_back_to_stale(self, monkeypatch):
        """A live-fetch failure on a stale cache should still return the
        stale data rather than None -- a few-days-old ROCE beats losing
        fundamental grounding entirely because of one bad HTTP response."""
        stale_data = {"roce": 1.0}
        stale_ts = self._ts_days_before_now(30)
        monkeypatch.setattr(
            "ledger.db.get_cached_fundamentals", lambda t: (stale_data, stale_ts)
        )
        monkeypatch.setattr(
            "fundamentals.screener_public.fetch_fundamentals", lambda *a, **k: None
        )

        result = get_fundamentals_cached("RELIANCE")

        assert result == stale_data

    def test_corrupt_cache_timestamp_is_treated_as_stale(self, monkeypatch):
        bad_data = {"roce": 1.0}
        fresh_data = {"roce": 2.0}
        monkeypatch.setattr(
            "ledger.db.get_cached_fundamentals",
            lambda t: (bad_data, "not-a-real-timestamp"),
        )
        monkeypatch.setattr("ledger.db.save_fundamentals_cache", lambda t, d: None)
        monkeypatch.setattr(
            "fundamentals.screener_public.fetch_fundamentals",
            lambda *a, **k: fresh_data,
        )

        result = get_fundamentals_cached("RELIANCE")

        assert result == fresh_data  # doesn't crash, just refetches

    def test_custom_max_age_days_is_respected(self, monkeypatch):
        """A 3-day-old entry is fresh under the default 7-day window but
        stale under an explicit max_age_days=1."""
        data = {"roce": 4.0}
        ts_3_days_old = self._ts_days_before_now(3)
        fresh_data = {"roce": 7.0}
        monkeypatch.setattr(
            "ledger.db.get_cached_fundamentals", lambda t: (data, ts_3_days_old)
        )
        monkeypatch.setattr("ledger.db.save_fundamentals_cache", lambda t, d: None)
        monkeypatch.setattr(
            "fundamentals.screener_public.fetch_fundamentals",
            lambda *a, **k: fresh_data,
        )

        result = get_fundamentals_cached("RELIANCE", max_age_days=1)

        assert result == fresh_data  # 3 days > 1-day window -> refetched

