#!/usr/bin/env python3
"""pyalt compiler driver (source of the standalone pyalt.exe).

Usage:
    pyalt run   program.pya      # compile and run
    pyalt build program.pya      # compile to a native executable
    pyalt buildpy module.pya     # compile to a Python-importable extension
    pyalt check program.pya      # type-check; print inferred types
    pyalt emit  program.pya      # print the generated C
    pyalt parse program.pya      # dump the AST
"""

import argparse
import os
import subprocess
import sys

__version__ = "0.1.0"

from compiler.lexer import Lexer
from compiler.parser import Parser
from compiler.errors import CompileError
from compiler.astdump import dump
from compiler.typechecker import TypeChecker
from compiler.cemitter import CEmitter
from compiler.pyemitter import PyExtEmitter
from compiler.build import find_toolchain
from compiler.modules import load_program

# when frozen into the standalone pyalt.exe, bundled files live in the
# extraction dir (sys._MEIPASS); otherwise next to this source file
if getattr(sys, "frozen", False):
    PROJECT_DIR = sys._MEIPASS
else:
    PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.path.join(PROJECT_DIR, "runtime")
def build_dir_for(source_path):
    """Build artifacts live in a build/ folder NEXT TO THE SOURCE FILE —
    independent of where the terminal happens to be standing (a cwd-based
    dir crashes when the cwd is unwritable, e.g. an admin shell in
    system32)."""
    src_dir = os.path.dirname(os.path.abspath(source_path))
    d = os.path.join(src_dir, "build")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        print(f"pyalt: cannot create build folder {d}: {e.strerror}",
              file=sys.stderr)
        return None
    return d


def build_exe(path, src, out=None):
    prog = load_program(path)
    c_code = CEmitter(prog.module, prog.tc, path, prog.src,
                      deps=prog.deps).emit()
    bdir = build_dir_for(path)
    if bdir is None:
        return None
    stem = os.path.splitext(os.path.basename(path))[0]
    c_path = os.path.join(bdir, stem + ".c")
    exe_path = out or os.path.join(bdir, stem + ".exe")
    with open(c_path, "w", encoding="utf-8") as fh:
        fh.write(c_code)
    toolchain = find_toolchain(cache_dir=bdir)
    if toolchain is None:
        print("pyalt: no C compiler found (need gcc, clang, or Visual Studio "
              "with C++ build tools)", file=sys.stderr)
        return None
    ok, output = toolchain.compile(c_path, exe_path, RUNTIME_DIR)
    if not ok:
        print(f"pyalt: C compilation failed ({toolchain.describe()}) — this is "
              f"a pyalt bug, please report it:", file=sys.stderr)
        print(output, file=sys.stderr)
        return None
    return exe_path


def build_pyd(path, src, out=None):
    """Compile a .pya module into a Python-importable native extension."""
    prog = load_program(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    emitter = PyExtEmitter(prog.module, prog.tc, stem, path, prog.src,
                           deps=prog.deps)
    c_code = emitter.emit()
    for fname, reason in getattr(emitter, "skipped", []):
        print(f"note: '{fname}' not exported ({reason})", file=sys.stderr)
    bdir = build_dir_for(path)
    if bdir is None:
        return None
    ext = ".pyd" if os.name == "nt" else ".so"
    c_path = os.path.join(bdir, stem + "_ext.c")
    pyd_path = out or os.path.join(bdir, stem + ext)
    with open(c_path, "w", encoding="utf-8") as fh:
        fh.write(c_code)
    toolchain = find_toolchain(cache_dir=bdir)
    if toolchain is None:
        print("pyalt: no C compiler found", file=sys.stderr)
        return None
    ok, output = toolchain.compile_pyd(c_path, pyd_path, RUNTIME_DIR)
    if not ok:
        print(f"pyalt: extension compilation failed ({toolchain.describe()}):",
              file=sys.stderr)
        print(output, file=sys.stderr)
        return None
    return pyd_path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pyalt",
                                 description="pyalt compiler — a small, fast, "
                                             "Python-like compiled language")
    ap.add_argument("--version", action="version",
                    version=f"pyalt {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("lex", "parse", "check", "emit", "build", "run", "buildpy"):
        p = sub.add_parser(name)
        p.add_argument("file", help="a .pya source file")
        if name in ("build", "run", "buildpy"):
            p.add_argument("-o", "--output", help="output path")
    args = ap.parse_args(argv)

    with open(args.file, encoding="utf-8") as fh:
        src = fh.read()

    try:
        if args.cmd == "lex":
            for t in Lexer(src, args.file).tokenize():
                print(t)
            return 0
        if args.cmd == "parse":
            tokens = Lexer(src, args.file).tokenize()
            module = Parser(tokens, args.file, src).parse_module()
            print(dump(module))
            return 0
        if args.cmd == "check":
            prog = load_program(args.file)
            print(f"OK {args.file}")
            if prog.deps:
                print("imports:")
                for name, _, dtc in prog.deps:
                    print(f"  {name} ({len(dtc.funcs)} function(s))")
            if prog.tc.funcs:
                print("functions:")
                for sig in prog.tc.funcs.values():
                    print(f"  {sig}")
            if prog.tc.globals:
                print("globals:")
                for name, t in prog.tc.globals.items():
                    print(f"  {name}: {t}")
            return 0
        if args.cmd == "emit":
            prog = load_program(args.file)
            print(CEmitter(prog.module, prog.tc, args.file, prog.src,
                           deps=prog.deps).emit())
            return 0
        if args.cmd == "buildpy":
            pyd = build_pyd(args.file, src, out=getattr(args, "output", None))
            if pyd is None:
                return 1
            stem = os.path.splitext(os.path.basename(pyd))[0]
            print(pyd)
            print(f'use it:  import sys; sys.path.insert(0, r"{os.path.dirname(pyd)}"); '
                  f"import {stem}")
            return 0
        # build / run
        exe = build_exe(args.file, src, out=getattr(args, "output", None))
        if exe is None:
            return 1
        if args.cmd == "build":
            print(exe)
            return 0
        proc = subprocess.run([exe])
        return proc.returncode
    except CompileError as e:
        print(e.pretty(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
