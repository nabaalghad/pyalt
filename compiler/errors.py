"""Compile errors with file/line/col and a caret pointing at the offending source."""


class CompileError(Exception):
    def __init__(self, filename, line, col, message, source=None):
        super().__init__(message)
        self.filename = filename
        self.line = line
        self.col = col
        self.message = message
        self.source = source

    def pretty(self):
        out = [f"{self.filename}:{self.line}:{self.col}: error: {self.message}"]
        if self.source is not None:
            src_lines = self.source.splitlines()
            if 1 <= self.line <= len(src_lines):
                text = src_lines[self.line - 1]
                out.append("    " + text)
                out.append("    " + " " * (self.col - 1) + "^")
        return "\n".join(out)
