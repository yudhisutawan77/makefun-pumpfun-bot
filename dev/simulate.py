#!/usr/bin/env python3
"""Synthetic replay driver for main.py (dev-only, not part of the entry).

Generates its OWN pump.fun-style bonding-curve tape: fresh coins are born,
trade with regime-dependent flow (runner / rug / grinder / flat), and die
or graduate. It speaks the same agent-fun/replay/v2 wire protocol the
real harness speaks (hello, batch, done on stdin; one reply per batch on
stdout), executes the bot's orders with latency, fees, slippage and the
hello caps, and reports net PnL vs hold cash, drawdown, fills and
distinct mints.

This is a smoke test, not the official grade: the tape is synthetic, so
a win here only proves the bot is protocol-correct, deterministic, and
not obviously money-losing on plausible flow. Run: python3 simulate.py
[--seed N] [--coins N] [--ticks N]
"""

import json
import random
import subprocess
import sys

LAMPORTS_PER_SOL = 1_000_000_000
START_SLOT = 500_000_000
SLOTS_PER_TICK = 25
VT0_SOL = 30_000_000_000            # virtual SOL at birth
VT0_TOK = 1_073_000_000_000_000      # virtual tokens at birth
RT0_TOK = 793_100_000_000_000        # real tokens at birth
FEE_BPS = 125                       # protocol 95 + creator 30
TX_FEE = 105_000                    # base 5000 + priority 100000
CAPITAL = 10 * LAMPORTS_PER_SOL
MAX_POSITIONS = 5
MAX_ORDER_BPS = 100
MAX_ORDERS = 16

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def fake_addr(rng):
    return "".join(rng.choice(B58) for _ in range(44))


def fake_sig(rng):
    return "".join(rng.choice(B58) for _ in range(88))


class SimCoin:
    def __init__(self, rng, mint, born_tick, regime):
        self.mint = mint
        self.born_tick = born_tick
        self.regime = regime
        self.vsol = VT0_SOL
        self.vtok = VT0_TOK
        self.real_sol = 0
        self.real_tok = RT0_TOK
        self.creator = fake_addr(rng)
        self.user_pool = [fake_addr(rng) for _ in range(40)]
        self.alive = True
        self.completed = False

    def price(self):
        return self.vsol / self.vtok

    def buy_pressure(self, age_ticks):
        """Probability a trade this tick is a buy, by regime and age."""
        if self.regime == "runner":
            return 0.78 if age_ticks < 120 else 0.30
        if self.regime == "rug":
            return 0.72 if age_ticks < 25 else 0.18
        if self.regime == "grinder":
            return 0.62
        return 0.50

    def trade_rate(self, age_ticks):
        if self.regime == "runner":
            return 2.2 if age_ticks < 120 else 0.7
        if self.regime == "rug":
            return 2.6 if age_ticks < 25 else 0.4
        if self.regime == "grinder":
            return 0.9
        return 0.55


def trade_size_sol(rng):
    roll = rng.random()
    if roll < 0.70:
        return rng.uniform(0.005, 0.08)
    if roll < 0.95:
        return rng.uniform(0.08, 0.6)
    return rng.uniform(0.6, 2.5)


def run_sim(seed=7, n_coins=240, n_ticks=2160, bot_path="main.py"):
    rng = random.Random(seed)
    regimes = ["runner"] * 12 + ["rug"] * 28 + ["grinder"] * 15 + ["flat"] * 45
    coins = []
    for i in range(n_coins):
        mint = fake_addr(rng)
        born = rng.randint(0, n_ticks - 200)
        coins.append(SimCoin(rng, mint, born, rng.choice(regimes)))
    coins.sort(key=lambda c: (c.born_tick, c.mint))

    proc = subprocess.Popen(
        [sys.executable, bot_path],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    stdin, stdout = proc.stdin, proc.stdout

    hello = {
        "type": "hello", "protocol": "agent-fun/replay/v2", "seed": str(seed),
        "universe": {"program": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
                     "quote": "SOL", "mayhem": "excluded",
                     "exclude_cashback": True,
                     "sample": {"hash": "sha256", "modulus": 16, "residue": 15}},
        "capital": {"lamports": str(CAPITAL)},
        "caps": {"max_open_positions": MAX_POSITIONS,
                 "max_order_bps_of_real_reserve": MAX_ORDER_BPS,
                 "max_orders_per_batch": MAX_ORDERS,
                 "shorts": False, "leverage": False},
        "latency_slots": 2,
        "fees": {"protocol_bps_fallback": 95, "creator_bps_fallback": 30,
                 "base_fee_lamports": "5000", "priority_fee_lamports": "100000",
                 "failed_tx_bps": 200},
    }
    stdin.write(json.dumps(hello) + "\n")
    stdin.flush()

    cash = CAPITAL
    positions = {}          # mint -> {"tokens": int, "cost": int}
    pending = []            # orders queued for execution next tick
    chain_seq = 0
    fills_total = 0
    round_trips = set()
    bought = {}             # mint -> buy lamports (for round-trip check)
    equity_peak = CAPITAL
    max_dd = 0.0
    replies = []

    def mark_price(coin):
        return coin.price()

    def equity_now():
        total = cash
        for mint, pos in positions.items():
            coin = by_mint[mint]
            total += int(pos["tokens"] * mark_price(coin) * 0.9875)
        return total

    by_mint = {c.mint: c for c in coins}

    for tick in range(n_ticks):
        slot = START_SLOT + tick * SLOTS_PER_TICK
        events = []

        # 1. Execute orders queued last tick at current curve state.
        fills = []
        still_pending = []
        for order in pending:
            coin = by_mint.get(order["mint"])
            if coin is None or not coin.alive:
                fills.append(reject(order, slot, "not_in_universe"))
                continue
            if order["side"] == "buy":
                quote = int(order["quote_lamports"])
                if quote > cash:
                    fills.append(reject(order, slot, "insufficient_cash"))
                    continue
                if len(positions) >= MAX_POSITIONS and order["mint"] not in positions:
                    fills.append(reject(order, slot, "max_open_positions"))
                    continue
                if coin.real_sol > 0 and quote * 10000 > coin.real_sol * MAX_ORDER_BPS:
                    fills.append(reject(order, slot, "max_order_size"))
                    continue
                exec_price = mark_price(coin)
                if exec_price > order["decision_price"] * 1.01:
                    fills.append(reject(order, slot, "max_slippage"))
                    continue
                fee = quote * FEE_BPS // 10000 + TX_FEE
                net = quote - quote * FEE_BPS // 10000
                k = coin.vsol * coin.vtok
                new_vsol = coin.vsol + net
                new_vtok = k // new_vsol
                tokens = coin.vtok - new_vtok
                if tokens <= 0:
                    fills.append(reject(order, slot, "zero_amount"))
                    continue
                coin.vsol, coin.vtok = new_vsol, new_vtok
                coin.real_sol += quote
                coin.real_tok -= tokens
                cash -= quote + TX_FEE
                pos = positions.setdefault(order["mint"],
                                         {"tokens": 0, "cost": 0})
                pos["tokens"] += tokens
                pos["cost"] += quote
                bought[order["mint"]] = bought.get(order["mint"], 0) + quote
                fills.append(fill_rec(order, slot, "buy", quote, tokens,
                                      quote * FEE_BPS // 10000))
                fills_total += 1
            else:
                pos = positions.get(order["mint"])
                if pos is None or pos["tokens"] <= 0:
                    fills.append(reject(order, slot, "insufficient_tokens"))
                    continue
                tokens = min(int(order["token_amount"]), pos["tokens"])
                exec_price = mark_price(coin)
                if exec_price < order["decision_price"] * 0.99:
                    fills.append(reject(order, slot, "max_slippage"))
                    continue
                k = coin.vsol * coin.vtok
                new_vtok = coin.vtok + tokens
                new_vsol = k // new_vtok
                gross = coin.vsol - new_vsol
                fee = gross * FEE_BPS // 10000
                net = gross - fee
                coin.vsol, coin.vtok = new_vsol, new_vtok
                coin.real_sol -= net
                coin.real_tok += tokens
                cash += net - TX_FEE
                pos["tokens"] -= tokens
                pos["cost"] = pos["cost"] * pos["tokens"] // (
                    pos["tokens"] + tokens) if pos["tokens"] else 0
                if pos["tokens"] == 0:
                    del positions[order["mint"]]
                    if bought.get(order["mint"], 0) >= 10_000_000:
                        round_trips.add(order["mint"])
                fills.append(fill_rec(order, slot, "sell", net, tokens, fee))
                fills_total += 1
        pending = still_pending

        # 2. Generate this tick's market trades.
        for coin in coins:
            if not coin.alive or tick < coin.born_tick:
                continue
            age = tick - coin.born_tick
            if age > 900:
                coin.alive = False
                continue
            rate = coin.trade_rate(age)
            n_trades = rng.choices([0, 1, 2, 3, 4],
                                   weights=[0.25, 0.35, 0.22, 0.12, 0.06])[0]
            if rng.random() < rate / 2.0:
                n_trades += 1
            for _ in range(n_trades):
                is_buy = rng.random() < coin.buy_pressure(age)
                user = rng.choice(coin.user_pool)
                chain_seq += 1
                if is_buy:
                    q_sol = trade_size_sol(rng)
                    quote = int(q_sol * LAMPORTS_PER_SOL)
                    net = quote - quote * FEE_BPS // 10000
                    k = coin.vsol * coin.vtok
                    new_vsol = coin.vsol + net
                    new_vtok = k // new_vsol
                    tokens = coin.vtok - new_vtok
                    if tokens <= 0:
                        continue
                    coin.vsol, coin.vtok = new_vsol, new_vtok
                    coin.real_sol += quote
                    coin.real_tok -= tokens
                    sol_amt, tok_amt = quote, tokens
                else:
                    tokens = int(rng.uniform(0.0005, 0.02) * coin.vtok)
                    tokens = min(tokens, coin.vtok // 2)
                    if tokens <= 0:
                        continue
                    k = coin.vsol * coin.vtok
                    new_vtok = coin.vtok + tokens
                    new_vsol = k // new_vtok
                    gross = coin.vsol - new_vsol
                    coin.vsol, coin.vtok = new_vsol, new_vtok
                    coin.real_sol = max(0, coin.real_sol - gross)
                    coin.real_tok += tokens
                    sol_amt, tok_amt = gross, tokens
                events.append(trade_event(rng, coin, is_buy, sol_amt,
                                          tok_amt, user, slot, chain_seq))
                if coin.vsol >= 85_000_000_000 and not coin.completed:
                    coin.completed = True
                    events.append(complete_event(rng, coin, slot, chain_seq))
                    coin.alive = False
        # CreateEvents for coins born this tick.
        for coin in coins:
            if coin.born_tick == tick:
                chain_seq += 1
                events.append(create_event(rng, coin, slot, chain_seq))

        marks = []
        for coin in coins:
            if coin.alive and tick >= coin.born_tick:
                marks.append({
                    "mint": coin.mint, "slot": slot,
                    "virtual_token_reserves": str(coin.vtok),
                    "virtual_quote_reserves": str(coin.vsol),
                    "real_token_reserves": str(max(0, coin.real_tok)),
                    "real_quote_reserves": str(max(0, coin.real_sol)),
                })

        account = {
            "cash_lamports": str(cash),
            "positions": [
                {"mint": m, "token_amount": str(p["tokens"]),
                 "cost_lamports": str(p["cost"])}
                for m, p in sorted(positions.items())
            ],
        }
        batch = {"type": "batch", "seq": tick, "slot": slot,
                 "events": events, "marks": marks, "fills": fills,
                 "account": account}
        stdin.write(json.dumps(batch) + "\n")
        stdin.flush()
        reply_line = stdout.readline()
        if not reply_line:
            print("BOT DIED at tick", tick)
            break
        replies.append(reply_line)
        try:
            reply = json.loads(reply_line)
        except ValueError:
            print("BAD REPLY at tick", tick, reply_line[:200])
            break
        assert reply.get("type") == "reply" and reply.get("seq") == tick, \
            "reply must echo batch seq"
        # Queue this batch's orders with their decision prices.
        decision_prices = {c.mint: mark_price(c) for c in coins
                           if c.alive and tick >= c.born_tick}
        for order in reply.get("orders", [])[:MAX_ORDERS]:
            order = dict(order)
            order["decided_slot"] = slot
            order["decision_price"] = decision_prices.get(order["mint"], 0)
            pending.append(order)

        eq = equity_now()
        if eq > equity_peak:
            equity_peak = eq
        dd = (equity_peak - eq) / equity_peak if equity_peak else 0
        if dd > max_dd:
            max_dd = dd

    stdin.write(json.dumps({"type": "done", "seq": n_ticks,
                            "reason": "end_of_data",
                            "account": {"cash_lamports": str(cash),
                                        "positions": []}}) + "\n")
    stdin.flush()
    proc.wait(timeout=30)

    final_equity = equity_now()
    pnl = final_equity - CAPITAL
    print("seed=%d coins=%d ticks=%d" % (seed, n_coins, n_ticks))
    print("net PnL: %.4f SOL (%d lamports)  vs hold cash: 0.0000 SOL"
          % (pnl / LAMPORTS_PER_SOL, pnl))
    print("final equity: %.4f SOL | max drawdown: %.2f%%"
          % (final_equity / LAMPORTS_PER_SOL, max_dd * 100))
    print("fills: %d | distinct mints round-tripped (>=0.01 SOL): %d"
          % (fills_total, len(round_trips)))
    print("open positions at end: %d | cash: %.4f SOL"
          % (len(positions), cash / LAMPORTS_PER_SOL))
    print("guardrail >=20 distinct mints: %s"
          % ("PASS" if len(round_trips) >= 20 else "FAIL"))
    return pnl, bytes("".join(replies), "utf-8")


def reject(order, slot, reason):
    return {"client_id": order["client_id"], "mint": order["mint"],
            "side": order["side"], "status": "rejected", "reason": reason,
            "decided_slot": order["decided_slot"], "slot": slot,
            "quote_lamports": "0", "token_amount": "0",
            "fee_lamports": "0", "tx_fee_lamports": "0",
            "fee_fallback": False}


def fill_rec(order, slot, side, quote, tokens, fee):
    return {"client_id": order["client_id"], "mint": order["mint"],
            "side": side, "status": "filled", "reason": None,
            "decided_slot": order["decided_slot"], "slot": slot,
            "quote_lamports": str(quote), "token_amount": str(tokens),
            "fee_lamports": str(fee), "tx_fee_lamports": str(TX_FEE),
            "fee_fallback": False}


def base_event(rng, coin, slot, chain_seq):
    return {"mint": coin.mint, "slot": slot,
            "signature": fake_sig(rng), "block_time": 1793600000 + slot // 25,
            "chain_seq": chain_seq}


def trade_event(rng, coin, is_buy, sol_amt, tok_amt, user, slot, chain_seq):
    ev = base_event(rng, coin, slot, chain_seq)
    ev.update({
        "event": "TradeEvent", "sol_amount": str(sol_amt),
        "token_amount": str(tok_amt), "is_buy": is_buy, "user": user,
        "timestamp": str(1793600000 + slot // 25),
        "virtual_sol_reserves": str(coin.vsol),
        "virtual_token_reserves": str(coin.vtok),
        "real_sol_reserves": str(max(0, coin.real_sol)),
        "real_token_reserves": str(max(0, coin.real_tok)),
        "fee_recipient": fake_addr(rng), "fee_basis_points": "95",
        "fee": str(sol_amt * 95 // 10000),
        "creator": coin.creator, "creator_fee_basis_points": "30",
        "creator_fee": str(sol_amt * 30 // 10000),
        "track_volume": True,
        "total_unclaimed_tokens": "0", "total_claimed_tokens": "0",
        "current_sol_volume": str(coin.real_sol),
        "last_update_timestamp": str(1793600000 + slot // 25),
        "ix_name": "trade", "mayhem_mode": False,
        "cashback_fee_basis_points": "0", "cashback": "0",
        "buyback_fee_basis_points": "0", "buyback_fee": "0",
        "shareholders": [], "quote_mint": "So11111111111111111111111111111111111111112",
        "quote_amount": str(sol_amt),
        "virtual_quote_reserves": str(coin.vsol),
        "real_quote_reserves": str(max(0, coin.real_sol)),
        "holder_rewards_bps": "0", "holder_rewards": "0",
        "creator_fee_unclaimed": "0",
    })
    return ev


def create_event(rng, coin, slot, chain_seq):
    ev = base_event(rng, coin, slot, chain_seq)
    ev.update({
        "event": "CreateEvent", "name": "SIM", "symbol": "SIM",
        "uri": "https://example.invalid/sim",
        "bonding_curve": fake_addr(rng), "user": coin.creator,
        "creator": coin.creator,
        "timestamp": str(1793600000 + slot // 25),
        "virtual_token_reserves": str(coin.vtok),
        "virtual_sol_reserves": str(coin.vsol),
        "real_token_reserves": str(coin.real_tok),
        "token_total_supply": "1000000000000000",
        "token_program": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "is_mayhem_mode": False, "is_cashback_enabled": False,
        "quote_mint": "So11111111111111111111111111111111111111112",
        "virtual_quote_reserves": str(coin.vsol),
        "creator_fee_bps": "30", "is_holder_reward": False, "depth": 6,
    })
    return ev


def complete_event(rng, coin, slot, chain_seq):
    ev = base_event(rng, coin, slot, chain_seq)
    ev.update({
        "event": "CompleteEvent", "user": coin.creator,
        "bonding_curve": fake_addr(rng),
        "timestamp": str(1793600000 + slot // 25),
        "quote_mint": "So11111111111111111111111111111111111111112",
    })
    return ev


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--coins", type=int, default=240)
    ap.add_argument("--ticks", type=int, default=2160)
    ap.add_argument("--bot", default="main.py")
    ap.add_argument("--twice", action="store_true",
                    help="run twice and compare bot stdout byte for byte")
    args = ap.parse_args()
    pnl1, out1 = run_sim(args.seed, args.coins, args.ticks, args.bot)
    if args.twice:
        print("--- second run (determinism check) ---")
        pnl2, out2 = run_sim(args.seed, args.coins, args.ticks, args.bot)
        print("deterministic stdout:", "YES" if out1 == out2 else "NO - MISMATCH")
        print("PnL identical:", "YES" if pnl1 == pnl2 else "NO")
