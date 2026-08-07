# Streak Backtests

Streak (streak.zerodha.com) is used for backtesting the strategy logic for
each regime bucket before any of it is wired into the live pipeline.

## The rule: backtest first, code second

Don't write a single line of Kite Connect strategy logic until the
corresponding Streak backtest shows it has a positive expectancy over at
least 6 months of historical data. This folder documents what passed,
what failed, and what was changed as a result.

## One file per regime + strategy

Name format: `{regime}_{strategy_name}_backtest.md`

Example: `bull_52wk_breakout_backtest.md`

Each file should capture:
- Strategy logic (in plain English, matching what you built in Streak)
- Backtest period and universe
- Win rate, average win, average loss, max drawdown
- Decision: adopted / rejected / modified

## Regime → starting strategy suggestions

These are starting points to test. Replace them with what the data shows.

| Regime | Strategy to test first |
|---|---|
| Bull | 52-week high breakout + volume > 1.5x 20d avg |
| Bull | Supertrend (10,3) buy signal, hold until red flip |
| Sideways | RSI mean reversion: buy < 35, sell > 65 |
| Sideways | Bollinger Band bounce on daily chart |
| Bear | Avoid longs. Test: cash / short Nifty via F&O |
| Extreme fear | Avoid longs entirely. Capital preservation. |
| Euphoria | Tighten stops on existing longs. No new entries. |

## How to build a strategy in Streak

1. Go to streak.zerodha.com → Create Strategy
2. Select "Nifty 500" or your universe as the stock group
3. Set the entry/exit conditions using the no-code builder
4. Run backtest: at least 1 year of data, ideally 3-5 years
5. Check: Win rate > 50%, Profit factor > 1.5, Max drawdown < 20%
6. If it passes: document here, then translate the logic to the
   Researcher's strategy_bucket in signal_generator.py

## Transferring logic to the pipeline

Once a strategy passes backtesting, its entry signal becomes a
`technical_score` component in `researcher/signal_generator.py`.
The Streak logic tells you exactly what conditions to check via
Kite Connect's historical OHLC data.
