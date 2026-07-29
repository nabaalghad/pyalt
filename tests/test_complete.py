"""Tests for the 'complete the language' wave: classes, try/except/raise,
dict/set deletion, and `from x import y`."""

import os
import subprocess
import tempfile
import unittest

from compiler.lexer import Lexer
from compiler.parser import Parser
from compiler.typechecker import TypeChecker
from compiler.cemitter import CEmitter
from compiler.errors import CompileError
from compiler.modules import load_program
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


class ClassTypecheckTests(unittest.TestCase):
    def test_basic_class(self):
        _, tc = check("class Point:\n    x: float\n    y: float\n"
                      "p = Point(1.0, 2.0)\na = p.x\n")
        self.assertEqual(str(tc.globals["p"]), "Point")
        self.assertEqual(str(tc.globals["a"]), "float")

    def test_ctor_arg_mismatch(self):
        check_error(self, "class P:\n    x: int\np = P(1.5)\n",
                    "should be int but is float")

    def test_unknown_field(self):
        check_error(self, "class P:\n    x: int\np = P(1)\na = p.zz\n",
                    "no field 'zz'")

    def test_field_assign_type_mismatch(self):
        check_error(self, "class P:\n    x: int\np = P(1)\np.x = \"no\"\n",
                    "field 'x' is int")

    def test_method_and_self(self):
        _, tc = check("class Counter:\n"
                      "    n: int\n"
                      "    def inc(self, by: int):\n"
                      "        self.n = self.n + by\n"
                      "    def get(self) -> int:\n"
                      "        return self.n\n"
                      "c = Counter(0)\nc.inc(5)\nv = c.get()\n")
        self.assertEqual(str(tc.globals["v"]), "int")

    def test_self_referencing_class_type(self):
        # a class may reference itself in its own fields (linked structures)
        _, tc = check("class Node:\n    value: int\n    nxt: Node\n"
                      "def tip(n: Node) -> int:\n    return n.value\n")
        info = tc.classes["PC_Node"]
        self.assertEqual(str(info.field_map["nxt"]), "Node")

    def test_class_in_containers(self):
        check("class P:\n    x: int\n"
              "ps: list[P] = []\nps.append(P(1))\nv = ps[0].x\n")

    def test_print_instance_rejected_at_emit(self):
        module, tc = check("class P:\n    x: int\np = P(1)\nprint(p)\n")
        with self.assertRaises(CompileError) as cm:
            CEmitter(module, tc, "<t>", "").emit()
        self.assertIn("cannot print", str(cm.exception))

    def test_instance_equality_rejected(self):
        check_error(self, "class P:\n    x: int\n"
                          "a = P(1) == P(1)\n", "mismatched operand")


class ExceptionTypecheckTests(unittest.TestCase):
    def test_raise_needs_str(self):
        check_error(self, "raise 42\n", "needs a str")

    def test_bind_is_str(self):
        _, tc = check("try:\n    x = 1\nexcept as m:\n    print(m)\n")
        self.assertEqual(str(tc.globals["m"]), "str")


class FromImportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pyalt_fi_")
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, src):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
        return path

    def test_from_import(self):
        self.write("util.pya", "def double(n: int) -> int:\n    return n * 2\n"
                               "def triple(n: int) -> int:\n    return n * 3\n")
        main = self.write("main.pya",
                          "from util import double, triple\n"
                          "print(double(2) + triple(3))\n")
        prog = load_program(main)
        self.assertEqual(len(prog.deps), 1)

    def test_from_import_unknown_name(self):
        self.write("util.pya", "def real(n: int) -> int:\n    return n\n")
        main = self.write("main.pya", "from util import fake\n")
        with self.assertRaises(CompileError) as cm:
            load_program(main)
        self.assertIn("no function 'fake'", str(cm.exception))

    def test_alias_shadow_rejected(self):
        self.write("util.pya", "def f(n: int) -> int:\n    return n\n")
        main = self.write("main.pya", "from util import f\nf = 5\n")
        with self.assertRaises(CompileError) as cm:
            load_program(main)
        self.assertIn("imported function", str(cm.exception))


@unittest.skipUnless(TOOLCHAIN, "no C compiler available")
class CompleteEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory(prefix="pyalt_full_")
        cls.counter = 0

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    @classmethod
    def run_pya(cls, src):
        cls.counter += 1
        stem = os.path.join(cls.dir.name, f"prog{cls.counter}")
        module, tc = check(src)
        c_code = CEmitter(module, tc, "<full>", src).emit()
        with open(stem + ".c", "w", encoding="utf-8") as fh:
            fh.write(c_code)
        ok, output = TOOLCHAIN.compile(stem + ".c", stem + ".exe", RUNTIME_DIR)
        if not ok:
            raise AssertionError(f"C compilation failed:\n{output}\n{c_code}")
        proc = subprocess.run([stem + ".exe"], capture_output=True, text=True,
                              timeout=60, cwd=cls.dir.name)
        return proc.returncode, proc.stdout, proc.stderr

    def expect(self, src, expected):
        code, out, err = self.run_pya(src)
        self.assertEqual(code, 0, f"stderr: {err}")
        self.assertEqual(out, expected)

    def test_class_fields_methods(self):
        self.expect("class Point:\n"
                    "    x: float\n"
                    "    y: float\n"
                    "    def dist2(self, o: Point) -> float:\n"
                    "        dx = self.x - o.x\n"
                    "        dy = self.y - o.y\n"
                    "        return dx * dx + dy * dy\n"
                    "    def scale(self, k: float):\n"
                    "        self.x = self.x * k\n"
                    "        self.y = self.y * k\n"
                    "p = Point(3.0, 4.0)\n"
                    "print(p.dist2(Point(0.0, 0.0)))\n"
                    "p.scale(2.0)\n"
                    "print(p.x, p.y)\n",
                    "25.0\n6.0 8.0\n")

    def test_instances_in_list(self):
        self.expect("class Box:\n"
                    "    v: int\n"
                    "boxes: list[Box] = []\n"
                    "for i in range(5):\n"
                    "    boxes.append(Box(i * 10))\n"
                    "total = 0\n"
                    "for b in boxes:\n"
                    "    total = total + b.v\n"
                    "print(total)\n",
                    "100\n")

    def test_nested_instances(self):
        # class-typed fields: an instance holding another instance
        self.expect("class Inner:\n"
                    "    v: int\n"
                    "class Outer:\n"
                    "    inner: Inner\n"
                    "    label: str\n"
                    'o = Outer(Inner(42), "box")\n'
                    "print(o.label, o.inner.v)\n"
                    "o.inner.v = 43\n"
                    "print(o.inner.v)\n",
                    "box 42\n43\n")

    def test_uninitialized_instance_caught(self):
        code, out, err = self.run_pya(
            "class P:\n    x: int\n"
            "def maybe(flag: bool) -> int:\n"
            "    p: P = P(1)\n"
            "    return p.x\n"
            "q: list[P] = []\n"
            "try:\n"
            "    r: P = P(0)\n"
            "    print(r.x)\n"
            "except as m:\n"
            "    print(m)\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "0\n")

    def test_exceptions_full(self):
        self.expect("def risky(n: int) -> int:\n"
                    '    if n < 0:\n'
                    '        raise "negative"\n'
                    "    return n * 2\n"
                    "total = 0\n"
                    "for i in range(5):\n"
                    "    try:\n"
                    "        total = total + risky(i - 2)\n"
                    "    except as m:\n"
                    "        total = total + 100\n"
                    "print(total)\n",
                    "206\n")  # -2,-1 -> +100 each; 0,1,2 -> 0+2+4

    def test_runtime_errors_catchable(self):
        self.expect("try:\n"
                    "    xs = [1]\n"
                    "    print(xs[9])\n"
                    "except as m:\n"
                    "    print(\"caught:\", m)\n"
                    "try:\n"
                    "    a = 1\n"
                    "    b = 0\n"
                    "    c = a // b\n"
                    "except as m:\n"
                    "    print(\"caught:\", m)\n"
                    'try:\n'
                    '    s = read_file("no_such_file_xyz.txt")\n'
                    "except as m:\n"
                    "    print(\"caught\")\n",
                    "caught: list index out of range\n"
                    "caught: integer division by zero\n"
                    "caught\n")

    def test_nested_try_and_reraise(self):
        self.expect("try:\n"
                    "    try:\n"
                    '        raise "inner"\n'
                    "    except as m:\n"
                    '        raise "outer: " + m\n'
                    "except as m2:\n"
                    "    print(m2)\n",
                    "outer: inner\n")

    def test_try_with_loops_and_break(self):
        self.expect("found = -1\n"
                    "for i in range(10):\n"
                    "    try:\n"
                    "        if i == 3:\n"
                    '            raise "hit"\n'
                    "    except:\n"
                    "        found = i\n"
                    "        break\n"
                    "print(found)\n",
                    "3\n")

    def test_uncaught_still_aborts(self):
        code, out, err = self.run_pya('raise "boom"\n')
        self.assertEqual(code, 1)
        self.assertIn("boom", err)

    def test_dict_deletion(self):
        self.expect('d = {"a": 1, "b": 2, "c": 3}\n'
                    'v = d.pop("b")\n'
                    "print(v, len(d))\n"
                    "print(d)\n"
                    'd["b"] = 20\n'
                    "print(d)\n"
                    "for k in d:\n"
                    "    print(k)\n",
                    "2 2\n{'a': 1, 'c': 3}\n{'a': 1, 'c': 3, 'b': 20}\n"
                    "a\nc\nb\n")

    def test_set_removal(self):
        self.expect("s = {1, 2, 3, 4}\n"
                    "s.remove(3)\n"
                    "print(s, len(s), 3 in s)\n"
                    "s.add(3)\n"
                    "print(s)\n",
                    "{1, 2, 4} 3 False\n{1, 2, 4, 3}\n")

    def test_deletion_stress_with_reuse(self):
        self.expect("d: dict[int, int] = {}\n"
                    "for i in range(1000):\n"
                    "    d[i] = i\n"
                    "for i in range(0, 1000, 2):\n"
                    "    v = d.pop(i)\n"
                    "print(len(d))\n"
                    "for i in range(0, 1000, 2):\n"
                    "    d[i] = i * 2\n"
                    "print(len(d), d[10], d[11])\n",
                    "500\n1000 20 11\n")

    def test_keys_values_after_deletion(self):
        self.expect('d = {"x": 1, "y": 2, "z": 3}\n'
                    'v = d.pop("y")\n'
                    "print(d.keys(), d.values())\n",
                    "['x', 'z'] [1, 3]\n")


@unittest.skipUnless(TOOLCHAIN, "no C compiler available")
class FromImportEndToEndTests(unittest.TestCase):
    def test_from_import_runs(self):
        with tempfile.TemporaryDirectory(prefix="pyalt_fie2e_") as d:
            util = os.path.join(d, "util.pya")
            with open(util, "w", encoding="utf-8") as fh:
                fh.write("def double(n: int) -> int:\n    return n * 2\n")
            main = os.path.join(d, "main.pya")
            with open(main, "w", encoding="utf-8") as fh:
                fh.write("from util import double\nprint(double(21))\n")
            prog = load_program(main)
            c_code = CEmitter(prog.module, prog.tc, main, prog.src,
                              deps=prog.deps).emit()
            stem = os.path.join(d, "out")
            with open(stem + ".c", "w", encoding="utf-8") as fh:
                fh.write(c_code)
            ok, output = TOOLCHAIN.compile(stem + ".c", stem + ".exe",
                                           RUNTIME_DIR)
            self.assertTrue(ok, output)
            proc = subprocess.run([stem + ".exe"], capture_output=True,
                                  text=True, timeout=60)
            self.assertEqual(proc.stdout, "42\n")


if __name__ == "__main__":
    unittest.main()
