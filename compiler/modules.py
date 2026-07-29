"""pyalt module loader: turns a main .pya file plus its imports into a fully
type-checked multi-module program.

Rules:
- `import utils` loads utils.pya from the importing file's directory.
- Imported modules may contain only function definitions (and imports of
  their own) — no top-level code, so nothing "runs" on import.
- Imports may nest; the graph is deduplicated (diamonds are fine) and cycles
  are a compile error.
- Two distinct files with the same module name in one program is an error
  (module names become C symbol prefixes).
"""

import os

from . import ast_nodes as A
from .errors import CompileError
from .lexer import Lexer
from .parser import Parser
from .typechecker import TypeChecker, BUILTIN_NAMES


class Program:
    def __init__(self, path, src, module, tc, deps):
        self.path = path
        self.src = src
        self.module = module      # main module AST
        self.tc = tc              # main module's TypeChecker
        self.deps = deps          # [(name, module_ast, tc)] dependency-first


def load_program(main_path):
    loaded = {}       # abspath -> (name, module_ast, tc)
    order = []        # dependency-first list of loaded dep entries
    visiting = []     # abspath stack for cycle detection
    name_paths = {}   # module name -> abspath (collision guard)

    def load(path, is_main, err_file=None, err_src=None, err_node=None):
        ap = os.path.normcase(os.path.abspath(path))
        if ap in visiting:
            raise CompileError(err_file, err_node.line, err_node.col,
                               f"circular import of "
                               f"'{os.path.splitext(os.path.basename(path))[0]}'",
                               err_src)
        if ap in loaded:
            return loaded[ap]
        if not os.path.exists(path):
            raise CompileError(err_file, err_node.line, err_node.col,
                               f"cannot find module "
                               f"'{os.path.splitext(os.path.basename(path))[0]}' "
                               f"(looked for {path})", err_src)
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        name = os.path.splitext(os.path.basename(path))[0]
        tokens = Lexer(src, path).tokenize()
        module = Parser(tokens, path, src).parse_module()

        imports = [s for s in module.body
                   if isinstance(s, (A.Import, A.FromImport))]
        visiting.append(ap)
        dep_funcs = {}
        dep_classes = {}
        for imp in imports:
            mod_name = imp.name if isinstance(imp, A.Import) else imp.module
            if mod_name in BUILTIN_NAMES:
                raise CompileError(path, imp.line, imp.col,
                                   f"module name '{mod_name}' conflicts with "
                                   f"a builtin", src)
            dep_path = os.path.join(os.path.dirname(ap), mod_name + ".pya")
            dep_name, _, dep_tc = load(dep_path, False, path, src, imp)
            dep_funcs[dep_name] = dep_tc.funcs
            dep_classes.update(dep_tc.classes)
        visiting.pop()

        if not is_main:
            for s in module.body:
                if not isinstance(s, (A.FuncDef, A.Import, A.FromImport,
                                      A.ClassDef)):
                    raise CompileError(path, s.line, s.col,
                                       f"imported module '{name}' may only "
                                       f"contain functions, classes and "
                                       f"imports (top-level code does not "
                                       f"run on import)", src)
            prev = name_paths.get(name)
            if prev is not None and prev != ap:
                raise CompileError(err_file, err_node.line, err_node.col,
                                   f"two different modules named '{name}' in "
                                   f"this program ({prev} and {ap})", err_src)
            name_paths[name] = ap

        tc = TypeChecker(path, src, modules=dep_funcs,
                         dep_classes=dep_classes,
                         cprefix="" if is_main else name + "__")
        tc.check_module(module)
        entry = (name, module, tc)
        loaded[ap] = entry
        if not is_main:
            order.append(entry)
        return entry

    name, module, tc = load(main_path, True)
    with open(main_path, encoding="utf-8") as fh:
        src = fh.read()
    return Program(main_path, src, module, tc, list(order))
