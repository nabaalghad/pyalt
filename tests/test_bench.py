"""The benchmark .pya programs must always lex, parse and typecheck."""

import glob
import os
import unittest

from compiler.lexer import Lexer
from compiler.parser import Parser
from compiler.typechecker import TypeChecker

BENCH_DIR = os.path.join(os.path.dirname(__file__), "..", "bench")


SPEC_BENCHMARKS = ["wordfreq", "csvparse", "mandelbrot", "stringclean"]


class BenchFrontendTests(unittest.TestCase):
    def test_all_bench_programs_typecheck(self):
        files = sorted(glob.glob(os.path.join(BENCH_DIR, "*.pya")))
        names = {os.path.splitext(os.path.basename(p))[0] for p in files}
        for required in SPEC_BENCHMARKS:
            self.assertIn(required, names, f"missing SPEC benchmark {required}")
        for path in files:
            with self.subTest(bench=os.path.basename(path)):
                with open(path, encoding="utf-8") as fh:
                    src = fh.read()
                tokens = Lexer(src, path).tokenize()
                module = Parser(tokens, path, src).parse_module()
                TypeChecker(path, src).check_module(module)

    def test_spec_benchmarks_have_python_twins(self):
        for name in SPEC_BENCHMARKS:
            twin = os.path.join(BENCH_DIR, name + ".py")
            self.assertTrue(os.path.exists(twin),
                            f"missing CPython twin for {name}.pya")


if __name__ == "__main__":
    unittest.main()
