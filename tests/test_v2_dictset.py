"""v2 dict/set tests: parser + typechecker units, then end-to-end native runs."""

import os
import subprocess
import tempfile
import unittest

from compiler.lexer import Lexer
from compiler.parser import Parser
from compiler import ast_nodes as A
from compiler.typechecker import TypeChecker
from compiler.types import INT, STR, BOOL, FLOAT, DictT, SetT, ListT
from compiler.cemitter import CEmitter
from compiler.errors import CompileError
from compiler.build import find_toolchain

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME_DIR = os.path.join(PROJECT_DIR, "runtime")
BUILD_DIR = os.path.join(PROJECT_DIR, "build")

TOOLCHAIN = find_toolchain(cache_dir=BUILD_DIR)


def parse(src):
    tokens = Lexer(src, "<t>").tokenize()
    return Parser(tokens, "<t>", src).parse_module()


def check(src):
    module = parse(src)
    tc = TypeChecker("<t>", src)
    tc.check_module(module)
    return tc


def check_error(testcase, src, snippet):
    with testcase.assertRaises(CompileError) as cm:
        check(src)
    testcase.assertIn(snippet.lower(), str(cm.exception).lower())


class ParserV2Tests(unittest.TestCase):
    def test_empty_braces_is_dict(self):
        m = parse("d: dict[str, int] = {}\n")
        self.assertIsInstance(m.body[0].value, A.DictLit)
        self.assertEqual(m.body[0].value.keys, [])

    def test_dict_literal(self):
        m = parse('d = {"a": 1, "b": 2}\n')
        lit = m.body[0].value
        self.assertIsInstance(lit, A.DictLit)
        self.assertEqual(len(lit.keys), 2)

    def test_set_literal(self):
        m = parse("s = {1, 2, 3}\n")
        lit = m.body[0].value
        self.assertIsInstance(lit, A.SetLit)
        self.assertEqual(len(lit.elts), 3)

    def test_trailing_commas(self):
        m = parse('d = {"a": 1,}\ns = {1, 2,}\n')
        self.assertEqual(len(m.body[0].value.keys), 1)
        self.assertEqual(len(m.body[1].value.elts), 2)

    def test_dict_type_annotation(self):
        m = parse("d: dict[str, list[int]] = {}\n")
        ann = m.body[0].ann
        self.assertIsInstance(ann, A.DictType)
        self.assertIsInstance(ann.val, A.ListType)


class TypecheckV2Tests(unittest.TestCase):
    def test_dict_inference(self):
        tc = check('d = {"a": 1}\n')
        self.assertEqual(tc.globals["d"], DictT(STR, INT))

    def test_set_inference(self):
        tc = check("s = {1.5, 2.5}\n")
        self.assertEqual(tc.globals["s"], SetT(FLOAT))

    def test_empty_dict_needs_annotation(self):
        check_error(self, "d = {}\n", "annotate")

    def test_empty_set_builtin(self):
        tc = check("s: set[str] = set()\n")
        self.assertEqual(tc.globals["s"], SetT(STR))

    def test_set_builtin_needs_annotation(self):
        check_error(self, "s = set()\n", "annotation")

    def test_dict_index_get_set(self):
        tc = check('d = {"a": 1}\nd["b"] = 2\nx = d["a"]\n')
        self.assertEqual(tc.globals["x"], INT)

    def test_dict_wrong_key_type(self):
        check_error(self, 'd = {"a": 1}\nx = d[0]\n', "str keys")

    def test_dict_wrong_value_assign(self):
        check_error(self, 'd = {"a": 1}\nd["b"] = "x"\n', "int values")

    def test_unhashable_key_rejected(self):
        check_error(self, "d: dict[list[int], int] = {}\n", "hashable")

    def test_mixed_dict_values_rejected(self):
        check_error(self, 'd = {"a": 1, "b": "x"}\n', "same type")

    def test_in_dict_and_set(self):
        tc = check('d = {"a": 1}\nx = "a" in d\ns = {1, 2}\ny = 3 not in s\n')
        self.assertEqual(tc.globals["x"], BOOL)
        self.assertEqual(tc.globals["y"], BOOL)

    def test_in_wrong_type(self):
        check_error(self, 'd = {"a": 1}\nx = 5 in d\n', "keys")

    def test_iteration_yields_keys(self):
        tc = check('d = {"a": 1}\nfor k in d:\n    print(k)\n')
        self.assertEqual(tc.globals["k"], STR)

    def test_methods(self):
        tc = check('d = {"a": 1}\nx = d.get("b", 0)\nks = d.keys()\n'
                   "vs = d.values()\ns = {1}\ns.add(2)\n")
        self.assertEqual(tc.globals["x"], INT)
        self.assertEqual(tc.globals["ks"], ListT(STR))
        self.assertEqual(tc.globals["vs"], ListT(INT))

    def test_len(self):
        tc = check('d = {"a": 1}\nn = len(d)\ns = {1}\nm = len(s)\n')
        self.assertEqual(tc.globals["n"], INT)

    def test_container_equality_rejected(self):
        check_error(self, "a = {1} == {1}\n", "container equality")


@unittest.skipUnless(TOOLCHAIN, "no C compiler available")
class EndToEndV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory(prefix="pyalt_v2_")
        cls.counter = 0

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    @classmethod
    def run_pya(cls, src):
        cls.counter += 1
        stem = os.path.join(cls.dir.name, f"prog{cls.counter}")
        module = parse(src)
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

    def test_dict_basics(self):
        self.expect('d = {"a": 1, "b": 2}\n'
                    'd["c"] = 3\n'
                    'd["a"] = 10\n'
                    'print(d)\n'
                    'print(len(d), d["a"], d["c"])\n'
                    'print("b" in d, "z" in d, "z" not in d)\n',
                    "{'a': 10, 'b': 2, 'c': 3}\n3 10 3\nTrue False True\n")

    def test_dict_get_keys_values(self):
        self.expect('d = {"x": 1.5}\n'
                    'print(d.get("x", 0.0), d.get("y", 9.9))\n'
                    'print(d.keys(), d.values())\n',
                    "1.5 9.9\n['x'] [1.5]\n")

    def test_dict_iteration_order(self):
        self.expect('d = {"b": 2, "a": 1, "c": 3}\n'
                    "total = 0\n"
                    "for k in d:\n"
                    "    total = total + d[k]\n"
                    "print(total)\n"
                    "for k in d:\n"
                    "    print(k)\n",
                    "6\nb\na\nc\n")

    def test_int_keys(self):
        self.expect("d = {10: \"x\", 20: \"y\"}\n"
                    "print(d[20], len(d))\n"
                    "d[30] = \"z\"\n"
                    "print(30 in d)\n",
                    "y 2\nTrue\n")

    def test_set_basics(self):
        self.expect('s = {"b", "a", "b"}\n'
                    "print(s, len(s))\n"
                    's.add("c")\n'
                    's.add("a")\n'
                    "print(s, len(s))\n"
                    'print("a" in s, "z" in s)\n',
                    "{'b', 'a'} 2\n{'b', 'a', 'c'} 3\nTrue False\n")

    def test_empty_set(self):
        self.expect("s: set[int] = set()\nprint(s, len(s))\ns.add(5)\nprint(s)\n",
                    "set() 0\n{5}\n")

    def test_growth_many_keys(self):
        self.expect("d: dict[int, int] = {}\n"
                    "for i in range(1000):\n"
                    "    d[i * 7] = i\n"
                    "print(len(d), d[0], d[6993])\n"
                    "hits = 0\n"
                    "for i in range(7000):\n"
                    "    if i in d:\n"
                    "        hits = hits + 1\n"
                    "print(hits)\n",
                    "1000 0 999\n1000\n")

    def test_wordcount_pattern(self):
        self.expect('words = "a b a c b a".split(" ")\n'
                    "counts: dict[str, int] = {}\n"
                    "for w in words:\n"
                    "    if w in counts:\n"
                    "        counts[w] = counts[w] + 1\n"
                    "    else:\n"
                    "        counts[w] = 1\n"
                    "print(counts)\n",
                    "{'a': 3, 'b': 2, 'c': 1}\n")

    def test_missing_key_dies(self):
        code, out, err = self.run_pya('d = {"a": 1}\nprint(d["zz"])\n')
        self.assertEqual(code, 1)
        self.assertIn("key not found", err)

    def test_float_keys(self):
        self.expect("d = {1.5: 10, 2.5: 20}\nprint(d[2.5], 1.5 in d, 9.9 in d)\n",
                    "20 True False\n")


if __name__ == "__main__":
    unittest.main()
