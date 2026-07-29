# Benchmark 1: wordfreq — CPython side. Identical logic to wordfreq.pya.
import time

stopwords = ["the", "and", "for", "with", "that", "this", "from", "over",
             "into", "your", "their", "about", "would", "there", "which"]

t0 = time.perf_counter()
stop = set()
for w in stopwords:
    stop.add(w)

with open("data/bench_wordfreq.txt", encoding="utf-8") as fh:
    lines = fh.read().splitlines()
counts = {}
tokens = 0
stop_hits = 0
for line in lines:
    for raw in line.lower().split(" "):
        word = raw.strip()
        if len(word) > 0:
            tokens = tokens + 1
            if word in stop:
                stop_hits = stop_hits + 1
            else:
                if word in counts:
                    counts[word] = counts[word] + 1
                else:
                    counts[word] = 1

top_word = ""
top_count = 0
for w in counts:
    c = counts[w]
    if c > top_count:
        top_word = w
        top_count = c
t1 = time.perf_counter()

print(f"lines={len(lines)} tokens={tokens} stop_hits={stop_hits}")
print(f"distinct={len(counts)} top={top_word} top_count={top_count}")
print(f"elapsed: {t1 - t0} seconds")
