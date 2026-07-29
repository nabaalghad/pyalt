# Monte Carlo trading-strategy backtest — pure Python version.
# 200 simulated price paths x 50,000 steps; EMA crossover strategy with
# stop-loss; deterministic Lehmer RNG so results are exactly reproducible.
import time


class Result:
    def __init__(self, equity, trades, wins):
        self.equity = equity
        self.trades = trades
        self.wins = wins


def run_path(seed, steps):
    state = seed % 2147483647
    if state == 0:
        state = 1
    price = 100.0
    ema_f = 100.0
    ema_s = 100.0
    equity = 1.0
    entry = 0.0
    pos = 0
    trades = 0
    wins = 0
    i = 0
    while i < steps:
        state = (state * 48271) % 2147483647
        u = float(state) * (1.0 / 2147483647.0)
        price = price * (1.0 + (u - 0.5) * 0.04)
        ema_f = ema_f * 0.9 + price * 0.1
        ema_s = ema_s * 0.98 + price * 0.02
        if pos == 0:
            if ema_f > ema_s:
                pos = 1
                entry = price
                trades = trades + 1
        else:
            if ema_f < ema_s or price < entry * 0.95:
                if price > entry:
                    wins = wins + 1
                equity = equity * (price / entry)
                pos = 0
        i = i + 1
    if pos == 1:
        if price > entry:
            wins = wins + 1
        equity = equity * (price / entry)
    return Result(equity, trades, wins)


t0 = time.perf_counter()
paths = 200
steps = 50000
total_trades = 0
total_wins = 0
sum_eq = 0.0
best = -1.0
worst = 1000000.0
for p in range(paths):
    r = run_path(p + 1, steps)
    total_trades = total_trades + r.trades
    total_wins = total_wins + r.wins
    sum_eq = sum_eq + r.equity
    if r.equity > best:
        best = r.equity
    if r.equity < worst:
        worst = r.equity
t1 = time.perf_counter()

print(f"paths={paths} steps={steps} trades={total_trades} wins={total_wins}")
print(f"mean_eq={int(sum_eq / float(paths) * 10000.0)} "
      f"best={int(best * 10000.0)} worst={int(worst * 10000.0)}")
print(f"elapsed: {t1 - t0} seconds")
