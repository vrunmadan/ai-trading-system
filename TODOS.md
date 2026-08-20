# TODOS

## Trading Pipeline

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

### Drawdown circuit breaker cannot see unrealised losses

**What:** Decide whether the -8% drawdown breaker should mark open positions to market, or document that it is realised-P&L-only by design.

**Why:** `risk_manager/portfolio_risk.py:77` computes `portfolio_value = CAPITAL + all_time_pnl`, and `all_time_pnl` sums `trades.pnl`, which is only written on exit. An open position down 40% contributes exactly zero. The outermost safety net is structurally incapable of firing on open-position losses, which is the scenario it exists for. This survives the ledger fix and needs its own decision.

**Effort:** M
**Priority:** P1

### Market breadth samples the 20 largest stocks, not a representative sample

**What:** Replace `tickers[:20]` with a representative (or full-universe) breadth sample.

**Why:** `researcher/regime_classifier.py:170` does `sample = tickers[:20]`. `universe/universe.csv` is ordered by market cap, so this reads the 20 largest Nifty names (RELIANCE, BHARTIARTL, HDFCBANK, ICICIBANK, INFY and so on), six of them financials. Breadth is meant to measure how *wide* participation is; sampling only megacaps measures the opposite, and in a narrowing late-cycle rally it reports bullish breadth precisely when true breadth is deteriorating. It carries weight ~2 of ~7.5 in the composite regime score, and the regime picks the strategy basket.

**Effort:** S
**Priority:** P1

### Monitor silently skips positions it cannot price, and cannot price BSE at all

**What:** Raise an alert when a price fetch fails. (PARTIALLY FIXED 2026-08-19: the `exchange` column now exists on `trades` and `monitor/position_monitor.py` uses it instead of a hardcoded `NSE:`, so BSE holdings can be priced. The silent-skip half is still open.)

**Why:** `monitor/position_monitor.py:156` hardcodes `kite.ltp(f"NSE:{ticker}")`. The `trades` table (`ledger/schema.sql:27`) has no `exchange` column, so the monitor cannot know otherwise — `signals` has one, `trades` does not. For any BSE-listed holding the lookup fails, and the handler at :158-162 logs, writes a 0.0 price row, and continues **without appending to `alerts`** — so no email is sent. A position whose ticker cannot be priced is never stop-loss checked, indefinitely, and you are never told.

**Effort:** S
**Priority:** P2

### Backtest exits at the stop price; the live system emails you once a day

**What:** Add the exit-fill assumption to the caveat block that `streak_backtests/backtest.py` already prints, and consider modelling next-open fills.

**Why:** `streak_backtests/backtest.py:336-339` triggers on the intraday low and fills at exactly `stop_line` or `trail_line`. Live, `monitor/position_monitor.py` runs once at 15:35 IST on a close-ish LTP and only sends an alert — the earliest real exit is the next session. Overnight gaps and the manual-action lag are therefore absent from every backtested loss, which inflates PF (PF = gross win / gross loss). The existing caveat block covers survivorship bias, flat costs, breadth omission and additive drawdown, but not this. Note the *relative* conclusion is probably safe: optimistic fills flatter high-turnover exits most, so they favour the 7% trail, and the 20% trail still won. It is the absolute expectancy that is overstated.

**Effort:** S
**Priority:** P2

### Diagnostic endpoints use a non-constant-time secret comparison, and reuse the HMAC key

**What:** Replace `secret != expected` with `hmac.compare_digest` behind a single decorator, and split `DIAGNOSTIC_SECRET` from `APPROVAL_SECRET`.

**Why:** `webhook_server.py:212, 322, 360, 596, 751, 797` each contain `if not expected or secret != expected:` — six copies of a non-constant-time comparison. `alerts/gmail_alert.py:82` correctly uses `hmac.compare_digest` for the *same secret*. Because `APPROVAL_SECRET` doubles as the diagnostic bearer password, any leak of it (it also travels in the URL query string, so it lands in Railway access logs and browser history) becomes the ability to forge trade approvals, not just to read diagnostics. Remote timing attacks are genuinely hard, so likelihood is low — but the fix is one line each plus a key split.

**Effort:** S
**Priority:** P2

### .env.example documents 24 of the 47 variables the code reads

**What:** Regenerate `.env.example` from the code, with defaults and one-line comments.

**Why:** 23 variables read via `os.getenv` are missing from `.env.example`, including `RESEND_API_KEY` and `RESEND_FROM` (without which `_configured()` is False, `_send_email` returns False, and the system runs full cycles while silently sending nothing), and `LONG_STOP_LOSS_PCT` / `TRAILING_STOP_PCT` (the entire exit design from the redesign). Two documented variables — `GMAIL_APP_PASSWORD` and `UNIVERSE_CSV_PATH` — are read by nothing.

**Effort:** S
**Priority:** P2

### Ledger may sit on ephemeral Railway storage

**What:** Confirm a Railway volume is mounted at the `LEDGER_DB_PATH` directory; fail loudly at startup if the path is not on persistent storage.

**Why:** `LEDGER_DB_PATH` defaults to `ledger/trading_ledger.db`, a path inside the container. Nothing in `railway.json`, the `Procfile` or the docs mentions a volume. If none is attached, every deploy silently wipes signals, trades, position_checks, the portfolio peak and the stored Kite token, and the system restarts from zero history with no error.

**Effort:** S
**Priority:** P1

### Rate-limit storms are indistinguishable from quiet markets

**What:** Add throttling with backoff to the ticker loop, a per-day bar cache, and a Kite-error count in the daily summary.

**Why:** `researcher/signal_generator.py:743-761` issues up to 500 sequential `kite.historical_data` calls per cycle with no sleep, backoff or concurrency limit. 429s are caught at :772, written to `cycle_log` as `verdict="ERROR"`, and skipped — so the daily summary reports "0 signals today", exactly as it would on a genuinely quiet day. Seven cycles a day also re-fetch the same daily bars about 3,500 times; a per-day cache cuts Kite load roughly 7x. Supersedes and expands the existing "Sequential signal generation" TODO.

**Effort:** M
**Priority:** P2

### Approval links have no replay, double-tap or post-expiry guard

**What:** Guard against approving a signal the EOD sweep already marked NO_RESPONSE, and expire links after the session. (PARTIALLY FIXED 2026-08-19: re-approval no longer writes a second trade row — the double-approve guard in `handle_email_action` returns the same basket without doubling recorded exposure. Link expiry and the post-sweep guard are still open.)

**Why:** `verify_token` is a pure function of action + id + secret, so a link is valid forever. Tapping Approve twice redirects to Kite twice with no dedup; tapping it days later opens a basket against a stale thesis at a moved price; tapping it after the 15:35 sweep silently overwrites NO_RESPONSE back to APPROVED. Tapping Approve and then Reject leaves whichever was last, silently.

**Effort:** S
**Priority:** P2

### Approval email is the entire human interface for a five-figure decision

**What:** Review the alert email as a design artifact, not just a transport.

**Why:** No UI scope was detected in the architecture doc, so the design review phase was skipped — but the Resend email in `alerts/gmail_alert.py:178-260` is the only surface where a person makes the actual money decision. Today it displays "Quantity: 0 shares", places Approve and Reject as adjacent taps with no confirmation step on either, and shows no stop-loss price, no position-size-as-percent-of-capital, and no indication of how many positions are already open. Worth a `/plan-design-review` pass once the quantity bug is fixed.

**Effort:** S
**Priority:** P3

### /diagnose_cycle raises ImportError on every single request

**What:** Import `STRATEGY_BASKETS` from `researcher.signal_generator`, not `researcher.regime_classifier`, and rewire the flag logic through `_passes_prefilter`.

**Why:** `webhook_server.py:369` does `from researcher.regime_classifier import classify_regime, STRATEGY_BASKETS`. `STRATEGY_BASKETS` is defined at `researcher/signal_generator.py:147` and has never existed in `regime_classifier`. Because the import sits inside the route body the module loads fine and the failure only occurs per-request, where the outer `except` at :578 swallows it into a generic 500 page. The architecture doc lists this endpoint in §15 as a working troubleshooting tool; it has presumably been broken since the strategy redesign moved the baskets. Compounding it, `_flag_cell` (`webhook_server.py:462`) only knows `52wk_breakout`, `supertrend_buy` and `rsi_mean_reversion` — two of which are in no live basket — so `bb_squeeze_break` and `adx_bull_strength` fall through to "unknown strategy". This is the tool you reach for at 2am when nothing fired, and it returns a 500.

**Context:** Found by the independent Eng review voice during `/autoplan` (2026-08-19) and reproduced by executing the import. Add a route smoke test that hits every endpoint with a valid secret and asserts non-500, so a refactor can never silently break a route again.

**Effort:** S
**Priority:** P1

### Indicators are computed on today's incomplete candle; the backtest used completed bars

**What:** Set `to_date` to the last completed trading day, or model partial bars in the backtester. Pick one so live and backtest agree.

**Why:** `researcher/signal_generator.py:733` sets `to_date = date.today()`, and Kite's `historical_data(..., "day")` returns today's *forming* candle during market hours. The cycle runs at 09:15, 10:15 ... 15:15 IST. So `_volume_ratio` divides **today's partial volume** by a 20-day full-day average: at the 09:15 cycle the ratio is near zero by construction and rises mechanically through the session. The `52wk_breakout` gate requires `vol >= 1.5`, so the flagship strategy is close to impossible in the morning and progressively easier in the afternoon — a time-of-day bias nobody chose. RSI, Supertrend, Bollinger position and `pct_from_52wk_high` can likewise flip between cycles on the same day. Meanwhile `streak_backtests/backtest.py:314` sets `entry_px = C[i]` — it triggers on a *completed* bar and fills at that close. The backtested edge is measured on a decision the live pipeline structurally cannot make.

**Context:** Found by the independent Eng review voice. Related to but distinct from the exit-fill mismatch below — this one is about entries.

**Effort:** S
**Priority:** P1

### microstructure_checks fails open — one Kite hiccup disables both circuit breakers

**What:** Return `False` when turnover data is unavailable, matching how the quote failure is handled two lines below.

**Why:** `trader/kite_client.py:137` — `get_20d_avg_turnover` returns `0.0` on any exception. The caller then logs "could not verify liquidity — proceeding with caution" and continues (:175), skipping the liquidity floor, and guards the ADV cap with `if avg_turnover > 0` (:186), skipping that too. Both circuit breakers are disabled by a single data hiccup, and the function still returns `True, "Microstructure checks passed."` — a message that is now false. The docstring calls this fail-safe; it is fail-open. Currently unreachable because `execute_trade` has no callers, so this is a latent defect that activates the moment the ledger wiring lands.

**Effort:** S
**Priority:** P1

### Regime classifier can emit a confident BULL from two hardcoded numbers

**What:** Make the Nifty and VIX fallbacks return a sentinel; refuse to emit a reading (or emit confidence 0) when a required input is synthetic.

**Why:** `researcher/regime_classifier.py:132` returns a hardcoded `22000.0, 0.0` when both Nifty history and Nifty LTP fail, and :161 returns a hardcoded `18.0, 0.0` for VIX. Both fall back silently with only a log line, and the caller cannot tell real from synthetic. With both fallbacks active, Nifty `+0.0%` scores `+0.5` and VIX `18.0` scores `+0.5`; if the breadth sample succeeds (it uses equity tokens, a different data path from index tokens) and reads >=65%, that adds `+2.0` for a total of `3.0` — `Regime.BULL` at 71% confidence, clearing the `< 60` gate in `main.py:93`. The system then runs a full BULL-basket scan on a regime derived from two invented numbers. The total-outage case is safe; this needs a *partial* failure, which is exactly what an entitlement problem on index instruments looks like.

**Effort:** S
**Priority:** P1

### atr_pct is not ATR — it understates volatility 3-4x and is labelled ATR(14) to Claude

**What:** Compute Wilder's ATR (the true-range loop already exists in `_supertrend`) or rename the field `range_14d_pct`.

**Why:** `researcher/signal_generator.py:394` computes `(max(highs[-14:]) - min(lows[-14:])) / ltp / 14 * 100` — the 14-day total range divided by 14, not the mean of 14 true ranges. A 14-day high-low span is typically only 2.5-4x a single day's range, so this yields roughly 0.2-0.3x the real ATR. `_call_claude` then renders it as `ATR(14) as % of price: {x}%` (:646). No deterministic gate consumes it, so this is not a hard-safety bug, but the Researcher is being told every stock is 3-4x calmer than it is, which directly affects the "would I stake 15-20% of the portfolio on this" judgement the prompt asks for.

**Effort:** S
**Priority:** P2

### portfolio_peak is a one-way ratchet with no reset path

**What:** Store the capital base alongside the peak, rebase when it changes, add a reset path, and dedupe the halt alert to once per day.

**Why:** `risk_manager/portfolio_risk.py:36` reads `CAPITAL` at module import, and `ledger/db.py:332` only ever raises the stored peak. If `TOTAL_CAPITAL_INR` is ever lowered — withdrew funds, re-scoped the experiment — from say 10L to 5L, portfolio value instantly becomes 5L against a stored peak of 10L, giving `drawdown_pct = -50%`, which trips the -8% breaker on every cycle permanently with no in-app way to clear it. `_send_halt_alert` has no dedupe, so it emails 7 times a day. Recovery requires manual SQL against a DB that may not survive a redeploy.

**Effort:** S
**Priority:** P2

### QC may be silently blocking every signal, and the daily summary would not say so

**What:** Verify the `max_tokens` parameter name against the pinned OpenAI SDK, raise the token budget, and distinguish "QC blocked" from "no candidate" in the daily summary.

**Why:** `qc_factchecker/validator.py:113` passes `max_tokens=450` to `client.chat.completions.create` for `QC_MODEL`. Newer OpenAI reasoning models reject `max_tokens` in favour of `max_completion_tokens`; if that applies here, every call raises, is caught at :148, returns `NEEDS_MORE_DATA`, and is blocked at `main.py:154`. 450 tokens is also tight for a three-field JSON response with a mandated disconfirming-evidence narrative, so truncation gives the same outcome. This fails closed, which is correct — but invisibly: the system just stops producing trades forever while `send_daily_cycle_summary` reports "No signals reached the confidence threshold today (75%+ required)", which is actively misleading because the signal did clear 75% and died at QC. Alert when QC errors N times consecutively.

**Effort:** S
**Priority:** P1

### auto_refresh_kite_token.py wants the Zerodha account password and TOTP seed

**What:** Delete the file, or gate it behind an explicit opt-in with the risk spelled out.

**Why:** `setup/auto_refresh_kite_token.py` reads `KITE_PASSWORD` and `KITE_TOTP_SECRET`. Together those grant full Zerodha web-UI access including funds movement — a category change from every other secret in the system, which are revocable API credentials scoped to trading. It is currently dormant (the scheduler registers `_safe_send_kite_login_email`, never `auto_refresh_token`), so nothing is exposed today. Leaving it in the tree as a tempting convenience is the risk.

**Effort:** S
**Priority:** P2

### LLM-generated text is interpolated into email HTML without escaping

**What:** Wrap interpolated model output in `html.escape()`.

**Why:** `alerts/gmail_alert.py:213` interpolates `signal.rationale[:400]` — Claude-generated text — directly into an HTML email body, as does `qc_verdict.rationale` (:218) and `sizing.notes` (:219). `webhook_server.py:_html_response` similarly interpolates `str(e)`, which can carry API response bodies, into HTML. Severity is low since the recipient is the sole user and mail clients sandbox aggressively, but it is an unescaped path from model output to rendered markup, and the news-headline prompt-injection channel feeds the same model.

**Effort:** S
**Priority:** P3

## Completed

### Approval writes status EXECUTED for trades that were never executed — FIXED 2026-08-20

**Was:** `ledger/db.py:108` did `status = "EXECUTED" if response == "APPROVED" else "MISSED"`. Approving only records intent and redirects to Kite, so EXECUTED asserted a fill nobody had checked for. Worse, REJECTED and NO_RESPONSE both collapsed into `MISSED` — a value **nothing in the codebase ever read** — while `auditor/weekly_audit.py` asked Gemini to find signals with `status: NO_RESPONSE, REJECTED, SKIPPED`. Those names never appeared in that column, so TASK 2 (missed-opportunity review) had been querying for states the writer never wrote, and the calibration task counted unverified approvals as taken trades.

**Fix — one meaning per value:**

| status | meaning |
|---|---|
| `PENDING` | alert sent, awaiting response |
| `APPROVED` | approved; fill NOT yet verified |
| `EXECUTED` | fill confirmed — the only state asserting the trade happened |
| `NOT_EXECUTED` | approved, but the order never reached the market |
| `REJECTED` | rejected |
| `NO_RESPONSE` | unanswered at the EOD sweep |
| `SKIPPED` | dropped before the alert |

- `update_signal_response` maps each response to its own honest status. `MISSED` is gone.
- `mark_signal_executed()` / `mark_signal_not_executed()` added; the reconciler is the only caller, so EXECUTED is written in exactly one place, after a fill is established. A Kite outage or a row still inside the grace window leaves the signal APPROVED rather than claiming either outcome.
- A simulated PAPER fill also reaches EXECUTED: within the simulation it did happen, and the Auditor needs it in the same bucket as a real fill to calibrate confidence against outcomes.
- `auditor/weekly_audit.py` prompt updated — TASK 2 now also looks for NOT_EXECUTED, and Gemini is told explicitly that APPROVED means the fill was never verified so it must not be counted as a taken trade.

**Backfill (idempotent, in the existing `init_db` migration block):** EXECUTED rows with no CONFIRMED trade are demoted to APPROVED, which is what actually happened; `MISSED` rows recover their true value from `user_response`, which kept it all along. Both are no-ops once applied, and a genuinely confirmed fill survives the demotion pass.

**Tests:** `tests/test_signal_status.py`, 12 tests — the response mapping, no status ever being `MISSED`, every value in the Auditor's vocabulary actually being produced, EXECUTED only after a confirmed fill, APPROVED held during the grace window and during a Kite outage, and both backfill directions including idempotency. Full suite: 93 passed.



### Nothing ever calls close_trade — FIXED 2026-08-19 (auto-close on INVALIDATED, paper only)

**Was:** `close_trade()` had zero callers, so even once trades were recorded every position stayed open forever, `pnl` was never written, `get_all_time_pnl()` stayed 0, and the weekly Auditor had no wins or losses to calibrate against.

**Fix:** `monitor/position_monitor.py` now calls `close_trade(trade_id, current_price)` when a position is INVALIDATED, **for PAPER rows with a CONFIRMED fill only**. The alert changes from "review and consider closing" to "closed at X (realised Y)".

**Three decisions worth recording:**

1. **PAPER only.** In paper mode the ledger *is* the simulation, so closing it is what completes the round trip. In LIVE mode the system never acts on your behalf (README, locked design decisions) — closing the row while the real Kite position is still open would make the ledger assert an exit that never happened, which is worse than not closing. LIVE positions still only alert. Closing them properly needs exit reconciliation against Kite (detecting a position has disappeared and at what price); that is not built.

2. **Exit price is the observed LTP at 15:35, not `stop_price`.** Filling at the stop line is the optimistic assumption `streak_backtests/backtest.py` makes (it triggers on the intraday low and fills exactly at the line). The live monitor only learns the stop broke at 15:35, so the honest simulated exit is the price it actually saw. This deliberately makes paper results slightly worse than the backtest, and closer to reality — see the backtest exit-fill TODO.

3. **CONFIRMED only.** A PENDING row deferred inside the reconciler grace window is not closed, because its fill was never established.

**Also fixed here — a bug in the reconciler shipped earlier the same day:** PAPER rows are never sent to Kite, so reconciling them against `positions()`/`holdings()` found nothing and aged every one of them out to NOT_EXECUTED, which would have made a paper round trip impossible in exactly the way this entry describes. `monitor/trade_reconciler.py` now confirms an unmatched PAPER row as a *simulated* fill at the expected price. A PAPER row that IS found in Kite still takes the real fill, since `PAPER_MODE` does not currently gate the approval path and a paper-tagged signal can be placed for real.

**Tests:** `tests/test_trade_reconciliation.py` grew to 20. New: simulated paper fill, real fill winning over the simulation, stop-triggered close, position left open while the thesis holds, close at observed price rather than the stop line, and LIVE never auto-closing. Four existing tests were repointed at LIVE approvals because the write-off and deferral paths are LIVE-only concerns; their PAPER expectations were what made paper trading impossible. Full suite: 81 passed.



### Ledger never records real trades — FIXED 2026-08-19 (ledger hybrid)

**Was:** nothing wrote to `trades`, so `get_open_positions()` always returned `[]`, `get_weekly_pnl()`/`get_all_time_pnl()` always returned 0, and every portfolio circuit breaker computed against permanently-empty state while logging "Portfolio risk gate: OK".

**Fix — option C (the hybrid) from the original entry, as designed:**
- `ledger/schema.sql` + `ledger/db.py` — `trades` gains `exchange`, `fill_status` (PENDING / CONFIRMED / NOT_EXECUTED) and `fill_note`, via the existing idempotent-migration list so the live Railway DB upgrades in place on boot. Verified against a simulated pre-change database: columns added, legacy rows preserved and defaulted to CONFIRMED (they were written by `execute_trade`, which places the order itself), `init_db` still idempotent.
- `alerts/gmail_alert.py` — approving writes a PENDING trade row via `log_pending_trade()` using the real sized quantity, *before* marking the signal APPROVED, so a ledger failure aborts cleanly instead of leaving a signal APPROVED with no position. `entry_price` holds the expected price (approved capital / quantity) until reconciliation replaces it. A double-approve guard stops a second tap writing a second row.
- `monitor/trade_reconciler.py` (new) — `reconcile_pending_trades()` merges `kite.positions()['net']` and `kite.holdings()` into one `EXCHANGE:SYMBOL` map and promotes each PENDING row to CONFIRMED (real average price, real quantity, partial fills recorded) or NOT_EXECUTED. Matching is on exchange *and* symbol. It never claims more than it asked for, defers rather than writing off inside a `MAX_PENDING_AGE_DAYS` grace window, and fails closed if Kite is unreachable — leaving rows PENDING and emailing, because marking them NOT_EXECUTED on an outage would silently erase real exposure.
- `get_open_positions()` — now excludes NOT_EXECUTED and includes PENDING, so several signals approved in one batch each see the others' capital.
- `main.py` / `webhook_server.py` — new `run_trade_reconciliation()` runs at 15:35 **before** the position monitor, so stops are evaluated against confirmed fills. Also exposed as `python main.py reconcile`.

**Tests:** `tests/test_trade_reconciliation.py`, 14 tests — the PENDING write, same-day exposure visibility, the double-approve guard, confirmation from positions and from holdings, partial fills, the never-claim-more rule, exchange-aware matching, NOT_EXECUTED dropping out of exposure, the grace-period deferral, the Kite-outage fail-closed path, the no-op case, and a full round trip through `close_trade` into `get_all_time_pnl`. Full suite: 75 passed.

**Still open:** the monitor alerts on INVALIDATED but does not call `close_trade`, so a round trip still needs a manual close — see the `close_trade` entry. Reconciliation matches on ticker + exchange, so a name you already held outside the system could in principle be matched; the no-pyramiding rule in the sizer makes that unlikely but it is not impossible.



### Approved position size never reaches the order — FIXED 2026-08-19

**Was:** `risk_sizer/sizer.py` returned `quantity=0` on the approved path and nothing filled it in, so `signals.sized_quantity` was always `0`. `alerts/gmail_alert.py:326` read it back as `row["sized_quantity"] or 1`; because `0` is falsy every approved trade opened a Kite basket for exactly **1 share**, and the alert email rendered "Quantity: 0 shares" next to a five-figure capital number.

**Fix:**
- `main.py` — new **Step 3b** after the Risk Sizer: fetch the live LTP via `trader.kite_client.get_ltp`, set `sizing.quantity = int(capital_to_deploy // ltp)`, and fail closed if the price is unavailable or the capital buys less than one share. Because this runs before `log_signal`, the persisted quantity and the alert email now both carry the real number.
- `alerts/gmail_alert.py` — removed the `or 1` fallback. A missing or zero quantity now returns `success=False` with an explanatory message and leaves the signal untouched, rather than silently substituting a number.
- `risk_sizer/sizer.py` — corrected the comments that pointed at a Trader step which is never invoked.

**Tests:** `tests/test_approval_quantity.py`, 10 tests covering the conversion (including flooring so the sizer budget is never exceeded, and the below-one-share case that used to become 1) and the approval path's refusal to invent a quantity. Full suite: 61 passed.

**Still open:** the quantity is computed at signal time, not at approval time, so a large intraday move between the alert and the tap leaves the share count slightly stale. Re-pricing at approval belongs with the ledger-linkage work, since that is where the trade row gets written.


