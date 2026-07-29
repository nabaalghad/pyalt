"""Honest matchup: the same Monte Carlo backtest under Numba.
Same Lehmer RNG, same strategy, same outputs as mc_backtest.py / .pya."""
import time
import numba
from numba import njit, prange
import numpy as np


@njit(cache=False)
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
    return equity, trades, wins


@njit(cache=False)
def run_all_serial(paths, steps):
    eqs = np.zeros(paths)
    trs = np.zeros(paths, dtype=np.int64)
    wns = np.zeros(paths, dtype=np.int64)
    for p in range(paths):
        e, t, w = run_path(p + 1, steps)
        eqs[p] = e
        trs[p] = t
        wns[p] = w
    return eqs, trs, wns


@njit(cache=False, parallel=True)
def run_all_parallel(paths, steps):
    eqs = np.zeros(paths)
    trs = np.zeros(paths, dtype=np.int64)
    wns = np.zeros(paths, dtype=np.int64)
    for p in prange(paths):
        e, t, w = run_path(p + 1, steps)
        eqs[p] = e
        trs[p] = t
        wns[p] = w
    return eqs, trs, wns


paths, steps = 200, 50000

# JIT compilation cost (first call)
t0 = time.perf_counter()
run_all_serial(2, 10)
t1 = time.perf_counter()
compile_serial = t1 - t0
t0 = time.perf_counter()
run_all_parallel(2, 10)
t1 = time.perf_counter()
compile_parallel = t1 - t0

# steady-state
t0 = time.perf_counter()
eqs, trs, wns = run_all_serial(paths, steps)
t1 = time.perf_counter()
serial_t = t1 - t0

t0 = time.perf_counter()
eqs_p, trs_p, wns_p = run_all_parallel(paths, steps)
t1 = time.perf_counter()
par_t = t1 - t0

print(f"numba version: {numba.__version__}, threads: {numba.get_num_threads()}")
print(f"trades={trs.sum()} wins={wns.sum()} mean_eq={int(eqs.mean() * 10000)} "
      f"best={int(eqs.max() * 10000)} worst={int(eqs.min() * 10000)}")
assert (eqs == eqs_p).all() and (trs == trs_p).all()
print(f"serial:   {serial_t:.4f}s   (first-call incl. JIT compile: {compile_serial:.2f}s)")
print(f"parallel: {par_t:.4f}s   (first-call incl. JIT compile: {compile_parallel:.2f}s)")
