# Benchmark 3: mandelbrot — CPython side. Identical logic to mandelbrot.pya.
import time


def mandel(cr, ci, max_iter):
    zr = 0.0
    zi = 0.0
    i = 0
    while i < max_iter:
        if zr * zr + zi * zi > 4.0:
            return i
        new_zr = zr * zr - zi * zi + cr
        zi = 2.0 * zr * zi + ci
        zr = new_zr
        i = i + 1
    return max_iter


t0 = time.perf_counter()
width = 1200
height = 800
total = 0
for py in range(height):
    for px in range(width):
        cr = -2.0 + 3.0 * float(px) / float(width)
        ci = -1.2 + 2.4 * float(py) / float(height)
        total = total + mandel(cr, ci, 100)
t1 = time.perf_counter()

print(f"total={total}")
print(f"elapsed: {t1 - t0} seconds")
