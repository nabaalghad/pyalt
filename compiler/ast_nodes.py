"""pyalt AST node definitions. Every node carries line/col for error reporting."""

from dataclasses import dataclass, field
from typing import Optional, Union


# -- types (annotations) --------------------------------------------------

@dataclass
class TypeName:
    name: str            # 'int' | 'float' | 'bool' | 'str' (validated by the typechecker)
    line: int = 0
    col: int = 0


@dataclass
class ListType:
    elem: "TypeAnn"
    line: int = 0
    col: int = 0


@dataclass
class DictType:
    key: "TypeAnn"
    val: "TypeAnn"
    line: int = 0
    col: int = 0


@dataclass
class SetType:
    elem: "TypeAnn"
    line: int = 0
    col: int = 0


TypeAnn = Union[TypeName, ListType, DictType, SetType]


# -- module / statements --------------------------------------------------

@dataclass
class Module:
    body: list


@dataclass
class Import:
    name: str            # `import utils` -> utils.pya next to the importer
    line: int = 0
    col: int = 0


@dataclass
class FromImport:
    module: str          # `from utils import clean, tokenize`
    names: list
    line: int = 0
    col: int = 0


@dataclass
class ClassDef:
    name: str
    fields: list         # [(name, TypeAnn, line, col)]
    methods: list        # [FuncDef] — first param is the implicit `self`
    line: int = 0
    col: int = 0


@dataclass
class Try:
    body: list
    handler: list
    bind: Optional[str]  # `except as msg:` binds the error message str
    line: int = 0
    col: int = 0


@dataclass
class Raise:
    value: object        # a str expression
    line: int = 0
    col: int = 0


@dataclass
class Param:
    name: str
    ann: TypeAnn
    line: int = 0
    col: int = 0


@dataclass
class FuncDef:
    name: str
    params: list
    return_ann: Optional[TypeAnn]
    body: list
    line: int = 0
    col: int = 0


@dataclass
class Assign:
    target: object       # Name or Index
    value: object
    line: int = 0
    col: int = 0


@dataclass
class AnnAssign:
    name: str
    ann: TypeAnn
    value: object
    line: int = 0
    col: int = 0


@dataclass
class ExprStmt:
    value: object
    line: int = 0
    col: int = 0


@dataclass
class If:
    cond: object
    body: list
    orelse: list         # [] | [If] for elif-chains | statements for else
    line: int = 0
    col: int = 0


@dataclass
class While:
    cond: object
    body: list
    line: int = 0
    col: int = 0


@dataclass
class For:
    var: str
    iterable: object
    body: list
    line: int = 0
    col: int = 0
    parallel: bool = False


@dataclass
class Return:
    value: Optional[object]
    line: int = 0
    col: int = 0


@dataclass
class Break:
    line: int = 0
    col: int = 0


@dataclass
class Continue:
    line: int = 0
    col: int = 0


@dataclass
class Pass:
    line: int = 0
    col: int = 0


# -- expressions ----------------------------------------------------------

@dataclass
class BinOp:
    left: object
    op: str              # + - * / // % ** and or
    right: object
    line: int = 0
    col: int = 0


@dataclass
class UnaryOp:
    op: str              # '-' 'not'
    operand: object
    line: int = 0
    col: int = 0


@dataclass
class Compare:
    left: object
    op: str              # == != < <= > >= in 'not in'
    right: object
    line: int = 0
    col: int = 0


@dataclass
class Call:
    func: object         # Name or Attribute
    args: list
    line: int = 0
    col: int = 0


@dataclass
class Attribute:
    value: object
    attr: str
    line: int = 0
    col: int = 0


@dataclass
class Index:
    value: object
    index: object
    line: int = 0
    col: int = 0


@dataclass
class Slice:
    value: object
    lo: Optional[object]
    hi: Optional[object]
    line: int = 0
    col: int = 0


@dataclass
class Name:
    id: str
    line: int = 0
    col: int = 0


@dataclass
class IntLit:
    value: int
    line: int = 0
    col: int = 0


@dataclass
class FloatLit:
    value: float
    line: int = 0
    col: int = 0


@dataclass
class StrLit:
    value: str
    line: int = 0
    col: int = 0


@dataclass
class BoolLit:
    value: bool
    line: int = 0
    col: int = 0


@dataclass
class ListLit:
    elts: list
    line: int = 0
    col: int = 0


@dataclass
class DictLit:
    keys: list
    values: list         # parallel to keys; both empty = the empty dict {}
    line: int = 0
    col: int = 0


@dataclass
class SetLit:
    elts: list           # never empty ({} is a dict; empty set is set())
    line: int = 0
    col: int = 0


@dataclass
class FString:
    parts: list          # StrLit (literal text) and expression nodes, in order
    line: int = 0
    col: int = 0
