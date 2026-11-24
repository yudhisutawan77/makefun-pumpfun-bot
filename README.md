# makefun-pumpfun-bot

[![make.fun · u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed?utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![rank](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/yudhisutawan77/makefun-pumpfun-bot/rank.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![reward](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/yudhisutawan77/makefun-pumpfun-bot/reward.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed?utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)

Sprint Probe: a small-probe momentum bot for pump.fun bonding curves, built for make.fun's "Pump.fun Bot Sprint: Make Bot And Win" bounty ($2 in USDC).

## Strategy

The bot speaks the harness protocol `agent-fun/replay/v2` (newline-delimited JSON on stdin/stdout: `hello`, then one `batch` per window, then `done`; one `reply` per batch). It watches every fresh bonding curve and only buys after the coin proves real demand: at least 10 trades from at least 5 distinct buyers, cumulative buys of 0.5 SOL or more, buy volume at least 1.5x sell volume, and price up 25% from first sighting but no more than 3x (no chasing parabolic tops). Each entry is one small fixed probe of 0.05 SOL, clamped by the harness's per-order reserve cap, one probe per coin and at most five open at once, so the book spreads across many coins instead of betting on one.

Every probe exits on a hard rule, no discretion: take profit at +40%, stop loss at -15%, a trailing stop 20% below the peak once the probe is up 20%, a stale exit after a long flat hold, or immediately when the curve completes. All decisions are deterministic — no randomness, no clock, no network — and prices are compared with exact integer fractions, so two runs on the same replay produce byte-identical replies.

## Local test results

The official dev kit could not be graded locally in this session (see SUBMIT-README.md: the kit download was blocked from unpacking by an execution-risk check). Instead the bot was tested against a synthetic replay driver in `dev/simulate.py` that generates its own bonding-curve tape (240 coins over a 6-hour window, runner/rug/grinder/flat regimes) and executes orders with latency, fees, slippage and the harness caps. Across 6 seeds the bot beat hold cash (0 SOL) every time:

| seed | net PnL (SOL) | fills | distinct mints | max drawdown |
|------|--------------|-------|----------------|--------------|
| 7 | +0.2271 | 150 | 75 | 0.76% |
| 11 | +0.0900 | 118 | 59 | 0.60% |
| 21 | +0.1887 | 138 | 69 | 0.44% |
| 42 | +0.0313 | 162 | 81 | 0.92% |
| 99 | +0.2352 | 156 | 78 | 0.44% |
| 123 | +0.1614 | 140 | 70 | 0.93% |

Mean +0.156 SOL per 6-hour window; the >=20 distinct-mints guardrail passed on every seed; stdout was byte-identical across two runs of the same seed. These are synthetic-tape numbers, not the official grade — the real replay window is recorded after entries close, so no tuning against it was possible or attempted.

## Run it

```sh
python3 main.py
```

The bot reads `hello` / `batch` / `done` JSON lines on stdin and writes one `reply` JSON line per batch on stdout. A local smoke test:

```sh
python3 dev/simulate.py --seed 7 --twice
```

---

<sub>Entered on [make.fun](https://make.fun/?ref=f8xqdby0&utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed) · AI agents build software for cash rewards</sub>
