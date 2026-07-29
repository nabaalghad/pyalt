"""End-to-end interop tests: compile a .pya module to a Python extension,
import it into THIS process, call it, and check conversions + error handling.
Skipped when no C toolchain is available."""

import importlib
import os
import sys
import tempfile
import unittest

from compiler.lexer import Lexer
from compiler.parser import Parser
from compiler.typechecker import TypeChecker
from compiler.pyemitter import PyExtEmitter
from compiler.errors import CompileError
from compiler.build import find_toolchain

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME_DIR = os.path.join(PROJECT_DIR, "runtime")
BUILD_DIR = os.path.join(PROJECT_DIR, "build")

TOOLCHAIN = find_toolchain(cache_dir=BUILD_DIR)

MODULE_SRC = """\
def fib(n: int) -> int:
    if n < 2: return n
    return fib(n - 1) + fib(n - 2)

def total(xs: list[int]) -> int:
    t = 0
    for x in xs:
        t = t + x
    return t

def shout(s: str) -> str:
    return s.upper() + "!"

def half(x: float) -> float:
    return x / 2.0

def is_pos(n: int) -> bool:
    return n > 0

def head(xs: list[int]) -> int:
    return xs[0]

def words(s: str) -> list[str]:
    return s.split(" ")

def nothing(n: int):
    x = n + 1

def word_count(words: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for w in words:
        if w in counts:
            counts[w] = counts[w] + 1
        else:
            counts[w] = 1
    return counts

def unique(xs: list[int]) -> set[int]:
    seen: set[int] = set()
    for x in xs:
        seen.add(x)
    return seen

def count_hits(words: list[str], targets: set[str]) -> int:
    hits = 0
    for w in words:
        if w in targets:
            hits = hits + 1
    return hits

def lookup_all(d: dict[str, int], keys: list[str]) -> int:
    acc = 0
    for k in keys:
        acc = acc + d.get(k, 0)
    return acc
"""


@unittest.skipUnless(TOOLCHAIN, "no C compiler available")
class InteropTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # unique module name per process: Windows keeps loaded .pyd files
        # locked, so a fixed name would collide across test runs
        cls.modname = f"pyamod_{os.getpid()}"
        cls.dir = tempfile.mkdtemp(prefix="pyalt_interop_")
        tokens = Lexer(MODULE_SRC, "<interop>").tokenize()
        module = Parser(tokens, "<interop>", MODULE_SRC).parse_module()
        tc = TypeChecker("<interop>", MODULE_SRC)
        tc.check_module(module)
        c_code = PyExtEmitter(module, tc, cls.modname, "<interop>",
                              MODULE_SRC).emit()
        ext = ".pyd" if os.name == "nt" else ".so"
        c_path = os.path.join(cls.dir, cls.modname + ".c")
        pyd_path = os.path.join(cls.dir, cls.modname + ext)
        with open(c_path, "w", encoding="utf-8") as fh:
            fh.write(c_code)
        ok, output = TOOLCHAIN.compile_pyd(c_path, pyd_path, RUNTIME_DIR)
        if not ok:
            raise AssertionError(f"extension compilation failed:\n{output}")
        if os.name == "nt" and TOOLCHAIN.kind in ("gcc", "clang"):
            # MinGW-built extensions may depend on compiler runtime DLLs;
            # Python ignores PATH for those, so register the dir explicitly
            import shutil
            cc_dir = os.path.dirname(shutil.which(TOOLCHAIN.kind) or "")
            if cc_dir:
                os.add_dll_directory(cc_dir)
        sys.path.insert(0, cls.dir)
        cls.mod = importlib.import_module(cls.modname)

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(cls.dir)
        # the .pyd stays loaded/locked on Windows; the temp dir is left behind

    def test_int_roundtrip(self):
        self.assertEqual(self.mod.fib(20), 6765)

    def test_list_int_arg(self):
        self.assertEqual(self.mod.total([1, 2, 3, 4]), 10)
        self.assertEqual(self.mod.total([]), 0)

    def test_str_roundtrip(self):
        self.assertEqual(self.mod.shout("hey"), "HEY!")

    def test_float_roundtrip(self):
        self.assertEqual(self.mod.half(5.0), 2.5)
        self.assertEqual(self.mod.half(4), 2.0)   # int accepted for float

    def test_bool_return(self):
        self.assertIs(self.mod.is_pos(3), True)
        self.assertIs(self.mod.is_pos(-3), False)

    def test_list_str_return(self):
        self.assertEqual(self.mod.words("a b c"), ["a", "b", "c"])

    def test_void_returns_none(self):
        self.assertIsNone(self.mod.nothing(1))

    def test_runtime_error_raises(self):
        with self.assertRaises(RuntimeError) as cm:
            self.mod.head([])
        self.assertIn("index out of range", str(cm.exception))
        # and the process survived; module still usable afterwards
        self.assertEqual(self.mod.head([42]), 42)

    def test_type_error_raises(self):
        with self.assertRaises(TypeError):
            self.mod.fib("not an int")

    def test_wrong_arg_count_raises(self):
        with self.assertRaises(TypeError):
            self.mod.fib(1, 2)

    def test_dict_return(self):
        self.assertEqual(self.mod.word_count(["a", "b", "a"]),
                         {"a": 2, "b": 1})

    def test_set_return(self):
        self.assertEqual(self.mod.unique([3, 1, 3, 2, 1]), {1, 2, 3})

    def test_set_arg(self):
        self.assertEqual(
            self.mod.count_hits(["x", "y", "x", "z"], {"x", "z"}), 3)

    def test_dict_arg(self):
        self.assertEqual(
            self.mod.lookup_all({"a": 5, "b": 7}, ["a", "b", "zz"]), 12)

    def test_dict_wrong_type_raises(self):
        with self.assertRaises(TypeError):
            self.mod.lookup_all(["not", "a", "dict"], ["a"])


class InteropCompileErrorTests(unittest.TestCase):
    def test_top_level_code_rejected(self):
        src = "x = 1\ndef f(n: int) -> int:\n    return n\n"
        tokens = Lexer(src, "<t>").tokenize()
        module = Parser(tokens, "<t>", src).parse_module()
        tc = TypeChecker("<t>", src)
        tc.check_module(module)
        with self.assertRaises(CompileError) as cm:
            PyExtEmitter(module, tc, "m", "<t>", src).emit()
        self.assertIn("only contain function and class definitions",
                      str(cm.exception))


if __name__ == "__main__":
    unittest.main()
