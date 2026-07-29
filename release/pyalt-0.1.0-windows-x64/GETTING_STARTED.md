# pyalt 0.1.0 — getting started (60 seconds)

A small, fast, Python-like compiled language. This package is self-contained:
**no Python needed.** You only need a C compiler on the machine that *builds*
programs (Visual Studio's free Build Tools, or gcc/clang — auto-detected).

## 1. Install

Unzip this folder anywhere (e.g. `C:\pyalt`). Then either:

- open PowerShell **in this folder** and run:

  ```powershell
  powershell -ExecutionPolicy Bypass -File .\install.ps1
  ```

  (the `-ExecutionPolicy Bypass` part is required — Windows blocks
  downloaded scripts by default). This appends `bin\` to your user PATH —
  one added entry, nothing else touched. Then **open a NEW terminal**
  (PATH changes don't reach already-open ones), **or**
- skip installing and use the exe directly: `C:\pyalt\bin\pyalt.exe`

## 2. Hello, speed

Create `hello.pya`:

```python
def fib(n: int) -> int:
    if n < 2: return n
    return fib(n - 1) + fib(n - 2)

print(f"fib(30) = {fib(30)}")
```

```
pyalt run hello.pya          # compiles to native code and runs
pyalt build hello.pya        # -> build\hello.exe, standalone, ~200 KB
```

## 3. All your CPU cores (the thing Python's GIL can't do)

```
pyalt run examples\09_parallel.pya
```

Change `parallel for` back to `for` and rerun — watch the time multiply.

## 4. The showdown

`examples\mc_backtest*` is a Monte Carlo trading backtest, identical logic in
Python and pyalt, measured on the same machine:

| | time |
|---|---|
| Python 3.11 | 3.09 s |
| pyalt | 0.049 s — **62x** |
| pyalt `parallel for` | 0.0089 s — **347x** |

## Commands

```
pyalt run     prog.pya     compile + run
pyalt build   prog.pya     -> standalone .exe
pyalt buildpy mod.pya      -> Python-importable extension (needs Python, obviously)
pyalt check   prog.pya     type-check, show inferred types
pyalt emit    prog.pya     show the generated C
```

Honest note: pyalt's compiler is currently bootstrapped (hosted) — but this
package is self-contained and the programs it produces are 100% native
machine code. Full documentation, benchmarks and source: the README of the
repository this release was downloaded from.
