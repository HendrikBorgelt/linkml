import logging
import os
import string
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

import click
from jsonasobj2 import JsonObj, as_dict
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.collection import Collection
from rdflib.namespace import RDF, RDFS, SH, XSD
from rdflib.term import Node

from linkml._version import __version__
from linkml.generators.common.annotations import declared_annotation
from linkml.generators.common.subproperty import get_subproperty_values
from linkml.generators.shacl.shacl_data_type import ShaclDataType
from linkml.generators.shacl.shacl_ifabsent_processor import ShaclIfAbsentProcessor
from linkml.generators.shacl.shacl_shared_uris import (
    NodeShapeSpec,
    PropertyGroup,
    RolePlan,
    SharedClassUriGroup,
    cardinality_bounds,
    group_slots_by_path,
    is_ordered_list,
    plan_role_shapes,
    relaxed_constraints,
    role_union_shape_name,
    shape_slots,
    slot_path,
    slot_ranges,
    slot_value_key,
    subsumed_slots,
)
from linkml.utils.generator import Generator, shared_arguments
from linkml.utils.language_tags import LanguageTagResolver
from linkml_runtime.linkml_model.meta import (
    AnonymousSlotExpression,
    ClassDefinition,
    ElementName,
    PresenceEnum,
    SlotDefinition,
)
from linkml_runtime.utils.rdf_canonicalize import canonicalize_rdf_graph
from linkml_runtime.utils.rdf_terms import TermKind, type_term, value_term
from linkml_runtime.utils.yamlutils import TypedNode, extended_float, extended_int, extended_str

logger = logging.getLogger(__name__)


MESSAGE_TEMPLATE_FIELDS = ("name", "title", "description", "comments", "class", "path")
"""Placeholders permitted in ``--message-template`` (see :attr:`ShaclGenerator.message_template`)."""


def _validate_message_template(template: str) -> None:
    """Validate a ``--message-template`` string, failing fast with a helpful error.

    Only the bare placeholders in :data:`MESSAGE_TEMPLATE_FIELDS` are permitted.
    Attribute access (``{name.foo}``), indexing (``{name[0]}``), positional fields
    (``{0}`` / ``{}``), conversions (``{name!r}``) and format specs (``{name:>5}``)
    are all rejected, as are unbalanced braces. Validation runs once, up front, so a
    malformed template is caught even for schemas that contain no slots.

    :param template: the raw template string.
    :raises ValueError: if the template contains an unsupported placeholder or is
        otherwise malformed.
    """
    allowed = frozenset(MESSAGE_TEMPLATE_FIELDS)
    hint = "Allowed placeholders: " + ", ".join(f"{{{name}}}" for name in MESSAGE_TEMPLATE_FIELDS)
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise ValueError(f"Invalid placeholder in --message-template ({exc}). {hint}") from None
    for _literal_text, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name not in allowed:
            raise ValueError(f"Invalid placeholder '{{{field_name}}}' in --message-template. {hint}")
        if conversion is not None or format_spec:
            raise ValueError(
                f"Invalid placeholder '{{{field_name}}}' in --message-template: "
                f"conversions and format specs are not supported. {hint}"
            )


@dataclass
class ShaclGenerator(Generator):
    """Generate SHACL (Shapes Constraint Language) shapes from a LinkML schema.

    SHACL shapes are used to validate RDF data. Each LinkML class is converted
    to a ``sh:NodeShape`` with property constraints derived from the class's slots.

    **Shape naming modes.** The generator supports two naming modes controlled by ``use_class_uri_names``:

    **Default mode** (``use_class_uri_names=True``):
        Shapes are named using the ``class_uri``. If multiple LinkML classes share
        the same ``class_uri``, they are emitted into a single shape.
        This is the long-standing LinkML default: the class IRI doubles as the shape
        IRI and as the shape's ``sh:targetClass``.

        Example: LinkML classes ``Customer`` and ``VipCustomer`` (``is_a: Customer``), both
        with ``class_uri schema:Person``, produce a single shape ``<schema:Person>``.

    **Native names mode** (``use_class_uri_names=False``):
        Shapes are named using the native LinkML class name from the schema.
        Each LinkML class produces a distinct shape, even if they share a ``class_uri``.

        Example: The same two classes produce two shapes, ``Customer`` and ``VipCustomer``.
        ``Customer`` keeps ``sh:targetClass schema:Person``; ``VipCustomer`` becomes a role
        shape, checked where a slot's range is ``VipCustomer`` (see below).

    Use native names mode when multiple LinkML classes intentionally map to the
    same external ontology class and you need distinct validation shapes per class.
    See `#3011 <https://github.com/linkml/linkml/issues/3011>`_ for background.

    **Closed and open shapes.** A closed shape (``sh:closed true``) rejects every property it does not declare.
    A class's own ``extra_slots`` decides, as in the JSON Schema generator:

    * ``allowed: true``: open;
    * ``allowed: false``, or neither ``allowed`` nor ``range_expression``: closed;
    * a ``range_expression`` without ``allowed``: open, and a warning is logged. SHACL Core cannot constrain the
      properties a shape does not declare, so the expression is not enforced (a SHACL-SPARQL ``sh:sparql``
      constraint could, but is not generated).

    Without ``extra_slots``, :attr:`closed` (``--closed``/``--non-closed``) decides, and the shapes of mixins and
    abstract classes are open. ``extra_slots`` is not inherited: the metamodel does not pass it down, see
    `#3887 <https://github.com/linkml/linkml/issues/3887>`_ and `#4145 <https://github.com/linkml/linkml/issues/4145>`_.
    Nor does it loosen the shapes of a class's ancestors: a node checked against a closed ancestor shape, for
    instance through ``sh:node`` or through RDFS inference, must still do without undeclared properties.

    A closed shape gets ``sh:ignoredProperties``: ``rdf:type`` and the properties of its descendants (``is_a`` and
    mixins), so that their instances pass it. Open shapes do not get it, as it would have no effect
    (`#3539 <https://github.com/linkml/linkml/issues/3539>`_). They say ``sh:closed false`` only with ``--closed``,
    where it marks them as open on purpose; with ``--non-closed`` they carry no ``sh:closed``, as SHACL shapes are
    open by default.

    **Classes sharing a class_uri.** RDF cannot tell such classes apart: a node typed with the ``class_uri`` is
    checked against every shape that targets it. With class_uri names the classes are emitted into one shape,
    with one ``sh:targetClass``, one property shape per predicate (see below) and the ``sh:ignoredProperties`` of
    all of them; it is closed only if each of the classes would be.

    With native names, separate closed shapes with the same target could never all be satisfied, so only the
    group's *root* (the class without an ancestor in the group) keeps ``sh:targetClass``. The other classes
    become *role shapes*: shapes without a target, checked only where a slot's range names them, through
    ``sh:class <class_uri>`` plus ``sh:node <role shape>``. The root's shape then applies to every node of the
    ``class_uri``. So if the group has several roots, or a class relaxes the root (``slot_usage`` lets a subclass
    accept more than its parent, or ``extra_slots`` opens a subclass of a closed root), none of its classes is
    targeted; a *role union shape* ``<local name of the class_uri>Roles`` targets the ``class_uri`` instead and
    requires a node to conform to at least one of the group's shapes. A role union shape is always open. Each of
    these cases is logged as a warning.

    **Slots sharing a slot_uri.** A property shape checks every value on its path, whichever LinkML slot the
    value was meant for. Separate property shapes for slots sharing a ``slot_uri`` would therefore contradict
    each other by design: with different ranges, every value fails the shapes of the other slots, and each
    slot's ``sh:maxCount`` counts the other slots' values too. So slots of a shape that share a ``slot_uri`` are
    emitted as one property shape, with ``sh:or`` over their ranges, and each slot keeps its own cardinality as
    a qualified count (``sh:qualifiedValueShape``). This is logged as a warning, as it is often a modelling
    accident. A second warning names a slot whose values another of the merged slots accepts anyway: its range
    and value constraints then never reject a value, and at most decide which values count towards its own
    cardinality.

    A slot with ``slot_uri: rdf:type`` shares its predicate with the node's own class assertion. Its property
    shape therefore also accepts the node's own types, and does not count them
    (`#3931 <https://github.com/linkml/linkml/issues/3931>`_).

    **Recommended slots.** ``recommended: true`` marks a slot that should be filled, but whose absence is not an
    error. With ``recommended_warnings``, such slots become constraints with ``sh:severity sh:Warning``. The
    option is off by default, because SHACL reports ``sh:conforms false`` for any result, warnings included.

    The warnings cannot sit in the class shapes themselves. A node conforms to a shape it is checked against
    through ``sh:node`` (or ``sh:or``, ``sh:not``, ...) only if that check produces no result at all, so a
    warning there would turn into a violation of the referencing shape. Each class with recommended slots
    therefore gets a separate ``<shape>Recommendations`` shape, targeted like its class shape. This matters
    most with native names: class ranges are checked with ``sh:node`` there, so class shapes are nested
    wherever a class is used as a range. With class_uri names, ranges are checked with ``sh:class`` and nothing
    is nested, but the same layout is used for consistency. Role shapes have no target, so their
    recommendations are checked through the slots that reference them: the referencing class's recommendations
    shape points (``sh:node``) at the role's recommendations, for the values of that slot only.
    """

    # ClassVars
    closed: bool = True
    """Default closedness of the shapes of classes without their own ``extra_slots``.

    If True, these shapes are closed (``sh:closed true`` with ``sh:ignoredProperties``), except for the shapes of
    mixins and abstract classes; if False, they are open. An explicit ``extra_slots`` on a class takes precedence
    over this option; see *Closed and open shapes* in the class documentation.

    Open shapes say ``sh:closed false`` only if this option is True. With False they carry no ``sh:closed`` at
    all, as SHACL shapes are open by default.
    """
    suffix: str = None
    """parameterized suffix to be appended. No suffix per default."""
    include_annotations: bool = False
    """True means include all class / slot / type annotations in generated Node or Property shapes"""
    exclude_imports: bool = False
    """If True, elements from imported ontologies won't be included in the generator's output"""
    use_class_uri_names: bool = True
    """
    Control how SHACL shape URIs are generated.

    If True (default): Shape URIs are derived from class_uri. Classes sharing a class_uri
    will be merged into a single shape.

    If False: Shape URIs use native LinkML class names. Each class gets a distinct shape
    even when sharing class_uri; of such classes only one keeps ``sh:targetClass`` (see the
    class documentation). The --suffix option still works in either mode.
    """
    expand_subproperty_of: bool = True
    """If True, expand subproperty_of to sh:in constraints with slot descendants"""

    default_language: str | None = None
    """Default BCP 47 language tag for human-readable string literals.

    When set, ``sh:name``, ``sh:description``, ``rdfs:label``, and
    ``rdfs:comment`` literals are emitted with the specified language tag.
    Conforms to :rfc:`5646` (BCP 47).
    """

    message_template: str | None = None
    """Template for ``sh:message`` on property shapes.

    When set, each property shape receives an ``sh:message`` literal built from
    this template.  The following placeholders are expanded:

    * ``{name}`` — the slot's LinkML name, exactly as written in the schema
    * ``{title}`` — the slot title (human-readable), falls back to *name*
    * ``{description}`` — the slot description, falls back to empty string
    * ``{comments}`` — the slot comments joined with ``; ``, falls back to empty string
    * ``{class}`` — the enclosing class name
    * ``{path}``  — the fully-expanded property IRI

    Example: ``"Validation of {name} failed!"`` →
    ``sh:message "Validation of has_speed failed!"``

    If ``default_language`` is set the literal is tagged with it. The message text
    is a single template, so it deliberately follows ``default_language`` only and
    ignores any per-slot ``in_language``.
    """

    diff_stable: bool = False
    """Reduce blank-node label churn. See :ref:`rdf-in-version-control`."""

    emit_rules: bool = True
    """Emit ``sh:sparql`` constraints from LinkML ``rules:`` blocks.

    When ``True`` (default), recognised rule patterns are translated into
    SHACL-SPARQL constraints (``sh:SPARQLConstraint``) on the corresponding
    ``sh:NodeShape``.  Currently two patterns are recognised:

    * *Boolean guard* — a precondition with ``value_presence: PRESENT`` on a
      value slot and a postcondition with ``equals_string: "true"`` on a
      boolean flag slot.
    * *Exclusive value* — a precondition with ``equals_string`` on a slot and
      a postcondition with ``maximum_cardinality`` on the *same* slot.

    See `W3C SHACL §5 <https://www.w3.org/TR/shacl/#sparql-constraints>`_
    and `linkml/linkml#2464 <https://github.com/linkml/linkml/issues/2464>`_.
    """
    recommended_warnings: bool = False
    """Emit slots marked ``recommended: true`` as ``sh:Warning`` constraints, in separate
    ``<shape>Recommendations`` shapes. Off by default; see *Recommended slots* in the class
    documentation for why, and for how the shapes are built.
    """

    include_range_class_descendants: bool = False
    """Accept instances typed with the ``class_uri`` of a subclass wherever a class is a slot's range.

    SHACL without RDFS inference does not know the subclass relation, so by default an instance of
    ``SubstanceSample`` (``SIO:001378``) is rejected where the range is ``EvaluatedEntity`` (``prov:Entity``)
    unless it is also typed ``prov:Entity``. With this option ``sh:class`` becomes an ``sh:or`` over the range
    class and all of its descendants (``is_a`` and mixins).

    With native names, ranges are checked with ``sh:node`` and only the role shapes of classes sharing a
    ``class_uri`` get ``sh:class`` too, so the option only affects those.
    """

    generatorname = os.path.basename(__file__)
    generatorversion = "0.0.1"
    valid_formats = ["ttl"]
    file_extension = "shacl.ttl"
    visit_all_class_slots = False
    uses_schemaloader = False

    def _resolve_language(self, element=None) -> str | None:
        """Return the BCP 47 language tag for *element*, or ``None``.

        Delegates to :class:`linkml.utils.language_tags.LanguageTagResolver`.
        Resolution order is element-level ``in_language`` first, then the
        generator-level default.
        """
        return self._language_resolver.resolve(element)

    def __post_init__(self) -> None:
        # Resolver must be assigned before ``super().__post_init__()`` so that
        # any hook the parent invokes during initialisation can safely call
        # ``_resolve_language``. The resolver also validates the default tag
        # once here; per-element tags are validated lazily, with at most one
        # warning per distinct malformed tag.
        self._language_resolver = LanguageTagResolver(self.default_language)
        # Role shapes of classes sharing a class_uri; worked out per schema in as_graph()
        self._roles = RolePlan()
        super().__post_init__()
        self.message_template = (self.message_template or "").strip() or None
        if self.message_template is not None:
            _validate_message_template(self.message_template)
        self.generate_header()

    def generate_header(self) -> str:
        out = f"\n# metamodel_version: {self.schema.metamodel_version}"
        if self.schema.version:
            out += f"\n# version: {self.schema.version}"
        return out

    def serialize(self, **args) -> str:
        g = self.as_graph()
        fmt = "turtle" if self.format in ["owl", "ttl"] else self.format
        return canonicalize_rdf_graph(g, output_format=fmt, diff_stable=self.diff_stable)

    def as_graph(self) -> Graph:
        """Build the SHACL shapes graph.

        One node shape is added per shape IRI (see :meth:`_node_shapes`), then the role union shapes and, with
        :attr:`recommended_warnings`, the recommendation shapes.
        """
        sv = self.schemaview
        g = Graph()
        g.bind("sh", SH)
        for pfx in self.schema.prefixes.values():
            g.bind(str(pfx.prefix_prefix), pfx.prefix_reference)

        classes = list(sv.all_classes(imports=not self.exclude_imports).values())
        self._warn_about_unenforced_extra_slots(classes)
        if self.use_class_uri_names:
            self._roles = RolePlan()
        else:
            self._roles = plan_role_shapes(
                sv,
                [cls.name for cls in classes],
                closed_classes={cls.name for cls in classes if self._is_closed(cls)},
                open_classes={cls.name for cls in classes if self._accepts_undeclared_properties(cls)},
            )
        self._warn_about_role_groups()

        ifabsent_processor = ShaclIfAbsentProcessor(sv)
        node_shapes = self._node_shapes()
        for shape in node_shapes:
            self._add_node_shape(g, shape, ifabsent_processor)
        self._add_role_union_shapes(g)
        if self.recommended_warnings:
            self._add_recommendation_shapes(g, node_shapes)
        return g

    # -------------------------------------------------------------------
    # Node shapes
    # -------------------------------------------------------------------

    def _node_shapes(self) -> list[NodeShapeSpec]:
        """The node shapes to emit: classes grouped by shape IRI, with the slots of each shape grouped by predicate.

        With class_uri names, classes sharing a ``class_uri`` share a shape IRI and are emitted into one shape.
        Their slots are combined (see :func:`shape_slots`), so that the shape still has one property shape per
        predicate. With native names every class has a shape of its own.
        """
        sv = self.schemaview
        classes_by_shape: dict[URIRef, list[ClassDefinition]] = {}
        for cls in sv.all_classes(imports=not self.exclude_imports).values():
            classes_by_shape.setdefault(self._shape_uri(cls.name), []).append(cls)
        return [
            NodeShapeSpec(uri, classes, group_slots_by_path(sv, shape_slots(sv, [cls.name for cls in classes])))
            for uri, classes in classes_by_shape.items()
        ]

    def _shape_uri(self, class_name: ElementName) -> URIRef:
        """IRI of the node shape of class *class_name*: its ``class_uri`` or, with native names, its native IRI.

        The suffix is appended in both cases.
        """
        uri = self.schemaview.get_uri(class_name, expand=True, native=not self.use_class_uri_names)
        return URIRef(uri + (self.suffix or ""))

    def _is_closed(self, cls: ClassDefinition) -> bool:
        """Whether *cls* on its own gets a closed shape.

        The class's own ``extra_slots`` decides if it has one (see :func:`_extra_slots_closed`), whatever
        :attr:`closed` says. Otherwise :attr:`closed` decides, except that mixins and abstract classes are open.
        """
        closed = _extra_slots_closed(cls)
        if closed is None:
            closed = self.closed and not cls.mixin and not cls.abstract
        return bool(closed)

    def _accepts_undeclared_properties(self, cls: ClassDefinition) -> bool:
        """Whether a node of *cls* may carry properties that the schema does not declare for the class.

        That is the case if its shape is open, unless it is open only for being a mixin or abstract: such a class
        has no nodes of its own; they are instances of other classes, which decide for themselves.
        """
        if cls.extra_slots is None and (cls.mixin or cls.abstract):
            return False
        return not self._is_closed(cls)

    @staticmethod
    def _warn_about_unenforced_extra_slots(classes: list[ClassDefinition]) -> None:
        """Warn about classes whose ``extra_slots`` accept extra data matching a ``range_expression``.

        SHACL Core can constrain the values of a given path, but not "every property the shape does not declare",
        so the shapes of these classes are left open and the expression is not enforced.
        """
        # A SHACL-SPARQL constraint (sh:sparql) selecting the values of all undeclared predicates could enforce
        # the range_expression; it is not implemented.
        for cls in classes:
            extra_slots = cls.extra_slots
            if extra_slots is not None and extra_slots.allowed is None and extra_slots.range_expression:
                logger.warning(
                    "Class %s allows extra slots that match a range_expression. SHACL Core cannot constrain "
                    "properties that a shape does not declare, so its shape is left open and the range_expression "
                    "is not enforced.",
                    cls.name,
                )

    def _add_node_shape(self, g: Graph, shape: NodeShapeSpec, ifabsent_processor: ShaclIfAbsentProcessor) -> None:
        """Add the node shape described by *shape*, with one property shape per predicate and its classes' rules.

        A shape of several classes is closed only if each of the classes would be, and its
        ``sh:ignoredProperties`` combine theirs. Only closed shapes get ``sh:ignoredProperties``: an open shape
        accepts every property anyway (`#3539 <https://github.com/linkml/linkml/issues/3539>`_). ``sh:closed false``
        is only stated with :attr:`closed` (see there).
        """
        sv = self.schemaview
        shape_pv = _triple_adder(g, shape.uri)
        shape_pv(RDF.type, SH.NodeShape)
        if all(self._is_closed(cls) for cls in shape.classes):
            shape_pv(SH.closed, Literal(True))
            shape_pv(SH.ignoredProperties, self._build_ignored_properties(g, shape.classes))
        elif self.closed:
            # With closed shapes requested, say that this one (a mixin, an abstract class, or a class opened by
            # extra_slots) is open on purpose
            shape_pv(SH.closed, Literal(False))
        for cls in shape.classes:
            if cls.name not in self._roles.role_classes:
                shape_pv(SH.targetClass, URIRef(sv.get_uri(cls, expand=True)))
            if cls.title is not None:
                # Use rdfs:label for NodeShape titles per SHACL spec.
                # sh:name has rdfs:domain of sh:PropertyShape. See issue #3059.
                shape_pv(RDFS.label, Literal(cls.title, lang=self._resolve_language(cls)))
            if cls.description is not None:
                # Use rdfs:comment for NodeShape descriptions per SHACL spec.
                # sh:description has rdfs:domain of sh:PropertyShape, so using it
                # on NodeShapes causes RDFS-aware validators to incorrectly infer
                # the NodeShape is also a PropertyShape. See issue #3059.
                shape_pv(RDFS.comment, Literal(cls.description, lang=self._resolve_language(cls)))
            if cls.annotations and self.include_annotations:
                self._add_annotations(shape_pv, cls)

        for order, group in enumerate(shape.property_groups):
            if group.is_merged:
                self._warn_about_merged_slots(shape, group)
                self._warn_about_subsumed_slots(shape, group)
            implicit_types = self._implicit_type_uris(shape.classes) if group.path == RDF.type else None
            self._add_property_shape(g, shape.uri, group, order, implicit_types, ifabsent_processor)

        if self.emit_rules:
            for cls in shape.classes:
                self._add_rules(g, shape.uri, cls)

    def _build_ignored_properties(self, g: Graph, classes: list[ClassDefinition]) -> BNode:
        """``sh:ignoredProperties`` of a closed node shape: ``rdf:type`` and the properties only descendants have.

        A closed shape rejects every property it does not list; ignoring the properties of descendants lets an
        instance of a subclass pass the shape of its parent. Descendants through mixins count too: a mixin made
        closed with ``extra_slots`` would otherwise reject every class that uses it. Open shapes get no such
        list, as it would have no effect on them.
        """
        sv = self.schemaview
        ignored = {RDF.type}
        for cls in classes:
            own_properties = {URIRef(sv.get_uri(slot, expand=True)) for slot in sv.class_slots(cls.name)}
            subclass_properties = {
                URIRef(sv.get_uri(slot, expand=True))
                for subclass in sv.class_descendants(cls.name, imports=True, mixins=True, reflexive=False)
                for slot in sv.class_slots(subclass)
            }
            ignored |= subclass_properties - own_properties
        return _rdf_list(g, sorted(ignored, key=str))

    # -------------------------------------------------------------------
    # Property shapes
    # -------------------------------------------------------------------

    def _add_property_shape(
        self,
        g: Graph,
        shape_uri: URIRef,
        group: PropertyGroup,
        order: int,
        implicit_types: list[URIRef] | None,
        ifabsent_processor: ShaclIfAbsentProcessor,
    ) -> None:
        """Add the property shape of *group*: a single slot, or several slots merged because they share a predicate.

        :param implicit_types: only for an ``rdf:type`` slot: the class IRIs a node carries as its own type (see
            :meth:`_implicit_type_uris`). They are accepted as values and not counted.
        """
        sv = self.schemaview
        s = group.combined_slot()
        pnode = BNode()
        g.add((shape_uri, SH.property, pnode))
        prop_pv = _triple_adder(g, pnode)

        def prop_pv_literal(p, v):
            if v is not None:
                g.add((pnode, p, Literal(v)))

        def prop_pv_text(p, v):
            if v is not None:
                g.add((pnode, p, Literal(v, lang=self._resolve_language(s))))

        prop_pv(SH.path, self._value_path(g, group.path, s))
        prop_pv_literal(SH.order, order)
        prop_pv_text(SH.name, s.title)
        prop_pv_text(SH.description, s.description)

        # sh:message from a user template. The template is validated once in
        # __post_init__, so expansion here cannot raise. The message is a single
        # template string, so it is tagged with the generator default language
        # only (via _resolve_language(None)) and ignores per-slot in_language.
        if self.message_template is not None:
            msg_text = self.message_template.format(
                name=s.name,
                title=s.title or s.name,
                description=s.description or "",
                comments="; ".join(s.comments) if s.comments else "",
                **{"class": s.owner},
                path=str(group.path),
            ).strip()
            if msg_text:
                g.add((pnode, SH.message, Literal(msg_text, lang=self._resolve_language(None))))

        min_count, max_count = cardinality_bounds(s)
        if is_ordered_list(s):
            # The path of an ordered list is a SHACL sequence path over rdf:List members; SHACL value
            # nodes are a *set*, so sh:minCount/sh:maxCount would count distinct member values, not list
            # length -- a false violation on (a a b) with minimum_cardinality 3, a false pass on (a a a a)
            # with maximum_cardinality 3. We therefore only assert non-emptiness (sh:minCount 1) for
            # ordered lists and skip length-based counts. See #3531 / PR #3693 review.
            if s.required or s.minimum_cardinality or s.exact_cardinality:
                prop_pv_literal(SH.minCount, 1)
        elif implicit_types:
            self._add_explicit_type_counts(g, pnode, min_count, max_count, implicit_types)
        else:
            prop_pv_literal(SH.minCount, min_count)
            prop_pv_literal(SH.maxCount, max_count)

        if s.any_of:
            # It is not allowed to use any of and equals_string or equals_string_in in one
            # slot definition, as both are mapped to sh:in in SHACL
            if s.equals_string or s.equals_string_in:
                error = "'equals_string'/'equals_string_in' and 'any_of' are mutually exclusive"
                raise ValueError(f"{TypedNode.yaml_loc(str(s), suffix='')} {error}")
            prop_pv(SH["or"], _rdf_list(g, self._or_branches(g, group)))
            prop_pv_literal(SH.minInclusive, s.minimum_value)
            prop_pv_literal(SH.maxInclusive, s.maximum_value)
            # A slot-level pattern applies to every value, whichever branch it matches
            if s.pattern:
                prop_pv(SH.pattern, Literal(s.pattern))
        else:
            r = s.range
            if implicit_types and r in sv.all_classes():
                self._add_rdf_type_slot_range(g, prop_pv, r, implicit_types, s.owner)
            else:
                self._add_range(g, prop_pv, r, s.owner)
            if r in sv.all_classes() and not self._is_linkml_any(r):
                prop_pv(SH.nodeKind, SH.IRI if sv.get_identifier_slot(r) is not None else SH.BlankNodeOrIRI)
            self._add_value_constraints(g, pnode, s)

        if s.annotations and self.include_annotations:
            self._add_annotations(prop_pv, s)

        default_value = ifabsent_processor.process_slot(s, sv.get_class(s.owner))
        if default_value:
            if s.range in sv.all_types() and type_term(sv, s.range).kind.is_node:
                # The default is a value of the slot, so it has the slot's term kind.
                default_value = value_term(sv, s.range, str(default_value))
            prop_pv(SH.defaultValue, default_value)

        if group.is_merged:
            self._add_merged_slot_cardinalities(g, shape_uri, group)

    @staticmethod
    def _value_path(g: Graph, path: URIRef, slot: SlotDefinition) -> Node:
        """``sh:path`` that reaches *slot*'s values: the predicate, or a sequence path for an ordered list.

        Ordered multivalued slots are serialized as an rdf:List (see issue #3531), so the value constraints
        must apply to each list member rather than to the list head. The sequence path steps through the
        slot, walks the rdf:rest spine, then reads each rdf:first:
        ``sh:path ( <slot> [ sh:zeroOrMorePath rdf:rest ] rdf:first )``.
        """
        if not is_ordered_list(slot):
            return path
        rest_path = BNode()
        g.add((rest_path, SH.zeroOrMorePath, RDF.rest))
        return _rdf_list(g, [path, rest_path, RDF.first])

    def _add_value_constraints(self, g: Graph, node: Node, slot: SlotDefinition) -> None:
        """Add what *slot* demands of each value to *node*: bounds, fixed values, pattern and ``subproperty_of``."""
        add = _triple_adder(g, node)
        if slot.minimum_value is not None:
            add(SH.minInclusive, Literal(slot.minimum_value))
        if slot.maximum_value is not None:
            add(SH.maxInclusive, Literal(slot.maximum_value))
        if slot.equals_number is not None:
            add(SH.hasValue, Literal(slot.equals_number))
        if slot.pattern:
            add(SH.pattern, Literal(slot.pattern))
        if slot.equals_string or slot.equals_string_in:
            if slot.range != "string":
                raise ValueError(
                    f"slot: \"{slot.name}\" - 'equals_string' and 'equals_string_in' require range 'string' "
                    f"and not '{slot.range}'"
                )
            # equals_string and equals_string_in each map to an sh:in
            if slot.equals_string:
                add(SH["in"], _rdf_list(g, [Literal(slot.equals_string)]))
            if slot.equals_string_in:
                add(SH["in"], _rdf_list(g, [Literal(value) for value in slot.equals_string_in]))
        if self.expand_subproperty_of and slot.subproperty_of:
            subproperty_values = self._get_subproperty_values(slot)
            if subproperty_values:
                add(SH["in"], _rdf_list(g, subproperty_values))

    def _or_branches(self, g: Graph, group: PropertyGroup) -> list[BNode]:
        """Branches of the ``sh:or`` of *group*'s property shape.

        A single slot has one branch per ``any_of`` range. Merged slots have one branch per slot, which also
        carries that slot's value constraints, so that a ``pattern`` (say) only applies to the values of the slot
        that declares it. Slots that would give identical branches get one.
        """
        if not group.is_merged:
            slot = group.slots[0]
            return [self._any_of_branch_shape(g, expression, slot.owner) for expression in slot.any_of]
        distinct: dict[tuple, SlotDefinition] = {}
        for slot in group.slots:
            distinct.setdefault(slot_value_key(slot), slot)
        return [self._slot_value_shape(g, slot) for slot in distinct.values()]

    def _any_of_branch_shape(self, g: Graph, expression: AnonymousSlotExpression, referencing_class: str) -> BNode:
        """Shape of one ``any_of`` branch: its range and, if it has one, its own pattern.

        A branch may combine range and pattern (e.g. ``range: string`` with ``pattern: "^..."``) or specify a
        pattern alone (no range).
        """
        node = self._range_shape(g, expression.range, referencing_class)
        if expression.pattern:
            g.add((node, SH.pattern, Literal(expression.pattern)))
        return node

    def _slot_value_shape(self, g: Graph, slot: SlotDefinition) -> BNode:
        """Shape of the values *slot* accepts: its range (or its ``any_of`` branches) and its value constraints."""
        if slot.any_of:
            range_shapes = [self._any_of_branch_shape(g, expression, slot.owner) for expression in slot.any_of]
        else:
            range_shapes = [self._range_shape(g, slot.range, slot.owner)]
        if len(range_shapes) == 1:
            node = range_shapes[0]
        else:
            node = BNode()
            g.add((node, SH["or"], _rdf_list(g, range_shapes)))
        self._add_value_constraints(g, node, slot)
        return node

    def _add_merged_slot_cardinalities(self, g: Graph, shape_uri: URIRef, group: PropertyGroup) -> None:
        """Keep the cardinality of each merged slot, counting only the values that the slot itself accepts.

        The merged property shape has no counts of its own: each value may belong to any of the slots. Instead,
        each slot with a cardinality gets a property shape on the same path with ``sh:qualifiedValueShape``
        (its :meth:`_slot_value_shape`) and ``sh:qualifiedMinCount``/``sh:qualifiedMaxCount``.
        """
        for slot in group.slots:
            min_count, max_count = cardinality_bounds(slot)
            if not min_count and max_count is None:
                continue
            pnode = BNode()
            g.add((shape_uri, SH.property, pnode))
            g.add((pnode, SH.path, group.path))
            g.add((pnode, SH.name, Literal(slot.name)))
            g.add((pnode, SH.qualifiedValueShape, self._slot_value_shape(g, slot)))
            if min_count:
                g.add((pnode, SH.qualifiedMinCount, Literal(min_count)))
            if max_count is not None:
                g.add((pnode, SH.qualifiedMaxCount, Literal(max_count)))

    @staticmethod
    def _warn_about_merged_slots(shape: NodeShapeSpec, group: PropertyGroup) -> None:
        """Warn that slots sharing a predicate were merged: satisfiable SHACL, but likely not what the schema means."""
        other_fix = " (or their classes distinct class_uris)" if len(shape.classes) > 1 else ""
        logger.warning(
            "Slots %s share slot_uri %s, so shape %s checks them in one property shape: their ranges are combined "
            "with sh:or and each slot's cardinality becomes a qualified count. This may not be what you intended; "
            "consider giving the slots distinct slot_uris%s.",
            [f"{slot.owner}.{slot.name}" for slot in group.slots],
            group.path,
            shape.uri,
            other_fix,
        )

    def _warn_about_subsumed_slots(self, shape: NodeShapeSpec, group: PropertyGroup) -> None:
        """Warn about merged slots whose constraints never reject a value, because another slot accepts it anyway.

        See :func:`subsumed_slots`. Such a slot's constraints still decide which values count towards its
        cardinality, if it has one.
        """
        sv = self.schemaview
        range_classes = [sv.get_class(r) for slot in group.slots for r in slot_ranges(slot) if r and sv.get_class(r)]
        subsumed = subsumed_slots(
            sv,
            group.slots,
            self.use_class_uri_names,
            closed_classes={cls.name for cls in range_classes if self._is_closed(cls)},
            open_classes={cls.name for cls in range_classes if self._accepts_undeclared_properties(cls)},
        )
        for narrow, broad in subsumed:
            min_count, max_count = cardinality_bounds(narrow)
            if min_count or max_count is not None:
                consequence = "never reject a value; they only decide which values count towards its cardinality"
            else:
                consequence = "are never enforced"
            logger.warning(
                "Shape %s: every value that slot %s accepts is also accepted by slot %s (both on %s), so the range "
                "and value constraints of %s %s. Give %s its own slot_uri if they should apply to every value.",
                shape.uri,
                f"{narrow.owner}.{narrow.name}",
                f"{broad.owner}.{broad.name}",
                group.path,
                narrow.name,
                consequence,
                narrow.name,
            )

    # -------------------------------------------------------------------
    # Classes sharing a class_uri: role shapes (native names only)
    # -------------------------------------------------------------------

    def _warn_about_role_groups(self) -> None:
        """Explain what happens to each group of classes sharing a ``class_uri`` and warn where it cannot work."""
        sv = self.schemaview
        for group in self._roles.groups:
            if len(group.roots) > 1:
                logger.warning(
                    "Classes %s share class_uri %s without a common parent, so none of them gets sh:targetClass. "
                    "The role union shape %s targets the class instead and requires a node to conform to one of "
                    "the shapes of %s. This is probably not intended: give the classes a common parent or "
                    "distinct class_uris.",
                    list(group.roots),
                    group.class_uri,
                    self._role_union_shape_uri(group.class_uri),
                    list(group.classes),
                )
                closed_roots = [root for root in group.roots if self._is_closed(sv.get_class(root))]
                if closed_roots:
                    logger.warning(
                        "Classes %s share class_uri %s without a common parent, but %s are closed. A closed shape "
                        "rejects the properties of the other classes; open them with extra_slots (allowed: true), "
                        "make them abstract or mixins, or generate with --non-closed (which does not override "
                        "extra_slots).",
                        list(group.roots),
                        group.class_uri,
                        closed_roots,
                    )
            elif group.roles_relaxing_root:
                logger.warning(
                    "Classes %s share class_uri %s, but %s accept more than their parent %s (see the following "
                    "warnings). Targeting %s would reject their valid nodes, so none of the classes gets "
                    "sh:targetClass; the role union shape %s targets the class instead and requires a node to "
                    "conform to one of their shapes. Consider making the subclasses refine %s rather than relax it.",
                    list(group.classes),
                    group.class_uri,
                    list(group.roles_relaxing_root),
                    group.roots[0],
                    group.roots[0],
                    self._role_union_shape_uri(group.class_uri),
                    group.roots[0],
                )
            else:
                logger.warning(
                    "Classes %s share class_uri %s, so only %s gets sh:targetClass. The other classes become role "
                    "shapes, checked only where a slot's range names them.",
                    list(group.classes),
                    group.class_uri,
                    group.roots[0],
                )
            self._warn_about_relaxed_roles(group)

    def _warn_about_relaxed_roles(self, group: SharedClassUriGroup) -> None:
        """Warn about classes that accept more than their parent in the group: for some slot, or by being open.

        A role is meant to be a specialisation of its parent; if it relaxes a slot, or accepts properties that its
        parent's closed shape rejects, a node can conform to the role's shape and still fail the parent's.
        """
        sv = self.schemaview
        for name in group.classes:
            for parent in sv.class_ancestors(name, imports=True, mixins=True, reflexive=False):
                if parent not in group.classes:
                    continue
                if self._accepts_undeclared_properties(sv.get_class(name)) and self._is_closed(sv.get_class(parent)):
                    logger.warning(
                        "Class %s (role of %s, class_uri %s) accepts properties the schema does not declare "
                        "(extra_slots, or --non-closed), but the shape of %s is closed and rejects them.",
                        name,
                        parent,
                        group.class_uri,
                        parent,
                    )
                parent_slots = {slot.name: slot for slot in sv.class_induced_slots(parent)}
                for slot in sv.class_induced_slots(name):
                    if slot.name not in parent_slots:
                        continue
                    for relaxation in relaxed_constraints(sv, parent_slots[slot.name], slot):
                        logger.warning(
                            "Class %s (role of %s, class_uri %s) loosens slot %s: %s. A node valid for the role "
                            "may then be invalid for its parent.",
                            name,
                            parent,
                            group.class_uri,
                            slot.name,
                            relaxation,
                        )

    def _role_union_shape_uri(self, class_uri: str) -> URIRef:
        """IRI of the role union shape for *class_uri*, in the schema's default prefix (suffix appended)."""
        sv = self.schemaview
        uri = sv.expand_curie(f"{sv.schema.default_prefix}:{role_union_shape_name(sv, class_uri)}")
        return URIRef(uri + (self.suffix or ""))

    def _add_role_union_shapes(self, g: Graph) -> None:
        """Add a role union shape for each group of classes sharing a ``class_uri`` in which no class can be targeted.

        That is a group with several roots, or one where a class relaxes the root (see :class:`RolePlan`). The
        role union shape targets the ``class_uri`` instead and requires each node to conform to at least one of the
        group's shapes. It is open and has no property shapes of its own.
        """
        for group in self._roles.groups:
            if not group.needs_role_union:
                continue
            shape_pv = _triple_adder(g, self._role_union_shape_uri(group.class_uri))
            shape_pv(RDF.type, SH.NodeShape)
            shape_pv(SH.targetClass, URIRef(group.class_uri))
            if self.closed:
                # With closed shapes requested, say that this one is open on purpose
                shape_pv(SH.closed, Literal(False))
            # rdfs:comment rather than sh:description on node shapes, see issue #3059
            shape_pv(
                RDFS.comment,
                Literal(
                    f"Generated: the classes {', '.join(group.classes)} share this class and none of them can be "
                    "targeted alone; a node must conform to at least one of their shapes."
                ),
            )
            branches = []
            for name in group.classes:
                branch = BNode()
                g.add((branch, SH.node, self._shape_uri(name)))
                branches.append(branch)
            shape_pv(SH["or"], _rdf_list(g, branches))

    # -------------------------------------------------------------------
    # Recommended slots -> sh:Warning
    # -------------------------------------------------------------------

    def _add_recommendation_shapes(self, g: Graph, node_shapes: list[NodeShapeSpec]) -> None:
        """Emit ``recommended: true`` slots as ``sh:Warning`` constraints, in ``<shape>Recommendations`` shapes.

        The warnings get shapes of their own because a node conforms to a nested shape (``sh:node``, ``sh:or``,
        ...) only if it produces no validation result at all, whatever its severity: inside a hard shape, a
        warning would fail every shape that nests it.

        A recommendations shape is targeted like its node shape. Role shapes have no target, so theirs are
        reached through the shapes that reference them: the referencing shape's recommendations get a
        warning-level property shape on the referencing slot's path, with ``sh:node`` pointing at the role's
        recommendations. They then apply only to values reached through that slot. Where the path is shared
        with other slots or the slot has several ranges, the ``sh:node`` is limited to instances of the role
        class. References that would make the shapes recursive are skipped (see
        :attr:`RolePlan.recursive_references`).
        """
        sv = self.schemaview
        shape_of_class = {cls.name: shape for shape in node_shapes for cls in shape.classes}
        recommended = {
            shape.uri: [(group, slot) for group in shape.property_groups for slot in group.slots if slot.recommended]
            for shape in node_shapes
        }
        role_references = {
            shape.uri: [
                (group, slot, r)
                for group in shape.property_groups
                for slot in group.slots
                for r in slot_ranges(slot)
                if r in self._roles.role_classes
                and r in shape_of_class
                and (slot.owner, r) not in self._roles.recursive_references
            ]
            for shape in node_shapes
        }

        # A shape needs recommendations if it has recommended slots, or references a role shape that has some.
        needed = {uri for uri, slots in recommended.items() if slots}
        changed = True
        while changed:
            changed = False
            for uri, references in role_references.items():
                if uri not in needed and any(shape_of_class[r].uri in needed for _, _, r in references):
                    needed.add(uri)
                    changed = True

        def recommendations_uri(shape_uri: URIRef) -> URIRef:
            return URIRef(str(shape_uri) + "Recommendations")

        for shape in node_shapes:
            if shape.uri not in needed:
                continue
            recommendations = recommendations_uri(shape.uri)
            g.add((recommendations, RDF.type, SH.NodeShape))
            for cls in shape.classes:
                if cls.name not in self._roles.role_classes:
                    g.add((recommendations, SH.targetClass, URIRef(sv.get_uri(cls, expand=True))))

            for group, slot in recommended[shape.uri]:
                pnode = BNode()
                g.add((recommendations, SH.property, pnode))
                g.add((pnode, SH.path, self._value_path(g, group.path, slot)))
                g.add((pnode, SH.severity, SH.Warning))
                g.add((pnode, SH.message, Literal(f"Recommended: {slot.title or slot.name}")))
                if group.is_merged:
                    g.add((pnode, SH.qualifiedValueShape, self._slot_value_shape(g, slot)))
                    g.add((pnode, SH.qualifiedMinCount, Literal(1)))
                else:
                    g.add((pnode, SH.minCount, Literal(1)))

            for group, slot, r in role_references[shape.uri]:
                role_recommendations = recommendations_uri(shape_of_class[r].uri)
                if shape_of_class[r].uri not in needed:
                    continue
                pnode = BNode()
                g.add((recommendations, SH.property, pnode))
                g.add((pnode, SH.path, self._value_path(g, group.path, slot)))
                g.add((pnode, SH.severity, SH.Warning))
                g.add((pnode, SH.message, Literal(f"Recommendations for {r} (via {slot.title or slot.name}) not met")))
                if not group.is_merged and not slot.any_of:
                    g.add((pnode, SH.node, role_recommendations))
                    continue
                # Other slots' values on this path are none of this role's business: only check instances of r.
                instance_of_role = BNode()
                self._add_instance_of(g, _triple_adder(g, instance_of_role), r)
                not_role, role_only = BNode(), BNode()
                g.add((not_role, SH["not"], instance_of_role))
                g.add((role_only, SH.node, role_recommendations))
                g.add((pnode, SH["or"], _rdf_list(g, [not_role, role_only])))

    # -------------------------------------------------------------------
    # Rules → sh:sparql
    # -------------------------------------------------------------------

    def _add_rules(self, g: Graph, shape_uri: URIRef, cls: ClassDefinition) -> None:
        """Emit ``sh:sparql`` constraints from LinkML ``rules:`` blocks.

        Each recognised rule is converted into an ``sh:SPARQLConstraint``
        attached to *shape_uri*.  Unrecognised patterns are logged at
        ``DEBUG`` level and silently skipped.

        Currently recognised patterns:

        * **Boolean guard** — a *precondition* with
          ``value_presence: PRESENT`` on a value slot and a *postcondition*
          with ``equals_string: "true"`` on a boolean flag slot.

        * **Exclusive value** — a *precondition* with ``equals_string`` on
          a slot and a *postcondition* with ``maximum_cardinality`` on the
          *same* slot.  Enforces that when a specific value is present in a
          multivalued slot, the total number of values must not exceed the
          given cardinality (typically 1 for mutual exclusion).

        See `W3C SHACL §5 <https://www.w3.org/TR/shacl/#sparql-constraints>`_.
        """
        if not cls.rules:
            return

        sv = self.schemaview
        for rule in cls.rules:
            if getattr(rule, "deactivated", False):
                continue

            if getattr(rule, "bidirectional", False):
                logger.warning(
                    "Rule in class %r has bidirectional=true; "
                    "SHACL-SPARQL generation does not support bidirectional rules. "
                    "Skipping this rule entirely.",
                    cls.name,
                )
                continue

            if getattr(rule, "open_world", False):
                logger.warning(
                    "Rule in class %r has open_world=true; "
                    "SHACL operates under closed-world assumption. "
                    "The constraint is emitted but may not match open-world semantics.",
                    cls.name,
                )

            if getattr(rule, "elseconditions", None):
                logger.warning(
                    "Rule in class %r has elseconditions; "
                    "only the forward (if/then) branch is emitted as sh:sparql. "
                    "The else branch cannot be represented in SHACL-SPARQL.",
                    cls.name,
                )

            sparql_query = self._rule_to_sparql(sv, cls, rule)
            if sparql_query is None:
                logger.debug(
                    "Skipping unsupported rule pattern in class %r: %s",
                    cls.name,
                    getattr(rule, "description", "(no description)"),
                )
                continue

            constraint = BNode()
            g.add((shape_uri, SH.sparql, constraint))
            g.add((constraint, RDF.type, SH.SPARQLConstraint))

            message = getattr(rule, "description", None)
            if message:
                g.add((constraint, SH.message, Literal(message)))

            g.add((constraint, SH.select, Literal(sparql_query)))

    def _rule_to_sparql(self, sv, cls: ClassDefinition, rule) -> str | None:
        """Convert a ``ClassRule`` to a SPARQL SELECT query string.

        Returns ``None`` when the rule does not match any supported pattern.
        """
        pre = getattr(rule, "preconditions", None)
        post = getattr(rule, "postconditions", None)
        if not pre or not post:
            return None

        pre_slots = getattr(pre, "slot_conditions", None) or {}
        post_slots = getattr(post, "slot_conditions", None) or {}

        # Pattern: boolean guard
        # preconditions: exactly one slot with value_presence PRESENT
        # postconditions: exactly one slot with equals_string "true"
        if len(pre_slots) == 1 and len(post_slots) == 1:
            pre_slot_name = next(iter(pre_slots))
            post_slot_name = next(iter(post_slots))

            pre_cond = pre_slots[pre_slot_name]
            post_cond = post_slots[post_slot_name]

            # Note: PresenceEnum.PRESENT is a PermissibleValue, but parsed schemas
            # return PresenceEnum instances — wrapping ensures type-compatible comparison.
            is_value_present = getattr(pre_cond, "value_presence", None) == PresenceEnum(PresenceEnum.PRESENT)
            is_flag_true = getattr(post_cond, "equals_string", None) == "true"

            if is_value_present and is_flag_true:
                return self._build_boolean_guard_sparql(sv, cls, post_slot_name, pre_slot_name)

            # Pattern: exclusive value
            # preconditions: slot X has equals_string (a specific enum value)
            # postconditions: same slot X has maximum_cardinality N
            # Semantics: "If value V is present in slot X, then X has at most N values."
            pre_equals = getattr(pre_cond, "equals_string", None)
            post_max_card = getattr(post_cond, "maximum_cardinality", None)

            if pre_equals is not None and post_max_card is not None and pre_slot_name == post_slot_name:
                return self._build_exclusive_value_sparql(sv, cls, pre_slot_name, pre_equals, int(post_max_card))

        return None

    def _build_boolean_guard_sparql(self, sv, cls: ClassDefinition, flag_slot_name: str, value_slot_name: str) -> str:
        """Build a SPARQL SELECT query for the boolean-guard pattern.

        The query detects violations where the value property is present
        but the boolean flag is absent or not ``true``.

        Conforms to `SHACL §5.3.1
        <https://www.w3.org/TR/shacl/#sparql-constraints-prebound>`_:
        ``$this`` is pre-bound to each focus node.
        """
        flag_uri = self._slot_uri(sv, flag_slot_name, cls)
        value_uri = self._slot_uri(sv, value_slot_name, cls)

        return (
            f"SELECT $this WHERE {{\n"
            f"    OPTIONAL {{ $this <{flag_uri}> ?flag . }}\n"
            f"    OPTIONAL {{ $this <{value_uri}> ?value . }}\n"
            f"    FILTER (\n"
            f'        ( !BOUND(?flag) || str(?flag) != "true" ) &&\n'
            f"        BOUND(?value)\n"
            f"    )\n"
            f"}}"
        )

    def _build_exclusive_value_sparql(
        self,
        sv,
        cls: ClassDefinition,
        slot_name: str,
        value_name: str,
        max_card: int,
    ) -> str | None:
        """Build a SPARQL SELECT query for the exclusive-value pattern.

        Detects violations where a specific value is present in a multivalued
        slot but the total number of values exceeds *max_card*.

        For the common case ``max_card == 1``, the query checks whether the
        exclusive value coexists with any other value (simple existence test).
        For ``max_card > 1``, a subquery counts all values and checks against
        the limit.

        The exclusive value is resolved to its full IRI via the slot's enum
        ``meaning`` field.  If the slot is not an enum or the value has no
        ``meaning``, the value is compared as a plain literal.

        Conforms to `SHACL §5.3.1
        <https://www.w3.org/TR/shacl/#sparql-constraints-prebound>`_:
        ``$this`` is pre-bound to each focus node.
        """
        slot_uri = self._slot_uri(sv, slot_name, cls)
        value_ref = self._resolve_enum_value_ref(sv, slot_name, value_name)

        if max_card == 1:
            return (
                f"SELECT $this WHERE {{\n"
                f"    $this <{slot_uri}> {value_ref} .\n"
                f"    $this <{slot_uri}> ?other .\n"
                f"    FILTER (?other != {value_ref})\n"
                f"}}"
            )

        return (
            f"SELECT $this WHERE {{\n"
            f"    $this <{slot_uri}> {value_ref} .\n"
            f"    {{\n"
            f"        SELECT $this (COUNT(?val) AS ?count)\n"
            f"        WHERE {{ $this <{slot_uri}> ?val . }}\n"
            f"        GROUP BY $this\n"
            f"        HAVING (?count > {max_card})\n"
            f"    }}\n"
            f"}}"
        )

    def _resolve_enum_value_ref(self, sv, slot_name: str, value_name: str) -> str:
        """Resolve an enum value name to a SPARQL term (IRI or literal).

        Looks up the slot's range as an enum, finds the permissible value
        matching *value_name*, and returns its ``meaning`` as a full IRI
        wrapped in angle brackets.  Falls back to a quoted literal if the
        slot is not an enum or the value lacks a ``meaning``.
        """
        slot = sv.get_slot(slot_name)
        if slot:
            range_name = slot.range
            if range_name and range_name in sv.all_enums():
                enum = sv.get_enum(range_name)
                pv = enum.permissible_values.get(value_name)
                if pv and pv.meaning:
                    iri = sv.expand_curie(pv.meaning)
                    return f"<{iri}>"
        return f'"{value_name}"'

    def _slot_uri(self, sv, slot_name: str, cls: ClassDefinition) -> str:
        """Resolve a slot name to a full IRI string for use in SPARQL queries, the same way as ``sh:path``."""
        return str(slot_path(sv, sv.get_slot(slot_name) or SlotDefinition(slot_name)))

    # -------------------------------------------------------------------
    # rdf:type slots (linkml#3931)
    #
    # A slot with slot_uri rdf:type records extra types of a node, but its predicate also carries the node's
    # own class assertion (``a <class_uri>``). The property shape of such a slot therefore accepts the node's
    # implicit types besides the slot's range, and does not count them.
    # -------------------------------------------------------------------

    def _implicit_type_uris(self, classes: list[ClassDefinition]) -> list[URIRef]:
        """IRIs a node of one of *classes* may carry as its own ``rdf:type``.

        These are the ``class_uri`` values of the classes, of their descendants and of their ancestors
        (``is_a`` and mixins): an instance of a subclass is checked against its parents' shapes as well, and a
        node reached through a role shape is typically also typed with the shared ``class_uri`` of its parent.
        """
        sv = self.schemaview
        related: set[str] = set()
        for cls in classes:
            related.update(sv.class_descendants(cls.name, imports=True, mixins=True, reflexive=True))
            related.update(sv.class_ancestors(cls.name, imports=True, mixins=True, reflexive=False))
        return sorted({URIRef(sv.get_uri(name, expand=True)) for name in related}, key=str)

    def _add_rdf_type_slot_range(
        self, g: Graph, prop_pv: Callable, r: ElementName, implicit_types: list[URIRef], referencing_class: str
    ) -> None:
        """Range of an ``rdf:type`` slot: a value is an instance of range class *r*, or one of the implicit types."""
        branches = [self._range_shape(g, r, referencing_class), self._sh_in_shape(g, implicit_types)]
        prop_pv(SH["or"], _rdf_list(g, branches))

    def _add_explicit_type_counts(
        self, g: Graph, pnode: BNode, min_count: int | None, max_count: int | None, implicit_types: list[URIRef]
    ) -> None:
        """Cardinality of an ``rdf:type`` slot, counting only values that are not implicit types.

        Plain ``sh:minCount``/``sh:maxCount`` would count the node's own class assertion too, so the bounds are
        emitted as qualified counts over ``[ sh:not [ sh:in (implicit types) ] ]``.
        """
        if min_count is None and max_count is None:
            return
        explicit_value = BNode()
        g.add((explicit_value, SH["not"], self._sh_in_shape(g, implicit_types)))
        g.add((pnode, SH.qualifiedValueShape, explicit_value))
        if min_count is not None:
            g.add((pnode, SH.qualifiedMinCount, Literal(min_count)))
        if max_count is not None:
            g.add((pnode, SH.qualifiedMaxCount, Literal(max_count)))

    @staticmethod
    def _sh_in_shape(g: Graph, values: list[URIRef]) -> BNode:
        """Blank node shape ``[ sh:in (values) ]``."""
        node = BNode()
        g.add((node, SH["in"], _rdf_list(g, values)))
        return node

    # -------------------------------------------------------------------
    # Ranges
    # -------------------------------------------------------------------

    LINKML_ANY_URI = "https://w3id.org/linkml/Any"

    def _add_range(self, g: Graph, func: Callable, r: ElementName, referencing_class: str) -> None:
        """Constrain values to range *r* (a class, type, enum or built-in datatype) in a slot of *referencing_class*."""
        sv = self.schemaview
        if r in sv.all_classes():
            self._add_class_range(g, func, r, referencing_class)
        elif r in sv.all_types():
            self._add_type(func, r)
        elif r in sv.all_enums():
            self._add_enum(g, func, r)
        else:
            add_simple_data_type(func, r)

    def _range_shape(self, g: Graph, r: ElementName, referencing_class: str) -> BNode:
        """Blank node shape holding the constraints for values of range *r* (see :meth:`_add_range`)."""
        node = BNode()
        self._add_range(g, _triple_adder(g, node), r, referencing_class)
        return node

    def _is_linkml_any(self, r: ElementName) -> bool:
        """Whether class *r* is ``linkml:Any``, the unconstrained range."""
        sv = self.schemaview
        cls = sv.get_class(r)
        if cls and getattr(cls, "class_uri", None) == "linkml:Any":
            return True
        return sv.get_uri(r, expand=True, native=not self.use_class_uri_names) == self.LINKML_ANY_URI

    def _add_class_range(self, g: Graph, func: Callable, r: ElementName, referencing_class: str) -> None:
        """Constrain values to range class *r*, in a slot of class *referencing_class*.

        * class_uri names: ``sh:class <class_uri>``, so that validators check the value's RDF type.
        * native names: ``sh:node <native shape of r>``. A native shape IRI (e.g. ``dcatapplus:Resource``) is a
          shape identifier and not an RDF class, so ``sh:class`` on it would never match. If *r* shares its
          ``class_uri`` with other classes, ``sh:class`` is added: ``sh:node`` does not check the type, and a role
          shape is only ever checked through such a reference. The ``sh:node`` is left out where it would make the
          shapes recursive (see :attr:`RolePlan.recursive_references`).

        ``linkml:Any`` adds no constraint. The suffix is part of the shape IRI that ``sh:node`` refers to.
        """
        if self._is_linkml_any(r):
            return
        if self.use_class_uri_names:
            self._add_instance_of(g, func, r)
            return
        if r in self._roles.shared_classes:
            self._add_instance_of(g, func, r)
        if (referencing_class, r) not in self._roles.recursive_references:
            func(SH["node"], self._shape_uri(r))

    def _accepted_class_uris(self, r: ElementName) -> list[URIRef]:
        """``class_uri`` of *r* and, with :attr:`include_range_class_descendants`, those of its descendants."""
        sv = self.schemaview
        uris = [URIRef(sv.get_uri(r, expand=True))]
        if self.include_range_class_descendants:
            descendants = sv.class_descendants(r, imports=True, mixins=True, reflexive=False)
            uris += sorted({URIRef(sv.get_uri(d, expand=True)) for d in descendants} - set(uris), key=str)
        return uris

    def _add_instance_of(self, g: Graph, func: Callable, r: ElementName) -> None:
        """Constrain values to instances of class *r*: ``sh:class``, or ``sh:or`` over the accepted class IRIs."""
        uris = self._accepted_class_uris(r)
        if len(uris) == 1:
            func(SH["class"], uris[0])
            return
        branches = []
        for uri in uris:
            branch = BNode()
            g.add((branch, SH["class"], uri))
            branches.append(branch)
        func(SH["or"], _rdf_list(g, branches))

    def _add_enum(self, g: Graph, func: Callable, r: ElementName) -> None:
        sv = self.schemaview
        enum = sv.get_enum(r)
        values = [
            URIRef(sv.expand_curie(pv.meaning)) if pv.meaning else Literal(pv_name)
            for pv_name, pv in enum.permissible_values.items()
        ]
        func(SH["in"], _rdf_list(g, values))

    # SHACL §4.1.3 node kinds for the node term kinds of LinkML types.
    _SH_NODE_KINDS: ClassVar[dict[TermKind, URIRef]] = {
        TermKind.IRI: SH.IRI,
        TermKind.BLANK_NODE: SH.BlankNode,
        TermKind.BLANK_NODE_OR_IRI: SH.BlankNodeOrIRI,
    }

    def _add_type(self, func: Callable, r: ElementName) -> None:
        sv = self.schemaview
        # Types can inherit URI and pattern constraints.
        rt = sv.induced_type(r)
        term = type_term(sv, r) if rt.uri else None
        if term is not None and term.kind.is_node:
            func(SH.nodeKind, self._SH_NODE_KINDS[term.kind])
        elif term is not None:
            func(SH.nodeKind, SH.Literal)
            func(SH.datatype, URIRef(term.datatype))
            if rt.pattern:
                func(SH.pattern, Literal(rt.pattern))
            if rt.annotations and self.include_annotations:
                self._add_annotations(func, rt)
        else:
            logger.error(f"No URI for type {rt.name}")

    def _get_subproperty_values(self, slot) -> list:
        """
        Get all valid values from slot hierarchy for subproperty_of constraint.

        They become an ``sh:in`` constraint, following the metamodel semantics:
        "any ontological child (related to X via an is_a relationship), is a valid
        value for the slot".

        Values take the term kind of the slot's range type:
        - IRI types (uri, uriorcurie): URIRef objects with full URIs
        - literal types (string, curie): Literal objects with slot names or CURIEs

        :param slot: SlotDefinition with subproperty_of set
        :return: List of URIRef or Literal objects for sh:in constraint
        """
        sv = self.schemaview

        # sh:in matches terms exactly (SHACL §4.8.3), so the values take the slot's term kind.
        use_uris = slot.range in sv.all_types() and type_term(sv, slot.range).kind.is_node

        # Get string values from shared utility
        # For URI ranges, get full URIs; for string ranges, get formatted names
        string_values = get_subproperty_values(sv, slot, expand_uri=True if use_uris else None)

        # Convert to RDF types
        if use_uris:
            return [URIRef(v) for v in string_values]
        else:
            return [Literal(v) for v in string_values]

    def _add_annotations(self, func: Callable, item) -> None:
        # TODO: migrate some of this logic to SchemaView
        sv = self.schemaview
        annotations = item.annotations
        # item could be a class, slot or type
        # annotation type could be dict (on types) or JsonObj (on slots)
        if type(annotations) is JsonObj:
            annotations = as_dict(annotations)
        for a in annotations.values():
            declared = declared_annotation(sv, item, a["tag"], a["value"], self._resolve_language(item))
            if declared is not None:
                func(declared.predicate, declared.value)
                continue
            # If ':' is in the tag, treat it as a CURIE, otherwise string Literal
            if ":" in a["tag"]:
                N_predicate = URIRef(sv.expand_curie(a["tag"]))
            else:
                N_predicate = Literal(a["tag"], datatype=XSD.string)
            # If the value is a string and ':' is in the value, treat it as a CURIE,
            # otherwise treat as Literal with derived XSD datatype.
            # String annotations are language-tagged when default_language is set;
            # non-string types (bool, int, float) keep their XSD datatype.
            lang = self._resolve_language(item)
            if type(a["value"]) is extended_str and ":" in a["value"]:
                N_object = URIRef(sv.expand_curie(a["value"]))
            elif isinstance(a["value"], str) and lang:
                N_object = Literal(a["value"], lang=lang)
            else:
                N_object = Literal(a["value"], datatype=self._getXSDtype(a["value"]))

            func(N_predicate, N_object)

    def _getXSDtype(self, value):
        value_type = type(value)
        if value_type is bool:
            return XSD.boolean
        elif value_type is extended_str:
            return XSD.string
        elif value_type is extended_int:
            return XSD.integer
        elif value_type is extended_float:
            # TODO: distinguish between xsd:decimal and xsd:double?
            return XSD.decimal
        else:
            return None


def _extra_slots_closed(cls: ClassDefinition) -> bool | None:
    """Whether the ``extra_slots`` of *cls* closes its shape, or ``None`` if the class has no ``extra_slots``.

    This follows the metamodel, as :meth:`JsonSchemaGenerator.get_additional_properties
    <linkml.generators.jsonschemagen.JsonSchemaGenerator.get_additional_properties>` does:

    * ``allowed: true`` opens the shape, ``allowed: false`` closes it, whatever the ``range_expression``;
    * a ``range_expression`` without ``allowed`` opens it: SHACL Core cannot constrain the properties a shape does
      not declare, so the expression is not enforced (see
      :meth:`ShaclGenerator._warn_about_unenforced_extra_slots`);
    * neither ``allowed`` nor ``range_expression`` closes it.

    Only the class's own ``extra_slots`` counts. The metamodel does not pass it down to subclasses, and documents
    an absent ``extra_slots`` as closed, so it is not inherited here; see
    `#3887 <https://github.com/linkml/linkml/issues/3887>`_ and `#4145 <https://github.com/linkml/linkml/issues/4145>`_.

    >>> _extra_slots_closed(ClassDefinition("C"))
    >>> _extra_slots_closed(ClassDefinition("C", extra_slots={"allowed": True}))
    False
    >>> _extra_slots_closed(ClassDefinition("C", extra_slots={"range_expression": {"range": "string"}}))
    False
    >>> _extra_slots_closed(ClassDefinition("C", extra_slots={}))
    True
    """
    extra_slots = cls.extra_slots
    if extra_slots is None:
        return None
    if extra_slots.allowed is not None:
        return not extra_slots.allowed
    return not extra_slots.range_expression


def add_simple_data_type(func: Callable, r: ElementName) -> None:
    for datatype in list(ShaclDataType):
        if datatype.linkml_type == r:
            func(SH.datatype, datatype.uri_ref)


def _triple_adder(g: Graph, subject: Node) -> Callable[[URIRef, Node | None], None]:
    """Function that adds ``(subject, predicate, value)`` to *g*, skipping ``None`` values."""

    def add(predicate: URIRef, value: Node | None) -> None:
        if value is not None:
            g.add((subject, predicate, value))

    return add


def _rdf_list(g: Graph, items: list[Node]) -> BNode:
    """Add an RDF list of *items* to *g* and return its head node."""
    head = BNode()
    Collection(g, head, items)
    return head


@shared_arguments(ShaclGenerator)
@click.command(name="shacl")
@click.option(
    "--closed/--non-closed",
    default=True,
    show_default=True,
    help=(
        "Use '--closed' to generate closed SHACL shapes (shapes of mixins and abstract classes stay open). "
        "Use '--non-closed' to generate open SHACL shapes, which then carry no sh:closed. "
        "A class's own extra_slots takes precedence over both."
    ),
)
@click.option(
    "-s",
    "--suffix",
    default=None,
    show_default=True,
    help="Use --suffix to append given string to SHACL class name (e. g. --suffix Shape: Person becomes PersonShape).",
)
@click.option(
    "--include-annotations/--exclude-annotations",
    default=False,
    show_default=True,
    help="Use --include-annotations to include annotations of slots, types, and classes in the generated SHACL shapes.",
)
@click.option(
    "--exclude-imports/--include-imports",
    default=False,
    show_default=True,
    help="Use --exclude-imports to exclude imported elements from the generated SHACL shapes. This is useful when "
    "extending a substantial ontology to avoid large output files.",
)
@click.option(
    "--use-class-uri-names/--use-native-names",
    default=True,
    show_default=True,
    help="If --use-class-uri-names (default), SHACL shape names are based on class_uri. "
    "If --use-native-names, SHACL shape names are based on LinkML class names from the schema file. "
    "Suffixes from the --suffix option can still be appended.",
)
@click.option(
    "--expand-subproperty-of/--no-expand-subproperty-of",
    default=True,
    show_default=True,
    help="If --expand-subproperty-of (default), slots with subproperty_of will generate sh:in constraints "
    "containing all slot descendants. Use --no-expand-subproperty-of to disable this behavior.",
)
@click.option(
    "--default-language",
    default=None,
    show_default=True,
    help=(
        "Default BCP 47 language tag for human-readable string literals "
        "(e.g. en, de, zh-Hans).  When set, sh:name, sh:description, "
        "rdfs:label and rdfs:comment are emitted with the specified "
        "language tag."
    ),
)
@click.option(
    "--message-template",
    default=None,
    show_default=True,
    help=(
        "Template string for sh:message on each property shape. "
        "Placeholders: {name} (slot name), {title} (slot title or name), "
        "{description} (slot description), {comments} (slot comments joined with '; '), "
        "{class} (class name), {path} (fully-expanded property IRI). "
        'Example: "{name} ({class}): {description} [{comments}]"'
    ),
)
@click.option(
    "--emit-rules/--no-emit-rules",
    default=True,
    show_default=True,
    help=(
        "Emit sh:sparql constraints from LinkML rules: blocks. "
        "When enabled (default), recognised rule patterns (e.g. boolean-guard) "
        "are translated into SHACL-SPARQL constraints on the corresponding "
        "sh:NodeShape. Use --no-emit-rules to suppress rule generation."
    ),
)
@click.option(
    "--recommended-warnings/--no-recommended-warnings",
    default=False,
    show_default=True,
    help=(
        "Emit slots marked 'recommended: true' as sh:Warning constraints, in separate '<shape>Recommendations' "
        "shapes. Off by default, since any warning makes sh:conforms false."
    ),
)
@click.option(
    "--include-range-class-descendants/--no-include-range-class-descendants",
    default=False,
    show_default=True,
    help=(
        "Accept instances typed with the class_uri of a subclass wherever a class is a slot's range "
        "(sh:class becomes an sh:or over the class and its descendants). Useful when validating without "
        "RDFS inference."
    ),
)
@click.option(
    "--diff-stable/--no-diff-stable",
    default=False,
    show_default=True,
    help=(
        "Reduce blank-node label churn across edits. See "
        "https://linkml.io/linkml/howtos/collaborative-development.html#rdf-in-version-control"
    ),
)
@click.version_option(__version__, "-V", "--version")
def cli(yamlfile, **args):
    """Generate SHACL turtle from a LinkML model"""
    gen = ShaclGenerator(yamlfile, **args)
    print(gen.serialize())


if __name__ == "__main__":
    cli()
