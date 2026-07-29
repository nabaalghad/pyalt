"""Module system tests: loader units, cross-module typechecking, and
end-to-end multi-file native compilation."""

import os
import subprocess
import tempfile
import unittest

from compiler.modules import load_program
from compiler.cemitter import CEmitter
from compiler.errors import CompileError
from compiler.build import find_toolchain

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME_DIR = os.path.join(PROJECT_DIR, "runtime")
BUILD_DIR = os.path.join(PROJECT_DIR, "build")

TOOLCHAIN = find_toolchain(cache_dir=BUILD_DIR)


class LoaderTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pyalt_mod_")
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, src):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
        return path

    def load_error(self, main_path, snippet):
        with self.assertRaises(CompileError) as cm:
            load_program(main_path)
        self.assertIn(snippet.lower(), str(cm.exception).lower())
        return cm.exception


class LoaderTests(LoaderTestBase):
    def test_simple_import(self):
        self.write("util.pya", "def double(n: int) -> int:\n    return n * 2\n")
        main = self.write("main.pya",
                          "import util\nprint(util.double(21))\n")
        prog = load_program(main)
        self.assertEqual(len(prog.deps), 1)
        self.assertEqual(prog.deps[0][0], "util")

    def test_transitive_imports(self):
        self.write("c.pya", "def base(n: int) -> int:\n    return n + 1\n")
        self.write("b.pya", "import c\n"
                            "def mid(n: int) -> int:\n    return c.base(n) * 2\n")
        main = self.write("a.pya", "import b\nprint(b.mid(5))\n")
        prog = load_program(main)
        names = [d[0] for d in prog.deps]
        self.assertEqual(names, ["c", "b"])  # dependency-first order

    def test_diamond_dedup(self):
        self.write("d.pya", "def one(n: int) -> int:\n    return n\n")
        self.write("b.pya", "import d\n"
                            "def fb(n: int) -> int:\n    return d.one(n)\n")
        self.write("c.pya", "import d\n"
                            "def fc(n: int) -> int:\n    return d.one(n)\n")
        main = self.write("a.pya",
                          "import b\nimport c\nprint(b.fb(1) + c.fc(2))\n")
        prog = load_program(main)
        names = [d[0] for d in prog.deps]
        self.assertEqual(names.count("d"), 1)  # loaded once

    def test_missing_module(self):
        main = self.write("main.pya", "import ghost\nprint(1)\n")
        self.load_error(main, "cannot find module 'ghost'")

    def test_circular_import(self):
        self.write("x.pya", "import y\ndef fx(n: int) -> int:\n    return n\n")
        self.write("y.pya", "import x\ndef fy(n: int) -> int:\n    return n\n")
        main = self.write("main.pya", "import x\nprint(1)\n")
        self.load_error(main, "circular import")

    def test_imported_module_top_level_code_rejected(self):
        self.write("bad.pya", "x = 1\ndef f(n: int) -> int:\n    return n\n")
        main = self.write("main.pya", "import bad\nprint(1)\n")
        self.load_error(main, "only contain functions")

    def test_unknown_function_in_module(self):
        self.write("util.pya", "def real(n: int) -> int:\n    return n\n")
        main = self.write("main.pya", "import util\nprint(util.fake(1))\n")
        e = self.load_error(main, "has no function 'fake'")
        self.assertIn("real", e.message)  # suggests what IS available

    def test_cross_module_type_error(self):
        self.write("util.pya", "def double(n: int) -> int:\n    return n * 2\n")
        main = self.write("main.pya",
                          'import util\nprint(util.double("nope"))\n')
        self.load_error(main, "should be int but is str")

    def test_module_used_as_value(self):
        self.write("util.pya", "def f(n: int) -> int:\n    return n\n")
        main = self.write("main.pya", "import util\nx = util\n")
        self.load_error(main, "is a module")

    def test_module_name_shadowing_rejected(self):
        self.write("util.pya", "def f(n: int) -> int:\n    return n\n")
        main = self.write("main.pya", "import util\nutil = 5\n")
        self.load_error(main, "imported module")

    def test_import_inside_function_rejected(self):
        self.write("util.pya", "def f(n: int) -> int:\n    return n\n")
        main = self.write("main.pya",
                          "def g(n: int) -> int:\n    import util\n    return n\n")
        self.load_error(main, "top level")


@unittest.skipUnless(TOOLCHAIN, "no C compiler available")
class EndToEndModuleTests(LoaderTestBase):
    def run_main(self, main_path):
        prog = load_program(main_path)
        c_code = CEmitter(prog.module, prog.tc, main_path, prog.src,
                          deps=prog.deps).emit()
        stem = os.path.splitext(main_path)[0]
        with open(stem + ".c", "w", encoding="utf-8") as fh:
            fh.write(c_code)
        ok, output = TOOLCHAIN.compile(stem + ".c", stem + ".exe", RUNTIME_DIR)
        if not ok:
            raise AssertionError(f"C compilation failed:\n{output}\n{c_code}")
        proc = subprocess.run([stem + ".exe"], capture_output=True, text=True,
                              timeout=60, cwd=self.dir)
        return proc.returncode, proc.stdout, proc.stderr

    def test_two_file_program(self):
        self.write("mathkit.pya",
                   "def triple(n: int) -> int:\n    return n * 3\n"
                   "def is_even(n: int) -> bool:\n    return n % 2 == 0\n")
        main = self.write("main.pya",
                          "import mathkit\n"
                          "print(mathkit.triple(14))\n"
                          "print(mathkit.is_even(4), mathkit.is_even(7))\n")
        code, out, err = self.run_main(main)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "42\nTrue False\n")

    def test_transitive_program_with_containers(self):
        self.write("base.pya",
                   "def clean(s: str) -> str:\n    return s.strip().lower()\n")
        self.write("mid.pya",
                   "import base\n"
                   "def tokens(line: str) -> list[str]:\n"
                   "    out: list[str] = []\n"
                   '    for raw in line.split(" "):\n'
                   "        w = base.clean(raw)\n"
                   "        if len(w) > 0:\n"
                   "            out.append(w)\n"
                   "    return out\n")
        main = self.write("main.pya",
                          "import mid\n"
                          'ws = mid.tokens("  The QUICK  fox ")\n'
                          "print(ws, len(ws))\n")
        code, out, err = self.run_main(main)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "['the', 'quick', 'fox'] 3\n")

    def test_same_function_name_in_two_modules(self):
        self.write("m1.pya", "def f(n: int) -> int:\n    return n + 1\n")
        self.write("m2.pya", "def f(n: int) -> int:\n    return n + 2\n")
        main = self.write("main.pya",
                          "import m1\nimport m2\nprint(m1.f(0), m2.f(0))\n")
        code, out, err = self.run_main(main)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "1 2\n")

    def test_example_07_runs(self):
        main = os.path.join(PROJECT_DIR, "examples", "07_modules.pya")
        prog = load_program(main)
        c_code = CEmitter(prog.module, prog.tc, main, prog.src,
                          deps=prog.deps).emit()
        stem = os.path.join(self.dir, "ex07")
        with open(stem + ".c", "w", encoding="utf-8") as fh:
            fh.write(c_code)
        ok, output = TOOLCHAIN.compile(stem + ".c", stem + ".exe", RUNTIME_DIR)
        self.assertTrue(ok, output)
        proc = subprocess.run([stem + ".exe"], capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("tokens=10 distinct=6", proc.stdout)


if __name__ == "__main__":
    unittest.main()
