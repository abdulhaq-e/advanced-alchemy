from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from functools import singledispatchmethod
from typing import TYPE_CHECKING, ClassVar, Collection, Generic, Literal, Optional, TypeVar

from litestar.dto.base_dto import AbstractDTO
from litestar.dto.config import DTOConfig
from litestar.dto.data_structures import DTOFieldDefinition
from litestar.dto.field import DTO_FIELD_META_KEY, DTOField, Mark
from litestar.types.empty import Empty
from litestar.typing import FieldDefinition
from litestar.utils.signature import ParsedSignature
from sqlalchemy import Column, inspect, orm, sql
from sqlalchemy.ext.associationproxy import AssociationProxy, AssociationProxyExtensionType
from sqlalchemy.ext.hybrid import HybridExtensionType, hybrid_property
from sqlalchemy.orm import (
    ColumnProperty,
    CompositeProperty,
    DeclarativeBase,
    InspectionAttr,
    Mapped,
    MappedColumn,
    NotExtension,
    QueryableAttribute,
    RelationshipDirection,
    RelationshipProperty,
)

from advanced_alchemy.exceptions import ImproperConfigurationError

if TYPE_CHECKING:
    from typing import Any, Generator

    from typing_extensions import TypeAlias

__all__ = ("SQLAlchemyDTO",)

T = TypeVar("T", bound="DeclarativeBase | Collection[DeclarativeBase]")

ElementType: TypeAlias = "Column | RelationshipProperty | CompositeProperty"
SQLA_NS = {**vars(orm), **vars(sql)}


@dataclass(frozen=True)
class SQLAlchemyDTOConfig(DTOConfig):
    """Additional controls for the generated SQLAlchemy DTO."""

    include_implicit_fields: bool | Literal["hybrid-only"] = True
    """Fields that are implicitly mapped are included.

    Turning this off will lead to exclude all fields not using ``Mapped`` annotation,

    When setting this to ``hybrid-only``, all implicitly mapped fields are excluded
    with the exception for hybrid properties.
    """


class SQLAlchemyDTO(AbstractDTO[T], Generic[T]):
    """Support for domain modelling with SQLAlchemy."""

    config: ClassVar[SQLAlchemyDTOConfig]

    @staticmethod
    def _ensure_sqla_dto_config(config: DTOConfig | SQLAlchemyDTOConfig) -> SQLAlchemyDTOConfig:
        if not isinstance(config, SQLAlchemyDTOConfig):
            return SQLAlchemyDTOConfig(**asdict(config))

        return config

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if hasattr(cls, "config"):
            cls.config = cls._ensure_sqla_dto_config(cls.config)

    @singledispatchmethod
    @classmethod
    def handle_orm_descriptor(
        cls,
        extension_type: NotExtension | AssociationProxyExtensionType | HybridExtensionType,
        orm_descriptor: InspectionAttr,  # noqa: ARG003
        key: str,  # noqa: ARG003
        model_type_hints: dict[str, FieldDefinition],  # noqa: ARG003
        model_name: str,  # noqa: ARG003
    ) -> list[DTOFieldDefinition]:
        msg = f"Unsupported extension type: {extension_type}"
        raise NotImplementedError(msg)

    @handle_orm_descriptor.register(NotExtension)
    @classmethod
    def _(
        cls,
        extension_type: NotExtension,
        key: str,
        orm_descriptor: InspectionAttr,
        model_type_hints: dict[str, FieldDefinition],
        model_name: str,
    ) -> list[DTOFieldDefinition]:
        if not isinstance(orm_descriptor, QueryableAttribute):
            msg = f"Unexpected descriptor type for '{extension_type}': '{orm_descriptor}'"
            raise NotImplementedError(msg)

        elem: ElementType
        if isinstance(orm_descriptor.property, ColumnProperty):
            if not isinstance(orm_descriptor.property.expression, Column):
                msg = f"Expected 'Column', got: '{orm_descriptor.property.expression}'"
                raise NotImplementedError(msg)
            elem = orm_descriptor.property.expression
        elif isinstance(orm_descriptor.property, (RelationshipProperty, CompositeProperty)):
            elem = orm_descriptor.property
        else:
            msg = f"Unhandled property type: '{orm_descriptor.property}'"
            raise NotImplementedError(msg)

        default, default_factory = _detect_defaults(elem)

        try:
            if (field_definition := model_type_hints[key]).origin is Mapped:
                (field_definition,) = field_definition.inner_types
            else:
                msg = f"Expected 'Mapped' origin, got: '{field_definition.origin}'"
                raise NotImplementedError(msg)
        except KeyError:
            field_definition = parse_type_from_element(elem)

        return [
            DTOFieldDefinition.from_field_definition(
                field_definition=replace(
                    field_definition,
                    name=key,
                    default=default,
                ),
                default_factory=default_factory,
                dto_field=elem.info.get(DTO_FIELD_META_KEY, DTOField()),
                model_name=model_name,
            ),
        ]

    @handle_orm_descriptor.register(AssociationProxyExtensionType)
    @classmethod
    def _(
        cls,
        extension_type: AssociationProxyExtensionType,
        key: str,
        orm_descriptor: InspectionAttr,
        model_type_hints: dict[str, FieldDefinition],
        model_name: str,
    ) -> list[DTOFieldDefinition]:
        if not isinstance(orm_descriptor, AssociationProxy):
            msg = f"Unexpected descriptor type '{orm_descriptor}' for '{extension_type}'"
            raise NotImplementedError(msg)

        if (field_definition := model_type_hints[key]).origin is AssociationProxy:
            (field_definition,) = field_definition.inner_types
        else:
            msg = f"Expected 'AssociationProxy' origin, got: '{field_definition.origin}'"
            raise NotImplementedError(msg)

        return [
            DTOFieldDefinition.from_field_definition(
                field_definition=replace(
                    field_definition,
                    name=key,
                    default=Empty,
                ),
                default_factory=None,
                dto_field=orm_descriptor.info.get(DTO_FIELD_META_KEY, DTOField(mark=Mark.READ_ONLY)),
                model_name=model_name,
            ),
        ]

    @handle_orm_descriptor.register(HybridExtensionType)
    @classmethod
    def _(
        cls,
        extension_type: HybridExtensionType,
        key: str,  # noqa: ARG003
        orm_descriptor: InspectionAttr,
        model_type_hints: dict[str, FieldDefinition],  # noqa: ARG003
        model_name: str,
    ) -> list[DTOFieldDefinition]:
        if not isinstance(orm_descriptor, hybrid_property):
            msg = f"Unexpected descriptor type '{orm_descriptor}' for '{extension_type}'"
            raise NotImplementedError(msg)

        getter_sig = ParsedSignature.from_fn(orm_descriptor.fget, {})

        field_defs = [
            DTOFieldDefinition.from_field_definition(
                field_definition=replace(
                    getter_sig.return_type,
                    name=orm_descriptor.__name__,
                    default=Empty,
                ),
                default_factory=None,
                dto_field=orm_descriptor.info.get(DTO_FIELD_META_KEY, DTOField(mark=Mark.READ_ONLY)),
                model_name=model_name,
            ),
        ]

        if orm_descriptor.fset is not None:
            setter_sig = ParsedSignature.from_fn(orm_descriptor.fset, {})
            field_defs.append(
                DTOFieldDefinition.from_field_definition(
                    field_definition=replace(
                        next(iter(setter_sig.parameters.values())),
                        name=orm_descriptor.__name__,
                        default=Empty,
                    ),
                    default_factory=None,
                    dto_field=orm_descriptor.info.get(DTO_FIELD_META_KEY, DTOField(mark=Mark.WRITE_ONLY)),
                    model_name=model_name,
                ),
            )

        return field_defs

    @classmethod
    def generate_field_definitions(cls, model_type: type[DeclarativeBase]) -> Generator[DTOFieldDefinition, None, None]:
        if (mapper := inspect(model_type)) is None:  # pragma: no cover
            msg = "Unexpected `None` value for mapper."  # type: ignore[unreachable]
            raise RuntimeError(msg)

        # includes SQLAlchemy names and other mapped class names in the forward reference resolution namespace
        namespace = {**SQLA_NS, **{m.class_.__name__: m.class_ for m in mapper.registry.mappers if m is not mapper}}
        model_type_hints = cls.get_model_type_hints(model_type, namespace=namespace)
        model_name = model_type.__name__
        include_implicit_fields = cls.config.include_implicit_fields

        # the same hybrid property descriptor can be included in `all_orm_descriptors` multiple times, once
        # for each method name it is bound to. We only need to see it once, so track views of it here.
        seen_hybrid_descriptors: set[hybrid_property] = set()
        skipped_descriptors: set[str] = set()
        for composite_property in mapper.composites:
            for attr in composite_property.attrs:
                if isinstance(attr, (MappedColumn, Column)):
                    skipped_descriptors.add(attr.name)
                elif isinstance(attr, str):
                    skipped_descriptors.add(attr)
        for key, orm_descriptor in mapper.all_orm_descriptors.items():
            if is_hybrid_property := isinstance(orm_descriptor, hybrid_property):
                if orm_descriptor in seen_hybrid_descriptors:
                    continue

                seen_hybrid_descriptors.add(orm_descriptor)

            if key in skipped_descriptors:
                continue

            should_skip_descriptor = False
            dto_field: DTOField | None = None
            if hasattr(orm_descriptor, "property"):
                dto_field = orm_descriptor.property.info.get(DTO_FIELD_META_KEY)  # pyright: ignore  # noqa: PGH003

            # Case 1
            is_field_marked_not_private = dto_field and dto_field.mark is not Mark.PRIVATE

            # Case 2
            should_exclude_anything_implicit = not include_implicit_fields and key not in model_type_hints

            # Case 3
            should_exclude_non_hybrid_only = (
                not is_hybrid_property and include_implicit_fields == "hybrid-only" and key not in model_type_hints
            )

            # Descriptor is marked with with either Mark.READ_ONLY or Mark.WRITE_ONLY (see Case 1):
            # - always include it regardless of anything else.
            # Descriptor is not marked:
            # - It's implicit BUT config excludes anything implicit (see Case 2): exclude
            # - It's implicit AND not hybrid BUT config includes hybrid-only implicit descriptors (Case 3): exclude
            should_skip_descriptor = not is_field_marked_not_private and (
                should_exclude_anything_implicit or should_exclude_non_hybrid_only
            )

            if should_skip_descriptor:
                continue

            yield from cls.handle_orm_descriptor(
                orm_descriptor.extension_type,
                key,
                orm_descriptor,
                model_type_hints,
                model_name,
            )

    @classmethod
    def detect_nested_field(cls, field_definition: FieldDefinition) -> bool:
        return field_definition.is_subclass_of(DeclarativeBase)


def _detect_defaults(elem: ElementType) -> tuple[Any, Any]:
    default: Any = Empty
    default_factory: Any = None  # pyright:ignore  # noqa: PGH003
    if sqla_default := getattr(elem, "default", None):
        if sqla_default.is_scalar:
            default = sqla_default.arg
        elif sqla_default.is_callable:

            def default_factory(d: Any = sqla_default) -> Any:
                return d.arg({})

        elif sqla_default.is_sequence or sqla_default.is_sentinel:
            # SQLAlchemy sequences represent server side defaults
            # so we cannot infer a reasonable default value for
            # them on the client side
            pass
        else:
            msg = "Unexpected default type"
            raise ValueError(msg)
    elif (
        isinstance(elem, RelationshipProperty)
        and detect_nullable_relationship(elem)
        or getattr(elem, "nullable", False)
    ):
        default = None

    return default, default_factory


def parse_type_from_element(elem: ElementType) -> FieldDefinition:
    """Parses a type from a SQLAlchemy element.

    Args:
        elem: The SQLAlchemy element to parse.

    Returns:
        FieldDefinition: The parsed type.

    Raises:
        ImproperlyConfiguredException: If the type cannot be parsed.
    """

    if isinstance(elem, Column):
        if elem.nullable:
            return FieldDefinition.from_annotation(Optional[elem.type.python_type])
        return FieldDefinition.from_annotation(elem.type.python_type)

    if isinstance(elem, RelationshipProperty):
        if elem.direction in (RelationshipDirection.ONETOMANY, RelationshipDirection.MANYTOMANY):
            collection_type = FieldDefinition.from_annotation(elem.collection_class or list)
            return FieldDefinition.from_annotation(collection_type.safe_generic_origin[elem.mapper.class_])

        if detect_nullable_relationship(elem):
            return FieldDefinition.from_annotation(Optional[elem.mapper.class_])

        return FieldDefinition.from_annotation(elem.mapper.class_)

    if isinstance(elem, CompositeProperty):
        return FieldDefinition.from_annotation(elem.composite_class)

    msg = f"Unable to parse type from element '{elem}'. Consider adding a type hint."  # type: ignore[unreachable]
    raise ImproperConfigurationError(
        msg,
    )


def detect_nullable_relationship(elem: RelationshipProperty) -> bool:
    """Detects if a relationship is nullable.

    This attempts to decide if we should allow a ``None`` default value for a relationship by looking at the
    foreign key fields. If all foreign key fields are nullable, then we allow a ``None`` default value.

    Args:
        elem: The relationship to check.

    Returns:
        bool: ``True`` if the relationship is nullable, ``False`` otherwise.
    """
    return elem.direction == RelationshipDirection.MANYTOONE and all(c.nullable for c in elem.local_columns)
