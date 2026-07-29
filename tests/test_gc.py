"""GC stress tests: run compiled programs with PYA_GC_MIN forced low so
collections fire constantly, then verify (a) memory stays bounded and
(b) live data structures survive marking intact."""

import os
import subprocess
import tempfile
import unittest

from compiler.lexer import Lexer
from compiler.parser import Parser
from compiler.typechecker import TypeChecker
from compiler.cemitter import CEmitter
from compiler.build import find_toolchain

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME_DIR = os.path.join(PROJECT_DIR, "runtime")
BUILD_DIR = os.path.join(PROJECT_DIR, "build")

TOOLCHAIN = find_toolchain(cache_dir=BUILD_DIR)


@unittest.skipUnless(TOOLCHAIN, "no C compiler available")
class GcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory(prefix="pyalt_gc_")
        cls.counter = 0

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    @classmethod
    def run_pya(cls, src, gc_min=None):
        cls.counter += 1
        stem = os.path.join(cls.dir.name, f"prog{cls.counter}")
        tokens = Lexer(src, "<gc>").tokenize()
        module = Parser(tokens, "<gc>", src).parse_module()
        tc = TypeChecker("<gc>", src)
        tc.check_module(module)
        c_code = CEmitter(module, tc, "<gc>", src).emit()
        with open(stem + ".c", "w", encoding="utf-8") as fh:
            fh.write(c_code)
        ok, output = TOOLCHAIN.compile(stem + ".c", stem + ".exe", RUNTIME_DIR)
        if not ok:
            raise AssertionError(f"C compilation failed:\n{output}")
        env = dict(os.environ)
        if gc_min is not None:
            env["PYA_GC_MIN"] = str(gc_min)
        proc = subprocess.run([stem + ".exe"], capture_output=True, text=True,
                              timeout=120, cwd=cls.dir.name, env=env)
        return proc.returncode, proc.stdout, proc.stderr

    def test_churn_stays_bounded(self):
        # ~300 MB of transient strings; live heap must stay tiny
        src = ("total = 0\n"
               "for i in range(300000):\n"
               '    s = "abcdefgh" * 128\n'
               "    total = total + len(s)\n"
               "live = gc_collect()\n"
               "print(total)\n"
               "print(live < 4000000)\n")
        code, out, err = self.run_pya(src, gc_min=4 << 20)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "307200000\nTrue\n")

    def test_data_survives_heavy_collection(self):
        # build real structures while forcing collections every ~4 MB;
        # then verify every value is still intact
        src = ("words: list[str] = []\n"
               "counts: dict[str, int] = {}\n"
               "for i in range(50000):\n"
               '    junk = "x" * 4000\n'
               '    w = "w" + str(i % 1000)\n'
               "    words.append(w)\n"
               "    if w in counts:\n"
               "        counts[w] = counts[w] + 1\n"
               "    else:\n"
               "        counts[w] = 1\n"
               "ok = True\n"
               "for i in range(1000):\n"
               '    key = "w" + str(i)\n'
               "    if counts[key] != 50:\n"
               "        ok = False\n"
               "print(len(words), len(counts), ok)\n"
               "print(words[0], words[49999])\n")
        code, out, err = self.run_pya(src, gc_min=4 << 20)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "50000 1000 True\nw0 w999\n")

    def test_views_keep_parents_alive(self):
        # split() returns zero-copy views into the parent buffer; under GC
        # pressure the parents must be retained via interior pointers
        src = ("kept: list[str] = []\n"
               "for i in range(20000):\n"
               '    line = "alpha beta gamma delta " + str(i)\n'
               '    parts = line.split(" ")\n'
               "    kept.append(parts[1])\n"
               '    junk = "y" * 5000\n'
               "ok = True\n"
               "for w in kept:\n"
               '    if w != "beta":\n'
               "        ok = False\n"
               "print(len(kept), ok)\n")
        code, out, err = self.run_pya(src, gc_min=4 << 20)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "20000 True\n")

    def test_dict_growth_under_pressure(self):
        # dict rehashing while collections fire between insertions
        src = ("d: dict[int, int] = {}\n"
               "for i in range(200000):\n"
               "    d[i] = i * 3\n"
               '    junk = "z" * 1000\n'
               "ok = True\n"
               "for i in range(0, 200000, 997):\n"
               "    if d[i] != i * 3:\n"
               "        ok = False\n"
               "print(len(d), ok)\n")
        code, out, err = self.run_pya(src, gc_min=4 << 20)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "200000 True\n")

    def test_gc_collect_returns_int(self):
        src = ("a = gc_collect()\n"
               "b = gc_collect()\n"
               "print(a >= 0, b >= 0)\n")
        code, out, err = self.run_pya(src)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "True True\n")

    def test_gc_off_switch(self):
        src = ("total = 0\n"
               "for i in range(1000):\n"
               '    s = "q" * 100\n'
               "    total = total + len(s)\n"
               "print(total)\n")
        env_backup = os.environ.get("PYA_GC")
        os.environ["PYA_GC"] = "off"
        try:
            code, out, err = self.run_pya(src, gc_min=1)
        finally:
            if env_backup is None:
                os.environ.pop("PYA_GC", None)
            else:
                os.environ["PYA_GC"] = env_backup
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "100000\n")


if __name__ == "__main__":
    unittest.main()
