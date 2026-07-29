"""CLI-feature tests: args(), input(), exists(), exit()."""

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
class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory(prefix="pyalt_cli_")
        cls.counter = 0

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    @classmethod
    def build(cls, src):
        cls.counter += 1
        stem = os.path.join(cls.dir.name, f"prog{cls.counter}")
        tokens = Lexer(src, "<cli>").tokenize()
        module = Parser(tokens, "<cli>", src).parse_module()
        tc = TypeChecker("<cli>", src)
        tc.check_module(module)
        c_code = CEmitter(module, tc, "<cli>", src).emit()
        with open(stem + ".c", "w", encoding="utf-8") as fh:
            fh.write(c_code)
        ok, output = TOOLCHAIN.compile(stem + ".c", stem + ".exe", RUNTIME_DIR)
        if not ok:
            raise AssertionError(f"C compilation failed:\n{output}")
        return stem + ".exe"

    def test_args(self):
        exe = self.build("a = args()\n"
                         "print(len(a))\n"
                         "for x in a:\n"
                         "    print(x)\n")
        proc = subprocess.run([exe, "alpha", "beta gamma", "42"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.stdout, "3\nalpha\nbeta gamma\n42\n")

    def test_args_empty(self):
        exe = self.build("print(len(args()))\n")
        proc = subprocess.run([exe], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.stdout, "0\n")

    def test_input(self):
        exe = self.build('name = input("who? ")\n'
                         'print("hello " + name)\n'
                         'n = int(input(""))\n'
                         "print(n * 2)\n")
        proc = subprocess.run([exe], input="world\n21\n",
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.stdout, "who? hello world\n42\n")

    def test_input_eof_catchable(self):
        exe = self.build("total = 0\n"
                         "count = 0\n"
                         "try:\n"
                         "    while True:\n"
                         '        line = input("")\n'
                         "        total = total + int(line)\n"
                         "        count = count + 1\n"
                         "except:\n"
                         "    pass\n"
                         "print(count, total)\n")
        proc = subprocess.run([exe], input="1\n2\n3\n",
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.stdout, "3 6\n")

    def test_exists(self):
        exe = self.build('print(exists("real.txt"), exists("ghost.txt"))\n')
        with open(os.path.join(self.dir.name, "real.txt"), "w") as fh:
            fh.write("x")
        proc = subprocess.run([exe], capture_output=True, text=True,
                              timeout=60, cwd=self.dir.name)
        self.assertEqual(proc.stdout, "True False\n")

    def test_exit_code(self):
        exe = self.build("a = args()\n"
                         "if len(a) == 0:\n"
                         '    print("usage")\n'
                         "    exit(2)\n"
                         'print("ran")\n')
        proc = subprocess.run([exe], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "usage\n")
        proc = subprocess.run([exe, "x"], capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "ran\n")


if __name__ == "__main__":
    unittest.main()
