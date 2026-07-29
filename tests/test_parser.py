import unittest

from compiler.lexer import Lexer
from compiler.parser import Parser
from compiler import ast_nodes as A
from compiler.errors import CompileError


def parse(src):
    tokens = Lexer(src, "<test>").tokenize()
    return Parser(tokens, "<test>", src).parse_module()


class ParserTests(unittest.TestCase):
    def test_funcdef(self):
        m = parse("def f(x: int) -> int:\n    return x + 1\n")
        fn = m.body[0]
        self.assertIsInstance(fn, A.FuncDef)
        self.assertEqual(fn.name, "f")
        self.assertEqual(fn.params[0].name, "x")
        self.assertIsInstance(fn.params[0].ann, A.TypeName)
        self.assertIsInstance(fn.return_ann, A.TypeName)
        self.assertIsInstance(fn.body[0], A.Return)

    def test_funcdef_inferred_return(self):
        m = parse("def f(x: float):\n    return x\n")
        self.assertIsNone(m.body[0].return_ann)

    def test_param_needs_type(self):
        with self.assertRaises(CompileError) as cm:
            parse("def f(x):\n    return x\n")
        self.assertIn("type annotation", str(cm.exception))

    def test_if_elif_else(self):
        m = parse("if a:\n    x = 1\nelif b:\n    x = 2\nelse:\n    x = 3\n")
        node = m.body[0]
        self.assertIsInstance(node, A.If)
        inner = node.orelse[0]
        self.assertIsInstance(inner, A.If)           # the elif
        self.assertTrue(inner.orelse)                # the else
        self.assertIsInstance(inner.orelse[0], A.Assign)

    def test_single_line_suite(self):
        m = parse("if x > 0: y = 1\n")               # SPEC §3 allows this form
        self.assertEqual(len(m.body[0].body), 1)
        self.assertIsInstance(m.body[0].body[0], A.Assign)

    def test_while_and_for(self):
        m = parse("while i < n:\n    i = i + 1\nfor w in words:\n    print(w)\n")
        self.assertIsInstance(m.body[0], A.While)
        self.assertIsInstance(m.body[1], A.For)
        self.assertEqual(m.body[1].var, "w")

    def test_ann_assign_empty_list(self):
        m = parse("xs: list[int] = []\n")
        st = m.body[0]
        self.assertIsInstance(st, A.AnnAssign)
        self.assertIsInstance(st.ann, A.ListType)
        self.assertEqual(st.ann.elem.name, "int")
        self.assertEqual(st.value.elts, [])

    def test_index_assignment(self):
        m = parse("xs[0] = 5\n")
        self.assertIsInstance(m.body[0].target, A.Index)

    def test_cannot_assign_to_call(self):
        with self.assertRaises(CompileError) as cm:
            parse("f(x) = 1\n")
        self.assertIn("cannot assign", str(cm.exception))

    def test_method_chain(self):
        m = parse('t = line.lower().split(" ")\n')
        call = m.body[0].value
        self.assertIsInstance(call, A.Call)
        self.assertIsInstance(call.func, A.Attribute)
        self.assertEqual(call.func.attr, "split")
        inner = call.func.value
        self.assertIsInstance(inner, A.Call)          # line.lower()
        self.assertEqual(inner.func.attr, "lower")

    def test_precedence(self):
        m = parse("x = 1 + 2 * 3\n")
        v = m.body[0].value
        self.assertEqual(v.op, "+")
        self.assertEqual(v.right.op, "*")

    def test_power_right_assoc(self):
        m = parse("x = 2 ** 3 ** 2\n")
        v = m.body[0].value
        self.assertEqual(v.op, "**")
        self.assertEqual(v.right.op, "**")            # 2 ** (3 ** 2)

    def test_unary_minus_vs_power(self):
        m = parse("x = -2 ** 2\n")                    # -(2 ** 2), as in Python
        v = m.body[0].value
        self.assertIsInstance(v, A.UnaryOp)
        self.assertEqual(v.operand.op, "**")

    def test_in_and_not_in(self):
        m = parse("a = x in xs\nb = x not in xs\n")
        self.assertEqual(m.body[0].value.op, "in")
        self.assertEqual(m.body[1].value.op, "not in")

    def test_chained_comparison_rejected(self):
        with self.assertRaises(CompileError) as cm:
            parse("x = 1 < a < 2\n")
        self.assertIn("chained", str(cm.exception).lower())

    def test_missing_colon(self):
        with self.assertRaises(CompileError) as cm:
            parse("if x\n    y = 1\n")
        self.assertIn("':'", str(cm.exception))

    def test_missing_indent(self):
        with self.assertRaises(CompileError) as cm:
            parse("if x:\ny = 1\n")
        self.assertIn("indented block", str(cm.exception))

    def test_slice_and_index(self):
        m = parse("a = xs[1]\nb = xs[1:3]\nc = xs[:3]\nd = xs[1:]\n")
        self.assertIsInstance(m.body[0].value, A.Index)
        self.assertIsInstance(m.body[1].value, A.Slice)
        self.assertIsNone(m.body[2].value.lo)
        self.assertIsNone(m.body[3].value.hi)

    def test_fstring_expr(self):
        m = parse('print(f"n={n + 1}")\n')
        fstr = m.body[0].value.args[0]
        self.assertIsInstance(fstr, A.FString)
        self.assertIsInstance(fstr.parts[0], A.StrLit)
        self.assertIsInstance(fstr.parts[1], A.BinOp)

    def test_orphan_else(self):
        with self.assertRaises(CompileError) as cm:
            parse("else:\n    x = 1\n")
        self.assertIn("matching 'if'", str(cm.exception))

    def test_error_position_points_at_token(self):
        with self.assertRaises(CompileError) as cm:
            parse("x = 1 +\n")
        self.assertEqual(cm.exception.line, 1)


if __name__ == "__main__":
    unittest.main()
