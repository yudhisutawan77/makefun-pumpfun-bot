#!/usr/bin/env python3
"""Sprint Probe — a small-probe momentum bot for the pump.fun replay.

Protocol: agent-fun/replay/v2 (newline-delimited JSON on stdin/stdout).
The harness sends one `hello`, then one `batch` per slot window, then a
`done`. For every batch the bot answers with exactly one `reply` echoing
the batch `seq`, carrying zero or more orders.

Strategy in one paragraph: watch every fresh bonding curve, wait until a
coin proves real demand (several distinct buyers, buy volume clearly
above sell volume, price up ~25% from where we first saw it, but not a
parabolic top), then take one small fixed probe (0.05 SOL, clamped by the
harness's per-order reserve cap). Every probe exits on a hard rule:
take profit +40%, stop loss -15%, a trailing exit 20% below the peak once
the probe is up 20%, a stale exit after a long flat hold, or immediately
when the curve completes. One probe per coin, at most five open at once,
so the bot spreads across many coins and never bets the book on one.

Everything is deterministic: no randomness, no wall clock, no network.
All decisions are pure functions of the messages seen so far, so two
runs on the same replay produce byte-identical replies. Prices are
compared with exact integer fractions, never floats, so there is no
platform float drift either.
"""

import json
import sys
from fractions import Fraction

LAMPORTS_PER_SOL = 1_000_000_000

# --- Strategy knobs (fixed constants, not tuned to any tape) ---------------
PROBE_LAMPORTS = 50_000_000          # 0.05 SOL per probe
MIN_ROUND_TRIP_LAMPORTS = 10_000_000  # guardrail: round trip >= 0.01 SOL
MIN_DISTINCT_BUYERS = 5              # demand must come from several wallets
MIN_BUY_SOL_LAMPORTS = 500_000_000  # >= 0.5 SOL of cumulative buys
MIN_TRADES_SEEN = 10                 # ignore coins with almost no flow
ENTRY_GAIN_NUM = 5                   # price must be up >= 25% ...
ENTRY_GAIN_DEN = 4                   # ... from first sighting
ENTRY_MAX_GAIN_NUM = 3               # ... but no more than 3x ...
ENTRY_MAX_GAIN_DEN = 1               # ... (do not chase a parabolic top)
FLOW_BUY_SELL_NUM = 3                # buy volume >= 1.5x sell volume
FLOW_BUY_SELL_DEN = 2
VSOL_FLOOR_LAMPORTS = 30_000_000_000   # virtual SOL below this: too newborn
VSOL_CEIL_LAMPORTS = 85_000_000_000    # virtual SOL above this: near graduation
TAKE_PROFIT_NUM = 7                  # sell at +40%
TAKE_PROFIT_DEN = 5
STOP_LOSS_NUM = 17                   # sell at -15%
STOP_LOSS_DEN = 20
TRAIL_ARM_NUM = 6                    # trailing stop arms once up 20%
TRAIL_ARM_DEN = 5
TRAIL_DROP_NUM = 4                   # ... then sell 20% below the peak
TRAIL_DROP_DEN = 5
STALE_SLOTS = 5000                   # flat hold longer than this: exit
MAX_SLIPPAGE_BPS = 100               # 1% max slippage on every order


def as_int(text):
    """Parse a u64 decimal string from the wire into an int."""
    return int(text)


def curve_price(virtual_sol, virtual_token):
    """Price of one token in SOL, as an exact fraction (sol per token)."""
    if virtual_token <= 0:
        return None
    return Fraction(virtual_sol, virtual_token)


class CoinTrack:
    """Everything the bot remembers about one mint."""

    __slots__ = (
        "mint", "born_slot", "first_price", "last_price", "first_slot",
        "buy_lamports", "sell_lamports", "trade_count", "buyers",
        "completed", "probed", "vsol", "vtok", "real_sol",
    )

    def __init__(self, mint, slot):
        self.mint = mint
        self.born_slot = slot
        self.first_price = None
        self.last_price = None
        self.first_slot = slot
        self.buy_lamports = 0
        self.sell_lamports = 0
        self.trade_count = 0
        self.buyers = set()
        self.completed = False
        self.probed = False
        self.vsol = 0
        self.vtok = 0
        self.real_sol = 0

    def note_price(self, price, slot):
        if price is None:
            return
        if self.first_price is None:
            self.first_price = price
            self.first_slot = slot
        self.last_price = price

    def gain_vs_first(self):
        """Current price as a multiple of the first-seen price, or None."""
        if self.first_price is None or self.last_price is None:
            return None
        return self.last_price / self.first_price

    def flow_buy_dominant(self):
        if self.sell_lamports == 0:
            return self.buy_lamports > 0
        return (self.buy_lamports * FLOW_BUY_SELL_DEN
                >= self.sell_lamports * FLOW_BUY_SELL_NUM)

    def looks_demand_proven(self):
        """Entry filter: real, broad, buy-led demand with a sane move."""
        if self.completed or self.probed:
            return False
        if self.trade_count < MIN_TRADES_SEEN:
            return False
        if len(self.buyers) < MIN_DISTINCT_BUYERS:
            return False
        if self.buy_lamports < MIN_BUY_SOL_LAMPORTS:
            return False
        if not self.flow_buy_dominant():
            return False
        if self.vsol < VSOL_FLOOR_LAMPORTS or self.vsol > VSOL_CEIL_LAMPORTS:
            return False
        gain = self.gain_vs_first()
        if gain is None:
            return False
        low = Fraction(ENTRY_GAIN_NUM, ENTRY_GAIN_DEN)
        high = Fraction(ENTRY_MAX_GAIN_NUM, ENTRY_MAX_GAIN_DEN)
        return low <= gain <= high


class HeldProbe:
    """An open probe: what we paid and the best price seen since."""

    __slots__ = ("mint", "entry_price", "peak_price", "entry_slot",
               "filled", "sell_pending")

    def __init__(self, mint, entry_price, entry_slot):
        self.mint = mint
        self.entry_price = entry_price
        self.peak_price = entry_price
        self.entry_slot = entry_slot
        # False until the harness reports the buy fill: the probe only
        # becomes a real position then, and only then can it exit.
        self.filled = False
        # True while a sell order for this probe is awaiting its fill,
        # so the exit loop does not re-issue the same sell each batch.
        self.sell_pending = False

    def exit_reason(self, price, slot):
        """Return a reason string when the probe must close, else None."""
        if price is None:
            return None
        if price > self.peak_price:
            self.peak_price = price
        take_profit = self.entry_price * Fraction(TAKE_PROFIT_NUM,
                                                  TAKE_PROFIT_DEN)
        if price >= take_profit:
            return "take-profit"
        stop_loss = self.entry_price * Fraction(STOP_LOSS_NUM,
                                                STOP_LOSS_DEN)
        if price <= stop_loss:
            return "stop-loss"
        armed = self.entry_price * Fraction(TRAIL_ARM_NUM, TRAIL_ARM_DEN)
        if self.peak_price >= armed:
            trail = self.peak_price * Fraction(TRAIL_DROP_NUM,
                                               TRAIL_DROP_DEN)
            if price <= trail:
                return "trailing-stop"
        if slot - self.entry_slot >= STALE_SLOTS:
            return "stale"
        return None


class ReplayBot:
    """Owns the harness session: hello config, per-mint tracks, probes."""

    def __init__(self):
        self.capital = 0
        self.max_open_positions = 5
        self.max_order_bps = 100
        self.max_orders_per_batch = 16
        self.latency_slots = 0
        self.coins = {}
        self.probes = {}
        self.order_counter = 0
        self.current_slot = 0

    # -- hello ------------------------------------------------------------
    def apply_hello(self, msg):
        self.capital = as_int(msg["capital"]["lamports"])
        caps = msg["caps"]
        self.max_open_positions = int(caps["max_open_positions"])
        self.max_order_bps = int(caps["max_order_bps_of_real_reserve"])
        self.max_orders_per_batch = int(caps["max_orders_per_batch"])
        self.latency_slots = int(msg["latency_slots"])

    # -- per-batch state --------------------------------------------------
    def track(self, mint, slot):
        coin = self.coins.get(mint)
        if coin is None:
            coin = CoinTrack(mint, slot)
            self.coins[mint] = coin
        return coin

    def apply_events(self, events, slot):
        for ev in events:
            kind = ev.get("event")
            mint = ev.get("mint")
            if mint is None:
                continue
            coin = self.track(mint, slot)
            if kind == "CreateEvent":
                coin.vsol = as_int(ev["virtual_sol_reserves"])
                coin.vtok = as_int(ev["virtual_token_reserves"])
                coin.real_sol = 0
                price = curve_price(coin.vsol, coin.vtok)
                coin.note_price(price, slot)
            elif kind == "TradeEvent":
                coin.vsol = as_int(ev["virtual_sol_reserves"])
                coin.vtok = as_int(ev["virtual_token_reserves"])
                coin.real_sol = as_int(ev["real_sol_reserves"])
                amount = as_int(ev["sol_amount"])
                if ev.get("is_buy"):
                    coin.buy_lamports += amount
                    coin.buyers.add(ev.get("user", ""))
                else:
                    coin.sell_lamports += amount
                coin.trade_count += 1
                price = curve_price(coin.vsol, coin.vtok)
                coin.note_price(price, slot)
            elif kind == "CompleteEvent":
                coin.completed = True

    def apply_marks(self, marks):
        for mark in marks:
            coin = self.coins.get(mark["mint"])
            if coin is None:
                continue
            coin.vsol = as_int(mark["virtual_quote_reserves"])
            coin.vtok = as_int(mark["virtual_token_reserves"])
            coin.real_sol = as_int(mark["real_quote_reserves"])
            price = curve_price(coin.vsol, coin.vtok)
            coin.note_price(price, mark["slot"])

    def sync_account(self, account, slot):
        """Reconcile probes with the harness account (the truth).

        A filled probe whose mint is no longer held has been closed:
        drop it. An unfilled probe that never fills within a grace
        window was rejected: drop it and let the coin be probed again
        later. Nothing else is dropped: a fresh buy takes a few slots
        (latency) to appear as a position, and dropping it early would
        strand the position with no exit rule.
        """
        held = {p["mint"] for p in account.get("positions", [])}
        for mint in list(self.probes):
            probe = self.probes[mint]
            if probe.filled:
                if mint not in held:
                    del self.probes[mint]
            elif slot - probe.entry_slot > 500:
                del self.probes[mint]
                coin = self.coins.get(mint)
                if coin is not None:
                    coin.probed = False
        return held, as_int(account["cash_lamports"])

    def next_client_id(self, prefix, mint):
        self.order_counter += 1
        return "%s-%s-%d" % (prefix, mint[:8], self.order_counter)


    # -- decisions --------------------------------------------------------
    def probe_size_for(self, coin):
        """Probe size clamped by the harness per-order reserve cap."""
        if self.max_order_bps <= 0 or coin.real_sol <= 0:
            return 0
        cap = coin.real_sol * self.max_order_bps // 10000
        size = PROBE_LAMPORTS if PROBE_LAMPORTS < cap else cap
        if size < MIN_ROUND_TRIP_LAMPORTS:
            return 0
        return size

    def decide_orders(self, batch):
        """Build this batch's orders: exits first, then at most one probe."""
        slot = int(batch["slot"])
        self.current_slot = slot
        self.apply_events(batch.get("events", []), slot)
        self.apply_marks(batch.get("marks", []))
        held, cash = self.sync_account(batch.get("account", {}), slot)

        orders = []

        # 1. Exits: close every probe whose rule fired, or whose coin
        #    completed its curve. Full position each time; size comes
        #    from the harness account, never from our own estimate.
        positions = {
            p["mint"]: p for p in batch.get("account", {}).get("positions", [])
        }
        for mint in sorted(self.probes):
            probe = self.probes[mint]
            if not probe.filled or probe.sell_pending:
                continue
            pos = positions.get(mint)
            if pos is None:
                continue
            coin = self.coins.get(mint)
            price = coin.last_price if coin is not None else None
            reason = probe.exit_reason(price, slot)
            if reason is None and coin is not None and coin.completed:
                reason = "curve-complete"
            if reason is None:
                continue
            amount = as_int(pos["token_amount"])
            if amount <= 0:
                continue
            orders.append({
                "side": "sell",
                "client_id": self.next_client_id("s", mint),
                "mint": mint,
                "token_amount": str(amount),
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            })
            # Keep the probe until the sell fill is reported (note_fills
            # removes it) or the account shows the position gone
            # (sync_account removes it): deleting it here would strand
            # the position with no exit rule if the sell is rejected.
            probe.sell_pending = True
            del positions[mint]

        # 2. Entries: one fresh probe per batch, best candidate first.
        #    Candidates are sorted by buy volume so the choice among
        #    several eligible coins is deterministic.
        if len(self.probes) < self.max_open_positions:
            open_slots = self.max_open_positions - len(self.probes)
            room = self.max_orders_per_batch - len(orders)
            if open_slots > 0 and room > 0 and cash >= PROBE_LAMPORTS:
                candidates = [
                    c for c in self.coins.values()
                    if c.mint not in held and c.looks_demand_proven()
                ]
                candidates.sort(key=lambda c: (-c.buy_lamports, c.mint))
                for coin in candidates[:1]:
                    size = self.probe_size_for(coin)
                    if size <= 0 or size > cash:
                        continue
                    orders.append({
                        "side": "buy",
                        "client_id": self.next_client_id("b", coin.mint),
                        "mint": coin.mint,
                        "quote_lamports": str(size),
                        "max_slippage_bps": MAX_SLIPPAGE_BPS,
                    })
                    coin.probed = True
                    # Record the probe now at the last seen price; the
                    # fill reports the true entry through the account.
                    if coin.last_price is not None:
                        self.probes[coin.mint] = HeldProbe(
                            coin.mint, coin.last_price, slot)
                    break

        return orders

    def note_fills(self, fills):
        """Align probe bookkeeping with reported fills (cheap, exact)."""
        for fill in fills:
            mint = fill["mint"]
            if fill["side"] == "sell":
                if fill.get("status") == "filled":
                    self.probes.pop(mint, None)
                else:
                    probe = self.probes.get(mint)
                    if probe is not None:
                        # Sell rejected: allow the exit to be re-issued.
                        probe.sell_pending = False
                continue
            if fill.get("status") != "filled":
                continue
            if fill["side"] == "buy":
                probe = self.probes.get(mint)
                if probe is None and mint in self.coins:
                    coin = self.coins[mint]
                    if coin.last_price is not None:
                        probe = HeldProbe(
                            mint, coin.last_price, self.current_slot)
                        self.probes[mint] = probe
                if probe is not None:
                    probe.filled = True


def serve():
    """Read hello / batch / done lines, answer one reply per batch."""
    bot = ReplayBot()
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        kind = msg.get("type")
        if kind == "hello":
            bot.apply_hello(msg)
        elif kind == "batch":
            bot.note_fills(msg.get("fills", []))
            orders = bot.decide_orders(msg)
            reply = {"type": "reply", "seq": msg["seq"], "orders": orders}
            out.write(json.dumps(reply, separators=(",", ":")) + "\n")
            out.flush()
        elif kind == "done":
            break


if __name__ == "__main__":
    serve()
