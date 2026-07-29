# Benchmark 4: string-clean — CPython side. Identical logic to stringclean.pya.
import time

t0 = time.perf_counter()
with open("data/bench_messy.txt", encoding="utf-8") as fh:
    lines = fh.read().splitlines()
changed = 0
total_len = 0
the_lines = 0
for line in lines:
    cleaned = line.strip().lower().replace("  ", " ")
    if cleaned != line:
        changed = changed + 1
    total_len = total_len + len(cleaned)
    if cleaned.startswith("the "):
        the_lines = the_lines + 1
t1 = time.perf_counter()

print(f"lines={len(lines)} changed={changed}")
print(f"total_len={total_len} the_lines={the_lines}")
print(f"elapsed: {t1 - t0} seconds")
