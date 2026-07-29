"""pyalt type checker: walks the AST, infers a fixed type for every variable
and expression, and rejects anything it cannot prove.

Everything proved here is never checked again at runtime — this pass is where
the speed comes from.

v1 rules (see SPEC.md):
- Variables keep one type for their whole life.
- No implicit int<->float conversion; convert explicitly.
- Conditions must be bool (no truthiness).
- Functions must be defined before use; recursion needs a return annotation.
- Function bodies see only their parameters and locals (no globals in v1).

Side effects on the AST (consumed by the C emitter in phase 3):
- every expression node gets a `.ty` attribute (its inferred type)
- every FuncDef gets `.sig` (FuncSig) and `.locals` (name -> type)
- every for-over-range node gets `.is_range = True`
"""

from . import ast_nodes as A
from .errors import CompileError
from .types import INT, FLOAT, BOOL, STR, VOID, PRIMS, ListT, DictT, SetT, ClassT


class ClassInfo:
    def __init__(self, name, cname):
        self.name = name
        self.cname = cname
        self.fields = []      # [(name, Type)] in declaration order
        self.field_map = {}   # name -> Type
        self.methods = {}     # name -> FuncSig (params EXCLUDE self)

BUILTIN_NAMES = {
    "print", "len", "range", "int", "float", "str", "bool", "abs", "min",
    "max", "append", "pop", "sort", "read_file", "read_lines", "write_file",
    "clock", "set", "dict", "gc_collect", "args", "input", "exists", "exit",
}

STR_METHODS = {
    # name -> (param types, return type); None in return = "elem type"
    "split":      ((STR,), ListT(STR)),
    "strip":      ((), STR),
    "lower":      ((), STR),
    "upper":      ((), STR),
    "startswith": ((STR,), BOOL),
    "endswith":   ((STR,), BOOL),
    "replace":    ((STR, STR), STR),
    "find":       ((STR,), INT),
}


class FuncSig:
    def __init__(self, name, params, ret, node):
        self.name = name
        self.params = params      # list of (name, Type)
        self.ret = ret            # Type, or None while being inferred
        self.node = node

    def __str__(self):
        ps = ", ".join(f"{n}: {t}" for n, t in self.params)
        return f"{self.name}({ps}) -> {self.ret}"


class TypeChecker:
    def __init__(self, filename="<input>", source=None, modules=None,
                 dep_classes=None, cprefix=""):
        self.filename = filename
        self.source = source
        self.globals = {}         # name -> Type (top-level variables)
        self.funcs = {}           # name -> FuncSig
        self.imported = modules or {}  # module name -> {func name -> FuncSig}
        self.func_aliases = {}    # `from m import f` -> name -> (mod, FuncSig)
        self.cprefix = cprefix    # module qualifier for class C names
        self.class_names = {}     # local class name -> ClassT
        self.classes = dict(dep_classes or {})  # cname -> ClassInfo (shared)
        self.current = None       # FuncSig while checking a function body
        self.loop_depth = 0
        self.loop_kinds = []      # 'n' = normal loop, 'p' = parallel for
        self.in_parallel = False
        self.return_types = []    # [(Type, node)] for return-type inference
        self.had_value_return = False

    def error(self, message, node):
        raise CompileError(self.filename, getattr(node, "line", 0),
                           getattr(node, "col", 0), message, self.source)

    # -- entry point ------------------------------------------------------

    def check_module(self, mod):
        for stmt in mod.body:
            if isinstance(stmt, A.Import):
                pass  # resolved by the module loader before checking
            elif isinstance(stmt, A.FromImport):
                self._register_aliases(stmt)
            elif isinstance(stmt, A.ClassDef):
                self.check_classdef(stmt)
            elif isinstance(stmt, A.FuncDef):
                self.check_funcdef(stmt)
            else:
                self.check_stmt(stmt, self.globals)
        return mod

    def _register_aliases(self, stmt):
        if stmt.module not in self.imported:
            self.error(f"module '{stmt.module}' was not loaded", stmt)
        funcs = self.imported[stmt.module]
        for name in stmt.names:
            if name not in funcs:
                available = ", ".join(sorted(funcs)) or "none"
                self.error(f"module '{stmt.module}' has no function '{name}' "
                           f"(available: {available})", stmt)
            if (name in self.funcs or name in self.func_aliases
                    or name in BUILTIN_NAMES or name in self.class_names):
                self.error(f"'{name}' is already defined here; the import "
                           f"would shadow it", stmt)
            self.func_aliases[name] = (stmt.module, funcs[name])

    # -- declarations -----------------------------------------------------

    def resolve_type(self, ann):
        if isinstance(ann, A.TypeName):
            named = {"int": INT, "float": FLOAT, "bool": BOOL, "str": STR}
            if ann.name in named:
                return named[ann.name]
            if ann.name in self.class_names:
                return self.class_names[ann.name]
            self.error(f"unknown type '{ann.name}' — expected int, float, "
                       f"bool, str, list/dict/set[...], or a class name", ann)
        if isinstance(ann, A.ListType):
            return ListT(self.resolve_type(ann.elem))
        if isinstance(ann, A.DictType):
            key = self.resolve_type(ann.key)
            if key not in PRIMS:
                self.error("dict keys must be int, float, bool or str "
                           "(hashable types)", ann)
            return DictT(key, self.resolve_type(ann.val))
        if isinstance(ann, A.SetType):
            elem = self.resolve_type(ann.elem)
            if elem not in PRIMS:
                self.error("set elements must be int, float, bool or str "
                           "(hashable types)", ann)
            return SetT(elem)
        self.error("invalid type annotation", ann)

    def check_classdef(self, cd):
        if cd.name in self.class_names or cd.name in self.funcs:
            self.error(f"'{cd.name}' is already defined", cd)
        if cd.name in BUILTIN_NAMES:
            self.error(f"'{cd.name}' is a builtin name", cd)
        cname = f"PC_{self.cprefix}{cd.name}"
        info = ClassInfo(cd.name, cname)
        ct = ClassT(cd.name, cname)
        # register the name first: fields may reference the class itself
        # (linked structures) — the GC handles cycles, so this is sound
        self.class_names[cd.name] = ct
        self.classes[cname] = info
        for fname, ann, line, col in cd.fields:
            if fname in info.field_map:
                self.error(f"duplicate field '{fname}' in class {cd.name}", cd)
            t = self.resolve_type(ann)
            info.fields.append((fname, t))
            info.field_map[fname] = t
        # pass 1: method signatures (so methods can call each other)
        for m in cd.methods:
            if m.name in info.methods or m.name in info.field_map:
                self.error(f"duplicate member '{m.name}' in class {cd.name}", m)
            params = []
            for p in m.params[1:]:  # skip self
                params.append((p.name, self.resolve_type(p.ann)))
            ret = self.resolve_type(m.return_ann) if m.return_ann else None
            info.methods[m.name] = FuncSig(f"{cd.name}.{m.name}", params, ret, m)
        # pass 2: method bodies
        for m in cd.methods:
            sig = info.methods[m.name]
            env = {"self": ct}
            for p, (pn, pt) in zip(m.params[1:], sig.params):
                if p.name in env:
                    self.error(f"duplicate parameter '{p.name}'", p)
                env[p.name] = pt
            self.current = sig
            self.return_types = []
            self.had_value_return = False
            for st in m.body:
                self.check_stmt(st, env)
            if sig.ret is None:
                if not self.return_types:
                    sig.ret = VOID
                else:
                    first, _ = self.return_types[0]
                    for t, node in self.return_types[1:]:
                        if t != first:
                            self.error(f"inconsistent return types in "
                                       f"'{sig.name}': {first} and {t}", node)
                    sig.ret = first
            elif sig.ret != VOID and not self.had_value_return:
                self.error(f"method '{sig.name}' declares -> {sig.ret} but "
                           f"never returns a value", m)
            m.sig = sig
            m.locals = dict(env)
            self.current = None
        cd.info = info
        cd.ty = ct

    def check_funcdef(self, fd):
        if fd.name in self.funcs:
            self.error(f"function '{fd.name}' is already defined", fd)
        if fd.name in BUILTIN_NAMES:
            self.error(f"'{fd.name}' is a builtin and cannot be redefined", fd)
        env = {}
        params = []
        for p in fd.params:
            if p.name in env:
                self.error(f"duplicate parameter '{p.name}'", p)
            t = self.resolve_type(p.ann)
            env[p.name] = t
            params.append((p.name, t))
        ret = self.resolve_type(fd.return_ann) if fd.return_ann else None
        sig = FuncSig(fd.name, params, ret, fd)
        self.funcs[fd.name] = sig

        self.current = sig
        self.return_types = []
        self.had_value_return = False
        outer_loop_depth, self.loop_depth = self.loop_depth, 0
        for s in fd.body:
            self.check_stmt(s, env)
        self.loop_depth = outer_loop_depth

        if sig.ret is None:
            if not self.return_types:
                sig.ret = VOID
            else:
                first, _ = self.return_types[0]
                for t, node in self.return_types[1:]:
                    if t != first:
                        self.error(f"inconsistent return types in '{fd.name}': "
                                   f"{first} and {t}", node)
                sig.ret = first
        elif sig.ret != VOID and not self.had_value_return:
            self.error(f"function '{fd.name}' declares -> {sig.ret} "
                       f"but never returns a value", fd)

        fd.sig = sig
        fd.locals = dict(env)
        self.current = None

    # -- statements -------------------------------------------------------

    def check_stmt(self, s, env):
        if isinstance(s, (A.Import, A.FromImport)):
            self.error("imports must be at the top level of the file, not "
                       "inside functions or blocks", s)
        if isinstance(s, A.ClassDef):
            self.error("class definitions must be at top level", s)
        if isinstance(s, A.FuncDef):
            self.error("function definitions must be at top level in v1", s)
        if isinstance(s, A.Try):
            self._check_try(s, env)
            return
        if isinstance(s, A.Raise):
            t = self.check_expr(s.value, env)
            if t != STR:
                self.error(f"raise needs a str message, not {t}", s)
            return
        elif isinstance(s, A.AnnAssign):
            self._check_ann_assign(s, env)
        elif isinstance(s, A.Assign):
            self._check_assign(s, env)
        elif isinstance(s, A.ExprStmt):
            self.check_expr(s.value, env)
        elif isinstance(s, A.If):
            self._check_cond(s.cond, env)
            for st in s.body:
                self.check_stmt(st, env)
            for st in s.orelse:
                self.check_stmt(st, env)
        elif isinstance(s, A.While):
            self._check_cond(s.cond, env)
            self.loop_depth += 1
            self.loop_kinds.append("n")
            for st in s.body:
                self.check_stmt(st, env)
            self.loop_kinds.pop()
            self.loop_depth -= 1
        elif isinstance(s, A.For):
            self._check_for(s, env)
        elif isinstance(s, A.Return):
            if self.in_parallel:
                self.error("cannot 'return' from inside a parallel for", s)
            self._check_return(s, env)
        elif isinstance(s, A.Break):
            if self.loop_depth == 0:
                self.error("'break' outside a loop", s)
            if self.loop_kinds and self.loop_kinds[-1] == "p":
                self.error("'break' cannot exit a parallel for (iterations "
                           "run concurrently); use 'continue' or restructure", s)
        elif isinstance(s, A.Continue):
            if self.loop_depth == 0:
                self.error("'continue' outside a loop", s)
        elif isinstance(s, A.Pass):
            pass
        else:
            self.error(f"unsupported statement {type(s).__name__}", s)

    def _declare(self, name, t, env, node):
        if name in self.funcs:
            self.error(f"'{name}' is already a function name", node)
        if name in self.func_aliases:
            self.error(f"'{name}' is an imported function and cannot be "
                       f"used as a variable name", node)
        if name in self.class_names:
            self.error(f"'{name}' is a class name and cannot be used as a "
                       f"variable name", node)
        if name in self.imported:
            self.error(f"'{name}' is an imported module and cannot be used "
                       f"as a variable name", node)
        if name in BUILTIN_NAMES:
            self.error(f"'{name}' is a builtin and cannot be used as a "
                       f"variable name", node)
        env[name] = t

    def _check_ann_assign(self, s, env):
        t = self.resolve_type(s.ann)
        vt = self.check_expr(s.value, env, expected=t)
        if vt != t:
            self.error(f"'{s.name}' is declared {t} but the value is {vt}", s)
        if s.name in env and env[s.name] != t:
            self.error(f"'{s.name}' is already {env[s.name]}; variables keep "
                       f"one type in pyalt", s)
        if s.name not in env:
            self._declare(s.name, t, env, s)

    def _check_assign(self, s, env):
        if isinstance(s.target, A.Name):
            name = s.target.id
            if name in env:
                expected = env[name]
                vt = self.check_expr(s.value, env, expected=expected)
                if vt != expected:
                    self.error(f"'{name}' is {expected} but you're assigning "
                               f"{vt} — variables keep one type in pyalt", s)
                s.target.ty = expected
            else:
                vt = self.check_expr(s.value, env)
                if vt == VOID:
                    self.error("cannot assign the result of a void function", s)
                self._declare(name, vt, env, s)
                s.target.ty = vt
        elif isinstance(s.target, A.Attribute):  # p.x = v
            recv = self.check_expr(s.target.value, env)
            if not isinstance(recv, ClassT):
                self.error(f"{recv} has no assignable fields", s)
            info = self.classes[recv.cname]
            if s.target.attr not in info.field_map:
                self.error(f"class {recv.name} has no field "
                           f"'{s.target.attr}'", s)
            ft = info.field_map[s.target.attr]
            vt = self.check_expr(s.value, env, expected=ft)
            if vt != ft:
                self.error(f"field '{s.target.attr}' is {ft} but you're "
                           f"assigning {vt}", s)
            s.target.ty = ft
        else:  # Index target: xs[i] = v  or  d[k] = v
            base = self.check_expr(s.target.value, env)
            if base == STR:
                self.error("strings are immutable; build a new string instead", s)
            if isinstance(base, DictT):
                kt = self.check_expr(s.target.index, env)
                if kt != base.key:
                    self.error(f"this dict has {base.key} keys, not {kt}",
                               s.target.index)
                vt = self.check_expr(s.value, env, expected=base.val)
                if vt != base.val:
                    self.error(f"this dict holds {base.val} values but you're "
                               f"assigning {vt}", s)
                s.target.ty = base.val
                return
            if not isinstance(base, ListT):
                self.error(f"cannot index-assign into {base}", s)
            it = self.check_expr(s.target.index, env)
            if it != INT:
                self.error(f"list index must be int, not {it}", s.target.index)
            vt = self.check_expr(s.value, env, expected=base.elem)
            if vt != base.elem:
                self.error(f"this list holds {base.elem} but you're assigning "
                           f"{vt}", s)
            s.target.ty = base.elem

    def _check_cond(self, cond, env):
        t = self.check_expr(cond, env)
        if t != BOOL:
            hint = ""
            if isinstance(t, ListT) or t == STR:
                hint = " — test emptiness explicitly: len(x) > 0"
            elif t in (INT, FLOAT):
                hint = " — compare explicitly: x != 0"
            self.error(f"condition must be bool, not {t}{hint}", cond)

    def _check_for(self, s, env):
        it = s.iterable
        is_range = (isinstance(it, A.Call) and isinstance(it.func, A.Name)
                    and it.func.id == "range" and "range" not in self.funcs)
        if is_range:
            if not 1 <= len(it.args) <= 3:
                self.error("range() takes 1 to 3 arguments", it)
            for a in it.args:
                at = self.check_expr(a, env)
                if at != INT:
                    self.error(f"range() arguments must be int, not {at}", a)
            it.ty = ListT(INT)
            s.is_range = True
            elem = INT
        else:
            t = self.check_expr(it, env)
            if isinstance(t, ListT):
                elem = t.elem
            elif isinstance(t, DictT):
                elem = t.key  # iterating a dict yields its keys, like Python
            elif isinstance(t, SetT):
                elem = t.elem
            elif t == STR:
                elem = STR
            else:
                self.error(f"cannot iterate over {t}", it)
        if s.var in env:
            if env[s.var] != elem:
                self.error(f"loop variable '{s.var}' is already {env[s.var]} "
                           f"but this loop yields {elem}", s)
        else:
            self._declare(s.var, elem, env, s)
        s.var_ty = elem
        parallel = getattr(s, "parallel", False)
        if parallel:
            if self.in_parallel:
                self.error("parallel for cannot be nested inside another "
                           "parallel for", s)
            outer = set(env.keys()) - {s.var}
            pre_names = set(env.keys())
            self.in_parallel = True
        self.loop_depth += 1
        self.loop_kinds.append("p" if parallel else "n")
        for st in s.body:
            self.check_stmt(st, env)
        self.loop_kinds.pop()
        self.loop_depth -= 1
        if parallel:
            self.in_parallel = False
            captures = set()
            for st in s.body:
                self._pscan_stmt(st, outer, s.var, captures, env)
            s.captures = sorted((n, env[n]) for n in captures)
            s.body_locals = sorted((n, env[n]) for n in env
                                   if n not in pre_names and n != s.var)

    def _check_try(self, s, env):
        if self.in_parallel:
            pass  # try/except inside parallel bodies is fine (thread-local)
        for st in s.body:
            self.check_stmt(st, env)
        if s.bind is not None:
            if s.bind in env and env[s.bind] != STR:
                self.error(f"'{s.bind}' is already {env[s.bind]}; the caught "
                           f"message is str", s)
            if s.bind not in env:
                self._declare(s.bind, STR, env, s)
        for st in s.handler:
            self.check_stmt(st, env)

    # -- parallel-for safety scan (runs after normal type checking) --------

    @staticmethod
    def _root_name(e):
        while isinstance(e, (A.Index, A.Slice, A.Attribute)):
            e = e.value
        return e.id if isinstance(e, A.Name) else None

    def _pscan_stmt(self, s, outer, loopvar, captures, env):
        if isinstance(s, (A.Assign, A.AnnAssign)):
            if isinstance(s, A.AnnAssign):
                if s.name in outer:
                    self.error(f"cannot assign to outer variable '{s.name}' "
                               f"inside parallel for — iterations race; write "
                               f"results into an output list instead", s)
            elif isinstance(s.target, A.Name):
                if s.target.id in outer:
                    self.error(f"cannot assign to outer variable "
                               f"'{s.target.id}' inside parallel for — "
                               f"iterations race; write results into an "
                               f"output list instead", s)
            elif isinstance(s.target, A.Attribute):
                root = self._root_name(s.target.value)
                if root in outer:
                    self.error("cannot assign fields of an outer instance "
                               "inside parallel for — iterations race", s)
                self._pscan_expr(s.target.value, outer, loopvar, captures)
            else:  # index assignment
                root = self._root_name(s.target.value)
                if root in outer and not isinstance(s.target.value.ty, ListT):
                    self.error("inside parallel for, only outer LISTS may be "
                               "written by index (dict/set writes race on the "
                               "hash table)", s)
            self._pscan_expr(s.value, outer, loopvar, captures)
            if isinstance(s, A.Assign) and isinstance(s.target, A.Index):
                self._pscan_expr(s.target.value, outer, loopvar, captures)
                self._pscan_expr(s.target.index, outer, loopvar, captures)
        elif isinstance(s, A.ExprStmt):
            self._pscan_expr(s.value, outer, loopvar, captures)
        elif isinstance(s, A.If):
            self._pscan_expr(s.cond, outer, loopvar, captures)
            for st in s.body:
                self._pscan_stmt(st, outer, loopvar, captures, env)
            for st in s.orelse:
                self._pscan_stmt(st, outer, loopvar, captures, env)
        elif isinstance(s, A.While):
            self._pscan_expr(s.cond, outer, loopvar, captures)
            for st in s.body:
                self._pscan_stmt(st, outer, loopvar, captures, env)
        elif isinstance(s, A.For):
            if s.var in outer:
                self.error(f"nested loop variable '{s.var}' is an outer "
                           f"variable — rename it inside parallel for", s)
            self._pscan_expr(s.iterable, outer, loopvar, captures)
            for st in s.body:
                self._pscan_stmt(st, outer, loopvar, captures, env)
        elif isinstance(s, A.Return):
            self._pscan_expr(s.value, outer, loopvar, captures)
        elif isinstance(s, A.Try):
            for st in s.body:
                self._pscan_stmt(st, outer, loopvar, captures, env)
            for st in s.handler:
                self._pscan_stmt(st, outer, loopvar, captures, env)
        elif isinstance(s, A.Raise):
            self._pscan_expr(s.value, outer, loopvar, captures)

    MUTATING_METHODS = {"append", "pop", "sort", "add", "remove"}

    def _pscan_expr(self, e, outer, loopvar, captures):
        if e is None:
            return
        if isinstance(e, A.Name):
            if e.id in outer:
                captures.add(e.id)
            return
        if isinstance(e, A.Call):
            if isinstance(e.func, A.Attribute):
                if (getattr(e, "module", None) is None
                        and e.func.attr in self.MUTATING_METHODS):
                    root = self._root_name(e.func.value)
                    if root in outer:
                        self.error(f"cannot call .{e.func.attr}() on outer "
                                   f"'{root}' inside parallel for — structural "
                                   f"mutation races; collect per-index results "
                                   f"instead", e)
                if getattr(e, "module", None) is None:
                    self._pscan_expr(e.func.value, outer, loopvar, captures)
            elif isinstance(e.func, A.Name):
                if e.func.id == "gc_collect":
                    self.error("gc_collect() cannot run inside parallel for", e)
                if e.func.id in self.MUTATING_METHODS and e.args:
                    root = self._root_name(e.args[0])
                    if root in outer:
                        self.error(f"cannot call {e.func.id}() on outer "
                                   f"'{root}' inside parallel for — structural "
                                   f"mutation races", e)
            for a in e.args:
                self._pscan_expr(a, outer, loopvar, captures)
            return
        for field in ("left", "right", "operand", "value", "index", "lo", "hi",
                      "cond"):
            self._pscan_expr(getattr(e, field, None), outer, loopvar, captures)
        for field in ("elts", "keys", "values", "parts"):
            for x in getattr(e, field, []) or []:
                if not isinstance(x, str):
                    self._pscan_expr(x, outer, loopvar, captures)

    def _check_return(self, s, env):
        if self.current is None:
            self.error("'return' outside a function", s)
        declared = self.current.ret
        if s.value is None:
            t = VOID
        else:
            expected = declared if declared not in (None, VOID) else None
            t = self.check_expr(s.value, env, expected=expected)
        if declared is not None:
            if t == VOID and declared != VOID:
                self.error(f"'{self.current.name}' declares -> {declared}; "
                           f"this return needs a value", s)
            if t != declared:
                self.error(f"'{self.current.name}' declares -> {declared} "
                           f"but this returns {t}", s)
            if t != VOID:
                self.had_value_return = True
        else:
            self.return_types.append((t, s))
            if t != VOID:
                self.had_value_return = True

    # -- expressions ------------------------------------------------------

    def check_expr(self, e, env, expected=None):
        t = self._expr(e, env, expected)
        e.ty = t
        return t

    def _expr(self, e, env, expected):
        if isinstance(e, A.IntLit):
            return INT
        if isinstance(e, A.FloatLit):
            return FLOAT
        if isinstance(e, A.StrLit):
            return STR
        if isinstance(e, A.BoolLit):
            return BOOL
        if isinstance(e, A.Name):
            return self._name(e, env)
        if isinstance(e, A.BinOp):
            return self._binop(e, env)
        if isinstance(e, A.UnaryOp):
            return self._unary(e, env)
        if isinstance(e, A.Compare):
            return self._compare(e, env)
        if isinstance(e, A.Call):
            return self._call(e, env, expected)
        if isinstance(e, A.Index):
            return self._index(e, env)
        if isinstance(e, A.Slice):
            return self._slice(e, env)
        if isinstance(e, A.ListLit):
            return self._list_lit(e, env, expected)
        if isinstance(e, A.DictLit):
            return self._dict_lit(e, env, expected)
        if isinstance(e, A.SetLit):
            return self._set_lit(e, env, expected)
        if isinstance(e, A.FString):
            return self._fstring(e, env)
        if isinstance(e, A.Attribute):
            recv = self.check_expr(e.value, env)
            if isinstance(recv, ClassT):
                info = self.classes[recv.cname]
                if e.attr in info.field_map:
                    return info.field_map[e.attr]
                if e.attr in info.methods:
                    self.error(f"'.{e.attr}' is a method of {recv.name} — "
                               f"call it: .{e.attr}(...)", e)
                self.error(f"class {recv.name} has no field '{e.attr}'", e)
            self.error(f"'.{e.attr}' is a method — call it: .{e.attr}(...)", e)
        self.error(f"unsupported expression {type(e).__name__}", e)

    def _name(self, e, env):
        if e.id in env:
            return env[e.id]
        if e.id in self.imported:
            self.error(f"'{e.id}' is a module — call one of its functions, "
                       f"e.g. {e.id}.some_function(...)", e)
        if e.id in self.func_aliases:
            self.error(f"'{e.id}' is an imported function — call it with "
                       f"(...)", e)
        if e.id in self.class_names:
            self.error(f"'{e.id}' is a class — construct an instance with "
                       f"{e.id}(...)", e)
        if self.current is not None and e.id in self.globals:
            self.error(f"global '{e.id}' is not accessible inside a function "
                       f"in v1 — pass it as a parameter", e)
        if e.id in self.funcs:
            self.error(f"'{e.id}' is a function — call it with (...)", e)
        self.error(f"'{e.id}' is not defined", e)

    def _binop(self, e, env):
        op = e.op
        if op in ("and", "or"):
            lt = self.check_expr(e.left, env)
            rt = self.check_expr(e.right, env)
            if lt != BOOL or rt != BOOL:
                self.error(f"'{op}' needs bool operands, got {lt} and {rt}", e)
            return BOOL
        lt = self.check_expr(e.left, env)
        rt = self.check_expr(e.right, env)

        def mismatch():
            hint = ""
            if STR in (lt, rt) and (lt in (INT, FLOAT) or rt in (INT, FLOAT)):
                hint = " — to build a string, convert with str(...) or use an f-string"
            elif {lt, rt} == {INT, FLOAT}:
                hint = " — no implicit int/float conversion; use float(...) or int(...)"
            self.error(f"'{op}' has mismatched operand types: {lt} and {rt}{hint}", e)

        if op == "+":
            if lt == rt and lt in (INT, FLOAT, STR):
                return lt
            if lt == rt and isinstance(lt, ListT):
                return lt
            mismatch()
        if op in ("-",):
            if lt == rt and lt in (INT, FLOAT):
                return lt
            mismatch()
        if op == "*":
            if lt == rt and lt in (INT, FLOAT):
                return lt
            if lt == STR and rt == INT:
                return STR
            mismatch()
        if op == "/":
            if lt == rt and lt in (INT, FLOAT):
                return FLOAT
            mismatch()
        if op == "//":
            if lt == rt and lt == INT:
                return INT
            if lt == rt and lt == FLOAT:
                self.error("'//' is integer division; for floats use '/'", e)
            mismatch()
        if op == "%":
            if lt == rt and lt in (INT, FLOAT):
                return lt
            mismatch()
        if op == "**":
            if lt == rt and lt in (INT, FLOAT):
                return lt
            mismatch()
        self.error(f"unknown operator '{op}'", e)

    def _unary(self, e, env):
        t = self.check_expr(e.operand, env)
        if e.op == "-":
            if t in (INT, FLOAT):
                return t
            self.error(f"unary '-' needs int or float, not {t}", e)
        if e.op == "not":
            if t == BOOL:
                return BOOL
            self.error(f"'not' needs bool, not {t}", e)
        self.error(f"unknown unary operator '{e.op}'", e)

    def _compare(self, e, env):
        op = e.op
        lt = self.check_expr(e.left, env)
        rt = self.check_expr(e.right, env)
        if op in ("in", "not in"):
            if isinstance(rt, ListT):
                if rt.elem not in PRIMS:
                    self.error("membership tests need a list of int, float, "
                               "bool or str", e)
                if lt != rt.elem:
                    self.error(f"'{op}': looking for {lt} in {rt}", e)
                return BOOL
            if isinstance(rt, DictT):
                if lt != rt.key:
                    self.error(f"'{op}': this dict has {rt.key} keys, "
                               f"not {lt}", e)
                return BOOL
            if isinstance(rt, SetT):
                if lt != rt.elem:
                    self.error(f"'{op}': looking for {lt} in {rt}", e)
                return BOOL
            if rt == STR:
                if lt != STR:
                    self.error(f"'{op}' on a string needs a str, not {lt}", e)
                return BOOL
            self.error(f"'{op}' needs a list, dict, set or str on the right, "
                       f"not {rt}", e)
        if op in ("==", "!="):
            if lt == rt and lt in PRIMS:
                return BOOL
            if isinstance(lt, (ListT, DictT, SetT)) or isinstance(rt, (ListT, DictT, SetT)):
                self.error("container equality is not supported yet; compare "
                           "elements in a loop", e)
            self.error(f"'{op}' has mismatched operand types: {lt} and {rt}", e)
        # < <= > >=
        if lt == rt and lt in (INT, FLOAT, STR):
            return BOOL
        if {lt, rt} == {INT, FLOAT}:
            self.error(f"'{op}' has mismatched operand types: {lt} and {rt} "
                       f"— no implicit int/float conversion", e)
        self.error(f"'{op}' cannot compare {lt} and {rt}", e)

    def _index(self, e, env):
        base = self.check_expr(e.value, env)
        if isinstance(base, DictT):
            it = self.check_expr(e.index, env)
            if it != base.key:
                self.error(f"this dict has {base.key} keys, not {it}", e.index)
            return base.val
        it = self.check_expr(e.index, env)
        if it != INT:
            self.error(f"index must be int, not {it}", e.index)
        if isinstance(base, ListT):
            return base.elem
        if base == STR:
            return STR
        self.error(f"cannot index into {base}", e)

    def _slice(self, e, env):
        base = self.check_expr(e.value, env)
        for bound in (e.lo, e.hi):
            if bound is not None:
                bt = self.check_expr(bound, env)
                if bt != INT:
                    self.error(f"slice bounds must be int, not {bt}", bound)
        if isinstance(base, ListT) or base == STR:
            return base
        self.error(f"cannot slice {base}", e)

    def _list_lit(self, e, env, expected):
        if not e.elts:
            if isinstance(expected, ListT):
                return expected
            self.error("cannot infer the element type of []; annotate it, "
                       "e.g. xs: list[int] = []", e)
        elem_expected = expected.elem if isinstance(expected, ListT) else None
        t0 = self.check_expr(e.elts[0], env, expected=elem_expected)
        for x in e.elts[1:]:
            t = self.check_expr(x, env, expected=elem_expected)
            if t != t0:
                self.error(f"list elements must all be the same type; "
                           f"found {t0} and {t}", x)
        return ListT(t0)

    def _dict_lit(self, e, env, expected):
        if not e.keys:
            if isinstance(expected, DictT):
                return expected
            self.error("cannot infer the types of {}; annotate it, "
                       "e.g. d: dict[str, int] = {}", e)
        kexp = expected.key if isinstance(expected, DictT) else None
        vexp = expected.val if isinstance(expected, DictT) else None
        kt0 = self.check_expr(e.keys[0], env, expected=kexp)
        if kt0 not in PRIMS:
            self.error("dict keys must be int, float, bool or str", e.keys[0])
        vt0 = self.check_expr(e.values[0], env, expected=vexp)
        for k, v in zip(e.keys[1:], e.values[1:]):
            kt = self.check_expr(k, env, expected=kexp)
            if kt != kt0:
                self.error(f"dict keys must all be the same type; "
                           f"found {kt0} and {kt}", k)
            vt = self.check_expr(v, env, expected=vexp)
            if vt != vt0:
                self.error(f"dict values must all be the same type; "
                           f"found {vt0} and {vt}", v)
        return DictT(kt0, vt0)

    def _set_lit(self, e, env, expected):
        eexp = expected.elem if isinstance(expected, SetT) else None
        t0 = self.check_expr(e.elts[0], env, expected=eexp)
        if t0 not in PRIMS:
            self.error("set elements must be int, float, bool or str", e.elts[0])
        for x in e.elts[1:]:
            t = self.check_expr(x, env, expected=eexp)
            if t != t0:
                self.error(f"set elements must all be the same type; "
                           f"found {t0} and {t}", x)
        return SetT(t0)

    def _fstring(self, e, env):
        for part in e.parts:
            if isinstance(part, A.StrLit):
                part.ty = STR
                continue
            t = self.check_expr(part, env)
            if t not in PRIMS:
                self.error(f"cannot format {t} in an f-string; v1 formats "
                           f"int, float, bool and str", part)
        return STR

    # -- calls ------------------------------------------------------------

    def _call(self, e, env, expected=None):
        if isinstance(e.func, A.Attribute):
            base = e.func.value
            if (isinstance(base, A.Name) and base.id in self.imported
                    and base.id not in env):
                return self._module_call(e, base.id, env)
            return self._method_call(e, env)
        if not isinstance(e.func, A.Name):
            self.error("only named functions can be called in v1", e)
        name = e.func.id
        if name == "range":
            self.error("range(...) can only be used as the sequence of a "
                       "for loop", e)
        if name == "set":
            self._arity(name, e, 0)
            if isinstance(expected, SetT):
                return expected
            self.error("set() needs an annotation to know its element type, "
                       "e.g. s: set[str] = set()", e)
        if name == "dict":
            self.error("use a {} literal for dicts, e.g. "
                       "d: dict[str, int] = {}", e)
        if name in self.class_names:  # constructor: Point(1.0, 2.0)
            ct = self.class_names[name]
            info = self.classes[ct.cname]
            self._check_args(name, info.fields, e, env)
            e.ctor = ct
            return ct
        if name in self.func_aliases:  # from mod import name
            modname, sig = self.func_aliases[name]
            self._check_args(name, sig.params, e, env)
            e.module = modname
            e.mfunc = name
            return sig.ret
        if name in BUILTIN_NAMES:
            return self._builtin_call(name, e, env)
        if name in self.funcs:
            sig = self.funcs[name]
            if sig.ret is None:
                self.error(f"recursive call to '{name}' needs an explicit "
                           f"return type annotation on its def line", e)
            self._check_args(name, sig.params, e, env)
            return sig.ret
        if name in env or (self.current is not None and name in self.globals):
            self.error(f"'{name}' is a variable, not a function", e)
        self.error(f"function '{name}' is not defined (in pyalt v1, define "
                   f"functions before they are used)", e)

    def _module_call(self, e, modname, env):
        funcs = self.imported[modname]
        fname = e.func.attr
        if fname not in funcs:
            available = ", ".join(sorted(funcs)) or "none"
            self.error(f"module '{modname}' has no function '{fname}' "
                       f"(available: {available})", e)
        sig = funcs[fname]
        self._check_args(f"{modname}.{fname}", sig.params, e, env)
        e.module = modname  # consumed by the C emitter
        e.mfunc = fname
        return sig.ret

    def _check_args(self, name, params, e, env):
        if len(e.args) != len(params):
            self.error(f"'{name}' takes {len(params)} argument(s) but got "
                       f"{len(e.args)}", e)
        for i, (arg, (pn, pt)) in enumerate(zip(e.args, params)):
            at = self.check_expr(arg, env, expected=pt)
            if at != pt:
                self.error(f"argument {i + 1} to '{name}' ('{pn}') should be "
                           f"{pt} but is {at}", arg)

    def _arity(self, name, e, n):
        if len(e.args) != n:
            self.error(f"'{name}' takes {n} argument(s) but got {len(e.args)}", e)

    def _builtin_call(self, name, e, env):
        args = e.args
        if name == "print":
            for a in args:
                t = self.check_expr(a, env)
                if t == VOID:
                    self.error("cannot print the result of a void function", a)
            return VOID
        if name == "len":
            self._arity(name, e, 1)
            t = self.check_expr(args[0], env)
            if isinstance(t, (ListT, DictT, SetT)) or t == STR:
                return INT
            self.error(f"len() needs a str, list, dict or set, not {t}", e)
        if name in ("int", "float", "str", "bool"):
            self._arity(name, e, 1)
            t = self.check_expr(args[0], env)
            if t not in PRIMS:
                self.error(f"{name}() cannot convert {t}", e)
            return {"int": INT, "float": FLOAT, "str": STR, "bool": BOOL}[name]
        if name == "abs":
            self._arity(name, e, 1)
            t = self.check_expr(args[0], env)
            if t in (INT, FLOAT):
                return t
            self.error(f"abs() needs int or float, not {t}", e)
        if name in ("min", "max"):
            self._arity(name, e, 2)
            t1 = self.check_expr(args[0], env)
            t2 = self.check_expr(args[1], env)
            if t1 == t2 and t1 in (INT, FLOAT):
                return t1
            self.error(f"{name}() needs two ints or two floats, "
                       f"got {t1} and {t2}", e)
        if name == "append":
            self._arity(name, e, 2)
            t = self.check_expr(args[0], env)
            if not isinstance(t, ListT):
                self.error(f"append() needs a list first, not {t}", e)
            vt = self.check_expr(args[1], env, expected=t.elem)
            if vt != t.elem:
                self.error(f"appending {vt} to {t}", args[1])
            return VOID
        if name == "pop":
            self._arity(name, e, 1)
            t = self.check_expr(args[0], env)
            if not isinstance(t, ListT):
                self.error(f"pop() needs a list, not {t}", e)
            return t.elem
        if name == "sort":
            self._arity(name, e, 1)
            t = self.check_expr(args[0], env)
            if not isinstance(t, ListT) or t.elem not in (INT, FLOAT, STR):
                self.error(f"sort() needs a list of int, float or str, "
                           f"not {t}", e)
            return VOID
        if name == "read_file":
            self._check_args(name, [("path", STR)], e, env)
            return STR
        if name == "read_lines":
            self._check_args(name, [("path", STR)], e, env)
            return ListT(STR)
        if name == "write_file":
            self._check_args(name, [("path", STR), ("text", STR)], e, env)
            return VOID
        if name == "clock":
            self._arity(name, e, 0)
            return FLOAT
        if name == "gc_collect":
            self._arity(name, e, 0)
            return INT  # live heap bytes after the collection
        if name == "args":
            self._arity(name, e, 0)
            return ListT(STR)  # command-line arguments, exe name excluded
        if name == "input":
            self._check_args(name, [("prompt", STR)], e, env)
            return STR
        if name == "exists":
            self._check_args(name, [("path", STR)], e, env)
            return BOOL
        if name == "exit":
            self._check_args(name, [("code", INT)], e, env)
            return VOID
        self.error(f"builtin '{name}' cannot be called here", e)

    def _method_call(self, e, env):
        attr = e.func.attr
        recv = self.check_expr(e.func.value, env)
        if recv == STR:
            if attr not in STR_METHODS:
                self.error(f"str has no method '.{attr}()'", e)
            param_types, ret = STR_METHODS[attr]
            params = [(f"arg{i + 1}", t) for i, t in enumerate(param_types)]
            self._check_args(f"str.{attr}", params, e, env)
            return ret
        if isinstance(recv, ListT):
            if attr == "append":
                params = [("value", recv.elem)]
                self._check_args(f"list.append", params, e, env)
                return VOID
            if attr == "pop":
                self._arity("list.pop", e, 0)
                return recv.elem
            if attr == "sort":
                self._arity("list.sort", e, 0)
                if recv.elem not in (INT, FLOAT, STR):
                    self.error(f"cannot sort {recv}", e)
                return VOID
            self.error(f"{recv} has no method '.{attr}()'", e)
        if isinstance(recv, DictT):
            if attr == "get":
                params = [("key", recv.key), ("default", recv.val)]
                self._check_args("dict.get", params, e, env)
                return recv.val
            if attr == "pop":
                params = [("key", recv.key)]
                self._check_args("dict.pop", params, e, env)
                return recv.val
            if attr == "keys":
                self._arity("dict.keys", e, 0)
                return ListT(recv.key)
            if attr == "values":
                self._arity("dict.values", e, 0)
                return ListT(recv.val)
            self.error(f"{recv} has no method '.{attr}()' "
                       f"(available: get, pop, keys, values)", e)
        if isinstance(recv, SetT):
            if attr == "add":
                params = [("value", recv.elem)]
                self._check_args("set.add", params, e, env)
                return VOID
            if attr == "remove":
                params = [("value", recv.elem)]
                self._check_args("set.remove", params, e, env)
                return VOID
            self.error(f"{recv} has no method '.{attr}()' "
                       f"(available: add, remove)", e)
        if isinstance(recv, ClassT):
            info = self.classes[recv.cname]
            if attr in info.methods:
                sig = info.methods[attr]
                if sig.ret is None:
                    self.error(f"call to '{recv.name}.{attr}' before its "
                               f"return type is known — annotate its return "
                               f"type", e)
                self._check_args(f"{recv.name}.{attr}", sig.params, e, env)
                e.class_method = (recv.cname, attr)
                return sig.ret
            if attr in info.field_map:
                self.error(f"'.{attr}' is a field of {recv.name}, not a "
                           f"method", e)
            self.error(f"class {recv.name} has no method '.{attr}()'", e)
        self.error(f"{recv} has no method '.{attr}()'", e)
