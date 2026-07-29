import unittest

from compiler.lexer import Lexer
from compiler.errors import CompileError


def types(src):
    return [t.type for t in Lexer(src, "<test>").tokenize()]


class LexerTests(unittest.TestCase):
    def test_simple_line(self):
        self.assertEqual(types("x = 1 + 2.5"),
                         ["NAME", "=", "INT", "+", "FLOAT", "NEWLINE", "EOF"])

    def test_keywords_vs_names(self):
        toks = Lexer("for word in words:", "<test>").tokenize()
        self.assertEqual([t.type for t in toks[:4]], ["for", "NAME", "in", "NAME"])

    def test_two_char_operators(self):
        self.assertEqual(types("a // b ** c -> d"),
                         ["NAME", "//", "NAME", "**", "NAME", "->", "NAME",
                          "NEWLINE", "EOF"])

    def test_indent_dedent(self):
        ts = types("if x:\n    y = 1\nz = 2")
        self.assertIn("INDENT", ts)
        self.assertIn("DEDENT", ts)
        self.assertLess(ts.index("INDENT"), ts.index("DEDENT"))

    def test_blank_and_comment_lines_ignored(self):
        ts = types("x = 1\n\n# a comment\n\ny = 2\n")
        self.assertNotIn("INDENT", ts)
        self.assertEqual(ts.count("NEWLINE"), 2)

    def test_multiline_inside_brackets(self):
        ts = types("xs = [1,\n      2,\n      3]")
        self.assertEqual(ts.count("NEWLINE"), 1)  # only the final one
        self.assertNotIn("INDENT", ts)

    def test_tabs_rejected(self):
        with self.assertRaises(CompileError) as cm:
            Lexer("if x:\n\ty = 1", "<test>").tokenize()
        self.assertIn("tab", str(cm.exception).lower())

    def test_bad_dedent(self):
        with self.assertRaises(CompileError) as cm:
            Lexer("if x:\n        y = 1\n  z = 2", "<test>").tokenize()
        self.assertIn("unindent", str(cm.exception).lower())

    def test_unterminated_string(self):
        with self.assertRaises(CompileError) as cm:
            Lexer('s = "abc', "<test>").tokenize()
        self.assertIn("unterminated", str(cm.exception).lower())

    def test_string_escapes(self):
        toks = Lexer(r's = "a\nb\tc\\d"', "<test>").tokenize()
        s = [t for t in toks if t.type == "STRING"][0]
        self.assertEqual(s.value, "a\nb\tc\\d")

    def test_fstring_parts(self):
        toks = Lexer('msg = f"a{b}c"', "<test>").tokenize()
        fs = [t for t in toks if t.type == "FSTRING"][0]
        self.assertEqual(fs.value[0], ("text", "a"))
        self.assertEqual(fs.value[1][0], "expr")
        self.assertEqual(fs.value[1][1], "b")
        self.assertEqual(fs.value[2], ("text", "c"))

    def test_fstring_nested_brackets(self):
        toks = Lexer('msg = f"n={xs[i + 1]}"', "<test>").tokenize()
        fs = [t for t in toks if t.type == "FSTRING"][0]
        self.assertEqual(fs.value[1][1], "xs[i + 1]")

    def test_reserved_word(self):
        with self.assertRaises(CompileError) as cm:
            Lexer("x = lambda y: y", "<test>").tokenize()
        self.assertIn("reserved", str(cm.exception))

    def test_bool_literals(self):
        toks = Lexer("ok = True", "<test>").tokenize()
        b = [t for t in toks if t.type == "BOOL"][0]
        self.assertIs(b.value, True)

    def test_error_has_position(self):
        with self.assertRaises(CompileError) as cm:
            Lexer("x = 1\ny = @", "<test>").tokenize()
        self.assertEqual(cm.exception.line, 2)


if __name__ == "__main__":
    unittest.main()
