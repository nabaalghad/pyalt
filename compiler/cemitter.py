"""pyalt C emitter: typed AST (from the typechecker) -> a C source file.

Mapping:
  int -> int64_t, float -> double, bool -> bool, str -> PStr*, list[T] -> PList*
  user function f -> fn_f, variable x -> p_x, string literals -> pooled globals S0..
  top-level code -> main()

Relies on the typechecker having set .ty on every expression, .sig/.locals on
every FuncDef, and .is_range on for-over-range statements.
"""

from . import ast_nodes as A
from .errors import CompileError
from .types import INT, FLOAT, BOOL, STR, VOID, ListT, DictT, SetT, ClassT


def ctype(t):
    if t == INT:
        return "int64_t"
    if t == FLOAT:
        return "double"
    if t == BOOL:
        return "bool"
    if t == STR:
        return "PStr*"
    if isinstance(t, ListT):
        return "PList*"
    if isinstance(t, (DictT, SetT)):
        return "PDict*"
    if isinstance(t, ClassT):
        return f"{t.cname}*"
    if t == VOID:
        return "void"
    raise AssertionError(f"no C type for {t}")


def contains_try(stmts):
    for s in stmts:
        if isinstance(s, A.Try):
            return True
        for field in ("body", "orelse", "handler"):
            if contains_try(getattr(s, field, []) or []):
                return True
    return False


def key_kind(t):
    """Runtime hash-kind code: 0 = int/bool, 1 = float, 2 = str."""
    if t == FLOAT:
        return 1
    if t == STR:
        return 2
    return 0


def print_kind(t):
    """Runtime print-kind code: 0 = int, 1 = float, 2 = bool, 3 = str."""
    return {INT: 0, FLOAT: 1, BOOL: 2, STR: 3}[t]


def default_init(t):
    if t == INT:
        return "0"
    if t == FLOAT:
        return "0.0"
    if t == BOOL:
        return "false"
    if t == STR:
        return "pstr_empty()"
    if isinstance(t, ListT):
        return "plist_new(0)"
    if isinstance(t, DictT):
        return f"pdict_new({key_kind(t.key)})"
    if isinstance(t, SetT):
        return f"pdict_new({key_kind(t.elem)})"
    if isinstance(t, ClassT):
        return "NULL"
    raise AssertionError(f"no default for {t}")


def c_string_literal(text):
    """Encode a Python str as a C string literal + its UTF-8 byte length."""
    data = text.encode("utf-8")
    out = []
    for b in data:
        if b == 0x22:
            out.append('\\"')
        elif b == 0x5C:
            out.append("\\\\")
        elif b == 0x0A:
            out.append("\\n")
        elif b == 0x0D:
            out.append("\\r")
        elif b == 0x09:
            out.append("\\t")
        elif 0x20 <= b <= 0x7E:
            out.append(chr(b))
        else:
            out.append(f"\\{b:03o}")
    return '"' + "".join(out) + '"', len(data)


class CEmitter:
    def __init__(self, module, tc, filename="<input>", source=None, deps=None):
        self.module = module
        self.tc = tc
        self.filename = filename
        self.source = source
        self.deps = deps or []  # [(modname, module_ast, tc)] dependency-first
        self.qual = ""          # C-name prefix while emitting a dep module
        self.fwd = []           # forward declarations, collected during emit
        self.lits = {}          # text -> global name S0, S1, ...
        self.ntmp = 0
        self.aux = []           # parallel-for structs + worker functions
        self.par_count = 0
        self.name_map = {}      # var name -> C lvalue override (worker bodies)
        self.struct_predecls = []  # class typedef forward declarations
        self.struct_defs = []      # class struct bodies + constructors
        self.try_count = 0
        self.try_stack = []     # frame var names of open try blocks
        self.loop_try_marks = []  # try_stack depth at each loop entry
        self.volatile = False   # declare locals volatile (fn contains try)

    def error(self, message, node):
        raise CompileError(self.filename, getattr(node, "line", 0),
                           getattr(node, "col", 0), message, self.source)

    def tmp(self, stem="t"):
        self.ntmp += 1
        return f"py_{stem}{self.ntmp}"

    def lit(self, text):
        if text not in self.lits:
            self.lits[text] = f"S{len(self.lits)}"
        return self.lits[text]

    # -- top level --------------------------------------------------------

    def _emit_module_defs(self, body, lines):
        """Emit one module's classes and functions into `lines`."""
        for s in body:
            if isinstance(s, A.ClassDef):
                self.emit_class(s, lines)
        for s in body:
            if isinstance(s, A.FuncDef):
                self.fwd.append(self.func_sig(s) + ";")
                lines.extend(self.emit_func(s))
                lines.append("")

    def _emit_deps(self):
        """Emit all imported modules' definitions with qualified C names."""
        lines = []
        for modname, mast, mtc in self.deps:
            self.qual = modname + "__"
            prev_tc, self.tc = self.tc, mtc
            self._emit_module_defs(mast.body, lines)
            self.tc = prev_tc
        self.qual = ""
        return lines

    TOP_DEFS = (A.FuncDef, A.Import, A.FromImport, A.ClassDef)

    def emit(self):
        func_lines = self._emit_deps()
        self._emit_module_defs(self.module.body, func_lines)
        top = [s for s in self.module.body
               if not isinstance(s, self.TOP_DEFS)]

        self.volatile = contains_try(top)
        vol = "volatile " if self.volatile else ""
        main_lines = ["int main(int argc, char **argv) {",
                      "    pya_init_args(argc, argv);",
                      "    pya_init_literals();"]
        for name, t in self.tc.globals.items():
            main_lines.append(f"    {ctype(t)} {vol}p_{name} = "
                              f"{default_init(t)};")
            main_lines.append(f"    (void)p_{name};")
        body = []
        for s in top:
            self.emit_stmt(s, body, 1)
        self.volatile = False
        main_lines.extend(body)
        main_lines.append("    return 0;")
        main_lines.append("}")
        return self._assemble(func_lines, main_lines)

    def emit_library(self):
        """Emit definitions only (no main) for embedding, e.g. in a Python
        extension. The embedder must call pya_init_literals() once."""
        for s in self.module.body:
            if not isinstance(s, self.TOP_DEFS):
                self.error("a Python-importable module may only contain "
                           "function and class definitions (top-level "
                           "statements would not run on import)", s)
        func_lines = self._emit_deps()
        self._emit_module_defs(self.module.body, func_lines)
        return self._assemble(func_lines, None)

    def emit_class(self, cd, lines):
        info = cd.info
        cn = info.cname
        self.struct_predecls.append(f"typedef struct {cn} {cn};")
        body = [f"struct {cn} {{"]
        if info.fields:
            for fname, ft in info.fields:
                body.append(f"    {ctype(ft)} f_{fname};")
        else:
            body.append("    char f__empty;")
        body.append("};")
        params = ", ".join(f"{ctype(ft)} a_{fn}" for fn, ft in info.fields)
        body.append(f"static {cn}* new_{cn}({params or 'void'}) {{")
        body.append(f"    {cn}* o = ({cn}*)pya_alloc(sizeof({cn}));")
        for fname, _ in info.fields:
            body.append(f"    o->f_{fname} = a_{fname};")
        body.append("    return o;")
        body.append("}")
        self.struct_defs.extend(body)
        self.struct_defs.append("")
        for m in cd.methods:
            sig_line = self.method_sig(cn, m)
            self.fwd.append(sig_line + ";")
            lines.extend(self.emit_func(m, method_of=cn))
            lines.append("")

    def method_sig(self, cn, m):
        params = [f"{cn}* p_self"]
        params += [f"{ctype(t)} p_{n}" for n, t in m.sig.params]
        return (f"static {ctype(m.sig.ret)} fn_{cn}__{m.name}"
                f"({', '.join(params)})")

    def _assemble(self, func_lines, main_lines):
        # assembled last: literal pool is complete only after emitting all code
        out = ["/* generated by pyalt v1 — do not edit */",
               '#include "pyalt.h"', ""]
        for text, name in self.lits.items():
            out.append(f"static PStr* {name};")
        out.append("")
        out.append("static void pya_init_literals(void) {")
        for text, name in self.lits.items():
            lit, blen = c_string_literal(text)
            out.append(f"    {name} = pstr_new({lit}, {blen});")
        out.append("    pya_gc_seal(); /* literals above are immortal */")
        out.append("}")
        out.append("")
        out.extend(self.struct_predecls)
        out.append("")
        out.extend(self.struct_defs)
        out.extend(self.fwd)
        out.append("")
        out.extend(self.aux)
        out.extend(func_lines)
        if main_lines is not None:
            out.extend(main_lines)
        return "\n".join(out) + "\n"

    def func_sig(self, fd):
        params = ", ".join(f"{ctype(t)} p_{n}" for n, t in fd.sig.params)
        return (f"static {ctype(fd.sig.ret)} fn_{self.qual}{fd.name}"
                f"({params or 'void'})")

    def emit_func(self, fd, method_of=None):
        has_try = contains_try(fd.body)
        if method_of:
            sig = self.method_sig(method_of, fd)
            param_names = {"self"} | {n for n, _ in fd.sig.params}
        else:
            sig = self.func_sig(fd)
            param_names = {n for n, _ in fd.sig.params}
        if has_try:
            # setjmp rule: locals modified after setjmp and read after longjmp
            # must be volatile — shadow the params, volatile everything
            sig_shadow = sig
            for n in sorted(param_names, key=len, reverse=True):
                sig_shadow = sig_shadow.replace(f" p_{n}", f" a_{n}")
            lines = [sig_shadow + " {"]
            all_params = ([("self", None)] if method_of else []) + list(fd.sig.params)
            for n, t in all_params:
                cty = f"{method_of}*" if n == "self" else ctype(t)
                lines.append(f"    {cty} volatile p_{n} = a_{n};")
        else:
            lines = [sig + " {"]
        vol = "volatile " if has_try else ""
        for name, t in fd.locals.items():
            if name in param_names:
                continue
            lines.append(f"    {ctype(t)} {vol}p_{name} = {default_init(t)};")
            lines.append(f"    (void)p_{name};")
        prev_vol, self.volatile = self.volatile, has_try
        for s in fd.body:
            self.emit_stmt(s, lines, 1)
        self.volatile = prev_vol
        if fd.sig.ret != VOID:
            lines.append(f'    pya_die("function \'{fd.name}\' reached its end '
                         f'without returning a value");')
        lines.append("}")
        return lines

    # -- statements -------------------------------------------------------

    def emit_stmt(self, s, out, ind):
        pad = "    " * ind
        if isinstance(s, (A.Assign, A.AnnAssign)):
            self._emit_assign(s, out, pad)
        elif isinstance(s, A.ExprStmt):
            self._emit_expr_stmt(s, out, pad)
        elif isinstance(s, A.If):
            out.append(f"{pad}if ({self.ex(s.cond)}) {{")
            for st in s.body:
                self.emit_stmt(st, out, ind + 1)
            if s.orelse:
                out.append(f"{pad}}} else {{")
                for st in s.orelse:
                    self.emit_stmt(st, out, ind + 1)
            out.append(f"{pad}}}")
        elif isinstance(s, A.While):
            self.loop_try_marks.append(len(self.try_stack))
            out.append(f"{pad}while ({self.ex(s.cond)}) {{")
            for st in s.body:
                self.emit_stmt(st, out, ind + 1)
            out.append(f"{pad}}}")
            self.loop_try_marks.pop()
        elif isinstance(s, A.For):
            self.loop_try_marks.append(len(self.try_stack))
            self._emit_for(s, out, ind)
            self.loop_try_marks.pop()
        elif isinstance(s, A.Try):
            self._emit_try(s, out, ind)
        elif isinstance(s, A.Raise):
            out.append(f"{pad}pya_raise({self.ex(s.value)});")
        elif isinstance(s, A.Return):
            if self.try_stack:  # unwind this function's try frames first
                out.append(f"{pad}pya_try_top = {self.try_stack[0]}.prev;")
            if s.value is None:
                out.append(f"{pad}return;")
            else:
                out.append(f"{pad}return {self.ex(s.value)};")
        elif isinstance(s, A.Break):
            self._emit_loop_escape(out, pad)
            out.append(f"{pad}break;")
        elif isinstance(s, A.Continue):
            self._emit_loop_escape(out, pad)
            out.append(f"{pad}continue;")
        elif isinstance(s, A.Pass):
            out.append(f"{pad};")
        else:
            self.error(f"cannot emit statement {type(s).__name__}", s)

    def _emit_loop_escape(self, out, pad):
        """break/continue may jump out of try blocks opened inside the loop —
        pop the frame stack back to the loop-entry state."""
        mark = self.loop_try_marks[-1] if self.loop_try_marks else 0
        if len(self.try_stack) > mark:
            out.append(f"{pad}pya_try_top = {self.try_stack[mark]}.prev;")

    def _emit_try(self, s, out, ind):
        pad = "    " * ind
        self.try_count += 1
        tf = f"py_tf{self.try_count}"
        out.append(f"{pad}{{")
        out.append(f"{pad}    PyaTryFrame {tf};")
        out.append(f"{pad}    {tf}.prev = pya_try_top;")
        out.append(f"{pad}    pya_try_top = &{tf};")
        out.append(f"{pad}    if (setjmp({tf}.jb) == 0) {{")
        self.try_stack.append(tf)
        for st in s.body:
            self.emit_stmt(st, out, ind + 2)
        self.try_stack.pop()
        out.append(f"{pad}        pya_try_top = {tf}.prev;")
        out.append(f"{pad}    }} else {{")
        out.append(f"{pad}        pya_try_top = {tf}.prev;")
        if s.bind is not None:
            out.append(f"{pad}        p_{s.bind} = pstr_from_c(pya_exc_buf);")
        for st in s.handler:
            self.emit_stmt(st, out, ind + 2)
        out.append(f"{pad}    }}")
        out.append(f"{pad}}}")

    def _emit_assign(self, s, out, pad):
        if isinstance(s, A.AnnAssign):
            out.append(f"{pad}p_{s.name} = {self.ex(s.value)};")
        elif isinstance(s.target, A.Name):
            lval = self.name_map.get(s.target.id, f"p_{s.target.id}")
            out.append(f"{pad}{lval} = {self.ex(s.value)};")
        elif isinstance(s.target, A.Attribute):  # p.x = v
            recv_t = s.target.value.ty
            recv = self.ex(s.target.value)
            out.append(f"{pad}(({recv_t.cname}*)pya_fld({recv}))"
                       f"->f_{s.target.attr} = {self.ex(s.value)};")
        elif isinstance(s.target.value.ty, DictT):  # d[k] = v
            base_t = s.target.value.ty
            base = self.ex(s.target.value)
            key = self.pval(base_t.key, self.ex(s.target.index))
            val = self.pval(s.value.ty, self.ex(s.value))
            out.append(f"{pad}pdict_set({base}, {key}, {val});")
        else:  # list Index target
            base = self.ex(s.target.value)
            idx = self.ex(s.target.index)
            val = self.pval(s.value.ty, self.ex(s.value))
            out.append(f"{pad}plist_set({base}, {idx}, {val});")

    def _emit_expr_stmt(self, s, out, pad):
        call = s.value
        if (isinstance(call, A.Call) and isinstance(call.func, A.Name)
                and call.func.id == "print"):
            parts = []
            for i, a in enumerate(call.args):
                if i:
                    parts.append("pya_print_sp();")
                parts.append(self.print_one(a))
            parts.append("pya_print_nl();")
            out.append(pad + " ".join(parts))
            return
        c = self.ex(s.value)
        if s.value.ty == VOID:
            out.append(f"{pad}{c};")
        else:
            out.append(f"{pad}(void)({c});")

    def print_one(self, a):
        t = a.ty
        c = self.ex(a)
        if t == INT:
            return f"pya_print_i({c});"
        if t == FLOAT:
            return f"pya_print_f({c});"
        if t == BOOL:
            return f"pya_print_b({c});"
        if t == STR:
            return f"pya_print_s({c});"
        if isinstance(t, ListT):
            suffix = {INT: "i", FLOAT: "f", BOOL: "b", STR: "s"}.get(t.elem)
            if suffix is None:
                self.error("printing nested lists is not supported in v1", a)
            return f"pya_print_list_{suffix}({c});"
        if isinstance(t, DictT):
            if t.val not in (INT, FLOAT, BOOL, STR):
                self.error("printing dicts with container values is not "
                           "supported yet", a)
            return f"pya_print_dict({c}, {print_kind(t.key)}, {print_kind(t.val)});"
        if isinstance(t, SetT):
            return f"pya_print_set({c}, {print_kind(t.elem)});"
        if isinstance(t, ClassT):
            self.error(f"cannot print a {t.name} instance directly — print "
                       f"its fields (e.g. an f-string)", a)
        self.error(f"cannot print {t}", a)

    def _emit_for(self, s, out, ind):
        if getattr(s, "parallel", False):
            self._emit_parallel_for(s, out, ind)
            return
        pad = "    " * ind
        var = f"p_{s.var}"
        if getattr(s, "is_range", False):
            args = s.iterable.args
            start_t, stop_t, step_t = self.tmp("start"), self.tmp("stop"), self.tmp("step")
            if len(args) == 1:
                out.append(f"{pad}int64_t {start_t} = 0;")
                out.append(f"{pad}int64_t {stop_t} = {self.ex(args[0])};")
                out.append(f"{pad}for ({var} = {start_t}; {var} < {stop_t}; {var}++) {{")
            elif len(args) == 2:
                out.append(f"{pad}int64_t {start_t} = {self.ex(args[0])};")
                out.append(f"{pad}int64_t {stop_t} = {self.ex(args[1])};")
                out.append(f"{pad}for ({var} = {start_t}; {var} < {stop_t}; {var}++) {{")
            else:
                out.append(f"{pad}int64_t {start_t} = {self.ex(args[0])};")
                out.append(f"{pad}int64_t {stop_t} = {self.ex(args[1])};")
                out.append(f"{pad}int64_t {step_t} = {self.ex(args[2])};")
                out.append(f'{pad}if ({step_t} == 0) pya_die("range() step cannot be 0");')
                out.append(f"{pad}for ({var} = {start_t}; "
                           f"({step_t} > 0) ? ({var} < {stop_t}) : ({var} > {stop_t}); "
                           f"{var} += {step_t}) {{")
            for st in s.body:
                self.emit_stmt(st, out, ind + 1)
            out.append(f"{pad}}}")
            return
        it_t = s.iterable.ty
        seq, i = self.tmp("it"), self.tmp("i")
        out.append(f"{pad}{{")
        if it_t == STR:
            out.append(f"{pad}    PStr* {seq} = {self.ex(s.iterable)};")
            out.append(f"{pad}    for (int64_t {i} = 0; {i} < {seq}->len; {i}++) {{")
            out.append(f"{pad}        {var} = pstr_index({seq}, {i});")
        elif isinstance(it_t, (DictT, SetT)):
            elem = it_t.key if isinstance(it_t, DictT) else it_t.elem
            out.append(f"{pad}    PDict* {seq} = {self.ex(s.iterable)};")
            out.append(f"{pad}    for (int64_t {i} = 0; {i} < {seq}->nentries; "
                       f"{i}++) {{")
            out.append(f"{pad}        if ({seq}->entries[{i}].dead) continue;")
            out.append(f"{pad}        {var} = "
                       f"{self.pval_get(f'{seq}->entries[{i}].key', elem)};")
        else:
            out.append(f"{pad}    PList* {seq} = {self.ex(s.iterable)};")
            out.append(f"{pad}    for (int64_t {i} = 0; {i} < {seq}->len; {i}++) {{")
            out.append(f"{pad}        {var} = "
                       f"{self.pval_get(f'plist_get({seq}, {i})', it_t.elem)};")
        for st in s.body:
            self.emit_stmt(st, out, ind + 2)
        out.append(f"{pad}    }}")
        out.append(f"{pad}}}")

    def _emit_parallel_for(self, s, out, ind):
        """parallel for: extract the body into a worker function; chunk the
        iteration space across pya_nthreads() threads; captured outer
        variables travel in a per-worker context struct."""
        pad = "    " * ind
        self.par_count += 1
        pid = self.par_count
        struct = f"Par{pid}"
        worker = f"par_worker{pid}"
        caps = getattr(s, "captures", [])
        var_t = s.var_ty
        it_t = s.iterable.ty
        is_range = getattr(s, "is_range", False)

        # ---- context struct ----
        fields = [f"    int64_t i0, i1;"]
        if is_range:
            fields.append("    int64_t rstart, rstep;")
        else:
            fields.append(f"    {ctype(it_t)} seq;")
        for name, t in caps:
            fields.append(f"    {ctype(t)} p_{name};")
        aux = [f"typedef struct {{"] + fields + [f"}} {struct};", ""]

        # ---- worker function ----
        aux.append(f"static void {worker}(void* __p) {{")
        aux.append(f"    {struct}* __c = ({struct}*)__p;")
        aux.append(f"    {ctype(var_t)} p_{s.var} = {default_init(var_t)};")
        aux.append(f"    (void)p_{s.var};")
        for name, t in getattr(s, "body_locals", []):
            aux.append(f"    {ctype(t)} p_{name} = {default_init(t)};")
            aux.append(f"    (void)p_{name};")
        aux.append("    for (int64_t __k = __c->i0; __k < __c->i1; __k++) {")
        if is_range:
            aux.append(f"        p_{s.var} = __c->rstart + __k * __c->rstep;")
        elif it_t == STR:
            aux.append(f"        p_{s.var} = pstr_index(__c->seq, __k);")
        elif isinstance(it_t, (DictT, SetT)):
            elem = it_t.key if isinstance(it_t, DictT) else it_t.elem
            aux.append("        if (__c->seq->entries[__k].dead) continue;")
            aux.append(f"        p_{s.var} = "
                       f"{self.pval_get(f'__c->seq->entries[__k].key', elem)};")
        else:  # list
            aux.append(f"        p_{s.var} = "
                       f"{self.pval_get(f'plist_get(__c->seq, __k)', it_t.elem)};")
        prev_map = self.name_map
        self.name_map = {name: f"__c->p_{name}" for name, _ in caps}
        body_lines = []
        for st in s.body:
            self.emit_stmt(st, body_lines, 2)
        self.name_map = prev_map
        aux.extend(body_lines)
        aux.append("    }")
        aux.append("}")
        aux.append("")
        self.aux.extend(aux)

        # ---- spawn site ----
        total, ctxs, T, chunk, x = (self.tmp("total"), self.tmp("ctxs"),
                                    self.tmp("T"), self.tmp("chunk"),
                                    self.tmp("x"))
        out.append(f"{pad}{{")
        if is_range:
            args = s.iterable.args
            st_t, sp_t, se_t = self.tmp("rs"), self.tmp("rp"), self.tmp("re")
            if len(args) == 1:
                out.append(f"{pad}    int64_t {st_t} = 0;")
                out.append(f"{pad}    int64_t {sp_t} = {self.ex(args[0])};")
                out.append(f"{pad}    int64_t {se_t} = 1;")
            elif len(args) == 2:
                out.append(f"{pad}    int64_t {st_t} = {self.ex(args[0])};")
                out.append(f"{pad}    int64_t {sp_t} = {self.ex(args[1])};")
                out.append(f"{pad}    int64_t {se_t} = 1;")
            else:
                out.append(f"{pad}    int64_t {st_t} = {self.ex(args[0])};")
                out.append(f"{pad}    int64_t {sp_t} = {self.ex(args[1])};")
                out.append(f"{pad}    int64_t {se_t} = {self.ex(args[2])};")
            out.append(f"{pad}    int64_t {total} = "
                       f"pya_range_count({st_t}, {sp_t}, {se_t});")
        else:
            seq_t = self.tmp("seq")
            bound = "nentries" if isinstance(it_t, (DictT, SetT)) else "len"
            out.append(f"{pad}    {ctype(it_t)} {seq_t} = {self.ex(s.iterable)};")
            out.append(f"{pad}    int64_t {total} = {seq_t}->{bound};")
        out.append(f"{pad}    int {T} = pya_nthreads();")
        out.append(f"{pad}    if ((int64_t){T} > {total}) "
                   f"{T} = (int)({total} > 0 ? {total} : 1);")
        out.append(f"{pad}    {struct}* {ctxs} = "
                   f"({struct}*)pya_alloc(sizeof({struct}) * (size_t){T});")
        out.append(f"{pad}    int64_t {chunk} = ({total} + {T} - 1) / {T};")
        out.append(f"{pad}    for (int {x} = 0; {x} < {T}; {x}++) {{")
        out.append(f"{pad}        {ctxs}[{x}].i0 = (int64_t){x} * {chunk};")
        out.append(f"{pad}        {ctxs}[{x}].i1 = "
                   f"{ctxs}[{x}].i0 + {chunk} > {total} ? {total} : "
                   f"{ctxs}[{x}].i0 + {chunk};")
        if is_range:
            out.append(f"{pad}        {ctxs}[{x}].rstart = {st_t};")
            out.append(f"{pad}        {ctxs}[{x}].rstep = {se_t};")
        else:
            out.append(f"{pad}        {ctxs}[{x}].seq = {seq_t};")
        for name, _ in caps:
            src = self.name_map.get(name, f"p_{name}")
            out.append(f"{pad}        {ctxs}[{x}].p_{name} = {src};")
        out.append(f"{pad}    }}")
        out.append(f"{pad}    pya_parallel_run({worker}, (char*){ctxs}, "
                   f"sizeof({struct}), {T});")
        out.append(f"{pad}}}")

    # -- PVal helpers -----------------------------------------------------

    def pval(self, t, c):
        if t == INT:
            return f"pval_i({c})"
        if t == FLOAT:
            return f"pval_f({c})"
        if t == BOOL:
            return f"pval_i((int64_t)({c}))"
        return f"pval_p({c})"

    def pval_get(self, c, t):
        if t == INT:
            return f"({c}.i)"
        if t == FLOAT:
            return f"({c}.f)"
        if t == BOOL:
            return f"({c}.i != 0)"
        if t == STR:
            return f"((PStr*){c}.p)"
        if isinstance(t, (DictT, SetT)):
            return f"((PDict*){c}.p)"
        if isinstance(t, ClassT):
            return f"(({t.cname}*){c}.p)"
        return f"((PList*){c}.p)"

    # -- expressions ------------------------------------------------------

    def ex(self, e):
        if isinstance(e, A.IntLit):
            return f"{e.value}LL"
        if isinstance(e, A.FloatLit):
            return repr(e.value)
        if isinstance(e, A.StrLit):
            return self.lit(e.value)
        if isinstance(e, A.BoolLit):
            return "true" if e.value else "false"
        if isinstance(e, A.Name):
            return self.name_map.get(e.id, f"p_{e.id}")
        if isinstance(e, A.BinOp):
            return self._binop(e)
        if isinstance(e, A.UnaryOp):
            op = "-" if e.op == "-" else "!"
            return f"({op}{self.ex(e.operand)})"
        if isinstance(e, A.Compare):
            return self._compare(e)
        if isinstance(e, A.Call):
            return self._call(e)
        if isinstance(e, A.Index):
            base_t = e.value.ty
            if base_t == STR:
                return f"pstr_index({self.ex(e.value)}, {self.ex(e.index)})"
            if isinstance(base_t, DictT):
                key = self.pval(base_t.key, self.ex(e.index))
                return self.pval_get(f"pdict_get({self.ex(e.value)}, {key})",
                                     base_t.val)
            return self.pval_get(f"plist_get({self.ex(e.value)}, {self.ex(e.index)})",
                                 base_t.elem)
        if isinstance(e, A.Slice):
            lo = self.ex(e.lo) if e.lo is not None else "0"
            hi = self.ex(e.hi) if e.hi is not None else "PYA_END"
            fn = "pstr_slice" if e.value.ty == STR else "plist_slice"
            return f"{fn}({self.ex(e.value)}, {lo}, {hi})"
        if isinstance(e, A.ListLit):
            if not e.elts:
                return "plist_new(0)"
            vals = ", ".join(self.pval(x.ty, self.ex(x)) for x in e.elts)
            return f"plist_of({len(e.elts)}, {vals})"
        if isinstance(e, A.DictLit):
            kk = key_kind(e.ty.key)
            if not e.keys:
                return f"pdict_new({kk})"
            pairs = ", ".join(
                f"{self.pval(k.ty, self.ex(k))}, {self.pval(v.ty, self.ex(v))}"
                for k, v in zip(e.keys, e.values))
            return f"pdict_of({kk}, {len(e.keys)}, {pairs})"
        if isinstance(e, A.SetLit):
            kk = key_kind(e.ty.elem)
            vals = ", ".join(self.pval(x.ty, self.ex(x)) for x in e.elts)
            return f"pset_of({kk}, {len(e.elts)}, {vals})"
        if isinstance(e, A.FString):
            return self._fstring(e)
        if isinstance(e, A.Attribute):  # field get: p.x
            recv_t = e.value.ty
            return (f"(({recv_t.cname}*)pya_fld({self.ex(e.value)}))"
                    f"->f_{e.attr}")
        self.error(f"cannot emit expression {type(e).__name__}", e)

    def _binop(self, e):
        a, b = self.ex(e.left), self.ex(e.right)
        op, lt = e.op, e.left.ty
        if op == "and":
            return f"({a} && {b})"
        if op == "or":
            return f"({a} || {b})"
        if op == "+":
            if lt == STR:
                return f"pstr_concat({a}, {b})"
            if isinstance(lt, ListT):
                return f"plist_concat({a}, {b})"
            return f"({a} + {b})"
        if op == "-":
            return f"({a} - {b})"
        if op == "*":
            if lt == STR:
                return f"pstr_repeat({a}, {b})"
            return f"({a} * {b})"
        if op == "/":
            return f"pya_div_f((double)({a}), (double)({b}))"
        if op == "//":
            return f"pya_floordiv_i({a}, {b})"
        if op == "%":
            if lt == FLOAT:
                return f"pya_mod_f({a}, {b})"
            return f"pya_mod_i({a}, {b})"
        if op == "**":
            if lt == FLOAT:
                return f"pow({a}, {b})"
            return f"pya_pow_i({a}, {b})"
        self.error(f"cannot emit operator '{op}'", e)

    def _compare(self, e):
        a, b = self.ex(e.left), self.ex(e.right)
        op, lt, rt = e.op, e.left.ty, e.right.ty
        if op in ("in", "not in"):
            if isinstance(rt, ListT):
                suffix = {INT: "i", FLOAT: "f", STR: "s"}.get(rt.elem, "i")
                if rt.elem == BOOL:
                    a = f"(int64_t)({a})"
                call = f"plist_contains_{suffix}({b}, {a})"
            elif isinstance(rt, (DictT, SetT)):
                kt = rt.key if isinstance(rt, DictT) else rt.elem
                call = f"pdict_contains({b}, {self.pval(kt, a)})"
            else:
                call = f"pstr_contains({b}, {a})"
            return f"(!{call})" if op == "not in" else call
        if lt == STR:
            if op == "==":
                return f"pstr_eq({a}, {b})"
            if op == "!=":
                return f"(!pstr_eq({a}, {b}))"
            return f"(pstr_cmp({a}, {b}) {op} 0)"
        return f"({a} {op} {b})"

    def _fstring(self, e):
        pieces = []
        for part in e.parts:
            if isinstance(part, A.StrLit):
                pieces.append(self.lit(part.value))
            elif part.ty == STR:
                pieces.append(self.ex(part))
            elif part.ty == INT:
                pieces.append(f"pya_fmt_i({self.ex(part)})")
            elif part.ty == FLOAT:
                pieces.append(f"pya_fmt_f({self.ex(part)})")
            else:  # BOOL
                pieces.append(f"pya_fmt_b({self.ex(part)})")
        if not pieces:
            return "pstr_empty()"
        acc = pieces[0]
        for p in pieces[1:]:
            acc = f"pstr_concat({acc}, {p})"
        return acc

    # -- calls ------------------------------------------------------------

    def _call(self, e):
        module = getattr(e, "module", None)
        if module is not None:  # cross-module call (qualified or aliased)
            args = ", ".join(self.ex(a) for a in e.args)
            return f"fn_{module}__{e.mfunc}({args})"
        ctor = getattr(e, "ctor", None)
        if ctor is not None:  # constructor: Point(1.0, 2.0)
            args = ", ".join(self.ex(a) for a in e.args)
            return f"new_{ctor.cname}({args})"
        cm = getattr(e, "class_method", None)
        if cm is not None:  # instance method: p.dist(q)
            cname, mname = cm
            recv = f"(({cname}*)pya_fld({self.ex(e.func.value)}))"
            args = ", ".join([recv] + [self.ex(a) for a in e.args])
            return f"fn_{cname}__{mname}({args})"
        if isinstance(e.func, A.Attribute):
            return self._method_call(e)
        name = e.func.id
        if name in self.tc.funcs:
            args = ", ".join(self.ex(a) for a in e.args)
            return f"fn_{self.qual}{name}({args})"
        return self._builtin_call(name, e)

    def _builtin_call(self, name, e):
        args = e.args
        if name == "print":
            self.error("print(...) can only be used as a statement", e)
        if name == "len":
            return f"(({self.ex(args[0])})->len)"
        if name == "abs":
            fn = "fabs" if args[0].ty == FLOAT else "pya_abs_i"
            return f"{fn}({self.ex(args[0])})"
        if name in ("min", "max"):
            suffix = "f" if args[0].ty == FLOAT else "i"
            return f"pya_{name}_{suffix}({self.ex(args[0])}, {self.ex(args[1])})"
        if name in ("int", "float", "str", "bool"):
            return self._conversion(name, args[0])
        if name == "append":
            return (f"plist_append({self.ex(args[0])}, "
                    f"{self.pval(args[1].ty, self.ex(args[1]))})")
        if name == "pop":
            return self.pval_get(f"plist_pop({self.ex(args[0])})", e.ty)
        if name == "sort":
            elem = args[0].ty.elem
            suffix = {INT: "i", FLOAT: "f", STR: "s"}[elem]
            return f"plist_sort_{suffix}({self.ex(args[0])})"
        if name == "read_file":
            return f"pya_read_file({self.ex(args[0])})"
        if name == "read_lines":
            return f"pya_read_lines({self.ex(args[0])})"
        if name == "write_file":
            return f"pya_write_file({self.ex(args[0])}, {self.ex(args[1])})"
        if name == "clock":
            return "pya_clock()"
        if name == "gc_collect":
            return "pya_gc_collect()"
        if name == "args":
            return "pya_args()"
        if name == "input":
            return f"pya_input({self.ex(args[0])})"
        if name == "exists":
            return f"pya_exists({self.ex(args[0])})"
        if name == "exit":
            return f"exit((int)({self.ex(args[0])}))"
        if name == "set":
            return f"pdict_new({key_kind(e.ty.elem)})"
        self.error(f"cannot emit builtin '{name}'", e)

    def _conversion(self, target, arg):
        src = arg.ty
        c = self.ex(arg)
        if target == "int":
            if src == STR:
                return f"pya_str_to_i({c})"
            if src == INT:
                return c
            return f"((int64_t)({c}))"
        if target == "float":
            if src == STR:
                return f"pya_str_to_f({c})"
            if src == FLOAT:
                return c
            return f"((double)({c}))"
        if target == "str":
            if src == STR:
                return c
            if src == INT:
                return f"pya_fmt_i({c})"
            if src == FLOAT:
                return f"pya_fmt_f({c})"
            return f"pya_fmt_b({c})"
        # bool
        if src == STR:
            return f"(({c})->len > 0)"
        if src == BOOL:
            return c
        return f"(({c}) != 0)"

    def _method_call(self, e):
        attr = e.func.attr
        recv = e.func.value
        r = self.ex(recv)
        args = [self.ex(a) for a in e.args]
        if recv.ty == STR:
            table = {"split": "pstr_split", "strip": "pstr_strip",
                     "lower": "pstr_lower", "upper": "pstr_upper",
                     "startswith": "pstr_startswith", "endswith": "pstr_endswith",
                     "replace": "pstr_replace", "find": "pstr_find"}
            return f"{table[attr]}({', '.join([r] + args)})"
        if isinstance(recv.ty, DictT):
            if attr == "get":
                key = self.pval(recv.ty.key, args[0])
                default = self.pval(recv.ty.val, args[1])
                return self.pval_get(
                    f"pdict_get_default({r}, {key}, {default})", recv.ty.val)
            if attr == "pop":
                key = self.pval(recv.ty.key, args[0])
                return self.pval_get(f"pdict_del({r}, {key})", recv.ty.val)
            if attr == "keys":
                return f"pdict_keys({r})"
            if attr == "values":
                return f"pdict_values({r})"
        if isinstance(recv.ty, SetT):
            if attr == "add":
                return f"pdict_add({r}, {self.pval(recv.ty.elem, args[0])})"
            if attr == "remove":
                return (f"(void)pdict_del({r}, "
                        f"{self.pval(recv.ty.elem, args[0])})")
        # list methods
        if attr == "append":
            return f"plist_append({r}, {self.pval(e.args[0].ty, args[0])})"
        if attr == "pop":
            return self.pval_get(f"plist_pop({r})", e.ty)
        if attr == "sort":
            suffix = {INT: "i", FLOAT: "f", STR: "s"}[recv.ty.elem]
            return f"plist_sort_{suffix}({r})"
        self.error(f"cannot emit method '.{attr}()'", e)
