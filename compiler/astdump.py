"""Pretty-printer for pyalt ASTs (used by `pyalt parse`)."""

from dataclasses import is_dataclass, fields

from . import ast_nodes as A

SKIP_FIELDS = {"line", "col"}


def type_str(t):
    if isinstance(t, A.ListType):
        return f"list[{type_str(t.elem)}]"
    if isinstance(t, A.TypeName):
        return t.name
    return "?"


def dump(node):
    out = []
    _fmt(node, 0, out)
    return "\n".join(out)


def _fmt(node, indent, out):
    pad = "  " * indent
    scalars = []
    children = []
    for f in fields(node):
        if f.name in SKIP_FIELDS:
            continue
        v = getattr(node, f.name)
        if isinstance(v, (A.TypeName, A.ListType)):
            scalars.append(f"{f.name}={type_str(v)}")
        elif is_dataclass(v):
            children.append((f.name, [v]))
        elif isinstance(v, list) and v and all(is_dataclass(x) for x in v):
            children.append((f.name, v))
        else:
            scalars.append(f"{f.name}={v!r}")
    head = type(node).__name__
    if scalars:
        head += " " + " ".join(scalars)
    out.append(pad + head)
    for name, items in children:
        out.append(pad + "  " + name + ":")
        for item in items:
            _fmt(item, indent + 2, out)
