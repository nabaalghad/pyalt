# pyalt — v1 Language Specification

> Working codename: **pyalt** (final name TBD). File extension: **`.pya`**
>
> **Mission:** a compiled, statically-typed, Python-like language for the "glue code"
> of ML/AI — data loading, preprocessing, tokenizing, feature engineering — the code
> that starves GPUs today. Target: **≥10x faster than CPython** on such workloads.

---

## 1. Design principles

1. **Reads like Python.** If you know Python, you can read any pyalt program.
2. **Every type is known at compile time.** Mostly by inference — you annotate
   function parameters, the compiler figures out the rest. This is where the
   speed comes from: what the checker proves, the runtime never checks again.
3. **Small core, done well.** Features earn their place. Python's dynamism
   (`eval`, monkey-patching, everything-is-an-object) is *permanently* out —
   that is where Python's slowness lives.
4. **Benchmarks are the truth.** Every release must pass the benchmark suite
   (section 7). Speed claims are measured, never asserted.
5. **Compiles to C** (v1). Our compiler emits C; clang/gcc produces the native
   binary. Pipeline: `program.pya → [lex → parse → typecheck → emit C] → clang → binary`

---

## 2. Types (v1)

| Type      | Meaning                          | C mapping        |
|-----------|----------------------------------|------------------|
| `int`     | 64-bit signed integer            | `int64_t`        |
| `float`   | 64-bit IEEE double               | `double`         |
| `bool`    | `True` / `False`                 | `bool`           |
| `str`     | immutable UTF-8 string           | runtime `Str`    |
| `list[T]` | growable typed array (`T` = any v1 type) | runtime `List` (contiguous, unboxed for int/float/bool) |
| `dict[K, V]` *(v2)* | insertion-ordered hash map; `K` must be a prim | runtime `PDict` (compact entries + open-addressing index, cached str hashes) |
| `set[T]` *(v2)* | insertion-ordered hash set; `T` must be a prim | runtime `PDict` with unused values |
| classes *(v3.5)* | user-defined: typed fields + methods, no inheritance yet | plain C struct on the GC heap (`PC_<name>*`) |

- No `None` in v1. Every variable is initialized at declaration.
- No implicit int↔float conversion; use `float(x)` / `int(x)` explicitly.
- Lists are homogeneous: `list[int]`, `list[str]`, `list[list[float]]`.
- **Semantic divergences from Python (by design, documented):** `int` is
  64-bit machine precision (overflow wraps unchecked; Python's is
  arbitrary-precision); `str` is UTF-8 bytes (`len`/index/slice count bytes,
  not codepoints); `float` is IEEE-754 double matching CPython arithmetic
  bit-for-bit, though edge-case *formatting* may differ from Python `repr`.

## 3. Syntax (v1)

Python-style: newline-terminated statements, `#` comments, **indentation-based
blocks** (4 spaces), `:` before blocks.

### Variables — types inferred from the initializer
```python
count = 0            # int
rate = 0.5           # float
name = "tokenizer"   # str
flags = [True, False]  # list[bool]
xs: list[int] = []   # annotation required only when it can't be inferred (empty list)
```

### Operators
- Arithmetic: `+ - * / // % **`  (`/` on ints → float, `//` → int, as in Python)
- Comparison: `== != < <= > >=`
- Logic: `and or not`
- Strings: `+` (concat), `*` (repeat), `in` (substring test), f-strings: `f"n={n}"`
- Lists: `+` (concat), `in` (membership), indexing `xs[i]`, slicing `xs[a:b]`

### Control flow
```python
if x > 0:
    ...
elif x == 0:
    ...
else:
    ...

while i < n:
    i = i + 1

for i in range(10): ...        # also range(a, b), range(a, b, step)
for word in words: ...         # iterate any list
for ch in text: ...            # iterate str by character (as str of length 1)
break / continue
```

### Functions — parameter types annotated; return type inferred (annotation optional)
```python
def score(hits: int, total: int) -> float:
    return hits / total

def clamp(x: float, lo: float, hi: float):   # return type inferred: float
    if x < lo: return lo
    if x > hi: return hi
    return x
```

## 4. Built-in functions (v1)

- `print(x, ...)` — any v1 type
- `len(x)` — str or list
- `range(...)` — for loops only
- `int(x)`, `float(x)`, `str(x)`, `bool(x)` — conversions
- `abs(x)`, `min(a, b)`, `max(a, b)`
- `append(xs, v)`, `pop(xs)`, `sort(xs)` — list ops (method syntax `xs.append(v)` accepted, desugars to these)
- String methods: `s.split(sep)`, `s.strip()`, `s.lower()`, `s.upper()`,
  `s.startswith(p)`, `s.endswith(p)`, `s.replace(a, b)`, `s.find(sub)`
- File I/O: `read_file(path) -> str`, `read_lines(path) -> list[str]`,
  `write_file(path, text)`
- `clock() -> float` — seconds, monotonic; for benchmarking
- `gc_collect() -> int` — force a GC, returns live heap bytes
- CLI *(v3.6)*: `args() -> list[str]` (command-line arguments, exe name
  excluded; Unicode-correct on Windows), `input(prompt) -> str` (reads a
  line; end-of-input is a catchable error), `exists(path) -> bool`,
  `exit(code)` — process exit code for scripting

## 5. Explicitly OUT of v1/v2 (deferred)

class inheritance · exception types · tuple · None/Optional ·
closures/lambdas · generators/comprehensions · default args ·
string `%`/`.format` (f-strings only) · negative indexing · chained
comparison · `del` statement (use `.pop()`/`.remove()`)

### v3.5: classes, exceptions, deletion, from-import (shipped)

**Classes** — fields + methods, no inheritance yet:

```python
class Point:
    x: float
    y: float
    def dist2(self, o: Point) -> float:
        dx = self.x - o.x
        dy = self.y - o.y
        return dx * dx + dy * dy

p = Point(3.0, 4.0)        # constructor = positional fields
p.x = 5.0                  # field assignment
p.scale(2.0)               # methods; `self` is implicit-typed
```

Instances compile to plain C structs on the GC heap; they may sit in
lists/dicts (values) and reference other classes, including themselves
(the type checker accepts recursive types; *building* cyclic structures
awaits an Optional/None story — on the roadmap). Field access on an
uninitialized instance dies with a clear message. Instances can't cross the
Python boundary yet (`buildpy` skips such functions with a note), can't be
printed directly, and outer instances can't be mutated inside `parallel for`.

**Exceptions** — catch-all with a message, every runtime error catchable:

```python
try:
    x = xs[i]              # bounds, div-zero, missing keys, file errors...
except as msg:             # msg: str — the error message
    print("failed:", msg)

raise "custom failure"     # raise takes a str
```

No exception class hierarchy yet. Implemented with setjmp/longjmp frames;
functions containing `try` get volatile locals (setjmp correctness).
Uncaught errors abort with exit code 1 as before (or RuntimeError in Python
embeds). try/except works inside `parallel for` bodies (thread-local frames).

**Deletion** — `d.pop(k) -> V` (dies if missing), `s.remove(x)`. Tombstoned
open addressing; iteration order stays insertion order; reinsertion works.

**from-import** — `from utils import clean, tokenize` then bare `clean(s)`.
Same module resolution as `import`; the module name itself is not bound.

### v3: `parallel for` (shipped)

```python
parallel for i in range(n):      # iterations chunked across all CPU cores
    out[i] = expensive(i)        # no GIL — real threads
```

Works over `range`, lists, strs, dicts and sets. Threads = `PYA_THREADS` env
var or the core count. **Data races are compile errors**, not runtime bugs:

- Assigning an outer scalar variable inside the body → error (use an output
  list instead).
- Structural mutation of outer containers (`append`/`pop`/`sort`/`add`,
  dict writes) → error. Writing distinct **index slots of an outer list**
  (`out[i] = ...`) is the supported result channel.
- `break`, `return`, nested `parallel for`, `gc_collect()` inside the body →
  errors. `continue` is fine. Called functions are automatically safe
  (functions can't touch globals in pyalt).

Workers allocate into thread-local chunks (no lock on the hot path); GC is
deferred during the region and resumes after the join. Measured: ~5x scaling
→ **~265x vs CPython** on mandelbrot, **~15x** on per-line text processing
(current numbers live in BENCHMARKS.md; they vary slightly run to run).

### v2.6: memory management (shipped)

The runtime has a **conservative mark-sweep garbage collector** (Boehm-style,
built in): bump allocation into 4 MB chunks, 8-byte object headers, per-chunk
object tables so interior pointers (zero-copy string views) resolve to their
owning object, roots from the C stack + registers, size-class free lists on
sweep. String literals are immortal. Long-running programs now run in bounded
memory.

- `gc_collect() -> int` builtin: force a collection, returns live heap bytes.
- Env knobs: `PYA_GC=off` disables collection (old never-free behavior);
  `PYA_GC_MIN=<bytes>` sets the allocation volume between collections
  (default 256 MB — batch-friendly; servers should set it lower).
- Cost: ~5–25% on allocation-heavy workloads (see BENCHMARKS.md).

### v2.5: modules (shipped)

`import utils` loads `utils.pya` from the importing file's directory; call its
functions qualified: `utils.clean(s)`. Imported modules may contain only
function definitions (and their own imports) — nothing runs on import. Import
graphs may nest; diamonds are deduplicated; cycles are a compile error. The
whole program compiles into one native binary. Works for `buildpy`
Python-extension modules too (the main module's functions are exported).

### v2 additions (shipped)

`dict[K, V]` and `set[T]` with literals `{k: v}` / `{a, b}` (`{}` is an empty
dict; empty set is `s: set[T] = set()`), indexing `d[k]` / `d[k] = v`, `in`,
`len()`, iteration in insertion order (dicts yield keys), and methods
`d.get(k, default)`, `d.keys()`, `d.values()`, `s.add(x)`. Keys/elements must
be hashable prims (int, float, bool, str).

## 6. Permanently OUT (by design — this is where Python's slowness lives)

`eval`/`exec` · monkey-patching · runtime attribute creation · untyped
heterogeneous collections · reflection on arbitrary objects · the GIL (there
will never be one)

## 7. Benchmark suite (REQUIRED — the project's ground truth)

Every benchmark exists as two files with identical logic: `bench/xxx.pya` and
`bench/xxx.py` (CPython). A harness runs both, reports median-of-5 wall-clock
times and the speedup ratio. **Results are committed to `BENCHMARKS.md`; a
release that regresses is not a release.**

| # | Benchmark        | What it exercises                        | v1 target |
|---|------------------|------------------------------------------|-----------|
| 1 | wordfreq         | tokenize + count words over ~100MB text  | ≥10x      |
| 2 | csv-parse        | parse & aggregate a large CSV            | ≥10x      |
| 3 | mandelbrot       | pure numeric loops                       | ≥30x      |
| 4 | string-clean     | strip/lower/replace over millions of lines | ≥10x   |

## 8. Compiler contract

- **Errors are a feature.** Every compile error names the file, line, and a
  human explanation: `tokenize.pya:14: 'total' is int but '+' was given str —
  did you mean str(total)?`
- Always a working compiler: features land only with passing tests
  (golden-AST tests, type-checker accept/reject tests, end-to-end run tests).

## 9. Roadmap

Shipped since v1 (details in the sections above):
- **dict/set** with insertion-ordered hash tables (v2)
- **modules** — multi-file programs via `import` (v2.5)
- **memory management** — conservative mark-sweep GC (v2.6)
- **Python interop** — `buildpy` compiles Python-importable native extension
  modules; the trojan-horse adoption strategy is live

Next:
- **Optional/None** (enables building linked/cyclic class structures)
- class inheritance · exception types · tuple · closures
- class instances across the Python boundary
- **LLVM backend** eventually replaces C emission
