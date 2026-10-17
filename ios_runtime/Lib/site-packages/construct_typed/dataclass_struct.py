# -*- coding: utf-8 -*-
import dataclasses
import textwrap
import typing as t

import construct as cs
import typing_extensions
from construct.lib.containers import (
    globalPrintFullStrings,
    globalPrintPrivateEntries,
    recursion_lock,
)
from construct.lib.py3compat import bytestringtype, reprstring, unicodestringtype

from .generic_wrapper import Adapter, Construct, Context, PathType

T = t.TypeVar("T")


class DataclassFieldError(Exception):
    """Exception raised by csfield, csfield_noinit, csfield_const and csfield_default."""


def _rename_subcon(
    subcon: Construct[T, t.Any],
    doc: str | None = None,
    parsed: t.Callable[[t.Any, Context], None] | None = None,
) -> Construct[T, t.Any]:
    """Helper method to rename a subcon if doc or parsed are available."""
    if (doc is not None) or (parsed is not None):
        if doc is not None:
            doc = textwrap.dedent(doc).strip("\n")
        subcon = cs.Renamed(subcon, newdocs=doc, newparsed=parsed)
    return subcon


def csfield(
    subcon: Construct[T, t.Any],
    doc: str | None = None,
    parsed: t.Callable[[t.Any, Context], None] | None = None,
    *,
    kw_only: bool = False,
) -> T:
    """
    Field specifier for dataclasses that are passed to "DataclassStruct" and "DataclassBitStruct".

    Should not be used for fields that can be built from `None` (e.g. `cs.Default`, `cs.Const`,
    `cs.Rebuild`, `cs.Computed`, `cs.Padding`, `cs.Tell`, `cs.Pass`, `cs.Terminated`). For these
    fields, use `csfield_noinit()` instead, or - for `cs.Const` and `cs.Default` -
    `csfield_const()` or `csfield_default()`.

    Note on `kw_only`: if a preceding field in the dataclass has a default value (e.g. created with
    `csfield_default()`), every following field that has no default of its own (i.e. every plain
    `csfield()`) must be marked `kw_only=True`. Otherwise Python's `dataclasses` module raises
    ``TypeError: non-default argument '...' follows default argument`` when the class is defined.
    """
    if subcon.flagbuildnone is True:
        raise DataclassFieldError(
            "Fields that can build from None, should be used with ``csfield_noinit()``, ``csfield_default()`` or ``csfield_const()``."
        )

    subcon = _rename_subcon(subcon, doc, parsed)

    return t.cast(
        T,
        dataclasses.field(
            kw_only=kw_only,
            metadata={"subcon": subcon},
        ),
    )


def csfield_noinit(
    subcon: Construct[T, None],
    doc: str | None = None,
    parsed: t.Callable[[t.Any, Context], None] | None = None,
    *,
    # "init" should not be used by users. It is only for type checkers to see that this field is excluded from __init__.
    init: t.Literal[False] = False,
) -> T | None:
    """
    Field specifier for dataclasses that are passed to "DataclassStruct" and "DataclassBitStruct" for constructs that
    can be build from `None` and thus should be *excluded* from the dataclass constructor.

    It can be used for e.g. `cs.Rebuild`, `cs.Computed`, `cs.Padding`, `cs.Tell`, `cs.Pass` and `cs.Terminated`.

    `cs.Const` and `cs.Default` can also be used with this field specifier. Note however that the field will then
    be `None` until the struct is parsed, since the constant/default value is not applied by the constructor. If
    the constant/default value should already be available right after construction, use `csfield_const()` or
    `csfield_default()` instead.
    """
    if subcon.flagbuildnone is False:
        raise DataclassFieldError(
            "csfield_noinit() can only be used with constructs that have flagbuildnone=True (Const, Rebuild, Computed, Padding, Tell, Pass, Terminated)."
        )

    subcon = _rename_subcon(subcon, doc, parsed)

    return t.cast(
        T | None,
        dataclasses.field(
            default=None,
            init=False,
            metadata={"subcon": subcon},
        ),
    )


def csfield_const(
    subcon: Construct[T, t.Any],
    const: T,
    doc: str | None = None,
    parsed: t.Callable[[t.Any, Context], None] | None = None,
    *,
    # "init" should not be used by users. It is only for type checkers to see that this field is excluded from __init__.
    init: t.Literal[False] = False,
) -> T:
    """
    Field specifier for dataclasses that are passed to "DataclassStruct" and "DataclassBitStruct" for constants.
    Fields that are created with this field specifier are *excluded* from the dataclass constructor.

    The subcon that is passed to this function is automatically wrapped in a `cs.Const` construct.
    """
    if subcon.flagbuildnone is True:
        raise DataclassFieldError(
            "csfield_const() cannot be used with a subcon that can already build from None (e.g. another "
            "``cs.Const``, ``cs.Default`` or ``cs.Computed``). Pass the plain, unwrapped subcon instead."
        )
    if callable(const):
        raise DataclassFieldError(
            "csfield_const() cannot be used with context lambdas."
        )
    if isinstance(subcon, cs.Const):
        raise DataclassFieldError(
            "csfield_const() cannot be used with a subcon that is already a ``cs.Const``. Pass the plain, unwrapped subcon instead."
        )

    subcon = cs.Const(const, subcon)
    subcon = _rename_subcon(subcon, doc, parsed)

    return t.cast(
        T,
        dataclasses.field(
            default=const,
            init=False,
            metadata={"subcon": subcon},
        ),
    )


def csfield_default(
    subcon: Construct[T, t.Any],
    doc: str | None = None,
    parsed: t.Callable[[t.Any, Context], None] | None = None,
    *,
    default: T,
) -> T:
    """
    Field specifier for dataclasses that are passed to "DataclassStruct" and
    DataclassBitStruct" for fields with default values. Fields that are created
    with this field specifier are *included* in the dataclass constructor.

    The subcon that is passed to this function is automatically wrapped in a
    `cs.Default` construct.

    For default values that are calculated by the context, use
    `csfield_noinit(cs.Default(...))` instead.

    Note: since this field has a default value, every field that follows it and has no default of its
    own (i.e. every plain `csfield()`) must be marked `kw_only=True`. Otherwise Python's `dataclasses`
    module raises ``TypeError: non-default argument '...' follows default argument`` when the class
    is defined.
    """
    if subcon.flagbuildnone is True:
        raise DataclassFieldError(
            "csfield_default() cannot be used with a subcon that can already build from None (e.g. "
            "``cs.Const``, another ``cs.Default`` or ``cs.Computed``). Pass the plain, unwrapped subcon instead."
        )
    if callable(default):
        raise DataclassFieldError(
            "csfield_default() cannot be used with context lambdas. Use `csfield_noinit(cs.Default(...))` instead."
        )
    if isinstance(subcon, cs.Default):
        raise DataclassFieldError(
            "csfield_default() cannot be used with a subcon that is already a ``cs.Default``. Pass the plain, unwrapped subcon instead."
        )

    subcon = cs.Default(subcon, default)
    subcon = _rename_subcon(subcon, doc, parsed)

    return t.cast(
        T,
        dataclasses.field(
            default=default,
            init=True,
            metadata={"subcon": subcon},
        ),
    )


@typing_extensions.dataclass_transform(
    field_specifiers=(csfield, csfield_noinit, csfield_const, csfield_default)
)
class DataclassMixin:
    """
    Mixin for the dataclasses which are passed to "DataclassStruct" and "DataclassBitStruct".

    Note: This implementation is different to the 'cs.Container' of the original 'construct'
    library. In the original 'cs.Container' some names like "update", "keys", "items", ... can
    only accessed via key access (square brackets) and not via attribute access (dot operator),
    because they are also method names. This implementation is based on "dataclasses.dataclass"
    which only uses modul-level instead of instance-level helper methods.So no instance-level
    methods exists and every name can be used.
    """

    __dataclass_fields__: "t.ClassVar[t.Dict[str, dataclasses.Field[t.Any]]]"

    def __getitem__(self, key: str) -> t.Any:
        return getattr(self, key)

    def __setitem__(self, key: str, value: t.Any) -> None:
        setattr(self, key, value)

    @recursion_lock()
    def __str__(self) -> str:
        indentation = "\n    "
        text = [f"{self.__class__.__name__}: "]

        for field in dataclasses.fields(self):
            k = field.name
            v = getattr(self, field.name)
            if k.startswith("_") and not globalPrintPrivateEntries:
                continue
            text.extend([indentation, str(k), " = "])
            if v.__class__.__name__ == "EnumInteger":
                text.append("(enum) (unknown) %s" % (v,))
            elif v.__class__.__name__ == "EnumIntegerString":
                text.append("(enum) %s %s" % (v, v.intvalue))
            elif v.__class__.__name__ in ["HexDisplayedBytes", "HexDumpDisplayedBytes"]:
                text.append(indentation.join(str(v).split("\n")))
            elif isinstance(v, bytestringtype):
                printingcap = 16
                if len(v) <= printingcap or globalPrintFullStrings:
                    text.append("%s (total %d)" % (reprstring(v), len(v)))
                else:
                    text.append(
                        "%s... (truncated, total %d)"
                        % (reprstring(v[:printingcap]), len(v))
                    )
            elif isinstance(v, unicodestringtype):
                printingcap = 32
                if len(v) <= printingcap or globalPrintFullStrings:
                    text.append("%s (total %d)" % (reprstring(v), len(v)))
                else:
                    text.append(
                        "%s... (truncated, total %d)"
                        % (reprstring(v[:printingcap]), len(v))
                    )
            else:
                text.append(indentation.join(str(v).split("\n")))
        return "".join(text)


DataclassType = t.TypeVar("DataclassType", bound=DataclassMixin)


class DataclassStruct(Adapter[t.Any, t.Any, DataclassType, DataclassType]):
    """
    Adapter for a dataclasses for optimised type hints / static autocompletion in comparision to the original Struct.

    Before this construct can be created a dataclasses.dataclass type must be created, which must also derive from DataclassMixin. In this dataclass all fields must be assigned to a construct type using csfield.

    Internally, all fields are converted to a Struct, which does the actual parsing/building.

    Parses to a dataclasses.dataclass instance, and builds from such instance. Size is the sum of all subcon sizes, unless any subcon raises SizeofError.

    :param dc_type: Type of the dataclass, which also inherits from DataclassMixin
    :param reverse: Flag if the fields of the dataclass should be reversed

    Example::

        >>> import dataclasses
        >>> from construct import Bytes, Int8ub, this
        >>> from construct_typed import DataclassMixin, DataclassStruct, csfield
        >>> @dataclasses.dataclass
        ... class Image(DataclassMixin):
        ...     width: int = csfield(Int8ub)
        ...     height: int = csfield(Int8ub)
        ...     pixels: bytes = csfield(Bytes(this.height * this.width))
        >>> d = DataclassStruct(Image)
        >>> d.parse(b"\x01\x0212")
        Image(width=1, height=2, pixels=b'12')
    """

    subcon: "cs.Struct"  # type: ignore

    def __init__(
        self,
        dc_type: t.Type[DataclassType],
        reverse: bool = False,
    ) -> None:
        if not issubclass(dc_type, DataclassMixin):  # type: ignore
            raise TypeError(f"'{repr(dc_type)}' has to be a '{repr(DataclassMixin)}'")
        if not dataclasses.is_dataclass(dc_type):
            raise TypeError(f"'{repr(dc_type)}' has to be a 'dataclasses.dataclass'")
        self.dc_type = dc_type
        self.reverse = reverse

        # get all fields from the dataclass
        fields = dataclasses.fields(self.dc_type)
        if self.reverse:
            fields = tuple(reversed(fields))

        # extract the construct formats from the struct_type
        subcon_fields = {}
        for field in fields:
            subcon_fields[field.name] = field.metadata["subcon"]

        # init adatper
        super().__init__(cs.Struct(**subcon_fields))  # type: ignore

    def __getattr__(self, name: str) -> t.Any:
        return getattr(self.subcon, name)

    def _decode(
        self, obj: "cs.Container[t.Any]", context: Context, path: PathType
    ) -> DataclassType:
        # get all fields from the dataclass
        fields = dataclasses.fields(self.dc_type)

        # extract all fields from the container, that are used for create the dataclass object
        dc_init = {}
        for field in fields:
            if field.init:
                value = obj[field.name]
                dc_init[field.name] = value

        # create object of dataclass
        dc = self.dc_type(**dc_init)  # type: ignore

        # extract all other values from the container, an pass it to the dataclass
        for field in fields:
            if not field.init:
                value = obj[field.name]
                setattr(dc, field.name, value)

        return dc  # type: ignore

    def _encode(
        self, obj: DataclassType, context: Context, path: PathType
    ) -> t.Dict[str, t.Any]:
        if not isinstance(obj, self.dc_type):
            raise TypeError(f"'{repr(obj)}' has to be of type {repr(self.dc_type)}")

        # get all fields from the dataclass
        fields = dataclasses.fields(self.dc_type)

        # extract all fields from the container, that are used for create the dataclass object
        ret_dict: t.Dict[str, t.Any] = {}
        for field in fields:
            value = getattr(obj, field.name)
            ret_dict[field.name] = value

        return ret_dict


def DataclassBitStruct(
    dc_type: t.Type[DataclassType], reverse: bool = False
) -> "cs.Transformed[DataclassType, DataclassType] | cs.Restreamed[DataclassType, DataclassType]":
    r"""
    Makes a DataclassStruct inside a Bitwise.

    See :class:`~construct.core.Bitwise` and :class:`~construct_typed.dataclass_struct.DatclassStruct` for semantics and raisable exceptions.

    :param dc_type: Type of the dataclass, which also inherits from DataclassMixin
    :param reverse: Flag if the fields of the dataclass should be reversed

    Example::

        DataclassBitStruct  <-->  Bitwise(DataclassStruct(...))
        >>> import dataclasses
        >>> from construct import BitsInteger, Flag, Nibble, Padding
        >>> from construct_typed import DataclassBitStruct, DataclassMixin, csfield
        >>> @dataclasses.dataclass
        ... class TestDataclass(DataclassMixin):
        ...     a: int = csfield(Flag)
        ...     b: int = csfield(Nibble)
        ...     c: int = csfield(BitsInteger(10))
        ...     d: None = csfield(Padding(1))
        >>> d = DataclassBitStruct(TestDataclass)
        >>> d.parse(b"\x01\x02")
        TestDataclass(a=False, b=0, c=129, d=None)
    """
    return cs.Bitwise(DataclassStruct(dc_type, reverse))


# support legacy names
TStruct = DataclassStruct
TBitStruct = DataclassBitStruct
TContainerMixin = DataclassMixin
TContainerBase = DataclassMixin
TStructField = csfield
sfield = csfield
