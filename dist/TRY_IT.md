# Try pyalt's executables — no Python needed

Everything in this folder is a **standalone Windows exe** compiled from the
pyalt source in [../examples/](../examples/). They run on any Windows 10/11
machine with nothing installed — copy one anywhere, it just works.

Open a terminal in this folder (`cmd` or PowerShell) and try:

## 1. csv_report.exe — a real internal tool

```
csv_report.exe                                    (shows usage, exit code 2)
csv_report.exe sample_sales.csv                   (writes report.txt)
csv_report.exe sample_sales.csv my_report.txt     (named output)
csv_report.exe missing.csv                        (friendly error, exit code 1)
echo %errorlevel%                                 (see the exit code in cmd)
```

Then open `report.txt`. Swap in your own CSV (rows: `id,category,amount`) —
that's the whole workflow a colleague would use.

Source: [../examples/csv_report.pya](../examples/csv_report.pya) — ~60 lines
of Python-like code.

## 1b. csvstat.exe — group-by statistics for ANY csv

```
.\csvstat.exe us_births_raw.csv 0 4      (real CDC data: births per year)
.\csvstat.exe us_births_raw.csv 3 4      (same file, grouped by weekday)
.\csvstat.exe yourfile.csv <group_col> <value_col>
```

Point it at any CSV: pick the grouping column and the numeric column
(0-based), get count/sum/mean/min/max per group, sorted. Headers and bad
rows are skipped automatically — no preprocessing needed.

## 2. parallel_demo.exe — all your CPU cores, no GIL

```
parallel_demo.exe
set PYA_THREADS=1 && parallel_demo.exe            (compare: single-threaded)
set PYA_THREADS=
```

200,000 collatz computations. Watch the elapsed time change with the thread
count — this is the parallelism Python's GIL forbids.

## 3. classes_demo.exe — classes + exceptions

```
classes_demo.exe
```

A `Stats` class with methods, a custom `raise`, and a caught out-of-bounds
error — the "complete language" tour in 5 seconds.

## Build your own

From the repository root (needs Python + Visual Studio C++ tools):

```
python pyalt.py build examples\csv_report.pya -o dist\mytool.exe
```

Any `.pya` file becomes a standalone exe the same way.
