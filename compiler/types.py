"""pyalt type representations. Frozen dataclasses so types compare by value."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Prim:
    name: str

    def __str__(self):
        return self.name


@dataclass(frozen=True)
class ListT:
    elem: object

    def __str__(self):
        return f"list[{self.elem}]"


@dataclass(frozen=True)
class DictT:
    key: object          # must be a Prim (hashable)
    val: object

    def __str__(self):
        return f"dict[{self.key}, {self.val}]"


@dataclass(frozen=True)
class SetT:
    elem: object         # must be a Prim (hashable)

    def __str__(self):
        return f"set[{self.elem}]"


@dataclass(frozen=True)
class ClassT:
    name: str            # source-level class name
    cname: str           # unique C struct name, e.g. PC_geom__Point

    def __str__(self):
        return self.name


@dataclass(frozen=True)
class Void:
    def __str__(self):
        return "void"


INT = Prim("int")
FLOAT = Prim("float")
BOOL = Prim("bool")
STR = Prim("str")
VOID = Void()

NUMERIC = (INT, FLOAT)
PRIMS = (INT, FLOAT, BOOL, STR)
