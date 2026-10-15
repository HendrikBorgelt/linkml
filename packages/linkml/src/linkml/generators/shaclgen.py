import copy
import logging
import os
import re
import string
from collections.abc import Callable
from dataclasses import dataclass

import click
from jsonasobj2 import JsonObj, as_dict
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.collection import Collection
from rdflib.namespace import RDF, RDFS, SH, XSD

from linkml._version import __version__
from linkml.generators.common.subproperty import get_subproperty_values, is_uri_range
from linkml.generators.shacl.shacl_data_type import ShaclDataType
from linkml.generators.shacl.shacl_ifabsent_processor import ShaclIfAbsentProcessor
from linkml.utils.generator import Generator, shared_arguments
from linkml.utils.language_tags import LanguageTagResolver
from linkml_runtime.linkml_model.meta import (
    AnonymousSlotExpression,
    ClassDefinition,
    ElementName,
    PresenceEnum,
    SlotDefinition,
)
from linkml_runtime.utils.formatutils import underscore
from linkml_runtime.utils.rdf_canonicalize import canonicalize_rdf_graph, _deterministic_fallback_serialize
from linkml_runtime.utils.yamlutils import TypedNode, extended_float, extended_int, extended_str

logger = logging.getLogger(__name__)


MESSAGE_TEMPLATE_FIELDS = ("name", "title", "description", "comments", "class", "path")
"""Placeholders permitted in ``--message-template`` (see :attr:`ShaclGenerator.message_template`)."""

RECOMMENDED_MODES = ("ignore", "warning")
"""Values accepted by :attr:`ShaclGenerator.recommended`."""

MEMBER_VALUE_ATTRIBUTES = (
    "pattern",
    "equals_string",
    "equals_string_in",
    "equals_number",
    "minimum_value",
    "maximum_value",
    "subproperty_of",
)
"""Value constraints of a slot; in a merged shape they stay in the `sh:or` branch of their own slot."""


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

    Shape Naming Modes
    ------------------
    The generator supports two naming modes controlled by ``use_class_uri_names``:

    **Default mode** (``use_class_uri_names=True``):
        Shapes are named using the ``class_uri``. If multiple LinkML classes share
        the same ``class_uri``, their properties are merged into a single shape.
        This is the traditional RDF-centric behavior.

        Example: LinkML classes ``Entity`` and ``EvaluatedEntity`` both with
        ``class_uri prov:Entity`` produce a single shape ``<prov:Entity>``.

    **Native names mode** (``use_class_uri_names=False``):
        Shapes are named using the native LinkML class name from the schema.
        Each LinkML class produces a distinct shape, even if they share a ``class_uri``.
        The ``sh:targetClass`` still correctly points to the ``class_uri``.

        Example: The same two classes produce two shapes, each with
        ``sh:targetClass prov:Entity``.

    Use native names mode when multiple LinkML classes intentionally map to the
    same external ontology class and you need distinct validation shapes per class.
    See `#3011 <https://github.com/linkml/linkml/issues/3011>`_ for background.

    Shared ``class_uri`` (role shapes)
    ----------------------------------
    Classes that share a ``class_uri`` (e.g. ``Activity`` and ``DataGeneratingActivity``
    both on ``prov:Activity``) cannot all target that class: an instance would have to
    satisfy every shape at once, which is unsatisfiable for closed shapes. For each such
    group the generator

    * gives every class in the group its own (native-name) shape,
    * if the group has a single *root* (the class without an ancestor in the group), keeps
      ``sh:targetClass`` only on that root,
    * if the group has two or more roots, targets none of the classes and adds a *dummy* shape
      ``<local name of the class_uri>Roles`` instead: it targets the class, is open, and requires every
      such node to satisfy at least one of the group's role shapes (``sh:or``). A warning is logged,
      because such a structure is usually built by accident,
    * emits the target-less *role* shapes only where a slot's range names them, as
      ``sh:class <class_uri>`` plus ``sh:node <role shape>``.

    This only applies with native names. With class_uri names such classes are merged into one shape.

    Shared ``slot_uri``
    -------------------
    Slots of one class that share a ``slot_uri`` are merged into one property shape with
    ``sh:or`` over their ranges; their individual cardinalities are kept as
    ``sh:qualifiedValueShape`` constraints.

    Recommended slots
    -----------------
    With ``recommended="warning"``, slots marked ``recommended: true`` are emitted as
    ``sh:Warning`` constraints in separate ``...Recommendations`` shapes. Recommendations of
    target-less role shapes are reached through the slot that references them.
    """

    # ClassVars
    closed: bool = True
    """True means add 'sh:closed=true' to all shapes, except of mixin shapes and shapes, that have parents"""
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
    even when sharing class_uri. The --suffix option still works in either mode.
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
    recommended: str = "ignore"
    """How ``recommended: true`` slots are handled: ``ignore`` (default) or ``warning``.

    With ``warning``, each recommended slot becomes a ``sh:severity sh:Warning`` constraint in a
    separate ``<shape>Recommendations`` node shape. They are kept out of the hard shapes because a
    node only conforms to a nested shape (``sh:node``, ``sh:or``, ...) if it yields no results at
    all, whatever their severity.
    """

    include_range_class_descendants: bool = False
    """Accept instances typed with the ``class_uri`` of a subclass wherever a class is a slot's range.

    SHACL without RDFS inference does not know the subclass relation, so by default an instance of
    ``SubstanceSample`` (``SIO:001378``) is rejected where the range is ``EvaluatedEntity`` (``prov:Entity``)
    unless it is also typed ``prov:Entity``. With this option ``sh:class`` becomes an ``sh:or`` over the range
    class and all of its descendants (``is_a`` and mixins).
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
        self._collided_classes: set[str] = set()
        self._untargeted_classes: set[str] = set()
        self._dummy_groups: dict[str, list[str]] = {}
        self._cut_references: set[tuple[str, str]] = set()
        super().__post_init__()
        if self.recommended not in RECOMMENDED_MODES:
            raise ValueError(f"recommended must be one of {RECOMMENDED_MODES}, not {self.recommended!r}")
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
        return _deterministic_fallback_serialize(g, fmt)

    def as_graph(self) -> Graph:
        sv = self.schemaview
        g = Graph()
        g.bind("sh", SH)

        ifabsent_processor = ShaclIfAbsentProcessor(sv)

        for pfx in self.schema.prefixes.values():
            g.bind(str(pfx.prefix_prefix), pfx.prefix_reference)

        self._analyze_class_collisions()

        for c in sv.all_classes(imports=not self.exclude_imports).values():

            def shape_pv(p, v):
                if v is not None:
                    g.add((class_uri_with_suffix, p, v))

            class_uri = URIRef(sv.get_uri(c, expand=True))
            class_uri_with_suffix = self._shape_uri(c.name)
            shape_pv(RDF.type, SH.NodeShape)
            if c.name not in self._untargeted_classes:
                shape_pv(SH.targetClass, class_uri)

            if self.closed:
                if c.mixin or c.abstract:
                    shape_pv(SH.closed, Literal(False))
                else:
                    shape_pv(SH.closed, Literal(True))
            else:
                shape_pv(SH.closed, Literal(False))
            if c.title is not None:
                # Use rdfs:label for NodeShape titles per SHACL spec.
                # sh:name has rdfs:domain of sh:PropertyShape. See issue #3059.
                shape_pv(RDFS.label, Literal(c.title, lang=self._resolve_language(c)))
            if c.description is not None:
                # Use rdfs:comment for NodeShape descriptions per SHACL spec.
                # sh:description has rdfs:domain of sh:PropertyShape, so using it
                # on NodeShapes causes RDFS-aware validators to incorrectly infer
                # the NodeShape is also a PropertyShape. See issue #3059.
                shape_pv(RDFS.comment, Literal(c.description, lang=self._resolve_language(c)))

            shape_pv(SH.ignoredProperties, self._build_ignored_properties(g, c))

            if c.annotations and self.include_annotations:
                self._add_annotations(shape_pv, c)
            order = 0
            for slot_uri, members in self._slots_by_uri(c):
                s = self._merge_slots(c, slot_uri, members) if len(members) > 1 else members[0]
                # A slot with slot_uri rdf:type shares its predicate with the node's own class-typing
                # triple (class_uri); a plain property shape on it would constrain that triple as well.
                own_types = self._own_types(c) if slot_uri == RDF.type else None
                pnode = BNode()
                shape_pv(SH.property, pnode)

                def prop_pv(p, v):
                    if v is not None:
                        g.add((pnode, p, v))

                def prop_pv_literal(p, v):
                    if v is not None:
                        g.add((pnode, p, Literal(v)))

                def prop_pv_text(p, v):
                    if v is not None:
                        g.add((pnode, p, Literal(v, lang=self._resolve_language(s))))

                prop_pv(SH.path, slot_uri)
                prop_pv_literal(SH.order, order)
                order += 1
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
                        **{"class": c.name},
                        path=str(slot_uri),
                    ).strip()
                    if msg_text:
                        g.add((pnode, SH.message, Literal(msg_text, lang=self._resolve_language(None))))
                min_count, max_count = self._cardinality_bounds(s)
                prop_pv_literal(SH.minCount, min_count)
                prop_pv_literal(SH.maxCount, max_count)
                if own_types:
                    self._qualify_type_counts(g, pnode, own_types)
                prop_pv_literal(SH.minInclusive, s.minimum_value)
                prop_pv_literal(SH.maxInclusive, s.maximum_value)

                all_classes = sv.all_classes()
                if s.any_of:
                    # It is not allowed to use any of and equals_string or equals_string_in in one
                    # slot definition, as both are mapped to sh:in in SHACL
                    if s.equals_string or s.equals_string_in:
                        error = "'equals_string'/'equals_string_in' and 'any_of' are mutually exclusive"
                        raise ValueError(f"{TypedNode.yaml_loc(str(s), suffix='')} {error}")

                    or_node = BNode()
                    prop_pv(SH["or"], or_node)
                    Collection(g, or_node, self._any_of_shapes(g, s, members, c.name))
                else:
                    prop_pv_literal(SH.hasValue, s.equals_number)
                    r = s.range
                    if s.equals_string or s.equals_string_in:
                        # Check if range is "string" as this is mandatory for "equals_string" and "equals_string_in"
                        if r != "string":
                            raise ValueError(
                                f"slot: \"{slot_uri}\" - 'equals_string' and 'equals_string_in'"
                                f" require range 'string' and not '{r}'"
                            )

                    if r in all_classes:
                        cls_def = sv.get_class(r)
                        is_any = cls_def and getattr(cls_def, "class_uri", None) == "linkml:Any"
                        if own_types:
                            self._add_type_slot_range(g, prop_pv, r, own_types, c.name)
                        else:
                            self._add_class(g, prop_pv, r, c.name)
                        if not is_any:
                            if sv.get_identifier_slot(r) is not None:
                                prop_pv(SH.nodeKind, SH.IRI)
                            else:
                                prop_pv(SH.nodeKind, SH.BlankNodeOrIRI)
                    elif r in sv.all_types():
                        self._add_type(prop_pv, r)
                    elif r in sv.all_enums():
                        self._add_enum(g, prop_pv, r)
                    else:
                        add_simple_data_type(prop_pv, r)
                    if s.pattern:
                        prop_pv(SH.pattern, Literal(s.pattern))
                    if s.equals_string:
                        # Map equal_string and equal_string_in to sh:in
                        self._and_equals_string(g, prop_pv, [s.equals_string])
                    if s.equals_string_in:
                        # Map equal_string and equal_string_in to sh:in
                        self._and_equals_string(g, prop_pv, s.equals_string_in)
                    if self.expand_subproperty_of and s.subproperty_of:
                        # Map subproperty_of to sh:in with slot descendants
                        self._add_subproperty_constraint(g, prop_pv, s)

                if s.annotations and self.include_annotations:
                    self._add_annotations(prop_pv, s)

                default_value = ifabsent_processor.process_slot(s, c)
                if default_value:
                    prop_pv(SH.defaultValue, default_value)

                if len(members) > 1:
                    self._add_member_cardinalities(g, class_uri_with_suffix, slot_uri, members, c.name)

            if self.emit_rules:
                self._add_rules(g, class_uri_with_suffix, c)

        self._add_dummy_shapes(g)

        if self.recommended == "warning":
            self._add_recommendations(g)

        return g

    # -------------------------------------------------------------------
    # Shared class_uri: role shapes
    # -------------------------------------------------------------------

    def _analyze_class_collisions(self) -> None:
        """Find classes sharing a ``class_uri`` and decide which of them keep ``sh:targetClass``.

        Only applies with native names (``use_class_uri_names=False``), where every LinkML class gets a shape
        of its own. With class_uri names, classes sharing a ``class_uri`` end up in one merged shape, which is
        valid SHACL, so nothing is changed there.

        Sets :attr:`_collided_classes` (all classes in a group of two or more), :attr:`_untargeted_classes`
        (the collided classes that get no ``sh:targetClass``) and :attr:`_dummy_groups`. Separate shapes with
        the same target are conjoined, and being closed they could never all be satisfied.

        Within a group the *roots* are the classes without an ancestor (``is_a`` or mixin) in the group.

        * One root: it keeps the target, every other class becomes a target-less role shape.
        * Two or more roots: no class is targeted. A dummy shape targets the class instead and requires a
          node to satisfy at least one role of the group (see :meth:`_add_dummy_shapes`), which avoids having to
          choose one of the parents. This is usually a modelling accident, so it is warned about.

        Schemas without shared class URIs produce no warning at all.
        """
        self._collided_classes = set()
        self._untargeted_classes = set()
        self._dummy_groups = {}
        if self.use_class_uri_names:
            self._warn_slot_collisions()
            self._find_recursive_references()
            return
        sv = self.schemaview
        groups: dict[str, list[str]] = {}
        for name, cls in sv.all_classes(imports=not self.exclude_imports).items():
            groups.setdefault(sv.get_uri(cls, expand=True), []).append(name)

        for class_uri, names in groups.items():
            if len(names) < 2:
                continue
            self._collided_classes.update(names)
            members = set(names)
            roots = [
                n for n in names if not members & set(sv.class_ancestors(n, imports=True, mixins=True, reflexive=False))
            ]
            if len(roots) == 1:
                self._untargeted_classes.update(members - set(roots))
                logger.warning(
                    "Classes %s share class_uri %s. To keep the SHACL shapes satisfiable only %s gets "
                    "sh:targetClass, the others are role shapes reached through the slots that name them "
                    "(the previous behaviour targeted every class).",
                    sorted(names),
                    class_uri,
                    roots[0],
                )
            else:
                self._untargeted_classes.update(members)
                self._dummy_groups[class_uri] = sorted(names)
                logger.warning(
                    "Classes %s share class_uri %s without a common parent. None of them gets sh:targetClass; "
                    "a dummy shape %s targets the class instead and only requires a node to satisfy at least one "
                    "of the classes %s. This is usually not intended: give the classes a common parent or "
                    "distinct class_uris.",
                    sorted(roots),
                    class_uri,
                    self._dummy_shape_uri(class_uri),
                    sorted(names),
                )
            self._warn_group_consistency(class_uri, names, roots)
        self._warn_slot_collisions()
        self._find_recursive_references()

    def _dummy_shape_uri(self, class_uri: str) -> URIRef:
        """URI of the dummy shape of the group sharing *class_uri*: ``<local name>Roles`` in the schema's namespace.

        A numeric suffix is added if a class already has that name.
        """
        sv = self.schemaview
        local = re.split(r"[#/:]", class_uri.rstrip("#/"))[-1]
        name = f"{local}Roles"
        classes = set(sv.all_classes())
        while name in classes:
            name += "_"
        uri = URIRef(sv.expand_curie(f"{sv.schema.default_prefix}:{name}"))
        return URIRef(uri + self.suffix) if self.suffix else uri

    def _add_dummy_shapes(self, g: Graph) -> None:
        """Add the dummy shape of each group of classes sharing a ``class_uri`` without a common parent.

        The dummy is the only targeted shape for the class: it is open and requires the node to satisfy at
        least one role shape of the group (all classes of the group are listed, so a node shaped like any
        of them, subclasses included, passes). It asks for no property of its own.
        """
        for class_uri, names in self._dummy_groups.items():
            dummy = self._dummy_shape_uri(class_uri)
            g.add((dummy, RDF.type, SH.NodeShape))
            g.add((dummy, SH.targetClass, URIRef(class_uri)))
            g.add((dummy, SH.closed, Literal(False)))
            g.add(
                (
                    dummy,
                    SH.description,
                    Literal(
                        f"Generated: the classes {', '.join(names)} share this class and have no common parent; "
                        "a node must satisfy at least one of their shapes."
                    ),
                )
            )
            members = []
            for name in names:
                member = BNode()
                g.add((member, SH.node, self._shape_uri(name)))
                members.append(member)
            or_node = BNode()
            Collection(g, or_node, members)
            g.add((dummy, SH["or"], or_node))

    def _is_closed(self, name: str) -> bool:
        """Whether the shape generated for *name* is closed (mirrors the rule used in :meth:`as_graph`)."""
        cls = self.schemaview.get_class(name)
        return bool(self.closed and not cls.mixin and not cls.abstract)

    def _warn_group_consistency(self, class_uri: str, names: list[str], roots: list[str]) -> None:
        """Warn about role groups that cannot work as intended.

        * Several unrelated roots that are closed: each rejects the properties of the others.
        * A subclass that loosens what its parent demands: a node passing the child's shape could fail
          the parent's, which defeats the idea that a role is a specialisation of its parent.
        """
        sv = self.schemaview
        closed_roots = [r for r in roots if self._is_closed(r)]
        if len(roots) > 1 and closed_roots:
            logger.warning(
                "Classes %s share class_uri %s without a common parent, but %s are closed. A closed shape rejects "
                "the properties of the other classes; make them abstract or mixins, or generate "
                "with --non-closed.",
                sorted(roots),
                class_uri,
                sorted(closed_roots),
            )
        for name in names:
            for parent in sv.class_ancestors(name, imports=True, mixins=True, reflexive=False):
                if parent not in names:
                    continue
                parent_slots = {s.name: s for s in sv.class_induced_slots(parent)}
                for slot in sv.class_induced_slots(name):
                    if slot.name in parent_slots:
                        for problem in self._loosened(parent_slots[slot.name], slot):
                            logger.warning(
                                "Class %s (role of %s, class_uri %s) loosens slot %s: %s. A node valid for the "
                                "role may then be invalid for its parent.",
                                name,
                                parent,
                                class_uri,
                                slot.name,
                                problem,
                            )

    def _loosened(self, parent: SlotDefinition, child: SlotDefinition) -> list[str]:
        """Ways in which *child* demands less than *parent* for the same slot."""
        sv = self.schemaview
        problems: list[str] = []
        if parent.required and not child.required:
            problems.append("no longer required")
        if not parent.multivalued and child.multivalued:
            problems.append("multivalued although the parent allows one value")
        p_min, p_max = self._cardinality_bounds(parent)
        c_min, c_max = self._cardinality_bounds(child)
        if (p_min or 0) > (c_min or 0):
            problems.append(f"minimum count {c_min or 0} below the parent's {p_min}")
        if p_max is not None and (c_max is None or c_max > p_max):
            problems.append(f"maximum count {c_max} above the parent's {p_max}")
        if parent.range and child.range and parent.range != child.range:
            allowed = (
                set(sv.class_descendants(parent.range, imports=True, mixins=True))
                if sv.get_class(parent.range)
                else {parent.range}
            )
            if child.range not in allowed:
                problems.append(f"range {child.range} is not {parent.range} or a subclass of it")
        return problems

    def _warn_slot_collisions(self) -> None:
        """Warn once per class and property URI when several of its slots share a ``slot_uri``.

        Their property shapes would be conjoined on the same path and, with differing ranges or
        cardinalities, could never be satisfied, so they are merged into one (see :meth:`_merge_slots`).
        """
        for cls_name in self.schemaview.all_classes(imports=not self.exclude_imports):
            for slot_uri, members in self._slots_by_uri(self.schemaview.get_class(cls_name)):
                if len(members) > 1 and slot_uri != RDF.type:
                    logger.warning(
                        "Class %s: slots %s share slot_uri %s. They are merged into one property shape to keep the "
                        "SHACL shapes satisfiable (the previous behaviour emitted one conflicting shape each).",
                        cls_name,
                        sorted(m.name for m in members),
                        slot_uri,
                    )

    def _find_recursive_references(self) -> None:
        """Find references to role shapes that would make the generated ``sh:node`` shapes recursive.

        Recursive shapes are implementation-defined in SHACL. Role shapes are referenced with ``sh:node``,
        so a cycle of such references (``A`` has a slot ranging over role ``B`` whose slot ranges over ``A``)
        is cut at its back-edge: that reference is only checked with ``sh:class``. Targeted classes are
        visited first, so cycles are cut inside the role shapes rather than at their entry points.
        """
        sv = self.schemaview
        classes = list(sv.all_classes(imports=not self.exclude_imports))
        references = {
            name: list(
                dict.fromkeys(
                    r
                    for slot in sv.class_induced_slots(name)
                    for r in self._slot_ranges(slot)
                    if r in self._untargeted_classes
                )
            )
            for name in classes
        }
        self._cut_references = set()
        state: dict[str, bool] = {}  # class -> finished

        def visit(name: str) -> None:
            state[name] = False
            for r in references[name]:
                if state.get(r) is False:
                    self._cut_references.add((name, r))
                elif r not in state:
                    visit(r)
            state[name] = True

        for name in [n for n in classes if n not in self._untargeted_classes] + [
            n for n in classes if n in self._untargeted_classes
        ]:
            if name not in state:
                visit(name)

    def _shape_uri(self, name: ElementName) -> URIRef:
        """URI of the node shape generated for class *name* (suffix applied).

        Classes in a ``class_uri`` collision group always use their native name, since the class URI
        does not identify one of them.
        """
        sv = self.schemaview
        native = not self.use_class_uri_names or name in self._collided_classes
        uri = URIRef(sv.get_uri(name, expand=True, native=native))
        return URIRef(uri + self.suffix) if self.suffix else uri

    # -------------------------------------------------------------------
    # Shared slot_uri: merged property shapes
    # -------------------------------------------------------------------

    def _induced_slot_uri(self, slot: SlotDefinition) -> URIRef:
        """Expanded property URI of an induced slot."""
        sv = self.schemaview
        # fixed in linkml-runtime 1.1.3
        if slot.name in sv.element_by_schema_map():
            return URIRef(sv.get_uri(slot, expand=True))
        return URIRef(sv.expand_curie(f"{sv.schema.default_prefix}:{underscore(slot.name)}"))

    def _slots_by_uri(self, cls: ClassDefinition) -> list[tuple[URIRef, list[SlotDefinition]]]:
        """Induced slots of *cls* grouped by property URI, in slot order.

        Slots sharing a ``slot_uri`` end up in one group so that they can be emitted as a single
        property shape. ``rdf:type`` slots are never grouped; they have their own handling.
        """
        groups: dict[URIRef, list[SlotDefinition]] = {}
        result: list[tuple[URIRef, list[SlotDefinition]]] = []
        for slot in self.schemaview.class_induced_slots(cls.name):
            slot_uri = self._induced_slot_uri(slot)
            if slot_uri == RDF.type or slot_uri not in groups:
                members: list[SlotDefinition] = []
                result.append((slot_uri, members))
                if slot_uri != RDF.type:
                    groups[slot_uri] = members
            else:
                members = groups[slot_uri]
            members.append(slot)
        return result

    @staticmethod
    def _slot_ranges(slot: SlotDefinition) -> list[ElementName]:
        """Ranges a slot allows: those of its ``any_of`` branches, or its own range."""
        return [a.range for a in slot.any_of] if slot.any_of else [slot.range]

    def _merge_slots(self, cls: ClassDefinition, slot_uri: URIRef, members: list[SlotDefinition]) -> SlotDefinition:
        """Combine slots that share *slot_uri* into one slot whose range is the union of theirs.

        The result is multivalued without cardinalities: a value may be of any member's range. The
        members' own cardinalities are enforced separately (see :meth:`_add_member_cardinalities`).
        Value constraints (``pattern``, ``equals_string``...) are cleared on the result: they belong to one
        member and are emitted in that member's own ``sh:or`` branch (see :meth:`_any_of_shapes`).
        """
        ranges = list(dict.fromkeys(r for m in members for r in self._slot_ranges(m)))
        merged = copy.copy(members[0])
        merged.name = " / ".join(m.name for m in members)
        merged.title = next((m.title for m in members if m.title), None)
        merged.description = "; ".join(dict.fromkeys(m.description for m in members if m.description)) or None
        merged.range = None
        merged.any_of = [AnonymousSlotExpression(range=r) for r in ranges]
        merged.multivalued = True
        merged.required = False
        merged.identifier = False
        merged.minimum_cardinality = merged.maximum_cardinality = merged.exact_cardinality = None
        merged.ifabsent = None
        for attribute in MEMBER_VALUE_ATTRIBUTES:
            setattr(merged, attribute, None)
        return merged

    @staticmethod
    def _cardinality_bounds(slot: SlotDefinition) -> tuple[int | None, int | None]:
        """``(min, max)`` number of values of *slot*; ``None`` means unbounded."""
        if slot.minimum_cardinality is not None:
            min_count = slot.minimum_cardinality
        elif slot.exact_cardinality is not None:
            min_count = slot.exact_cardinality
        # Identifiers map to the node's IRI rather than a property triple,
        # so there's no arc to constrain with sh:minCount 1 — emitting it
        # would cause spurious violations on every instance.
        elif slot.required and not slot.identifier:
            min_count = 1
        else:
            min_count = None
        if slot.maximum_cardinality is not None:
            max_count = slot.maximum_cardinality
        elif slot.exact_cardinality is not None:
            max_count = slot.exact_cardinality
        elif not slot.multivalued:
            max_count = 1
        else:
            max_count = None
        return min_count, max_count

    def _range_shape(self, g: Graph, r: ElementName, source: str) -> BNode:
        """Blank node shape holding the constraints for values of range *r* (class, type, enum or datatype)."""
        sv = self.schemaview
        node = BNode()

        def node_pv(p, v):
            if v is not None:
                g.add((node, p, v))

        if r in sv.all_classes():
            self._add_class(g, node_pv, r, source)
        elif r in sv.all_types():
            self._add_type(node_pv, r)
        elif r in sv.all_enums():
            self._add_enum(g, node_pv, r)
        else:
            add_simple_data_type(node_pv, r)
        return node

    def _member_shape(self, g: Graph, slot: SlotDefinition, source: str) -> BNode:
        """Shape matching the values *slot* itself allows: its range (or the union of its ``any_of`` ranges)
        together with its own value constraints."""
        shapes = [self._range_shape(g, r, source) for r in self._slot_ranges(slot)]
        if len(shapes) == 1:
            node = shapes[0]
        else:
            node, or_node = BNode(), BNode()
            Collection(g, or_node, shapes)
            g.add((node, SH["or"], or_node))
        self._add_value_constraints(g, node, slot)
        return node

    def _add_value_constraints(self, g: Graph, node: BNode, slot: SlotDefinition) -> None:
        """Add *slot*'s own value constraints (pattern, bounds, fixed values, ``subproperty_of``) to *node*."""

        def pv(p, v):
            g.add((node, p, v))

        if slot.minimum_value is not None:
            pv(SH.minInclusive, Literal(slot.minimum_value))
        if slot.maximum_value is not None:
            pv(SH.maxInclusive, Literal(slot.maximum_value))
        if slot.equals_number is not None:
            pv(SH.hasValue, Literal(slot.equals_number))
        if slot.pattern:
            pv(SH.pattern, Literal(slot.pattern))
        if slot.equals_string or slot.equals_string_in:
            if slot.range != "string":
                raise ValueError(
                    f"slot: \"{slot.name}\" - 'equals_string' and 'equals_string_in' require range 'string' "
                    f"and not '{slot.range}'"
                )
            self._and_equals_string(g, pv, [slot.equals_string] if slot.equals_string else slot.equals_string_in)
        if self.expand_subproperty_of and slot.subproperty_of:
            self._add_subproperty_constraint(g, pv, slot)

    def _any_of_shapes(self, g: Graph, slot: SlotDefinition, members: list[SlotDefinition], source: str) -> list[BNode]:
        """Branches of the ``sh:or`` of *slot*.

        A plain slot has one branch per ``any_of`` range. A slot merged from *members* has one branch per
        member, carrying that member's value constraints, so that e.g. a ``pattern`` only applies to values
        of the slot it was declared on. Members that would give identical branches are emitted once.
        """
        if len(members) == 1:
            return [self._range_shape(g, a.range, source) for a in slot.any_of]
        seen: set[tuple] = set()
        branches: list[BNode] = []
        for member in members:
            key = (tuple(self._slot_ranges(member)), *(str(getattr(member, a, None)) for a in MEMBER_VALUE_ATTRIBUTES))
            if key not in seen:
                seen.add(key)
                branches.append(self._member_shape(g, member, source))
        return branches

    def _add_member_cardinalities(
        self, g: Graph, shape: URIRef, slot_uri: URIRef, members: list[SlotDefinition], source: str
    ) -> None:
        """Enforce each merged slot's own cardinality as a qualified count over the values of its range."""
        for member in members:
            min_count, max_count = self._cardinality_bounds(member)
            if not min_count and max_count is None:
                continue
            pnode = BNode()
            g.add((shape, SH.property, pnode))
            g.add((pnode, SH.path, slot_uri))
            g.add((pnode, SH.name, Literal(member.name)))
            g.add((pnode, SH.qualifiedValueShape, self._member_shape(g, member, source)))
            if min_count:
                g.add((pnode, SH.qualifiedMinCount, Literal(min_count)))
            if max_count is not None:
                g.add((pnode, SH.qualifiedMaxCount, Literal(max_count)))

    # -------------------------------------------------------------------
    # Recommended slots -> sh:Warning
    # -------------------------------------------------------------------

    def _add_recommendations(self, g: Graph) -> None:
        """Emit ``recommended: true`` slots as ``sh:Warning`` constraints in ``<shape>Recommendations`` shapes.

        A class with recommended slots gets a sibling shape, targeted like the class itself. A target-less
        role shape cannot be targeted, so its recommendations are checked through the slots that reference
        it: the referencing class' recommendations shape gets a warning-level property shape on that path
        with ``sh:node`` pointing at the role shape's recommendations. That way they only apply to values
        reached through that slot, not to every use of the predicate. Where the slot's predicate is shared
        or its range a union, the ``sh:node`` is guarded to values of the role class.
        """
        sv = self.schemaview
        classes = sv.all_classes(imports=not self.exclude_imports)
        slot_groups = {name: self._slots_by_uri(cls) for name, cls in classes.items()}
        own = {
            name: [s for _, members in groups for s in members if s.recommended] for name, groups in slot_groups.items()
        }
        # references to role classes: (referencing slot, role class, is the use of the predicate ambiguous)
        references: dict[str, list[tuple[SlotDefinition, str, bool]]] = {name: [] for name in classes}
        for name, groups in slot_groups.items():
            for _, members in groups:
                for slot in members:
                    for r in self._slot_ranges(slot):
                        if r in self._untargeted_classes and r in classes:
                            references[name].append((slot, r, len(members) > 1 or bool(slot.any_of)))

        # Depth-first search, dropping back-edges so that the generated shapes are never recursive.
        state: dict[str, bool] = {}  # class -> finished
        needed: dict[str, bool] = {}
        kept: dict[str, list[tuple[SlotDefinition, str, bool]]] = {name: [] for name in classes}

        def visit(name: str) -> None:
            state[name] = False
            needed[name] = bool(own[name])
            for slot, r, guarded in references[name]:
                if state.get(r) is False:
                    continue
                if r not in state:
                    visit(r)
                if needed[r]:
                    kept[name].append((slot, r, guarded))
                    needed[name] = True
            state[name] = True

        for name in classes:
            if name not in state:
                visit(name)

        def recommendations_uri(name: str) -> URIRef:
            return URIRef(str(self._shape_uri(name)) + "Recommendations")

        for name, cls in classes.items():
            if not needed[name]:
                continue
            shape = recommendations_uri(name)
            g.add((shape, RDF.type, SH.NodeShape))
            if name not in self._untargeted_classes:
                g.add((shape, SH.targetClass, URIRef(sv.get_uri(cls, expand=True))))
            shared = {uri for uri, members in slot_groups[name] if len(members) > 1}
            for slot in own[name]:
                slot_uri = self._induced_slot_uri(slot)
                pnode = BNode()
                g.add((shape, SH.property, pnode))
                g.add((pnode, SH.path, slot_uri))
                g.add((pnode, SH.severity, SH.Warning))
                g.add((pnode, SH.message, Literal(f"Recommended: {slot.title or slot.name}")))
                if slot_uri in shared:
                    g.add((pnode, SH.qualifiedValueShape, self._member_shape(g, slot, name)))
                    g.add((pnode, SH.qualifiedMinCount, Literal(1)))
                else:
                    g.add((pnode, SH.minCount, Literal(1)))
            for slot, r, guarded in kept[name]:
                pnode = BNode()
                g.add((shape, SH.property, pnode))
                g.add((pnode, SH.path, self._induced_slot_uri(slot)))
                g.add((pnode, SH.severity, SH.Warning))
                g.add((pnode, SH.message, Literal(f"Recommendations for {r} (via {slot.title or slot.name}) not met")))
                role_recommendations = recommendations_uri(r)
                if not guarded:
                    g.add((pnode, SH.node, role_recommendations))
                    continue
                # values of other ranges sharing this predicate are none of this role's business
                not_role, role_only, or_node = BNode(), BNode(), BNode()
                class_test = BNode()
                self._add_class_type(g, lambda p, v: g.add((class_test, p, v)), r)
                g.add((not_role, SH["not"], class_test))
                g.add((role_only, SH.node, role_recommendations))
                Collection(g, or_node, [not_role, role_only])
                g.add((pnode, SH["or"], or_node))

    LINKML_ANY_URI = "https://w3id.org/linkml/Any"

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
        """Resolve a slot name to a full IRI string for use in SPARQL queries.

        Mirrors the resolution logic used for ``sh:path`` in the main slot loop:
        prefer ``sv.get_uri()`` for slots registered in the schema map, fall
        back to ``default_prefix:underscored_name``.
        """
        slot = sv.get_slot(slot_name)
        if slot and slot_name in sv.element_by_schema_map():
            return sv.get_uri(slot, expand=True)
        pfx = sv.schema.default_prefix
        return sv.expand_curie(f"{pfx}:{underscore(slot_name)}")

    def _own_types(self, cls: ClassDefinition) -> list[URIRef]:
        """``class_uri`` of *cls*, of its ``is_a`` descendants and of its ancestors.

        Instances of *cls* carry one of these as their own ``rdf:type`` triple, which a slot with
        ``slot_uri: rdf:type`` shares a predicate with. Ancestors count too: an instance of *cls* is
        also an instance of its superclasses, and a node reached through a role shape is typically
        typed with the (shared) ``class_uri`` of the generic class as well.
        """
        sv = self.schemaview
        related = sv.class_descendants(cls.name, imports=True, mixins=True, reflexive=True) + sv.class_ancestors(
            cls.name, imports=True, mixins=True, reflexive=False
        )
        return sorted({URIRef(sv.get_uri(c, expand=True)) for c in related}, key=str)

    def _in_node(self, g: Graph, values: list[URIRef]) -> BNode:
        node, lst = BNode(), BNode()
        Collection(g, lst, values)
        g.add((node, SH["in"], lst))
        return node

    def _add_type_slot_range(
        self, g: Graph, prop_pv: Callable, r: ElementName, own_types: list[URIRef], source: str
    ) -> None:
        """Range constraint of an ``rdf:type`` slot: the value is the slot's range *or* the node's own class."""
        range_node = BNode()

        def range_pv(p, v):
            if v is not None:
                g.add((range_node, p, v))

        self._add_class(g, range_pv, r, source)
        or_node = BNode()
        Collection(g, or_node, [range_node, self._in_node(g, own_types)])
        prop_pv(SH["or"], or_node)

    def _qualify_type_counts(self, g: Graph, pnode: BNode, own_types: list[URIRef]) -> None:
        """Cardinalities of an ``rdf:type`` slot count only the values other than the node's own class."""
        moved = False
        for old, new in ((SH.minCount, SH.qualifiedMinCount), (SH.maxCount, SH.qualifiedMaxCount)):
            for o in list(g.objects(pnode, old)):
                g.remove((pnode, old, o))
                g.add((pnode, new, o))
                moved = True
        if moved:
            qshape = BNode()
            g.add((qshape, SH["not"], self._in_node(g, own_types)))
            g.add((pnode, SH.qualifiedValueShape, qshape))

    def _add_class(self, g: Graph, func: Callable, r: ElementName, source: str | None = None) -> None:
        """Add a class/shape constraint for range class *r*, referenced from class *source*.

        Skips the constraint when *r* resolves to ``linkml:Any`` — the
        LinkML meta-type representing an unconstrained range.

        In default mode (``use_class_uri_names=True``): emits ``sh:class <class_uri>``
        so validators check the RDF type hierarchy.

        In native names mode (``use_class_uri_names=False``): emits ``sh:node <native_shape_uri>``
        instead of ``sh:class``.  Using ``sh:class`` with a native shape URI (e.g.
        ``dcatapplus:Resource``) is incorrect because data nodes are never typed as
        that URI — it is a shape identifier, not an RDF class.  ``sh:node`` correctly
        validates the referenced node against the named shape.

        Because ``sh:node`` references a shape by its identifier, the ``suffix``
        option has to be applied here too — otherwise the reference points at a
        shape that was never emitted and the constraint silently passes.

        Classes that share their ``class_uri`` with another class and have no ``sh:targetClass`` of their
        own (role shapes) are always referenced by ``sh:node``, because nothing else would validate them.
        ``sh:class`` is emitted alongside, since ``sh:node`` does not check the node's type. A reference
        that would make the ``sh:node`` shapes recursive is left as ``sh:class`` only (see
        :meth:`_find_recursive_references`).
        """
        sv = self.schemaview
        cls = sv.get_class(r)
        if cls and getattr(cls, "class_uri", None) == "linkml:Any":
            return
        if sv.get_uri(r, expand=True, native=not self.use_class_uri_names) == self.LINKML_ANY_URI:
            return
        if self.use_class_uri_names:
            self._add_class_type(g, func, r)
            if r in self._untargeted_classes and (source, r) not in self._cut_references:
                func(SH["node"], self._shape_uri(r))
            return
        if r in self._collided_classes:
            self._add_class_type(g, func, r)
        if (source, r) not in self._cut_references:
            func(SH["node"], self._shape_uri(r))

    def _class_uris(self, r: ElementName) -> list[URIRef]:
        """``class_uri`` of *r*, plus its descendants' if :attr:`include_range_class_descendants` is set."""
        sv = self.schemaview
        uris = [URIRef(sv.get_uri(r, expand=True))]
        if self.include_range_class_descendants:
            descendants = sv.class_descendants(r, imports=True, mixins=True, reflexive=False)
            uris += sorted({URIRef(sv.get_uri(d, expand=True)) for d in descendants} - set(uris), key=str)
        return uris

    def _add_class_type(self, g: Graph, func: Callable, r: ElementName) -> None:
        """Constrain values to instances of class *r*: ``sh:class``, or an ``sh:or`` of it and its descendants."""
        uris = self._class_uris(r)
        if len(uris) == 1:
            func(SH["class"], uris[0])
            return
        members = []
        for uri in uris:
            member = BNode()
            g.add((member, SH["class"], uri))
            members.append(member)
        or_node = BNode()
        Collection(g, or_node, members)
        func(SH["or"], or_node)

    def _add_enum(self, g: Graph, func: Callable, r: ElementName) -> None:
        sv = self.schemaview
        enum = sv.get_enum(r)
        pv_node = BNode()
        Collection(
            g,
            pv_node,
            [
                URIRef(sv.expand_curie(pv.meaning)) if pv.meaning else Literal(pv_name)
                for pv_name, pv in enum.permissible_values.items()
            ],
        )
        func(SH["in"], pv_node)

    # Type URIs denoting non-literal (IRI or blank-node) values.
    # SHACL §4.8.1 <https://www.w3.org/TR/shacl/#NodeKindConstraintComponent>
    # defines sh:IRI, sh:BlankNode, and sh:BlankNodeOrIRI as valid node kinds.
    # These URIs map to sh:IRI or sh:BlankNodeOrIRI constraints (never sh:Literal).
    _NON_LITERAL_TYPE_URIS = frozenset(
        {
            "xsd:anyURI",  # uri, uriorcurie → sh:IRI
            "http://www.w3.org/ns/shex#nonLiteral",  # nodeidentifier → sh:BlankNodeOrIRI
            "http://www.w3.org/ns/shex#iri",  # future-proofing → sh:IRI
        }
    )
    # IRI-only subset: uri/uriorcurie must be strict IRI references (sh:IRI),
    # while nodeidentifier (shex:nonLiteral) allows blank nodes too (sh:BlankNodeOrIRI).
    # See RDF 1.1 §3.2–3.3 <https://www.w3.org/TR/rdf11-concepts/#section-IRIs>.
    _IRI_ONLY_TYPE_URIS = frozenset(
        {
            "xsd:anyURI",
        }
    )

    def _add_type(self, func: Callable, r: ElementName) -> None:
        sv = self.schemaview
        # Types can inherit URI and pattern constraints.
        rt = sv.induced_type(r)
        type_uri = rt.uri
        expanded = sv.get_uri(rt, expand=True) if type_uri else None
        if type_uri and (type_uri in self._NON_LITERAL_TYPE_URIS or expanded in self._NON_LITERAL_TYPE_URIS):
            if type_uri in self._IRI_ONLY_TYPE_URIS:
                func(SH.nodeKind, SH.IRI)
            else:
                func(SH.nodeKind, SH.BlankNodeOrIRI)
        elif type_uri:
            func(SH.nodeKind, SH.Literal)
            func(SH.datatype, URIRef(sv.get_uri(rt, expand=True)))
            if rt.pattern:
                func(SH.pattern, Literal(rt.pattern))
            if rt.annotations and self.include_annotations:
                self._add_annotations(func, rt)
        else:
            logger.error(f"No URI for type {rt.name}")

    def _and_equals_string(self, g: Graph, func: Callable, values: list) -> None:
        pv_node = BNode()
        Collection(
            g,
            pv_node,
            [Literal(v) for v in values],
        )
        func(SH["in"], pv_node)

    def _add_subproperty_constraint(self, g: Graph, func: Callable, slot) -> None:
        """
        Add sh:in constraint from subproperty_of slot hierarchy.

        Following metamodel semantics: "any ontological child (related to X via
        an is_a relationship), is a valid value for the slot"

        :param g: RDF graph to add to
        :param func: Function to call with predicate and object
        :param slot: SlotDefinition with subproperty_of set
        """
        values = self._get_subproperty_values(slot)
        if values:
            pv_node = BNode()
            Collection(g, pv_node, values)
            func(SH["in"], pv_node)

    def _get_subproperty_values(self, slot) -> list:
        """
        Get all valid values from slot hierarchy for subproperty_of constraint.

        Values are formatted according to range type:
        - uri/uriorcurie: Returns URIRef objects with full URIs
        - string: Returns Literal objects with slot names

        :param slot: SlotDefinition with subproperty_of set
        :return: List of URIRef or Literal objects for sh:in constraint
        """
        sv = self.schemaview

        # SHACL uses full URIs for URI-like ranges
        use_uris = is_uri_range(sv, slot.range)

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

    def _build_ignored_properties(self, g: Graph, c: ClassDefinition) -> BNode:
        def collect_child_properties(class_name: str, output: set) -> None:
            for childName in self.schemaview.class_children(class_name, imports=True, mixins=False, is_a=True):
                output.update(
                    {
                        URIRef(self.schemaview.get_uri(prop, expand=True))
                        for prop in self.schemaview.class_slots(childName)
                    }
                )
                collect_child_properties(childName, output)

        child_properties = set()
        collect_child_properties(c.name, child_properties)

        class_slot_uris = {
            URIRef(self.schemaview.get_uri(prop, expand=True)) for prop in self.schemaview.class_slots(c.name)
        }
        ignored_properties = child_properties.difference(class_slot_uris)

        list_node = BNode()
        ignored_properties.add(RDF.type)
        Collection(g, list_node, sorted(ignored_properties, key=str))

        return list_node


def add_simple_data_type(func: Callable, r: ElementName) -> None:
    for datatype in list(ShaclDataType):
        if datatype.linkml_type == r:
            func(SH.datatype, datatype.uri_ref)


@shared_arguments(ShaclGenerator)
@click.command(name="shacl")
@click.option(
    "--closed/--non-closed",
    default=True,
    show_default=True,
    help="Use '--closed' to generate closed SHACL shapes. Use '--non-closed' to generate open SHACL shapes.",
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
    "--recommended",
    type=click.Choice(RECOMMENDED_MODES),
    default="ignore",
    show_default=True,
    help=(
        "How slots marked 'recommended: true' are handled. 'warning' emits them as sh:Warning constraints in "
        "separate '<shape>Recommendations' shapes; 'ignore' (default) leaves them out."
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
@click.version_option(__version__, "-V", "--version")
def cli(yamlfile, **args):
    """Generate SHACL turtle from a LinkML model"""
    gen = ShaclGenerator(yamlfile, **args)
    print(gen.serialize())


if __name__ == "__main__":
    cli()
