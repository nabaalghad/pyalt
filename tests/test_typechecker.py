import unittest

from compiler.lexer import Lexer
from compiler.parser import Parser
from compiler.typechecker import TypeChecker
from compiler.types import INT, FLOAT, BOOL, STR, VOID, ListT
from compiler.errors import CompileError


def check(src):
    tokens = Lexer(src, "<test>").tokenize()
    module = Parser(tokens, "<test>", src).parse_module()
    tc = TypeChecker("<test>", src)
    tc.check_module(module)
    return tc


def check_error(testcase, src, snippet):
    with testcase.assertRaises(CompileError) as cm:
        check(src)
    testcase.assertIn(snippet.lower(), str(cm.exception).lower())
    return cm.exception


class InferenceTests(unittest.TestCase):
    def test_basic_inference(self):
        tc = check("a = 1\nb = 2.5\nc = \"hi\"\nd = True\n")
        self.assertEqual(tc.globals["a"], INT)
        self.assertEqual(tc.globals["b"], FLOAT)
        self.assertEqual(tc.globals["c"], STR)
        self.assertEqual(tc.globals["d"], BOOL)

    def test_list_inference(self):
        tc = check("xs = [1, 2, 3]\nys: list[str] = []\n")
        self.assertEqual(tc.globals["xs"], ListT(INT))
        self.assertEqual(tc.globals["ys"], ListT(STR))

    def test_int_division_is_float(self):
        tc = check("x = 3 / 2\ny = 3 // 2\n")
        self.assertEqual(tc.globals["x"], FLOAT)
        self.assertEqual(tc.globals["y"], INT)

    def test_function_return_inference(self):
        tc = check("def f(x: float):\n    if x < 0.0: return 0.0\n"
                   "    return x\n")
        self.assertEqual(tc.funcs["f"].ret, FLOAT)

    def test_void_function(self):
        tc = check("def report(n: int):\n    print(n)\n")
        self.assertEqual(tc.funcs["report"].ret, VOID)

    def test_annotated_recursion(self):
        tc = check("def fib(n: int) -> int:\n"
                   "    if n < 2: return n\n"
                   "    return fib(n - 1) + fib(n - 2)\n")
        self.assertEqual(tc.funcs["fib"].ret, INT)

    def test_for_over_range_and_list(self):
        tc = check("total = 0\nfor i in range(10):\n    total = total + i\n"
                   "words = [\"a\", \"bb\"]\nfor w in words:\n    print(w)\n")
        self.assertEqual(tc.globals["i"], INT)
        self.assertEqual(tc.globals["w"], STR)

    def test_str_iteration(self):
        tc = check("for ch in \"abc\":\n    print(ch)\n")
        self.assertEqual(tc.globals["ch"], STR)

    def test_method_calls(self):
        tc = check('parts = "a b".split(" ")\nn = "abc".find("b")\n'
                   'xs = [3, 1]\nxs.append(2)\nxs.sort()\nlast = xs.pop()\n')
        self.assertEqual(tc.globals["parts"], ListT(STR))
        self.assertEqual(tc.globals["n"], INT)
        self.assertEqual(tc.globals["last"], INT)

    def test_builtins(self):
        tc = check('n = len("abc")\nm = len([1, 2])\ns = str(42)\n'
                   'f = float(1)\nt = clock()\nlines = read_lines("x.txt")\n')
        self.assertEqual(tc.globals["n"], INT)
        self.assertEqual(tc.globals["s"], STR)
        self.assertEqual(tc.globals["f"], FLOAT)
        self.assertEqual(tc.globals["t"], FLOAT)
        self.assertEqual(tc.globals["lines"], ListT(STR))

    def test_fstring_is_str(self):
        tc = check('n = 5\nmsg = f"n={n} half={n / 2}"\n')
        self.assertEqual(tc.globals["msg"], STR)

    def test_index_and_slice(self):
        tc = check('xs = [1, 2, 3]\na = xs[0]\nb = xs[1:]\n'
                   's = "abc"\nc = s[0]\nd = s[1:2]\n')
        self.assertEqual(tc.globals["a"], INT)
        self.assertEqual(tc.globals["b"], ListT(INT))
        self.assertEqual(tc.globals["c"], STR)
        self.assertEqual(tc.globals["d"], STR)


class RejectionTests(unittest.TestCase):
    def test_int_plus_str(self):
        e = check_error(self, 'x = 1 + "a"\n', "mismatched")
        self.assertIn("str(", e.message)          # the helpful hint

    def test_no_implicit_int_float(self):
        check_error(self, "x = 1 + 2.0\n", "no implicit")

    def test_condition_must_be_bool(self):
        check_error(self, "if 1:\n    pass\n", "condition must be bool")

    def test_reassign_different_type(self):
        check_error(self, 'x = 1\nx = "a"\n', "variables keep one type")

    def test_undefined_name(self):
        check_error(self, "x = y + 1\n", "not defined")

    def test_wrong_arg_type(self):
        check_error(self, "def f(n: int) -> int:\n    return n\n"
                          'y = f("a")\n', "should be int but is str")

    def test_wrong_arg_count(self):
        check_error(self, "def f(n: int) -> int:\n    return n\n"
                          "y = f(1, 2)\n", "takes 1 argument")

    def test_unannotated_recursion(self):
        check_error(self, "def f(n: int):\n    return f(n)\n",
                    "return type annotation")

    def test_forward_call(self):
        check_error(self, "x = f(1)\ndef f(a: int) -> int:\n    return a\n",
                    "define functions before")

    def test_empty_list_needs_annotation(self):
        check_error(self, "xs = []\n", "annotate")

    def test_append_wrong_type(self):
        check_error(self, 'xs = [1]\nxs.append("a")\n', "should be int")

    def test_break_outside_loop(self):
        check_error(self, "break\n", "outside a loop")

    def test_global_in_function(self):
        check_error(self, "g = 1\ndef f(x: int) -> int:\n    return g\n",
                    "not accessible inside a function")

    def test_void_as_value(self):
        check_error(self, "def f(x: int):\n    print(x)\ny = f(1)\n",
                    "void")

    def test_range_outside_for(self):
        check_error(self, "x = range(3)\n", "for loop")

    def test_str_index_assignment(self):
        check_error(self, 's = "ab"\ns[0] = "c"\n', "immutable")

    def test_fstring_with_list(self):
        check_error(self, 'xs = [1]\nm = f"{xs}"\n', "f-string")

    def test_mixed_list(self):
        check_error(self, 'xs = [1, "a"]\n', "same type")

    def test_return_type_mismatch(self):
        check_error(self, "def f(n: int) -> int:\n    return 1.5\n",
                    "returns float")

    def test_declared_return_but_no_return(self):
        check_error(self, "def f(n: int) -> int:\n    print(n)\n",
                    "never returns")

    def test_inconsistent_inferred_returns(self):
        check_error(self, "def f(n: int):\n    if n > 0: return 1\n"
                          '    return "a"\n', "inconsistent return")

    def test_nested_def(self):
        check_error(self, "def f(x: int):\n    def g(y: int):\n"
                          "        return y\n", "top level")

    def test_unknown_method(self):
        check_error(self, 's = "a".explode()\n', "no method")

    def test_list_equality_rejected(self):
        check_error(self, "a = [1] == [1]\n", "container equality")


class ExamplesTypecheckTests(unittest.TestCase):
    def test_all_examples_typecheck(self):
        import glob
        import os
        from compiler.modules import load_program
        ex_dir = os.path.join(os.path.dirname(__file__), "..", "examples")
        files = sorted(glob.glob(os.path.join(ex_dir, "*.pya")))
        self.assertEqual(len(files), 13)
        for path in files:
            with self.subTest(example=os.path.basename(path)):
                load_program(path)  # resolves imports and typechecks


if __name__ == "__main__":
    unittest.main()
