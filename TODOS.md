# TODOS

## Trading Pipeline

### Ledger never records real trades — risk gates are structurally disarmed

**What:** Design and build a way for the live approval flow to actually write to the `trades` table, so `risk_manager/portfolio_risk.py`'s circuit breakers, `risk_sizer/sizer.py`'s exposure checks, and `monitor/position_monitor.py`'s daily stop-loss check operate on real state instead of permanent zero.

**Why:** `alerts/gmail_alert.py:handle_email_action()` — the function actually reached when you tap Approve — marks the signal APPROVED and redirects to a Kite basket URL. It never calls `log_trade()`. The only caller of `log_trade()` was `trader/kite_client.py:execute_trade()`, which itself has zero callers now that `telegram_bot.py` (its only caller) was deleted in this same review. Consequence: `get_open_positions()` always returns `[]`, `get_weekly_pnl()`/`get_all_time_pnl()` always return `0`, and every circuit breaker (drawdown, weekly loss, deployed-capital, position-count, sector-concentration) computes against permanently-empty state — they can never trip, no matter how much capital is actually deployed in your real Kite account. `position_monitor.py`'s daily stop-loss check also always finds zero positions and silently no-ops.

**Context:** Surfaced by the outside-voice review (Claude subagent, Codex not installed) during `/plan-eng-review`, independently re-verified — grepped the whole repo, confirmed `log_trade()`/`execute_trade()` have no live callers, and re-read `handle_email_action()` line by line. `gmail_alert.py:329`'s own comment says "Position monitor will reconcile if the trade doesn't go through" but `position_monitor.py` never calls `kite.positions()`/`kite.holdings()` — that reconciliation was never built.

Three design shapes were discussed and deferred to give this proper design time rather than rushing it mid-review:
- **A — log optimistically at Approve time.** Fast, but Kite's basket URL lets you edit quantity/price before placing or skip placing entirely — creates permanent phantom trades in the ledger with no correction mechanism.
- **B — reconcile-only against `kite.positions()`/`kite.holdings()`.** No phantom trades, but same-day signals can't see each other's exposure until the next reconciliation pass, and matching a live Kite position back to a `signal_id` needs a fuzzy-match heuristic (ticker+direction+quantity, no shared ID).
- **C — hybrid (recommended going in).** Approve inserts a `PENDING` row immediately (same-day exposure visible right away, closes the "5 signals approved in a batch, none saw each other" gap too). The EOD position monitor — which already runs daily with a live Kite client — reconciles: found in `kite.positions()` → promote to `CONFIRMED` with real fill price; not found → `NOT_EXECUTED`, stop counting it, alert that the system thinks the order wasn't placed. More scope (new signal states, matching logic, monitor changes) but the only option that's both correct and closes the reconciliation gap.

This is the single biggest finding from this review — worth its own design session, not a mid-review patch.

**Effort:** XL
**Priority:** P0
**Depends on:** None — but should land before any consideration of moving off PAPER_MODE

### PAPER_MODE doesn't gate the live approval path

**What:** Make `PAPER_MODE=true` actually prevent real Kite order screens from opening on Approve, matching what the README already documents.

**Why:** `PAPER_MODE` is only checked in `main.py` (a log label) and inside `trader/kite_client.py:execute_trade()` (dead code — see the ledger TODO above). The function the live approve path actually calls, `_build_kite_basket_url()` in `alerts/gmail_alert.py`, has zero `PAPER_MODE` reference. Right now, with `PAPER_MODE=true`, tapping Approve in an alert email redirects straight to a real Kite order-placement screen with a real order pre-filled — the only thing preventing an actual trade is you manually tapping "Place Order" inside Kite's own UI. That's a real control (verified safe during `/cso` — no server-side auto-execution happens), but it's not what `PAPER_MODE` or the README's "no real orders until validated in paper mode" claim describes.

**Context:** Surfaced by the same outside-voice pass, independently re-verified via grep — `PAPER_MODE` appears in exactly two live-reachable spots, neither of which touches the approve/redirect path. May end up folding into the ledger-reconciliation redesign above (e.g. `PAPER_MODE` could gate whether Approve shows a "this would open Kite with X" preview instead of the real basket URL, vs. writing a simulated vs. real ledger entry) — worth deciding together rather than as two separate patches.

**Effort:** S (standalone) or folds into the ledger redesign above
**Priority:** P0
**Depends on:** Consider alongside the ledger-reconciliation redesign

### NSE/BSE market holiday calendar

**What:** Plug a real NSE/BSE trading-holiday list into the scheduler's cron gate so the research cycle doesn't fire on exchange holidays.

**Why:** The scheduler currently fires every weekday (9:15-15:15 IST) regardless of market holidays. Harmless — the cycle finds no tradeable data and exits — but wastes Kite Connect and Claude API calls on every holiday (~15/year).

**Context:** Surfaced during `/plan-eng-review` while removing the dead `scheduler/market_scheduler.py` duplicate, which is where this gap was originally noted as a docstring comment. NSE publishes an annual holiday calendar; the gate needs to check today's date against that list before `main.run_cycle()` fires in `webhook_server.py`'s `_safe_run_cycle()`.

**Effort:** S
**Priority:** P3
**Depends on:** None

### Corporate action blackout check

**What:** Skip trades within N days of a stock's earnings, ex-dividend, or bonus/split date.

**Why:** `trader/kite_client.py`'s `microstructure_checks()` has an inline `# TODO` for this — LLM-estimated timelines for corporate actions are unreliable, and the risk of getting caught in a gap-down/gap-up around one is asymmetric to the position size.

**Context:** Surfaced during `/plan-eng-review`. Kite Connect doesn't expose corporate actions directly — needs a data source (NSE corporate announcements feed, or a paid data provider) before this can be implemented. Worth doing before `PAPER_MODE` flips to live trading.

**Effort:** M
**Priority:** P2
**Depends on:** A corporate-actions data source

### Sequential signal generation + redundant macro-news fetch

**What:** Hoist the per-cycle macro/market-outlook RSS fetch out of the per-ticker loop in `researcher/signal_generator.py` (fetched once, reused across all tickers, instead of re-fetched identically for every ticker), and add a max-runtime alert so a coalesced/skipped hourly cycle (`max_instances=1, coalesce=True` in `webhook_server.py`'s scheduler) isn't silent.

**Why:** At the current universe size (~62 tickers × up to 2 strategies) this is a latency annoyance, not a crisis — but signal generation is fully sequential (LLM call + Kite historical-data call + 3 RSS fetches per ticker, no concurrency), and it gets worse linearly as the universe grows. If a cycle ever runs past 60 minutes, APScheduler's `coalesce=True` silently drops the missed trigger with no alert.

**Context:** Surfaced by the outside-voice review during `/plan-eng-review`. Two separable pieces: the RSS dedup is a small, mechanical win; the max-runtime alert is a small monitoring addition (e.g. log a warning if a cycle's wall-clock exceeds some threshold, or have the EOD summary note if an expected cycle didn't run).

**Effort:** S-M
**Priority:** P3
**Depends on:** None

### Position sizing doesn't account for stop distance

**What:** Scale position size by risk-per-share (distance to stop), not just confidence score.

**Why:** `risk_sizer/sizer.py` sizes purely as a fraction of capital scaled by the Researcher's confidence score. The actual stop-loss (fixed 7% below entry, `monitor/position_monitor.py`) is computed independently at monitor time and never fed back into sizing — two trades with identical confidence get identical capital allocation regardless of how far away their stop is, so a tight-stop and a wide-stop trade carry very different real risk for the same position size.

**Context:** Surfaced by the outside-voice review during `/plan-eng-review`. `sizer.py`'s own docstring already floats replacing the fixed 7% stop with an ATR-based one as a future direction — this TODO is the sizing-side half of that same idea (risk-per-trade should be roughly constant, position size should be the variable, not the other way around).

**Effort:** M
**Priority:** P3
**Depends on:** Possibly the ATR-based stop work already implied in sizer.py's docstring

## Completed
