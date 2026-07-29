"""pyalt lexer.

Turns source text into a token stream, handling Python-style indentation
(INDENT/DEDENT tokens), f-strings, and friendly errors.

Token types are plain strings:
  NAME INT FLOAT STRING FSTRING BOOL NEWLINE INDENT DEDENT EOF
  keywords use their own spelling as the type ('if', 'while', ...)
  operators use their own spelling as the type ('+', '**', '->', ...)
"""

from .errors import CompileError

KEYWORDS = {
    "def", "return", "if", "elif", "else", "while", "for", "in",
    "break", "continue", "and", "or", "not", "pass", "import", "parallel",
    "class", "try", "except", "raise", "from", "as",
}

# Python words deliberately not part of pyalt — reserved so programs that
# use them fail loudly with a clear message instead of parsing as identifiers.
RESERVED = {
    "None", "lambda", "finally", "with", "yield", "global", "nonlocal",
    "del", "assert", "is", "async", "await", "match", "case",
}

TWO_CHAR_OPS = {"**", "//", "==", "!=", "<=", ">=", "->"}
ONE_CHAR_OPS = set("+-*/%<>=:,()[].{}")

ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"', "0": "\0"}


class Token:
    __slots__ = ("type", "value", "line", "col")

    def __init__(self, type, value, line, col):
        self.type = type
        self.value = value
        self.line = line
        self.col = col

    def __repr__(self):
        return f"Token({self.type!r}, {self.value!r}, {self.line}:{self.col})"


class Lexer:
    def __init__(self, source, filename="<input>", start_line=1, start_col=1,
                 expr_mode=False):
        self.src = source
        self.filename = filename
        self.pos = 0
        self.line = start_line
        self.col = start_col
        self.tokens = []
        self.indents = [0]
        # expr_mode lexes a bare expression (used for f-string interpolations):
        # no indentation handling, newlines ignored. Faked via paren_depth.
        self.expr_mode = expr_mode
        self.paren_depth = 1 if expr_mode else 0
        self.at_line_start = not expr_mode

    # -- low-level helpers ------------------------------------------------

    def peek(self, off=0):
        i = self.pos + off
        return self.src[i] if i < len(self.src) else ""

    def advance(self, n=1):
        for _ in range(n):
            if self.pos < len(self.src):
                if self.src[self.pos] == "\n":
                    self.line += 1
                    self.col = 1
                else:
                    self.col += 1
                self.pos += 1

    def add(self, type, value, line, col):
        self.tokens.append(Token(type, value, line, col))

    def error(self, message, line=None, col=None):
        raise CompileError(self.filename, line or self.line, col or self.col,
                           message, source=self.src)

    # -- main loop --------------------------------------------------------

    def tokenize(self):
        while True:
            if self.at_line_start and self.paren_depth == 0:
                self._handle_line_start()
            ch = self.peek()
            if ch == "":
                break
            if ch in " \t":
                self.advance()
                continue
            if ch == "#":
                while self.peek() not in ("", "\n"):
                    self.advance()
                continue
            if ch == "\n":
                nl_line, nl_col = self.line, self.col
                self.advance()
                if self.paren_depth > 0:
                    continue  # implicit continuation inside ( ) or [ ]
                if self.tokens and self.tokens[-1].type not in ("NEWLINE", "INDENT", "DEDENT"):
                    self.add("NEWLINE", "\n", nl_line, nl_col)
                self.at_line_start = True
                continue
            if ch.isdigit():
                self._number()
            elif ch.isalpha() or ch == "_":
                self._name()
            elif ch in "\"'":
                self._string(ch)
            else:
                self._operator()

        if not self.expr_mode:
            if self.tokens and self.tokens[-1].type not in ("NEWLINE", "INDENT", "DEDENT"):
                self.add("NEWLINE", "\n", self.line, self.col)
            while len(self.indents) > 1:
                self.indents.pop()
                self.add("DEDENT", None, self.line, self.col)
        self.add("EOF", None, self.line, self.col)
        return self.tokens

    # -- indentation ------------------------------------------------------

    def _handle_line_start(self):
        while True:
            count = 0
            while self.peek() == " ":
                self.advance()
                count += 1
            if self.peek() == "\t":
                self.error("tabs are not allowed for indentation; use 4 spaces")
            ch = self.peek()
            if ch == "\n":          # blank line — ignore entirely
                self.advance()
                continue
            if ch == "#":           # comment-only line — ignore entirely
                while self.peek() not in ("", "\n"):
                    self.advance()
                continue
            if ch == "":
                self.at_line_start = False
                return
            if count > self.indents[-1]:
                self.indents.append(count)
                self.add("INDENT", count, self.line, self.col)
            else:
                while count < self.indents[-1]:
                    self.indents.pop()
                    self.add("DEDENT", None, self.line, self.col)
                if count != self.indents[-1]:
                    self.error("unindent does not match any outer indentation level")
            self.at_line_start = False
            return

    # -- token kinds ------------------------------------------------------

    def _number(self):
        start, line, col = self.pos, self.line, self.col
        while self.peek().isdigit():
            self.advance()
        if self.peek() == "." and self.peek(1).isdigit():
            self.advance()
            while self.peek().isdigit():
                self.advance()
            self.add("FLOAT", float(self.src[start:self.pos]), line, col)
        else:
            self.add("INT", int(self.src[start:self.pos]), line, col)

    def _name(self):
        start, line, col = self.pos, self.line, self.col
        while self.peek().isalnum() or self.peek() == "_":
            self.advance()
        value = self.src[start:self.pos]
        if value in ("f", "F") and self.peek() in "\"'":
            self._fstring(self.peek(), line, col)
            return
        if value in ("True", "False"):
            self.add("BOOL", value == "True", line, col)
        elif value in RESERVED:
            self.error(f"'{value}' is reserved and not part of pyalt v1", line, col)
        elif value in KEYWORDS:
            self.add(value, value, line, col)
        else:
            self.add("NAME", value, line, col)

    def _string(self, quote):
        line, col = self.line, self.col
        self.advance()  # opening quote
        buf = []
        while True:
            ch = self.peek()
            if ch in ("", "\n"):
                self.error("unterminated string literal", line, col)
            if ch == "\\":
                self.advance()
                esc = self.peek()
                if esc not in ESCAPES:
                    self.error(f"unknown escape '\\{esc}'")
                buf.append(ESCAPES[esc])
                self.advance()
            elif ch == quote:
                self.advance()
                break
            else:
                buf.append(ch)
                self.advance()
        self.add("STRING", "".join(buf), line, col)

    def _fstring(self, quote, line, col):
        """Lex f"..." into parts: ('text', str) and ('expr', src, line, col).
        The parser sub-parses each expr part."""
        self.advance()  # opening quote (the 'f' was already consumed)
        parts = []
        buf = []

        def flush():
            if buf:
                parts.append(("text", "".join(buf)))
                buf.clear()

        while True:
            ch = self.peek()
            if ch in ("", "\n"):
                self.error("unterminated f-string", line, col)
            if ch == "\\":
                self.advance()
                esc = self.peek()
                if esc not in ESCAPES:
                    self.error(f"unknown escape '\\{esc}'")
                buf.append(ESCAPES[esc])
                self.advance()
            elif ch == "{":
                if self.peek(1) == "{":
                    buf.append("{")
                    self.advance(2)
                    continue
                flush()
                self.advance()  # '{'
                expr_line, expr_col = self.line, self.col
                expr_chars = []
                depth = 0
                while True:
                    c = self.peek()
                    if c in ("", "\n"):
                        self.error("unterminated expression in f-string", expr_line, expr_col)
                    if c == "}" and depth == 0:
                        break
                    if c in "([{":
                        depth += 1
                    elif c in ")]}":
                        depth -= 1
                    if c in "\"'":  # string literal inside the expression
                        q = c
                        expr_chars.append(c)
                        self.advance()
                        while self.peek() not in ("", "\n") and self.peek() != q:
                            expr_chars.append(self.peek())
                            self.advance()
                        if self.peek() == q:
                            expr_chars.append(q)
                            self.advance()
                        continue
                    expr_chars.append(c)
                    self.advance()
                self.advance()  # '}'
                src = "".join(expr_chars).strip()
                if not src:
                    self.error("empty expression in f-string", expr_line, expr_col)
                parts.append(("expr", src, expr_line, expr_col))
            elif ch == "}":
                if self.peek(1) == "}":
                    buf.append("}")
                    self.advance(2)
                else:
                    self.error("single '}' is not allowed in an f-string; use '}}'")
            elif ch == quote:
                self.advance()
                break
            else:
                buf.append(ch)
                self.advance()
        flush()
        self.add("FSTRING", parts, line, col)

    def _operator(self):
        line, col = self.line, self.col
        two = self.src[self.pos:self.pos + 2]
        if two in TWO_CHAR_OPS:
            self.advance(2)
            self.add(two, two, line, col)
            return
        ch = self.peek()
        if ch in ONE_CHAR_OPS:
            if ch in "([{":
                self.paren_depth += 1
            elif ch in ")]}":
                self.paren_depth = max(0, self.paren_depth - 1)
            self.advance()
            self.add(ch, ch, line, col)
            return
        self.error(f"unexpected character {ch!r}")
