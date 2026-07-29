"""pyalt Python-extension emitter (the interop / adoption path).

Takes a .pya module containing only function definitions and generates a
CPython extension module in C: every pyalt function becomes a callable Python
function, with automatic conversion at the boundary:

    int <-> int      float <-> float    bool <-> bool     str <-> str
    list[T] <-> list    dict[K, V] <-> dict    set[T] <-> set    void -> None

Runtime errors inside compiled code (bounds, division by zero, missing key...)
raise RuntimeError in Python instead of killing the process, via the pya_die
hook + setjmp/longjmp.
"""

from .cemitter import CEmitter, ctype, key_kind
from .errors import CompileError
from .types import INT, FLOAT, BOOL, STR, VOID, ListT, DictT, SetT


def mangle(t):
    if t == INT:
        return "i"
    if t == FLOAT:
        return "f"
    if t == BOOL:
        return "b"
    if t == STR:
        return "s"
    if isinstance(t, ListT):
        return "L" + mangle(t.elem)
    if isinstance(t, DictT):
        return "D" + mangle(t.key) + mangle(t.val)
    if isinstance(t, SetT):
        return "S" + mangle(t.elem)
    raise AssertionError(f"no mangling for {t}")


def is_ptr(t):
    """Types whose converters signal failure by returning NULL."""
    return t == STR or isinstance(t, (ListT, DictT, SetT))


def _collect_containers(t, acc):
    """Container types needing generated converters, dependencies first."""
    if isinstance(t, ListT):
        _collect_containers(t.elem, acc)
    elif isinstance(t, DictT):
        _collect_containers(t.key, acc)
        _collect_containers(t.val, acc)
    elif isinstance(t, SetT):
        _collect_containers(t.elem, acc)
    else:
        return
    if t not in acc:
        acc.append(t)


PRELUDE = r"""
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <setjmp.h>

static jmp_buf pya_jb;
static char pya_errmsg[256];

static void pya_raise_hook(const char *msg) {
    strncpy(pya_errmsg, msg, sizeof pya_errmsg - 1);
    pya_errmsg[sizeof pya_errmsg - 1] = '\0';
    longjmp(pya_jb, 1);
}

/* ---- python -> native (primitives) ---- */

static int64_t cv2n_i(PyObject *o) {
    return (int64_t)PyLong_AsLongLong(o); /* error left in PyErr */
}

static double cv2n_f(PyObject *o) {
    return PyFloat_AsDouble(o); /* accepts int too; error left in PyErr */
}

static bool cv2n_b(PyObject *o) {
    int t = PyObject_IsTrue(o);
    return t > 0; /* error left in PyErr when t < 0 */
}

static PStr *cv2n_s(PyObject *o) {
    Py_ssize_t len;
    const char *buf = PyUnicode_AsUTF8AndSize(o, &len);
    if (!buf) return NULL;
    return pstr_new(buf, (int64_t)len);
}

/* ---- native -> python (primitives) ---- */

static PyObject *n2py_i(int64_t v) { return PyLong_FromLongLong((long long)v); }
static PyObject *n2py_f(double v) { return PyFloat_FromDouble(v); }
static PyObject *n2py_b(bool v) { return PyBool_FromLong(v); }
static PyObject *n2py_s(PStr *s) {
    return PyUnicode_FromStringAndSize(s->data, (Py_ssize_t)s->len);
}
"""


class PyExtEmitter:
    def __init__(self, module, tc, modname, filename="<input>", source=None,
                 deps=None):
        self.module = module
        self.tc = tc
        self.modname = modname
        self.filename = filename
        self.source = source
        self.deps = deps or []

    def emit(self):
        core = CEmitter(self.module, self.tc, self.filename,
                        self.source, deps=self.deps).emit_library()
        sigs = []
        self.skipped = []  # (name, reason) — e.g. class types in signature
        for sig in self.tc.funcs.values():
            try:
                for _, t in sig.params:
                    mangle(t)
                if sig.ret != VOID:
                    mangle(sig.ret)
            except AssertionError:
                self.skipped.append(
                    (sig.name, "class types cannot cross the Python "
                               "boundary yet"))
                continue
            sigs.append(sig)
        if not sigs:
            raise CompileError(self.filename, 1, 1,
                               "module has no exportable functions "
                               "(class-typed signatures cannot cross the "
                               "Python boundary yet)", self.source)

        containers = []
        for sig in sigs:
            for _, t in sig.params:
                _collect_containers(t, containers)
            _collect_containers(sig.ret, containers)

        parts = [core, PRELUDE]
        for t in containers:
            parts.append(self._converters(t))
        for sig in sigs:
            parts.append(self._wrapper(sig))
        parts.append(self._module_def(sigs))
        return "\n".join(parts)

    # -- shared helpers ---------------------------------------------------

    def _elem_from_pval(self, t, pval_expr):
        if t == INT:
            return f"{pval_expr}.i"
        if t == FLOAT:
            return f"{pval_expr}.f"
        if t == BOOL:
            return f"({pval_expr}.i != 0)"
        if t == STR:
            return f"(PStr*){pval_expr}.p"
        return f"(PList*){pval_expr}.p" if isinstance(t, ListT) else f"(PDict*){pval_expr}.p"

    def _elem_to_pval(self, t, expr):
        if t == INT:
            return f"pval_i({expr})"
        if t == FLOAT:
            return f"pval_f({expr})"
        if t == BOOL:
            return f"pval_i((int64_t)({expr}))"
        return f"pval_p({expr})"

    def _err_check(self, t, var, cleanup):
        if is_ptr(t):
            return f"if (!{var}) {{ {cleanup} return NULL; }}"
        return f"if (PyErr_Occurred()) {{ {cleanup} return NULL; }}"

    # -- converters per container type ------------------------------------

    def _converters(self, t):
        if isinstance(t, ListT):
            return self._list_converters(t)
        if isinstance(t, DictT):
            return self._dict_converters(t)
        return self._set_converters(t)

    def _list_converters(self, t):
        m, em = mangle(t), mangle(t.elem)
        to_native = f"""
static PList *cv2n_{m}(PyObject *o) {{
    PyObject *seq = PySequence_Fast(o, "expected a list");
    if (!seq) return NULL;
    Py_ssize_t n = PySequence_Fast_GET_SIZE(seq);
    PList *l = plist_new((int64_t)n);
    for (Py_ssize_t i = 0; i < n; i++) {{
        PyObject *item = PySequence_Fast_GET_ITEM(seq, i);
        {ctype(t.elem)} v = cv2n_{em}(item);
        {self._err_check(t.elem, 'v', 'Py_DECREF(seq);')}
        l->data[i] = {self._elem_to_pval(t.elem, 'v')};
    }}
    l->len = (int64_t)n;
    Py_DECREF(seq);
    return l;
}}"""
        from_native = f"""
static PyObject *n2py_{m}(PList *l) {{
    PyObject *out = PyList_New((Py_ssize_t)l->len);
    if (!out) return NULL;
    for (int64_t i = 0; i < l->len; i++) {{
        PyObject *item = n2py_{em}({self._elem_from_pval(t.elem, 'l->data[i]')});
        if (!item) {{ Py_DECREF(out); return NULL; }}
        PyList_SET_ITEM(out, (Py_ssize_t)i, item);
    }}
    return out;
}}"""
        return to_native + "\n" + from_native

    def _dict_converters(self, t):
        m, km, vm = mangle(t), mangle(t.key), mangle(t.val)
        to_native = f"""
static PDict *cv2n_{m}(PyObject *o) {{
    if (!PyDict_Check(o)) {{
        PyErr_SetString(PyExc_TypeError, "expected a dict");
        return NULL;
    }}
    PDict *d = pdict_new({key_kind(t.key)});
    PyObject *k, *v;
    Py_ssize_t pos = 0;
    while (PyDict_Next(o, &pos, &k, &v)) {{
        {ctype(t.key)} kk = cv2n_{km}(k);
        {self._err_check(t.key, 'kk', '')}
        {ctype(t.val)} vv = cv2n_{vm}(v);
        {self._err_check(t.val, 'vv', '')}
        pdict_set(d, {self._elem_to_pval(t.key, 'kk')}, {self._elem_to_pval(t.val, 'vv')});
    }}
    return d;
}}"""
        from_native = f"""
static PyObject *n2py_{m}(PDict *d) {{
    PyObject *out = PyDict_New();
    if (!out) return NULL;
    for (int64_t i = 0; i < d->nentries; i++) {{
        if (d->entries[i].dead) continue;
        PyObject *ko = n2py_{km}({self._elem_from_pval(t.key, 'd->entries[i].key')});
        if (!ko) {{ Py_DECREF(out); return NULL; }}
        PyObject *vo = n2py_{vm}({self._elem_from_pval(t.val, 'd->entries[i].val')});
        if (!vo) {{ Py_DECREF(ko); Py_DECREF(out); return NULL; }}
        int rc = PyDict_SetItem(out, ko, vo);
        Py_DECREF(ko);
        Py_DECREF(vo);
        if (rc < 0) {{ Py_DECREF(out); return NULL; }}
    }}
    return out;
}}"""
        return to_native + "\n" + from_native

    def _set_converters(self, t):
        m, em = mangle(t), mangle(t.elem)
        to_native = f"""
static PDict *cv2n_{m}(PyObject *o) {{
    PyObject *it = PyObject_GetIter(o);
    if (!it) return NULL;
    PDict *d = pdict_new({key_kind(t.elem)});
    PyObject *item;
    while ((item = PyIter_Next(it))) {{
        {ctype(t.elem)} v = cv2n_{em}(item);
        Py_DECREF(item);
        {self._err_check(t.elem, 'v', 'Py_DECREF(it);')}
        pdict_add(d, {self._elem_to_pval(t.elem, 'v')});
    }}
    Py_DECREF(it);
    if (PyErr_Occurred()) return NULL;
    return d;
}}"""
        from_native = f"""
static PyObject *n2py_{m}(PDict *d) {{
    PyObject *out = PySet_New(NULL);
    if (!out) return NULL;
    for (int64_t i = 0; i < d->nentries; i++) {{
        if (d->entries[i].dead) continue;
        PyObject *eo = n2py_{em}({self._elem_from_pval(t.elem, 'd->entries[i].key')});
        if (!eo) {{ Py_DECREF(out); return NULL; }}
        int rc = PySet_Add(out, eo);
        Py_DECREF(eo);
        if (rc < 0) {{ Py_DECREF(out); return NULL; }}
    }}
    return out;
}}"""
        return to_native + "\n" + from_native

    # -- per-function wrapper ---------------------------------------------

    def _wrapper(self, sig):
        n = len(sig.params)
        lines = [f"static PyObject *w_{sig.name}(PyObject *self, PyObject *args) {{"]
        lines.append("    (void)self;")
        if n:
            decls = ", ".join(f"*o{i}" for i in range(n))
            lines.append(f"    PyObject {decls};")
            addrs = ", ".join(f"&o{i}" for i in range(n))
            lines.append(f'    if (!PyArg_UnpackTuple(args, "{sig.name}", '
                         f"{n}, {n}, {addrs})) return NULL;")
        else:
            lines.append(f'    if (!PyArg_UnpackTuple(args, "{sig.name}", '
                         f"0, 0)) return NULL;")
        for i, (pname, pt) in enumerate(sig.params):
            lines.append(f"    {ctype(pt)} a{i} = cv2n_{mangle(pt)}(o{i});")
            if is_ptr(pt):
                lines.append(f"    if (!a{i}) return NULL;")
            else:
                lines.append(f"    if (PyErr_Occurred()) return NULL;")
        lines.append("    if (setjmp(pya_jb)) {")
        lines.append("        PyErr_SetString(PyExc_RuntimeError, pya_errmsg);")
        lines.append("        return NULL;")
        lines.append("    }")
        call_args = ", ".join(f"a{i}" for i in range(n))
        if sig.ret == VOID:
            lines.append(f"    fn_{sig.name}({call_args});")
            lines.append("    Py_RETURN_NONE;")
        else:
            lines.append(f"    {ctype(sig.ret)} r = fn_{sig.name}({call_args});")
            lines.append(f"    return n2py_{mangle(sig.ret)}(r);")
        lines.append("}")
        return "\n".join(lines)

    def _module_def(self, sigs):
        entries = "\n".join(
            f'    {{"{s.name}", w_{s.name}, METH_VARARGS, "{s}"}},'
            for s in sigs)
        return f"""
static PyMethodDef pya_methods[] = {{
{entries}
    {{NULL, NULL, 0, NULL}}
}};

static struct PyModuleDef pya_moduledef = {{
    PyModuleDef_HEAD_INIT, "{self.modname}",
    "compiled pyalt module (native code)", -1, pya_methods,
    NULL, NULL, NULL, NULL
}};

PyMODINIT_FUNC PyInit_{self.modname}(void) {{
    pya_die_hook = pya_raise_hook;
    pya_init_literals();
    return PyModule_Create(&pya_moduledef);
}}
"""
