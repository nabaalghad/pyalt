# CPython serial twin of par_wordcount.pya — identical logic.
import time

t0 = time.perf_counter()
with open("data/bench_wordfreq.txt", encoding="utf-8") as fh:
    lines = fh.read().splitlines()
n = len(lines)
counts = []
for i in range(n):
    counts.append(0)

for j in range(n):
    c = 0
    for raw in lines[j].lower().split(" "):
        word = raw.strip()
        if len(word) > 4:
            c = c + 1
    counts[j] = c

total = 0
for j in range(n):
    total = total + counts[j]
t1 = time.perf_counter()

print(f"lines={n} long_words={total}")
print(f"elapsed: {t1 - t0} seconds")
