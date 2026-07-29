"""pyalt recursive-descent parser: token stream -> AST.

Grammar follows SPEC.md. Precedence (loosest to tightest):
  or < and < not < comparison < + - < * / // % < unary - < ** < postfix (call/index/attr)
Chained comparisons are rejected (SPEC §5).
"""

from . import ast_nodes as A
from .errors import CompileError
from .lexer import Lexer

COMPARE_OPS = {"==", "!=", "<", "<=", ">", ">="}


def describe(tok):
    if tok.type == "NEWLINE":
        return "end of line"
    if tok.type == "EOF":
        return "end of file"
    if tok.type in ("INDENT", "DEDENT"):
        return "indentation"
    if tok.type in ("NAME", "INT", "FLOAT", "BOOL"):
        return f"'{tok.value}'"
    if tok.type == "STRING":
        return f"string {tok.value!r}"
    if tok.type == "FSTRING":
        return "f-string"
    return f"'{tok.type}'"


class Parser:
    def __init__(self, tokens, filename="<input>", source=None):
        self.toks = tokens
        self.i = 0
        self.filename = filename
        self.source = source

    # -- helpers ----------------------------------------------------------

    def cur(self):
        return self.toks[self.i]

    def peek(self, k=1):
        j = min(self.i + k, len(self.toks) - 1)
        return self.toks[j]

    def advance(self):
        tok = self.toks[self.i]
        if self.i < len(self.toks) - 1:
            self.i += 1
        return tok

    def at(self, type):
        return self.cur().type == type

    def error(self, message, tok=None):
        tok = tok or self.cur()
        raise CompileError(self.filename, tok.line, tok.col, message, source=self.source)

    def expect(self, type, message=None):
        if not self.at(type):
            base = message or f"expected '{type}'"
            self.error(f"{base} (found {describe(self.cur())})")
        return self.advance()

    def expect_newline(self):
        if self.at("NEWLINE"):
            self.advance()
        elif not self.at("EOF") and not self.at("DEDENT"):
            self.error(f"unexpected {describe(self.cur())} — expected end of line")

    # -- module / statements ----------------------------------------------

    def parse_module(self):
        body = []
        while not self.at("EOF"):
            if self.at("NEWLINE"):
                self.advance()
                continue
            body.append(self.parse_statement())
        return A.Module(body)

    def parse_statement(self):
        t = self.cur()
        if t.type == "import":
            self.advance()
            name = self.expect("NAME", "expected a module name after 'import'")
            self.expect_newline()
            return A.Import(name.value, t.line, t.col)
        if t.type == "from":
            self.advance()
            mod = self.expect("NAME", "expected a module name after 'from'")
            self.expect("import", "expected 'import' after the module name")
            names = [self.expect("NAME", "expected a function name").value]
            while self.at(","):
                self.advance()
                names.append(self.expect("NAME", "expected a function name").value)
            self.expect_newline()
            return A.FromImport(mod.value, names, t.line, t.col)
        if t.type == "class":
            return self.parse_classdef()
        if t.type == "try":
            return self.parse_try()
        if t.type == "raise":
            self.advance()
            value = self.parse_expression()
            self.expect_newline()
            return A.Raise(value, t.line, t.col)
        if t.type == "def":
            return self.parse_funcdef()
        if t.type == "if":
            return self.parse_if("if")
        if t.type == "while":
            return self.parse_while()
        if t.type == "for":
            return self.parse_for()
        if t.type == "parallel":
            self.advance()
            if not self.at("for"):
                self.error("expected 'for' after 'parallel'")
            st = self.parse_for()
            st.parallel = True
            st.line, st.col = t.line, t.col
            return st
        if t.type == "return":
            self.advance()
            value = None
            if not self.at("NEWLINE") and not self.at("EOF") and not self.at("DEDENT"):
                value = self.parse_expression()
            self.expect_newline()
            return A.Return(value, t.line, t.col)
        if t.type == "break":
            self.advance()
            self.expect_newline()
            return A.Break(t.line, t.col)
        if t.type == "continue":
            self.advance()
            self.expect_newline()
            return A.Continue(t.line, t.col)
        if t.type == "pass":
            self.advance()
            self.expect_newline()
            return A.Pass(t.line, t.col)
        if t.type in ("elif", "else"):
            self.error(f"'{t.type}' without a matching 'if'", t)
        return self.parse_simple_statement()

    def parse_classdef(self):
        tok = self.expect("class")
        name = self.expect("NAME", "expected class name after 'class'")
        self.expect(":", "expected ':' after the class name")
        self.expect("NEWLINE", "expected a newline after ':'")
        self.expect("INDENT", "expected an indented class body")
        fields = []
        methods = []
        while not self.at("DEDENT") and not self.at("EOF"):
            if self.at("NEWLINE"):
                self.advance()
                continue
            if self.at("pass"):
                self.advance()
                self.expect_newline()
                continue
            if self.at("def"):
                methods.append(self.parse_funcdef(in_class=True))
                continue
            ft = self.expect("NAME", "expected a field declaration "
                                     "(name: type) or a method (def ...)")
            self.expect(":", f"expected ':' after field name '{ft.value}'")
            ann = self.parse_type()
            self.expect_newline()
            fields.append((ft.value, ann, ft.line, ft.col))
        self.expect("DEDENT")
        return A.ClassDef(name.value, fields, methods, tok.line, tok.col)

    def parse_try(self):
        tok = self.expect("try")
        self.expect(":", "expected ':' after 'try'")
        self.expect("NEWLINE", "expected a newline after 'try:'")
        self.expect("INDENT", "expected an indented block after 'try:'")
        body = []
        while not self.at("DEDENT") and not self.at("EOF"):
            if self.at("NEWLINE"):
                self.advance()
                continue
            body.append(self.parse_statement())
        self.expect("DEDENT")
        self.expect("except", "expected 'except' after the try block")
        bind = None
        if self.at("as"):
            self.advance()
            bind = self.expect("NAME", "expected a name after 'as'").value
        handler = self.parse_block()
        return A.Try(body, handler, bind, tok.line, tok.col)

    def parse_funcdef(self, in_class=False):
        tok = self.expect("def")
        name = self.expect("NAME", "expected function name after 'def'")
        self.expect("(", "expected '(' after function name")
        params = []
        if not self.at(")"):
            first = True
            while True:
                pt = self.expect("NAME", "expected parameter name")
                if in_class and first and pt.value == "self":
                    params.append(A.Param("self", None, pt.line, pt.col))
                    first = False
                    if self.at(","):
                        self.advance()
                        continue
                    break
                first = False
                if not self.at(":"):
                    self.error(
                        f"parameter '{pt.value}' needs a type annotation, "
                        f"e.g. {pt.value}: int")
                self.advance()
                ann = self.parse_type()
                params.append(A.Param(pt.value, ann, pt.line, pt.col))
                if self.at(","):
                    self.advance()
                    continue
                break
        if in_class and (not params or params[0].name != "self"):
            self.error("a method's first parameter must be 'self'", tok)
        self.expect(")", "expected ')' after parameters")
        return_ann = None
        if self.at("->"):
            self.advance()
            return_ann = self.parse_type()
        body = self.parse_block()
        return A.FuncDef(name.value, params, return_ann, body, tok.line, tok.col)

    def parse_if(self, kw):
        tok = self.expect(kw)
        cond = self.parse_expression()
        body = self.parse_block()
        orelse = []
        if self.at("elif"):
            orelse = [self.parse_if("elif")]
        elif self.at("else"):
            self.advance()
            orelse = self.parse_block()
        return A.If(cond, body, orelse, tok.line, tok.col)

    def parse_while(self):
        tok = self.expect("while")
        cond = self.parse_expression()
        body = self.parse_block()
        return A.While(cond, body, tok.line, tok.col)

    def parse_for(self):
        tok = self.expect("for")
        var = self.expect("NAME", "expected loop variable after 'for'")
        self.expect("in", "expected 'in' in for loop")
        iterable = self.parse_expression()
        body = self.parse_block()
        return A.For(var.value, iterable, body, tok.line, tok.col)

    def parse_block(self):
        self.expect(":", "expected ':' before an indented block")
        if not self.at("NEWLINE"):
            # single-line suite: `if x < lo: return lo`
            t = self.cur()
            if t.type in ("def", "if", "while", "for"):
                self.error("a compound statement cannot go on the same line as ':'; "
                           "put it in an indented block", t)
            return [self.parse_statement()]
        self.advance()  # NEWLINE
        self.expect("INDENT", "expected an indented block (indent with 4 spaces)")
        body = []
        while not self.at("DEDENT") and not self.at("EOF"):
            if self.at("NEWLINE"):
                self.advance()
                continue
            body.append(self.parse_statement())
        self.expect("DEDENT")
        return body

    def parse_simple_statement(self):
        t = self.cur()
        # annotated declaration: `xs: list[int] = []`
        if t.type == "NAME" and self.peek().type == ":":
            self.advance()  # NAME
            self.advance()  # ':'
            ann = self.parse_type()
            self.expect("=", "an annotated declaration needs an initial value, "
                             "e.g. xs: list[int] = []")
            value = self.parse_expression()
            self.expect_newline()
            return A.AnnAssign(t.value, ann, value, t.line, t.col)
        expr = self.parse_expression()
        if self.at("="):
            eq = self.cur()
            if not isinstance(expr, (A.Name, A.Index, A.Attribute)):
                self.error("cannot assign to this expression", eq)
            self.advance()
            value = self.parse_expression()
            self.expect_newline()
            return A.Assign(expr, value, expr.line, expr.col)
        self.expect_newline()
        return A.ExprStmt(expr, expr.line, expr.col)

    def parse_type(self):
        t = self.cur()
        if t.type != "NAME":
            self.error("expected a type (int, float, bool, str, or list[...])", t)
        self.advance()
        if t.value == "list":
            self.expect("[", "expected '[' after 'list' — e.g. list[int]")
            elem = self.parse_type()
            self.expect("]", "expected ']' to close list[...]")
            return A.ListType(elem, t.line, t.col)
        if t.value == "dict":
            self.expect("[", "expected '[' after 'dict' — e.g. dict[str, int]")
            key = self.parse_type()
            self.expect(",", "expected ',' between dict key and value types")
            val = self.parse_type()
            self.expect("]", "expected ']' to close dict[...]")
            return A.DictType(key, val, t.line, t.col)
        if t.value == "set":
            self.expect("[", "expected '[' after 'set' — e.g. set[str]")
            elem = self.parse_type()
            self.expect("]", "expected ']' to close set[...]")
            return A.SetType(elem, t.line, t.col)
        return A.TypeName(t.value, t.line, t.col)

    # -- expressions ------------------------------------------------------

    def parse_expression(self):
        return self.parse_or()

    def parse_or(self):
        left = self.parse_and()
        while self.at("or"):
            tok = self.advance()
            right = self.parse_and()
            left = A.BinOp(left, "or", right, tok.line, tok.col)
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.at("and"):
            tok = self.advance()
            right = self.parse_not()
            left = A.BinOp(left, "and", right, tok.line, tok.col)
        return left

    def parse_not(self):
        if self.at("not"):
            tok = self.advance()
            operand = self.parse_not()
            return A.UnaryOp("not", operand, tok.line, tok.col)
        return self.parse_comparison()

    def parse_comparison(self):
        left = self.parse_arith()
        op = None
        tok = self.cur()
        if tok.type in COMPARE_OPS:
            op = tok.type
            self.advance()
        elif tok.type == "in":
            op = "in"
            self.advance()
        elif tok.type == "not" and self.peek().type == "in":
            op = "not in"
            self.advance()
            self.advance()
        if op is None:
            return left
        right = self.parse_arith()
        nxt = self.cur()
        if nxt.type in COMPARE_OPS or nxt.type == "in":
            self.error("chained comparison is not supported in v1 (SPEC §5); "
                       "split it with 'and'", nxt)
        return A.Compare(left, op, right, tok.line, tok.col)

    def parse_arith(self):
        left = self.parse_term()
        while self.cur().type in ("+", "-"):
            tok = self.advance()
            right = self.parse_term()
            left = A.BinOp(left, tok.type, right, tok.line, tok.col)
        return left

    def parse_term(self):
        left = self.parse_unary()
        while self.cur().type in ("*", "/", "//", "%"):
            tok = self.advance()
            right = self.parse_unary()
            left = A.BinOp(left, tok.type, right, tok.line, tok.col)
        return left

    def parse_unary(self):
        if self.at("-"):
            tok = self.advance()
            operand = self.parse_unary()
            return A.UnaryOp("-", operand, tok.line, tok.col)
        if self.at("+"):  # unary plus is a no-op
            self.advance()
            return self.parse_unary()
        return self.parse_power()

    def parse_power(self):
        base = self.parse_postfix()
        if self.at("**"):
            tok = self.advance()
            exponent = self.parse_unary()  # right-associative; allows -x exponent
            return A.BinOp(base, "**", exponent, tok.line, tok.col)
        return base

    def parse_postfix(self):
        node = self.parse_atom()
        while True:
            if self.at("("):
                tok = self.advance()
                args = []
                if not self.at(")"):
                    while True:
                        args.append(self.parse_expression())
                        if self.at(","):
                            self.advance()
                            continue
                        break
                self.expect(")", "expected ')' to close the call")
                node = A.Call(node, args, tok.line, tok.col)
            elif self.at("["):
                tok = self.advance()
                if self.at(":"):
                    self.advance()
                    hi = None if self.at("]") else self.parse_expression()
                    self.expect("]", "expected ']' to close the slice")
                    node = A.Slice(node, None, hi, tok.line, tok.col)
                else:
                    first = self.parse_expression()
                    if self.at(":"):
                        self.advance()
                        hi = None if self.at("]") else self.parse_expression()
                        self.expect("]", "expected ']' to close the slice")
                        node = A.Slice(node, first, hi, tok.line, tok.col)
                    else:
                        self.expect("]", "expected ']' to close the index")
                        node = A.Index(node, first, tok.line, tok.col)
            elif self.at("."):
                self.advance()
                name = self.expect("NAME", "expected a name after '.'")
                node = A.Attribute(node, name.value, name.line, name.col)
            else:
                return node

    def parse_atom(self):
        t = self.cur()
        if t.type == "INT":
            self.advance()
            return A.IntLit(t.value, t.line, t.col)
        if t.type == "FLOAT":
            self.advance()
            return A.FloatLit(t.value, t.line, t.col)
        if t.type == "STRING":
            self.advance()
            return A.StrLit(t.value, t.line, t.col)
        if t.type == "BOOL":
            self.advance()
            return A.BoolLit(t.value, t.line, t.col)
        if t.type == "FSTRING":
            self.advance()
            return self._build_fstring(t)
        if t.type == "NAME":
            self.advance()
            return A.Name(t.value, t.line, t.col)
        if t.type == "(":
            self.advance()
            expr = self.parse_expression()
            self.expect(")", "expected ')' to close the parenthesis")
            return expr
        if t.type == "[":
            self.advance()
            elts = []
            if not self.at("]"):
                while True:
                    elts.append(self.parse_expression())
                    if self.at(","):
                        self.advance()
                        if self.at("]"):
                            break  # trailing comma
                        continue
                    break
            self.expect("]", "expected ']' to close the list")
            return A.ListLit(elts, t.line, t.col)
        if t.type == "{":
            return self._parse_brace_literal(t)
        self.error(f"unexpected {describe(t)} — expected an expression", t)

    def _parse_brace_literal(self, t):
        """{} = empty dict; {k: v, ...} = dict; {a, b, ...} = set."""
        self.advance()  # '{'
        if self.at("}"):
            self.advance()
            return A.DictLit([], [], t.line, t.col)
        first = self.parse_expression()
        if self.at(":"):  # dict literal
            self.advance()
            keys = [first]
            values = [self.parse_expression()]
            while self.at(","):
                self.advance()
                if self.at("}"):
                    break  # trailing comma
                keys.append(self.parse_expression())
                self.expect(":", "expected ':' between dict key and value")
                values.append(self.parse_expression())
            self.expect("}", "expected '}' to close the dict")
            return A.DictLit(keys, values, t.line, t.col)
        elts = [first]  # set literal
        while self.at(","):
            self.advance()
            if self.at("}"):
                break  # trailing comma
            elts.append(self.parse_expression())
        self.expect("}", "expected '}' to close the set")
        return A.SetLit(elts, t.line, t.col)

    def _build_fstring(self, tok):
        parts = []
        for p in tok.value:
            if p[0] == "text":
                parts.append(A.StrLit(p[1], tok.line, tok.col))
            else:
                _, src, line, col = p
                sub_tokens = Lexer(src, self.filename, start_line=line,
                                   start_col=col, expr_mode=True).tokenize()
                sub = Parser(sub_tokens, self.filename, self.source)
                expr = sub.parse_expression()
                if not sub.at("EOF"):
                    sub.error("unexpected text after the expression in this f-string")
                parts.append(expr)
        return A.FString(parts, tok.line, tok.col)
