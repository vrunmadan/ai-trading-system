"""
Market breadth sampling.

`_fetch_breadth` used to sample `tickers[:20]`. universe.csv is ordered by
market cap, so that read the twenty largest names. Breadth is meant to measure
how WIDE participation is, and sampling only megacaps reports a bullish
reading precisely during a narrowing late-cycle rally — the moment breadth is
most worth having. It carries weight ~2 of ~7.5 in the composite regime score,
and the regime chooses the strategy basket.
"""

import pytest

from researcher import regime_classifier as rc


@pytest.fixture(autouse=True)
def no_sector_cache(monkeypatch):
    monkeypatch.setattr(rc, "_SECTOR_CACHE", None)
    yield
    monkeypatch.setattr(rc, "_SECTOR_CACHE", None)


def _universe(n=500):
    return [f"T{i:03d}" for i in range(n)]


def test_sample_spans_the_whole_universe_not_its_head(monkeypatch):
    monkeypatch.setattr(rc, "_sector_map", lambda: {})
    tickers = _universe()
    sample = rc._breadth_sample(tickers, 20)

    positions = [tickers.index(t) for t in sample]
    assert min(positions) < 25, "should include large caps"
    assert max(positions) > 400, "must reach the small-cap end of the universe"
    assert max(positions) - min(positions) > 400


def test_old_head_slice_would_fail_that(monkeypatch):
    """Pins the actual defect, so a regression to tickers[:20] is caught."""
    tickers = _universe()
    head = tickers[:20]
    positions = [tickers.index(t) for t in head]
    assert max(positions) < 25          # the old behaviour
    monkeypatch.setattr(rc, "_sector_map", lambda: {})
    new = [tickers.index(t) for t in rc._breadth_sample(tickers, 20)]
    assert max(new) > max(positions)


def test_sample_size_is_respected(monkeypatch):
    monkeypatch.setattr(rc, "_sector_map", lambda: {})
    assert len(rc._breadth_sample(_universe(), 20)) == 20
    assert len(rc._breadth_sample(_universe(), 5)) == 5


def test_sample_is_deterministic(monkeypatch):
    """
    Same names every cycle, so consecutive breadth readings are comparable
    rather than jittering with the sample.
    """
    monkeypatch.setattr(rc, "_sector_map", lambda: {})
    tickers = _universe()
    assert rc._breadth_sample(tickers, 20) == rc._breadth_sample(tickers, 20)


def test_small_universe_is_returned_whole(monkeypatch):
    monkeypatch.setattr(rc, "_sector_map", lambda: {})
    tiny = ["A", "B", "C"]
    assert rc._breadth_sample(tiny, 20) == tiny


def test_empty_universe_is_safe(monkeypatch):
    monkeypatch.setattr(rc, "_sector_map", lambda: {})
    assert rc._breadth_sample([], 20) == []


def test_no_duplicates(monkeypatch):
    monkeypatch.setattr(rc, "_sector_map", lambda: {})
    sample = rc._breadth_sample(_universe(), 20)
    assert len(set(sample)) == len(sample)


# ---------------------------------------------------------------------------
# Sector spreading
# ---------------------------------------------------------------------------

def test_sample_spreads_across_sectors(monkeypatch):
    """
    A breadth reading dominated by one sector swings with that sector rather
    than with market participation.
    """
    tickers = _universe(200)
    # 40 consecutive names per sector — a plain stride would land on few.
    sectors = {t: f"SECTOR_{i // 40}" for i, t in enumerate(tickers)}
    monkeypatch.setattr(rc, "_sector_map", lambda: sectors)

    sample = rc._breadth_sample(tickers, 10)
    counts = {}
    for t in sample:
        counts[sectors[t]] = counts.get(sectors[t], 0) + 1

    assert len(counts) == 5, "all five sectors should be represented"
    assert max(counts.values()) <= 2


def test_degrades_to_a_plain_stride_without_sector_data(monkeypatch):
    """Sector lookup failing must not break breadth entirely."""
    monkeypatch.setattr(rc, "_sector_map", lambda: {})
    sample = rc._breadth_sample(_universe(), 20)
    assert len(sample) == 20
    assert len(set(sample)) == 20


def test_sector_map_failure_is_swallowed(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == "universe.loader":
            raise ImportError("no universe")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    monkeypatch.setattr(rc, "_SECTOR_CACHE", None)
    assert rc._sector_map() == {}


# ---------------------------------------------------------------------------
# Against the real universe file
# ---------------------------------------------------------------------------

def test_real_universe_sample_beats_the_head_slice():
    """
    Measured on the shipped universe.csv rather than a synthetic list, since
    the defect was a property of that file's cap ordering.
    """
    import csv
    import os

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "universe", "universe.csv",
    )
    if not os.path.exists(path):
        pytest.skip("universe.csv not present")

    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    tickers = [r["Ticker"] for r in rows]
    sectors = {r["Ticker"]: r["Sector"] for r in rows}

    def spread(sample):
        counts = {}
        for t in sample:
            counts[sectors[t]] = counts.get(sectors[t], 0) + 1
        return len(counts), max(counts.values())

    rc._SECTOR_CACHE = sectors
    try:
        head_sectors, head_max = spread(tickers[:20])
        new_sectors, new_max = spread(rc._breadth_sample(tickers, 20))
    finally:
        rc._SECTOR_CACHE = None

    assert new_sectors > head_sectors
    assert new_max <= head_max

    positions = [tickers.index(t) for t in rc._breadth_sample(tickers, 20)]
    assert max(positions) > len(tickers) * 0.8
