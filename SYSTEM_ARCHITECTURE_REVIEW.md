# AI Trading System — Full System Architecture & Review Document

**Purpose:** Complete, self-contained technical description of the system for an end-to-end review (code, control flow, decisions, thresholds, data model, and known risks). Written so a reviewer who has never seen the repo can follow every step from market-data ingestion to trade execution.

**Status:** `PAPER_MODE=true` by default. Code complete; the strategy/exit/universe layer was recently redesigned from 10-year backtest evidence (see §11). Not yet validated out-of-sample by paper trading.

**Instrument scope:** Indian equities, **long-only**, delivery (CNC). No shorting, no derivatives.

---

## 0. TL;DR — what the system does

Every trading hour, a scheduled job:
1. Checks portfolio-level circuit breakers (halt if breached).
2. Classifies the market **regime** (deterministic math on Nifty/VIX/breadth).
3. Scans a ~500-name universe; a cheap **deterministic pre-filter** selects genuine technical candidates, which an LLM (**Claude**) then scores for conviction.
4. A rules-based **Risk Sizer** sizes the single best signal.
5. An independent LLM (**GPT**) **adversarially QCs** it.
6. If all gates pass, it emails the user an **Approve/Reject** alert.
7. On approval, the user is redirected to a pre-filled **Kite** order screen and places the order themselves. **The system never auto-executes.**
8. A daily **Monitor** manages exits (−7% hard stop + 20% trailing stop, or regime invalidation).
9. A weekly **Auditor** (Gemini) reviews calibration and produces a hypothesis backlog (paper-tested, never auto-applied).

Three independent model families are used on purpose (Anthropic → OpenAI → Google) so failure modes are uncorrelated.

---

## 1. High-level data flow

```
                 ┌─────────────────────────────────────────────────────────┐
                 │  webhook_server.py  (single Railway process)             │
                 │  Flask app  +  APScheduler (background thread)           │
                 └─────────────────────────────────────────────────────────┘
   07:30 IST  ─────────────►  Kite login email ──► user taps ──► /kite_callback ──► token → SQLite
   09:15–15:15 hourly ──────►  run_cycle()  (the research pipeline, §5)
   15:35 IST  ─────────────►  run_position_monitor() + run_eod_sweep()  (§7, §8)
   Fri 16:00  ─────────────►  run_weekly_audit()  (§9)
   Sun 20:00  ─────────────►  run_universe_refresh()  (§10)

   run_cycle() pipeline (each arrow = a gate that can end the cycle):
     Portfolio Risk Gate ──► Regime Classifier ──► Signal Generator
        (rules)                (rules)              (pre-filter → Claude)
                                                          │
     Risk Sizer ◄─────────────────────────────────────────┘
        (rules)  ──► QC / Fact-Checker (GPT) ──► Ledger write ──► Gmail alert ──► Google Sheets mirror
                                                                       │
                                        user Approve/Reject (async, HMAC-signed link)
                                                                       │
                                              Approve ──► redirect to Kite basket ──► user places order
```

**Key architectural fact for the reviewer:** the "Trader" module (`trader/kite_client.py`, with microstructure checks and `execute_trade`) exists but is **not currently invoked by the approval flow**. Approval marks the signal `APPROVED` and hands off to a manual Kite basket order. See §6 and §13 (Open Issue #1) — this is the most important wiring gap.

---

## 2. Repository layout

```
main.py                      Orchestrator: run_cycle / run_position_monitor / run_weekly_audit / run_eod_sweep
webhook_server.py            Flask + APScheduler; approval endpoint, Kite callback, diagnostics
researcher/
  regime_classifier.py       Deterministic 5-regime classifier (Nifty/VIX/breadth)
  signal_generator.py        Indicators, strategy baskets, pre-filter, Claude synthesis
risk_manager/portfolio_risk.py   Portfolio-level circuit breakers (Step 0)
risk_sizer/sizer.py          Position sizing (6 checks) — no LLM
qc_factchecker/validator.py  GPT adversarial QC
alerts/gmail_alert.py        Email alerts + Approve/Reject HMAC links + Kite basket URL
trader/kite_client.py        Kite Connect wrapper, microstructure checks, execute_trade (see §6)
monitor/position_monitor.py  Daily stop/trailing/regime exit checks
auditor/weekly_audit.py      Gemini weekly calibration + hypothesis backlog
universe/
  loader.py                  Reads universe/universe.csv → tickers/sectors
  universe_refresher.py      Sunday Screener.in discovery job
  screener_client.py         Screener.in client
  diagnose_universe.py       CSV-vs-live-instruments diagnostics
ledger/
  db.py                      SQLite data-access layer
  schema.sql                 Ledger schema (7 tables)
  trading_ledger.db          SQLite DB (created by setup/init_db.py)
sheets/trade_logger.py       Google Sheets mirror (non-blocking)
setup/                       init_db, kite token refresh, connection tests, sheets auth
streak_backtests/backtest.py Offline backtesting harness (Yahoo data) — §12
universe/universe.csv        Live universe (Nifty 500, sector-mapped)
Procfile / railway.json      Deployment (startCommand: python webhook_server.py)
```

---

## 3. Tech stack & external dependencies

| Concern | Choice | Notes |
|---|---|---|
| Runtime | Python 3.10/3.11 | |
| Web/scheduler | Flask + APScheduler | one process, scheduler in a daemon thread |
| Hosting | Railway.app | always-on; health check `/health` |
| Market data + orders | Zerodha **Kite Connect** (paid tier for historical) | token expires midnight IST daily |
| Researcher LLM | **Claude Sonnet 5** (Anthropic) | synthesis only, structured JSON |
| QC LLM | **GPT-5.5** (OpenAI) | Structured Outputs (JSON) |
| Auditor LLM | **Gemini 3.5 Pro** (Google) | 2M context, whole week in one prompt |
| Email | **Resend** HTTP API (port 443) | see §4 discrepancy note |
| Persistence | SQLite (`ledger/trading_ledger.db`) | + optional Google Sheets mirror |
| News/sentiment | Google News RSS (no key) | best-effort, never blocks a cycle |

---

## 4. Configuration (environment variables)

All thresholds are env-overridable; defaults shown are the code defaults.

**Mode / keys:** `PAPER_MODE` (default true), `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `KITE_API_KEY`, `KITE_API_SECRET`, `KITE_ACCESS_TOKEN` (fallback; DB token preferred).

**Email/approval:** `RESEND_API_KEY`, `RESEND_FROM`, `ALERT_EMAIL`, `RAILWAY_URL`, `APPROVAL_SECRET` (HMAC signing key).

**Model IDs:** `RESEARCHER_MODEL=claude-sonnet-5`, `QC_MODEL=gpt-5.5`, `AUDITOR_MODEL=gemini-3.5-pro`.

**Portfolio gate (`risk_manager/portfolio_risk.py`):** `TOTAL_CAPITAL_INR=1000000`, `MAX_DRAWDOWN_PCT=8.0`, `MAX_DEPLOYED_PCT=65.0`, `WEEKLY_LOSS_LIMIT_INR=-15000`, `MAX_OPEN_POSITIONS=6`, `MAX_SECTOR_PCT=30.0`.

**Risk Sizer (`risk_sizer/sizer.py`):** `MAX_POSITION_PCT_OF_CAPITAL=20`, `MAX_SECTOR_PCT_OF_CAPITAL=35`, `MAX_WEEKLY_DRAWDOWN_PCT=10`, `MAX_CONCURRENT_POSITIONS=5`, `MIN_POSITION_INR=10000`.

**Signal (`researcher/signal_generator.py`):** `MIN_SIGNAL_CONFIDENCE=75`, `REGIME_INERTIA_PERIODS=2`.

**Monitor (`monitor/position_monitor.py`):** `LONG_STOP_LOSS_PCT=7.0`, `TRAILING_STOP_PCT=20.0`, `WEAKENING_ALERT_PCT=3.0`.

**Trader (`trader/kite_client.py`):** `MAX_ADV_PCT=2.0`, `CIRCUIT_BUFFER_PCT=1.5`, `CORP_ACTION_BLACKOUT_DAYS=3`, `MIN_DAILY_TURNOVER_INR=30000000` (₹3 Cr).

> **⚠ Config discrepancy (review item):** `.env.example` documents `GMAIL_APP_PASSWORD` for SMTP, but the code (`alerts/gmail_alert.py`) sends via the **Resend HTTP API** (`RESEND_API_KEY`/`RESEND_FROM`). The example file is stale relative to the code.
>
> **⚠ Two different sector caps:** the portfolio gate uses `MAX_SECTOR_PCT=30%` (of capital, halts the cycle) while the Risk Sizer uses `MAX_SECTOR_PCT_OF_CAPITAL=35%` (rejects the single trade). They are independent knobs with different names and values — intentional layering, but easy to confuse.

---

## 5. The research cycle — step by step (`main.run_cycle`)

Called hourly (09:15–15:15 IST) by the scheduler. Every step wraps its work in try/except and **returns on failure (fail-closed)** — a broken step never ships a trade.

### Step 0 — Portfolio Risk Gate  (`risk_manager/portfolio_risk.check_portfolio_risk`)
Runs **first**, before regime/signal. Deterministic. Portfolio value = `TOTAL_CAPITAL_INR + all realised P&L` (from `get_all_time_pnl()`); peak stored/updated in `portfolio_peak` (single-row table). Circuit breakers, checked in severity order — first hit halts the whole cycle and emails an alert:

```python
if drawdown_pct <= -MAX_DRAWDOWN_PCT:            # -8% below all-time peak
elif weekly_pnl <= WEEKLY_LOSS_LIMIT:            # <= -₹15,000 this week
elif deployed_pct >= MAX_DEPLOYED_PCT:           # >= 65% of capital deployed
elif open_count >= MAX_OPEN_POSITIONS:           # >= 6 open positions
elif sector_breaches:                            # any sector > 30% of capital
```
Fail-safe: if the gate itself raises, `run_cycle` returns (no trade).

### Step 1 — Regime Classifier  (`researcher/regime_classifier.classify_regime`)
Deterministic math (NOT an LLM) so it's reproducible and auditable. Inputs fetched from Kite:
- **Nifty 50 LTP vs its 200-day EMA** (% above/below) — structural trend, weight ≈3
- **India VIX** level + 5-day change — fear/greed, weight ≈2.5
- **Breadth**: % of up-to-20 sampled universe tickers above their 50-SMA, weight ≈2

Thresholds:
```python
VIX_CALM=15  VIX_ELEVATED=20  VIX_HIGH_FEAR=25  VIX_EXTREME=30
NIFTY_EUPHORIA=+8%  NIFTY_BULL=+2%  NIFTY_BEAR=-3%  NIFTY_CRASH=-12%
BREADTH_BULL=65%  BREADTH_BEAR=40%
```
A composite score maps to one of 5 regimes: `CRASH / BEAR / SIDEWAYS / BULL / EUPHORIA`. **Hard override:** `VIX ≥ 30` OR `Nifty ≤ −12%` forces `CRASH` regardless of score. **Regime inertia:** requires `REGIME_INERTIA_PERIODS=2` consecutive matching reads before switching; mid-transition it holds the previous regime and drops confidence by 25 (anti-whipsaw). Inertia state is **module-level in-memory** (`_regime_history`), reset on redeploy.

**Gate:** `if regime_reading.confidence < 60: return` — low-confidence cycles are skipped.

### Step 2 — Signal Generator  (`researcher/signal_generator.generate_signal`)
1. Look up the **strategy basket** for the current regime (§11). Empty basket (bear/crash) → return immediately.
2. Build a combined NSE+BSE instrument→token map (NSE overrides BSE for dual-listed).
3. For each universe ticker: fetch ~14 months of daily OHLCV from Kite; compute all indicators once (`_compute_indicators`): RSI-14 (Wilder), Supertrend(10,3), volume ratio 20d, % from 52-wk high, Bollinger position, SMA-50/200, ATR%, **ADX/±DI, CCI-20, EMA-50/200 golden-cross, Bollinger bandwidth/squeeze/breakout** (added for the new baskets).
4. **Deterministic pre-filter (`_passes_prefilter`)** — the cost-control gate. Only strategies whose hard technical rules are met on this ticker proceed. If none pass, the ticker is skipped entirely (`PREFILTER_SKIP` logged) with **no news fetch and no Claude call**. This collapses ~1,500 Claude calls/cycle to a few dozen on a 500-name universe. Fail-open: an unknown strategy passes through to Claude.
5. For surviving candidates only: fetch Google News RSS (macro/sector/company), then call **Claude Sonnet 5** (`_call_claude`) with the precomputed indicators + headlines. Claude returns strict JSON: `verdict` (TRADE/PASS), `technical_score`, `fundamental_score`, `confidence_score`, `rationale`, `disqualifying_factors`. Rubric: reject far more than approve; `confidence = 0.6·technical + 0.4·fundamental`.
6. Every evaluation is logged to `cycle_log`. **Gate:** keep only `verdict==TRADE` AND `confidence ≥ MIN_SIGNAL_CONFIDENCE (75)`. Across all tickers, the **single highest-confidence** signal is returned (or `None`). Trade frequency is an *output*, never a target.

### Step 3 — Risk Sizer  (`risk_sizer/sizer.size_position`)
Deterministic, no LLM ("the compliance desk"). Six checks, return on first failure:
```
1. Weekly P&L < -10% of capital            → reject (drawdown budget)
2. >= 5 concurrent open positions          → reject
3. Already holding this ticker             → reject (no pyramiding)
4. Sector already >= 35% of capital        → reject (correlation)
5. Free capital < ₹10,000                  → reject
Sizing: base = 20% of capital, scaled by confidence (70%→0.60×, 100%→1.00×),
        capped at free_capital×0.95 and sector headroom; if < ₹10,000 → reject.
```
Returns `capital_to_deploy` (INR); the share quantity is filled later at live price.

### Step 4 — QC / Fact-Checker  (`qc_factchecker/validator.validate_signal`)
**GPT-5.5**, independent lab, adversarial: its only job is to try to **falsify** the thesis (technical claims vs numbers, unverifiable fundamentals, regime mismatch, recency/narrative bias, sector risk). Structured-Outputs JSON. Verdict ∈ {`AGREE`, `DISAGREE`, `NEEDS_MORE_DATA`}. **Fail-safe:** any failure (missing key, parse error, API error) → `NEEDS_MORE_DATA`, which **blocks**. **Gate (in `run_cycle`):** only `AGREE` proceeds.

### Step 5 — Log + Alert + Mirror
`log_signal(signal, sizing, qc_verdict)` writes the `signals` row → `update_signal_alert_sent` → `send_trade_alert` (Resend email with HMAC Approve/Reject links) → mirror to Google Sheets (non-blocking; failure only warns).

---

## 6. Approval & execution

### Approval webhook  (`webhook_server.email_action` → `alerts.gmail_alert.handle_email_action`)
`GET /email_action?action=approve|reject&id=<n>&token=<hmac>`. The token is `HMAC-SHA256(action|id, APPROVAL_SECRET)`, verified with `hmac.compare_digest`. Invalid params → 400; bad token → 403.

- **Approve:** mark signal `APPROVED`; build a **Kite basket order URL** (pre-filled BUY qty×ticker); the server **302-redirects the user's browser to Kite**, where the user taps *Place Order* in their own authenticated session. A confirmation email is also sent. **No server-side order placement.**
- **Reject:** mark `REJECTED`.
- **No response by EOD:** the 15:35 sweep marks the signal `NO_RESPONSE` (a *locked design decision* — default is NO TRADE; auto-fire on timeout is deferred to month 4+).

### Trader  (`trader/kite_client.py`) — *present but not wired into the approval flow*
Contains `execute_trade()` with India-microstructure circuit breakers applied *before* any order:
- **ADV:** order shares must be < `MAX_ADV_PCT` (2%) of 20-day avg daily volume.
- **Circuit buffer:** skip if LTP within `CIRCUIT_BUFFER_PCT` (1.5%) of the day's upper/lower circuit.
- **Liquidity:** skip if 20-day avg turnover < `MIN_DAILY_TURNOVER_INR` (₹3 Cr).
- **Corp-action blackout (3d):** TODO, not implemented.
- PAPER_MODE logs the intended order; LIVE places a CNC market order via Kite.

> **Because the approve path redirects to a manual Kite basket instead of calling `execute_trade`, these microstructure checks are NOT applied to real orders today, and no `trades` row is created on approval.** See §13 Open Issue #1.

---

## 7. Monitor  (`monitor/position_monitor.check_open_positions`) — daily 15:35 IST
For each open position (`get_open_positions()`):
1. Fetch current LTP from Kite.
2. **Exit stop** = the higher of:
   - hard stop = `entry × (1 − 7%)`, and
   - trailing stop = `peak × (1 − 20%)`, where `peak = max(entry, all logged check prices, today)` — reconstructed from the `price_at_check` column, **no schema change**.
   Early on the hard stop binds; once the trade runs up, the trailing stop takes over. (This exit was chosen by backtest: 20% trailing beat 7% trailing, time exits, and profit targets; a profit target *reduced* expectancy, so there is none — §12.)
3. **Regime invalidation:** if entered in `bull`→ now `bear/crash`, `sideways`→`crash`, `euphoria`→`bear/crash`, the thesis is dead.
4. Status = `INVALIDATED` (stop or regime) / `WEAKENING` (P&L ≤ −3%) / `INTACT`; logged to `position_checks`; email sent for INVALIDATED/WEAKENING. **The monitor alerts; it does not auto-close positions** (consistent with the manual-execution model).

---

## 8. EOD sweep  (`main.run_eod_sweep`) — 15:35 IST
`send_eod_missed_opportunities()` marks any still-`PENDING` signals from today as `NO_RESPONSE`; `send_daily_cycle_summary()` emails a summary every day (even zero-signal days, for liveness visibility).

---

## 9. Weekly Auditor  (`auditor/weekly_audit.run_weekly_audit`) — Fri 16:00 IST
**Gemini 3.5 Pro**, whole week (signals + trades + position checks) in one 2M-token prompt. Three tasks: (1) **confidence calibration** by bucket (60–74 / 75–84 / 85+): did the confidence numbers predict actual win rates? (2) **missed-opportunity** review (NO_RESPONSE/REJECTED/SKIPPED); (3) **hypothesis backlog** — specific, testable ideas. Writes to `weekly_audits`. **Locked rule:** hypotheses are *paper-tested*, never auto-applied to the live Researcher. A `calibration_flag` triggers a Gmail alert.

---

## 10. Universe refresh  (`universe/universe_refresher.run_universe_refresh`) — Sun 20:00 IST
Runs a Screener.in fundamental screen: `ROCE>20 AND Debt/Equity<0.5 AND Sales growth>15 AND Market Cap>300 Cr`. Splits new candidates into **strong** (≥₹500 Cr) vs **watch** (<₹500 Cr) and flags degraded existing names. Emails a report. **Human decides** whether to add names. *Note:* the live universe is currently the sector-mapped **Nifty 500** (see §11) rather than a Screener export; the refresher is discovery/advisory.

---

## 11. Strategy library, regime baskets, exit (the redesign)

**Universe:** `universe/universe.csv` = **Nifty 500**, sector-mapped (18 sectors; 6 minor names tagged `OTHER`). Self-reconstitutes quarterly at the index level; NSE liquidity screening is built in. Replaced a hand-curated ~62-name SME/micro list that had heavy `NO_TOKEN`/thin-history rot.

**Strategy library (8 tested; 4 live).** Trend/breakout: `52wk_breakout`, `supertrend_buy`, `golden_cross_ema`, `adx_bull_strength`, `bb_squeeze_break`. Mean-reversion: `bb_mean_reversion`, `rsi_mean_reversion`, `cci_recovery`.

**Live `STRATEGY_BASKETS` (evidence-based):**
```
BULL      : 52wk_breakout, bb_squeeze_break, adx_bull_strength
EUPHORIA  : adx_bull_strength, bb_squeeze_break, 52wk_breakout
SIDEWAYS  : 52wk_breakout, bb_squeeze_break, golden_cross_ema
BEAR      : []   (cash)
CRASH     : []   (cash)
```
Rationale from the 10-year, cost-adjusted, regime-split backtest (§12):
- **Breakout family is the engine in every long-tradable regime** (PF ~2.1–3.7 net of 50 bps), including sideways (consolidation breakouts catch the next leg).
- **`supertrend_buy` removed from BULL** — weakest trend strategy there (PF ~1.5) with −93% additive drawdown (whipsaws in strong trends).
- **Mean-reversion removed from all live baskets** — on the full Nifty-500 universe with costs it posts PF 1.6–2.1 in sideways with −82% to −91% drawdowns (knife-catching on smaller-caps); beaten by breakouts everywhere.
- **Bear/Crash = cash** — the backtest's apparent edge there is hold-into-recovery bias (regime tagged at entry, 20% trail holds into the rebound), not tradable long-only.

**Exit design (in the Monitor):** −7% hard stop + 20% trailing stop, **no profit target, no time exit**. Backtest-validated: 20% trailing beat 7% trailing (PF ~0.95 → ~3.6 on large-caps) and both time exits and a 20% target, which *reduced* expectancy by capping the fat-tailed winners.

**Pre-filter (`_passes_prefilter`)** — deterministic hard gates mirroring the entry rules, permissive by design, fail-open:
```
52wk_breakout    : within 1.5% of 52wk high AND vol≥1.5× AND 50≤RSI≤70 AND Supertrend GREEN
bb_squeeze_break : bb_squeeze AND bb_breaking_upper AND RSI>50
adx_bull_strength: ADX>25 AND +DI>-DI AND above 50-SMA
golden_cross_ema : EMA50>EMA200 AND fresh golden cross
(mean-reversion rules retained in code but not in any live basket)
```

---

## 12. Backtesting harness  (`streak_backtests/backtest.py`)

Standalone, offline, **free Yahoo Finance data** (`pip install yfinance`) — no Kite token, no LLM. Replays 10y daily bars per name, applies each strategy's exact entry rules bar-by-bar (no look-ahead), simulates one position at a time.

- **Exits (configurable):** `--stop` (7), `--trail` (20), `--time-exit` (45), `--target` (0), plus a `combo` variant. Round-trip cost via `--cost-bps` subtracted per trade.
- **Regime split:** `--by-regime` tags each trade by the market regime **at entry** (Nifty vs 200-EMA + India VIX) and reports per strategy × regime.
- **Metrics:** Trades, Win%, AvgWin%, AvgLoss%, **Expect%** (mean return/trade), **PF** (gross win/gross loss), MaxDD% and TotP&L% on an **additive** 20%-per-trade model (chosen to avoid a compounding blow-up; not a real portfolio curve).
- **Reviewer caveats baked into the output:** uses **today's** index members (survivorship bias inflates results), no slippage beyond the flat cost, breadth omitted from the regime tag, and the additive drawdown is not the live portfolio drawdown (live caps at 5 concurrent positions).

This harness is how every strategy/exit/universe decision in §11 was made. It is a *research* tool, not part of the live trading path.

---

## 13. Known limitations, gaps & open risks (for the review)

**Open Issue #1 — Execution↔ledger linkage (highest priority).** The approve flow redirects to a manual Kite basket and marks the signal `APPROVED`, but does **not** call `execute_trade` or create a `trades` row. Consequences: (a) the microstructure circuit breakers (ADV/circuit/liquidity) are **not applied to real orders**; (b) the Monitor and Portfolio Risk Gate see **no open positions** (deployed capital stays 0, stops never fire) because nothing is written to `trades`. Needs a fill-confirmation path (poll Kite orders/positions, or a broker callback) that writes the executed trade back to the ledger.

**Other items:**
2. **Config drift:** `.env.example` references `GMAIL_APP_PASSWORD` but code uses Resend (§4).
3. **Two sector caps** with different names/values (30% gate vs 35% sizer) — confirm intended.
4. **Regime inertia is in-memory** (`_regime_history`), reset on every redeploy — a deploy mid-session loses smoothing state.
5. **Survivorship bias** in all backtests (current index members); treat live expectancy as *lower* than backtested.
6. **Regime-tag leakage** in the backtest: entry-regime + long trailing holds credit recovery gains to bear/crash — those rows are not tradable signals (already excluded from baskets, but worth understanding).
7. **Sector map staleness:** 494/500 names mapped from a 2019 NSE industry list + a hand-built supplement; 6 names tagged `OTHER`. Macro-sector granularity only.
8. **Single-process deployment:** Flask + scheduler in one Railway process; no redundancy. If it crashes between the hourly cycle and restart, that cycle is missed (`restartPolicyMaxRetries: 3`).
9. **Kite token is a daily manual step** (tap the 07:30 email) unless `auto_refresh_kite_token.py` is configured (needs stored credentials — a security tradeoff).
10. **Pre-filter thresholds are defaults, not tuned;** fail-open means a new strategy without a rule routes everything to Claude (cost, not correctness, risk).
11. **LLM determinism:** Researcher/QC verdicts are not reproducible run-to-run; the `cycle_log` captures each verdict for audit.
12. **No unit tests around the new indicators/pre-filter** beyond ad-hoc self-tests (tests/ covers regime, sizer, portfolio risk, kite microstructure).

---

## 14. Security-sensitive surface (for the review)

- **Secrets:** all via env (`.env` git-ignored). Keys: Anthropic/OpenAI/Google/Kite/Resend + `APPROVAL_SECRET`.
- **Approval links:** HMAC-SHA256 signed, constant-time compared. Single action per link; no auth beyond the signed token — anyone with the link can approve. Consider link expiry/nonce.
- **Diagnostic endpoints** (`/status`, `/cycle_history`, `/diagnose_cycle`, `/diagnose_universe`, `/refresh_universe`, `/send_test_email`) are gated by `?secret=APPROVAL_SECRET`. `/status` returns booleans only (no secret values).
- **Order placement:** never automated; the human places every order in their own Kite session. No funds move server-side.
- **`credentials/`** holds Google OAuth artifacts (`oauth_client.json`, `authorized_user.json`) — ensure these are git-ignored and reviewed.
- **Prior automated scan:** `.gstack/security-reports/` already contains a report dated 2026-08-17.

---

## 15. Entry points & how to run

```bash
# One-time
python setup/init_db.py                 # create ledger tables

# Local (runs scheduler + webhook)
python webhook_server.py

# Manual single actions
python main.py cycle                    # one research cycle now
python main.py monitor                  # position monitor now
python main.py audit                    # weekly audit now
python main.py eod                      # EOD sweep now

# Daily (until token auto-refresh is set up), before 09:15 IST
python setup/refresh_kite_token.py

# Offline research (no Kite/LLM)
python streak_backtests/backtest.py --universe streak_backtests/nifty500.csv --trail 20 --cost-bps 50 --by-regime
```

**Deployment:** Railway builds with Nixpacks and runs `python webhook_server.py`; health check `/health`. All env vars set in Railway → Variables. Kite app redirect URL must point to `<RAILWAY_URL>/kite_callback`.

---

*End of document. The single most important thing for a reviewer to resolve first is Open Issue #1 (execution↔ledger linkage) — until then the system generates and approves signals but does not track resulting positions, so exits and portfolio risk are effectively dormant in the live path.*


---

<!-- AUTONOMOUS DECISION LOG -->
## Decision Audit Trail

Produced by `/autoplan` on 2026-08-19 (branch `main`, commit `0ecac28`).
Restore point: `~/.gstack/projects/vrunmadan-ai-trading-system/main-autoplan-restore-20260819-172742.md`

| # | Phase | Decision | Class | Principle | Rationale | Rejected |
|---|-------|----------|-------|-----------|-----------|----------|
| 1 | CEO 0C-bis | Approach C (compute qty + log trade at approval; no server-side order) | Taste | P5, P3 | Fixes C1-C4 without reintroducing server-side order placement, which the design deliberately removed | A (wire execute_trade), B (fill reconciler -> TODOS) |
| 2 | CEO 0F | Mode = SELECTIVE EXPANSION | Mechanical | autoplan default | Feature iteration on an existing system | EXPANSION, HOLD, REDUCTION |
| 3 | CEO S1 | Verify Railway volume; fail loudly if LEDGER_DB_PATH is non-persistent | Mechanical | P2 | Ledger loss on redeploy is silent and total | defer |
| 4 | CEO S2 | Name a rescue action for all 6 error gaps; alert on GAP-A and GAP-B | Mechanical | P1 | Silent degradation of the exit system is indistinguishable from normal operation | log-only |
| 5 | CEO S3 | compare_digest on diagnostics; split DIAGNOSTIC_SECRET from APPROVAL_SECRET; header not query param; generic error page; delimit untrusted headlines | Mechanical | P1, P5 | One-line fixes; secret reuse couples a log leak to approval forgery | defer S4 link expiry to TODOS |
| 6 | CEO S4 | Compute quantity at approval from live LTP; write trade row; guard double-approve and post-sweep approval | Mechanical | P1, P2 | Email shows 0 shares, basket shows 1 share, intent is ~2 lakh | defer |
| 7 | CEO S6 | Add tests: approval path, qty conversion, monitor exits, peak reconstruction, ledger round trip, prefilter golden set | Mechanical | P1 | Every critical finding lives in untested code | partial coverage |
| 8 | CEO S7 | Per-day bar cache + rate limiting w/ backoff + batched cycle_log writes + index cycle_log(cycle_at) | Mechanical | P3 | 3,500 redundant fetches/day; 429s masquerade as quiet markets | defer |
| 9 | CEO S8 | Daily summary gains a health block incl. explicit LEDGER row counts | Mechanical | P1 | Would have made the severed trades edge visible on day one | defer |
| 10 | CEO S9 | PAPER_MODE must gate the approval path | Mechanical | P1, P5 | Approving in paper mode currently opens a real pre-filled Kite order | defer |
| 11 | CEO S11 | Design phase skipped (no UI scope); approval-email content logged to TODOS | Mechanical | detection rule | Only false-positive UI matches; email is flagged separately | run design phase |
| 12 | ENG S0 | Approach C scope = 5 files; complexity gate NOT triggered | Mechanical | eng rule | 5 files, 0 new classes/services, under the 8-file threshold | AskUserQuestion scope-reduction gate |
| 13 | ENG S0 | Bundle TODOS item 5 (sequential generation + redundant news fetch) into scope | Mechanical | P2 | Same file, same cycle; the per-day cache fix touches it anyway | keep deferred |
| 14 | ENG S3 | REGRESSION RULE fired: 5 tests added as critical requirements for commit 0ecac28 | Mechanical | iron rule | The exit redesign shipped with zero test coverage | AskUserQuestion (rule forbids) |
| 15 | ENG S4 | Per-day bar cache + rate limiting + batched cycle_log + index | Mechanical | P3 | 3,500 redundant fetches/day; 429s render as quiet markets | defer |
| 16 | DX | Regenerate .env.example from code (47 keys, 24 documented) | Mechanical | P1 | RESEND_API_KEY undocumented -> system is silently mute | defer |
| 17 | DX | Add `python main.py doctor` preflight | Mechanical | P5 | Single command to answer "is it configured correctly" | defer |
| 18 | DX | Rewrite README.md; make the architecture doc canonical | Mechanical | P4 DRY | README and the arch doc disagree about how the system works | leave both |
| 19 | ENG-VOICE | Integrate 10 new findings from the independent Eng subagent into TODOS.md | Mechanical | P1 | Cold reader surfaced defects the primary pass missed; 4 spot-verified by re-execution | discard |
| 20 | ENG-VOICE | Reorder work: fix the quantity bug BEFORE the ledger redesign | Mechanical | P5 | A PENDING trades row written from sized_quantity would record quantity 1 | original order |
| 21 | ENG-VOICE | Accept the correction that breakers are dormant, not structurally dead | Mechanical | accuracy | execute_trade can still write rows if invoked from tests or manually | keep original wording |
