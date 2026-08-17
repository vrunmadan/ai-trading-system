# AI Trading System — Project Scaffold

Status: **code complete, awaiting API credentials + paper-mode validation**.
All modules are implemented. `PAPER_MODE=true` is the default — no real orders until
the pipeline has been validated end-to-end in paper mode first.

## Architecture

```
Researcher  -->  Risk Sizer  -->  QC / Fact-Checker  -->  Gmail Alert
   (Claude)        (rules)          (GPT-5.5)             (approve/reject,
                                                            full trading-day
                                                            window, defaults
                                                            to NO TRADE if
                                                            you don't respond)
                                                                  |
                                                                  v
                                                              Trader
                                                          (Kite Connect API,
                                                           paper mode first)
                                                                  |
                                                                  v
                                                              Monitor
                                                        (daily stop/thesis check)
                                                                  |
                                                                  v
                                                              Auditor
                                                          (weekly, Gemini 3.5 Pro,
                                                           shadow-backtest +
                                                           hypothesis backlog —
                                                           does NOT directly
                                                           edit the live
                                                           Researcher)
```

Five regime buckets the Researcher classifies into before picking a strategy:
extreme fear/crash, bear, sideways, bull, euphoria. Each bucket has its own
basket of candidate strategies (to be filled in — start with momentum/breakout
for bull, mean-reversion for sideways, defensive/cash-heavy for crash, etc.)

## Locked design decisions (don't relitigate these without good reason)

- Default on no response to an alert = **NO TRADE**, logged as a missed
  opportunity for the Auditor. Auto-fire-on-timeout is explicitly deferred to
  month 4+, and even then only as a smaller-size, higher-confidence-bar,
  circuit-breakered mode — never the default.
- Trade frequency is an *output* of how many setups clear the confidence bar
  that week, not an input target. Do not add a "hit N trades/week" rule
  anywhere in this codebase.
- QC/Fact-Checker is instructed to try to falsify the Researcher's thesis,
  not confirm it.
- Auditor's findings go into a hypothesis backlog and get paper-tested before
  they change anything live.

## What you need to set up before any module is functional

1. **Kite Connect** app + API key (console.zerodha.com). Free tier = orders/
   positions. ₹500/mo tier = + live/historical market data (needed by the
   Researcher).
2. **Anthropic API key** (console.anthropic.com) — Researcher.
3. **OpenAI API key** (platform.openai.com) — QC/Fact-Checker (GPT-5.5).
4. **Google AI Studio key** (aistudio.google.com) — Auditor (Gemini 3.5 Pro).
5. **Resend API key** (resend.com, free tier) + your Gmail address — used to
   send the approve/reject alert email. See `alerts/gmail_alert.py` for the
   full list of required `.env` keys (`RESEND_API_KEY`, `ALERT_EMAIL`,
   `RAILWAY_URL`, `APPROVAL_SECRET`).
6. **Hosting** — Railway.app (or your own VPS) for the always-on backend.
   Claude Cowork / Desktop scheduled tasks stop when the desktop sleeps, so
   this pipeline cannot live there long-term.

Copy `.env.example` to `.env` and fill in the values above. **Never paste
real API keys or secrets into a chat with Claude or anyone else** — only
ever put them in `.env`, which should stay out of version control.

## Build order (matches the task list)

1. Project scaffold (this commit)
2. Researcher — regime classification + signal generation, paper data only
3. Risk Sizer — position sizing + exposure/correlation check
4. QC/Fact-Checker — independent model, adversarial validation
5. Gmail alert + approve/reject webhook
6. Trader — Kite Connect wrapper, paper mode first
7. Monitor — daily open-position check
8. Auditor — weekly shadow-backtest + hypothesis backlog
9. Deploy to Railway
10. Run in paper mode for several weeks before any real order placement

## Deployment to Railway (always-on backend)

Railway keeps the scheduler + webhook server alive 24/7 without your laptop.

**Step 1 — Push to Git**
```bash
git init && git add . && git commit -m "initial"
# Create a repo on GitHub, then:
git remote add origin https://github.com/you/ai-trading-system.git
git push -u origin main
```

**Step 2 — Connect to Railway**
1. Go to railway.app → New Project → Deploy from GitHub repo
2. Add all `.env` values as Railway environment variables (Settings → Variables)
   Do NOT commit `.env` — it contains secrets.
3. Railway auto-deploys from your `main` branch. The `railway.json` and `Procfile`
   tell it to run `python webhook_server.py`.

**Step 3 — Verify**
```bash
# Visit https://your-app.railway.app/health   # should return {"status": "ok"}
```
Trade approval runs over Gmail (see "What you need to set up" above) — no webhook
registration needed, the approve/reject links in the alert email are self-contained.

**Local testing** (before Railway):
```bash
python webhook_server.py   # runs scheduler + webhook server locally
```

**Daily morning routine (5 min, before 9:15 AM IST)**:
```bash
python setup/refresh_kite_token.py   # refresh Kite access token
# Copy the printed access token to Railway env vars → KITE_ACCESS_TOKEN
```
Once you automate the token refresh (month 2+), this becomes zero-touch.

## Data sources

- Quant/technical: Kite Connect historical + quote APIs (official, paid tier).
- Qualitative/news/sentiment: web search tool calls from the Researcher model
  itself, not scraped from Screener/Tickertape/Trendlyne. Those sites have no
  official API — premium subscriptions there improve the website, not
  programmatic access. Unofficial scrapers exist but are fragile and sit in
  a ToS gray zone; treat as an optional later add-on, not a dependency.
