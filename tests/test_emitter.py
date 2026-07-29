"""End-to-end tests: compile .pya source all the way to a native executable,
run it, and check the output. Skipped entirely if no C compiler is available."""

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
class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory(prefix="pyalt_e2e_")
        cls.counter = 0

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    @classmethod
    def run_pya(cls, src):
        """Compile source to native code, run it, return (exit, stdout, stderr)."""
        cls.counter += 1
        stem = os.path.join(cls.dir.name, f"prog{cls.counter}")
        tokens = Lexer(src, "<e2e>").tokenize()
        module = Parser(tokens, "<e2e>", src).parse_module()
        tc = TypeChecker("<e2e>", src)
        tc.check_module(module)
        c_code = CEmitter(module, tc, "<e2e>", src).emit()
        with open(stem + ".c", "w", encoding="utf-8") as fh:
            fh.write(c_code)
        ok, output = TOOLCHAIN.compile(stem + ".c", stem + ".exe", RUNTIME_DIR)
        if not ok:
            raise AssertionError(f"C compilation failed:\n{output}\n--- C ---\n{c_code}")
        proc = subprocess.run([stem + ".exe"], capture_output=True, text=True,
                              timeout=60, cwd=cls.dir.name)
        return proc.returncode, proc.stdout, proc.stderr

    def expect(self, src, expected_stdout):
        code, out, err = self.run_pya(src)
        self.assertEqual(code, 0, f"nonzero exit; stderr: {err}")
        self.assertEqual(out, expected_stdout)

    # -- the tests --------------------------------------------------------

    def test_print_primitives(self):
        self.expect('print(1 + 2)\nprint(2.5)\nprint(True)\nprint("hi", 3)\n',
                    "3\n2.5\nTrue\nhi 3\n")

    def test_python_arithmetic_semantics(self):
        self.expect("print(3 / 2)\n"
                    "print(7 // 2)\n"
                    "print(-7 // 2)\n"
                    "print(-7 % 3)\n"
                    "print(2 ** 10)\n"
                    "print(1.0)\n",
                    "1.5\n3\n-4\n2\n1024\n1.0\n")

    def test_fib(self):
        self.expect("def fib(n: int) -> int:\n"
                    "    if n < 2: return n\n"
                    "    return fib(n - 1) + fib(n - 2)\n"
                    "print(fib(20))\n",
                    "6765\n")

    def test_while_break_continue(self):
        self.expect("total = 0\n"
                    "i = 0\n"
                    "while True:\n"
                    "    i = i + 1\n"
                    "    if i > 10:\n"
                    "        break\n"
                    "    if i % 2 == 1:\n"
                    "        continue\n"
                    "    total = total + i\n"
                    "print(total)\n",
                    "30\n")

    def test_lists(self):
        self.expect("xs = [3, 1, 2]\n"
                    "xs.append(5)\n"
                    "xs.sort()\n"
                    "print(xs)\n"
                    "print(xs[0], xs[3])\n"
                    "print(xs[1:3])\n"
                    "ys = xs + [9]\n"
                    "print(len(ys), 9 in ys, 7 in ys)\n"
                    "last = ys.pop()\n"
                    "print(last, len(ys))\n",
                    "[1, 2, 3, 5]\n1 5\n[2, 3]\n5 True False\n9 4\n")

    def test_strings(self):
        self.expect('s = "  Hello World  "\n'
                    "t = s.strip()\n"
                    "print(t)\n"
                    "print(t.lower(), t.upper())\n"
                    'print(t.replace("World", "pyalt"))\n'
                    'print(t.find("World"), len(t))\n'
                    'print(t.startswith("Hello"), "World" in t)\n'
                    'print("ab" * 3 + "!")\n'
                    "print(t[0], t[6:])\n",
                    "Hello World\nhello world HELLO WORLD\nHello pyalt\n"
                    "6 11\nTrue True\nababab!\nH World\n")

    def test_split_and_iteration(self):
        self.expect('words = "the quick brown fox".split(" ")\n'
                    "print(len(words))\n"
                    "n = 0\n"
                    "for w in words:\n"
                    "    n = n + len(w)\n"
                    "print(n)\n"
                    'v = 0\n'
                    'for ch in "abc":\n'
                    "    v = v + 1\n"
                    "print(v)\n",
                    "4\n16\n3\n")

    def test_str_sort(self):
        self.expect('names = ["carol", "alice", "bob"]\n'
                    "names.sort()\n"
                    "print(names)\n",
                    "['alice', 'bob', 'carol']\n")

    def test_range_variants(self):
        self.expect("a = 0\n"
                    "for i in range(5):\n"
                    "    a = a + i\n"
                    "print(a)\n"
                    "b = 0\n"
                    "for j in range(2, 5):\n"
                    "    b = b + j\n"
                    "print(b)\n"
                    "c = 0\n"
                    "for k in range(10, 0, -2):\n"
                    "    c = c + k\n"
                    "print(c)\n",
                    "10\n9\n30\n")

    def test_fstrings(self):
        self.expect("n = 7\n"
                    "x = 2.5\n"
                    "ok = True\n"
                    's = "mid"\n'
                    'print(f"n={n} x={x} ok={ok} s={s} sum={n + 1}")\n',
                    "n=7 x=2.5 ok=True s=mid sum=8\n")

    def test_functions_with_lists(self):
        self.expect("def total(xs: list[int]) -> int:\n"
                    "    t = 0\n"
                    "    for x in xs:\n"
                    "        t = t + x\n"
                    "    return t\n"
                    "def shout(msg: str):\n"
                    '    print(msg.upper() + "!")\n'
                    "print(total([1, 2, 3]))\n"
                    'shout("hey")\n',
                    "6\nHEY!\n")

    def test_conversions(self):
        self.expect('print(int("42") + 1)\n'
                    'print(float("2.5") * 2.0)\n'
                    "print(str(123) + \"!\")\n"
                    "print(int(3.9), int(-3.9))\n"
                    "print(float(2))\n"
                    "print(bool(0), bool(3), bool(\"\"), bool(\"x\"))\n",
                    "43\n5.0\n123!\n3 -3\n2.0\nFalse True False True\n")

    def test_index_assignment(self):
        self.expect("xs = [1, 2, 3]\n"
                    "xs[1] = 20\n"
                    "print(xs)\n",
                    "[1, 20, 3]\n")

    def test_runtime_bounds_check(self):
        code, out, err = self.run_pya("xs = [1]\nprint(xs[5])\n")
        self.assertEqual(code, 1)
        self.assertIn("index out of range", err)

    def test_division_by_zero(self):
        code, out, err = self.run_pya("a = 1\nb = 0\nprint(a // b)\n")
        self.assertEqual(code, 1)
        self.assertIn("division by zero", err)

    def test_file_roundtrip(self):
        code, out, err = self.run_pya(
            'write_file("roundtrip.txt", "alpha\\nbeta\\ngamma")\n'
            'lines = read_lines("roundtrip.txt")\n'
            "print(len(lines))\n"
            "for line in lines:\n"
            "    print(line.upper())\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "3\nALPHA\nBETA\nGAMMA\n")

    def test_example_01_basics(self):
        with open(os.path.join(PROJECT_DIR, "examples", "01_basics.pya"),
                  encoding="utf-8") as fh:
            src = fh.read()
        code, out, err = self.run_pya(src)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "model=tiny-net steps=3000 budget=30.0\n"
                              "warning: large budget\n")

    def test_example_02_functions(self):
        with open(os.path.join(PROJECT_DIR, "examples", "02_functions.pya"),
                  encoding="utf-8") as fh:
            src = fh.read()
        code, out, err = self.run_pya(src)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "832040\n2.5\n1.0\n")

    def test_example_04_mandelbrot(self):
        with open(os.path.join(PROJECT_DIR, "examples", "04_mandelbrot.pya"),
                  encoding="utf-8") as fh:
            src = fh.read()
        code, out, err = self.run_pya(src)
        self.assertEqual(code, 0, err)
        self.assertIn("points inside:", out)


if __name__ == "__main__":
    unittest.main()
