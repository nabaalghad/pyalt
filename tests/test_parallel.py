"""parallel for tests: compile-time safety rules + end-to-end multi-threaded
correctness (results must be identical to serial execution)."""

import os
import subprocess
import tempfile
import unittest

from compiler.lexer import Lexer
from compiler.parser import Parser
from compiler.typechecker import TypeChecker
from compiler.cemitter import CEmitter
from compiler.errors import CompileError
from compiler.build import find_toolchain

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME_DIR = os.path.join(PROJECT_DIR, "runtime")
BUILD_DIR = os.path.join(PROJECT_DIR, "build")

TOOLCHAIN = find_toolchain(cache_dir=BUILD_DIR)


def check(src):
    tokens = Lexer(src, "<t>").tokenize()
    module = Parser(tokens, "<t>", src).parse_module()
    tc = TypeChecker("<t>", src)
    tc.check_module(module)
    return module, tc


def check_error(testcase, src, snippet):
    with testcase.assertRaises(CompileError) as cm:
        check(src)
    testcase.assertIn(snippet.lower(), str(cm.exception).lower())


class ParallelSafetyTests(unittest.TestCase):
    def test_outer_scalar_write_rejected(self):
        check_error(self, "total = 0\n"
                          "parallel for i in range(10):\n"
                          "    total = total + i\n",
                    "cannot assign to outer variable")

    def test_outer_list_index_write_allowed(self):
        module, tc = check("out = [0, 0, 0, 0]\n"
                           "parallel for i in range(4):\n"
                           "    out[i] = i * i\n")
        loop = module.body[1]
        self.assertTrue(loop.parallel)
        self.assertEqual([n for n, _ in loop.captures], ["out"])

    def test_outer_dict_write_rejected(self):
        check_error(self, "d: dict[int, int] = {}\n"
                          "parallel for i in range(4):\n"
                          "    d[i] = i\n",
                    "only outer lists")

    def test_outer_append_rejected(self):
        check_error(self, "xs: list[int] = []\n"
                          "parallel for i in range(4):\n"
                          "    xs.append(i)\n",
                    "structural mutation races")

    def test_outer_set_add_rejected(self):
        check_error(self, "s: set[int] = set()\n"
                          "parallel for i in range(4):\n"
                          "    s.add(i)\n",
                    "structural mutation races")

    def test_break_rejected(self):
        check_error(self, "parallel for i in range(4):\n"
                          "    if i > 2:\n"
                          "        break\n",
                    "cannot exit a parallel for")

    def test_return_rejected(self):
        check_error(self, "def f(n: int) -> int:\n"
                          "    parallel for i in range(n):\n"
                          "        return i\n"
                          "    return 0\n",
                    "cannot 'return' from inside")

    def test_nested_parallel_rejected(self):
        check_error(self, "parallel for i in range(4):\n"
                          "    parallel for j in range(4):\n"
                          "        pass\n",
                    "cannot be nested")

    def test_gc_collect_rejected(self):
        check_error(self, "parallel for i in range(4):\n"
                          "    x = gc_collect()\n",
                    "cannot run inside parallel")

    def test_continue_allowed(self):
        check("out = [0, 0, 0, 0]\n"
              "parallel for i in range(4):\n"
              "    if i == 2:\n"
              "        continue\n"
              "    out[i] = 1\n")

    def test_body_locals_are_private(self):
        module, tc = check("out = [0, 0]\n"
                           "parallel for i in range(2):\n"
                           "    acc = i * 10\n"
                           "    out[i] = acc\n")
        loop = module.body[1]
        self.assertEqual([n for n, _ in loop.body_locals], ["acc"])


@unittest.skipUnless(TOOLCHAIN, "no C compiler available")
class ParallelEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory(prefix="pyalt_par_")
        cls.counter = 0

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    @classmethod
    def run_pya(cls, src, threads=None):
        cls.counter += 1
        stem = os.path.join(cls.dir.name, f"prog{cls.counter}")
        module, tc = check(src)
        c_code = CEmitter(module, tc, "<par>", src).emit()
        with open(stem + ".c", "w", encoding="utf-8") as fh:
            fh.write(c_code)
        ok, output = TOOLCHAIN.compile(stem + ".c", stem + ".exe", RUNTIME_DIR)
        if not ok:
            raise AssertionError(f"C compilation failed:\n{output}\n{c_code}")
        env = dict(os.environ)
        if threads is not None:
            env["PYA_THREADS"] = str(threads)
        proc = subprocess.run([stem + ".exe"], capture_output=True, text=True,
                              timeout=120, cwd=cls.dir.name, env=env)
        return proc.returncode, proc.stdout, proc.stderr

    def expect(self, src, expected, threads=None):
        code, out, err = self.run_pya(src, threads=threads)
        self.assertEqual(code, 0, f"stderr: {err}")
        self.assertEqual(out, expected)

    SUM_SQUARES = ("n = 10000\n"
                   "out: list[int] = []\n"
                   "for i in range(n):\n"
                   "    out.append(0)\n"
                   "parallel for i in range(n):\n"
                   "    out[i] = i * i\n"
                   "total = 0\n"
                   "for i in range(n):\n"
                   "    total = total + out[i]\n"
                   "print(total)\n")

    def test_range_sum_correct(self):
        # sum of squares 0..9999 == 333283335000
        self.expect(self.SUM_SQUARES, "333283335000\n")

    def test_single_thread_same_result(self):
        self.expect(self.SUM_SQUARES, "333283335000\n", threads=1)

    def test_many_threads_same_result(self):
        self.expect(self.SUM_SQUARES, "333283335000\n", threads=16)

    def test_parallel_over_list_with_allocation(self):
        # each iteration allocates (split + strip) on its own thread
        src = ("lines: list[str] = []\n"
               "for i in range(2000):\n"
               '    lines.append("alpha beta gamma " + str(i))\n'
               "counts: list[int] = []\n"
               "for i in range(2000):\n"
               "    counts.append(0)\n"
               "parallel for j in range(2000):\n"
               "    n = 0\n"
               '    for w in lines[j].split(" "):\n'
               "        if len(w.strip()) > 0:\n"
               "            n = n + 1\n"
               "    counts[j] = n\n"
               "total = 0\n"
               "for j in range(2000):\n"
               "    total = total + counts[j]\n"
               "print(total)\n")
        self.expect(src, "8000\n", threads=8)

    def test_parallel_over_list_directly(self):
        src = ("words = [\"aa\", \"bbb\", \"c\", \"dddd\"]\n"
               "lens = [0, 0, 0, 0]\n"
               "idx = [0, 1, 2, 3]\n"
               "parallel for i in idx:\n"
               "    lens[i] = len(words[i])\n"
               "print(lens)\n")
        self.expect(src, "[2, 3, 1, 4]\n")

    def test_function_calls_inside_parallel(self):
        src = ("def collatz_steps(n: int) -> int:\n"
               "    steps = 0\n"
               "    v = n\n"
               "    while v != 1:\n"
               "        if v % 2 == 0:\n"
               "            v = v // 2\n"
               "        else:\n"
               "            v = 3 * v + 1\n"
               "        steps = steps + 1\n"
               "    return steps\n"
               "out: list[int] = []\n"
               "for i in range(1000):\n"
               "    out.append(0)\n"
               "parallel for i in range(1000):\n"
               "    out[i] = collatz_steps(i + 1)\n"
               "total = 0\n"
               "for i in range(1000):\n"
               "    total = total + out[i]\n"
               "print(total)\n")
        # verified against CPython: sum of collatz steps for 1..1000
        self.expect(src, "59542\n")

    def test_range_with_step_parallel(self):
        src = ("out: list[int] = []\n"
               "for i in range(100):\n"
               "    out.append(0)\n"
               "k = 0\n"
               "parallel for v in range(0, 200, 2):\n"
               "    out[v // 2] = v\n"
               "total = 0\n"
               "for i in range(100):\n"
               "    total = total + out[i]\n"
               "print(total)\n")
        self.expect(src, "9900\n", threads=4)

    def test_gc_after_parallel_region(self):
        # allocations made by workers must be collectable afterwards
        src = ("out: list[int] = []\n"
               "for i in range(5000):\n"
               "    out.append(0)\n"
               "parallel for i in range(5000):\n"
               '    s = "block" * 200\n'
               "    out[i] = len(s)\n"
               "live = gc_collect()\n"
               "total = 0\n"
               "for i in range(5000):\n"
               "    total = total + out[i]\n"
               "print(total, live < 8000000)\n")
        code, out, err = self.run_pya(src, threads=8)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "5000000 True\n")


if __name__ == "__main__":
    unittest.main()
