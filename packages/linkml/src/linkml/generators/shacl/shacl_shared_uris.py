"""Schema analysis for LinkML classes and slots that share an RDF IRI.

LinkML tells classes and slots apart by name, but RDF only sees their IRIs. Where classes share a ``class_uri``
or slots share a ``slot_uri``, a SHACL validator cannot tell which LinkML element a node or a triple belongs
to, so :class:`~linkml.generators.shaclgen.ShaclGenerator` has to emit shapes that hold for all of them at once.
This module works out which elements are affected and how they are grouped; turning that into SHACL is left to
the generator.

Nothing here builds RDF or modifies the :class:`~linkml_runtime.utils.schemaview.SchemaView`.
"""

import copy
import re
from dataclasses import dataclass, field
from functools import cached_property

from rdflib import RDF, URIRef

from linkml_runtime.linkml_model.meta import AnonymousSlotExpression, ClassDefinition, ElementName, SlotDefinition
from linkml_runtime.utils.formatutils import camelcase, underscore
from linkml_runtime.utils.schemaview import SchemaView

SLOT_VALUE_CONSTRAINTS = (
    "pattern",
    "equals_string",
    "equals_string_in",
    "equals_number",
    "minimum_value",
    "maximum_value",
    "subproperty_of",
)
"""Slot attributes that constrain each value, rather than the number of values.

When slots sharing a predicate are merged, these stay with the slot that declares them (in that slot's own
``sh:or`` branch) instead of applying to every value of the predicate.
"""


# ---------------------------------------------------------------------------
# Single slots
# ---------------------------------------------------------------------------


def slot_path(sv: SchemaView, slot: SlotDefinition) -> URIRef:
    """Predicate IRI (``sh:path``) of *slot*: its ``slot_uri``, or else its native IRI."""
    if slot.name in sv.element_by_schema_map():
        return URIRef(sv.get_uri(slot, expand=True))
    # A slot the schema map does not know gets a native IRI in the schema's default prefix.
    return URIRef(sv.expand_curie(f"{sv.schema.default_prefix}:{underscore(slot.name)}"))


def slot_ranges(slot: SlotDefinition) -> list[ElementName]:
    """Ranges a value of *slot* may have: those of its ``any_of`` branches, or else its own ``range``.

    Unlike :meth:`~linkml_runtime.utils.schemaview.SchemaView.slot_range_as_union`, this leaves out the slot's
    own ``range`` when ``any_of`` is set, and ignores ``exactly_one_of``. That matches what the generator emits:
    ``any_of`` becomes ``sh:or`` over the branches' ranges, in place of the slot's range. The other boolean slot
    expressions (``exactly_one_of``, ``all_of``, ``none_of``) are not translated to SHACL yet.

    >>> slot_ranges(SlotDefinition("s", range="string"))
    ['string']
    >>> slot_ranges(SlotDefinition("s", range="Any", any_of=[{"range": "A"}, {"range": "B"}]))
    ['A', 'B']
    """
    return [a.range for a in slot.any_of] if slot.any_of else [slot.range]


def cardinality_bounds(slot: SlotDefinition) -> tuple[int | None, int | None]:
    """``(minimum, maximum)`` number of values *slot* allows, where ``None`` means unbounded.

    ``minimum_cardinality``/``maximum_cardinality`` take precedence over ``exact_cardinality``, which takes
    precedence over ``required``/``multivalued``. An identifier is never counted as required: it becomes the
    node's IRI, not a triple a property shape could count.

    >>> cardinality_bounds(SlotDefinition("s", required=True))
    (1, 1)
    >>> cardinality_bounds(SlotDefinition("s", multivalued=True, exact_cardinality=2))
    (2, 2)
    >>> cardinality_bounds(SlotDefinition("s", identifier=True, required=True))
    (None, 1)
    """
    if slot.minimum_cardinality is not None:
        min_count = slot.minimum_cardinality
    elif slot.exact_cardinality is not None:
        min_count = slot.exact_cardinality
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


def slot_value_key(slot: SlotDefinition) -> tuple:
    """What *slot* demands of each value: its ranges and value constraints. Equal keys accept equal values."""
    return (tuple(slot_ranges(slot)), *(str(getattr(slot, attribute)) for attribute in SLOT_VALUE_CONSTRAINTS))


def relaxed_constraints(sv: SchemaView, parent: SlotDefinition, child: SlotDefinition) -> list[str]:
    """Ways in which *child*, a subclass's version of a slot, accepts more than *parent*'s version of it.

    >>> relaxed_constraints(None, SlotDefinition("s", required=True), SlotDefinition("s"))
    ['at least 0 values instead of 1']
    >>> relaxed_constraints(None, SlotDefinition("s"), SlotDefinition("s", multivalued=True))
    ['any number of values instead of at most 1']
    """
    relaxations = []
    parent_min, parent_max = cardinality_bounds(parent)
    child_min, child_max = cardinality_bounds(child)
    if (child_min or 0) < (parent_min or 0):
        relaxations.append(f"at least {child_min or 0} values instead of {parent_min}")
    if parent_max is not None and child_max is None:
        relaxations.append(f"any number of values instead of at most {parent_max}")
    elif parent_max is not None and child_max > parent_max:
        relaxations.append(f"at most {child_max} values instead of {parent_max}")
    if parent.range and child.range and parent.range != child.range:
        if sv.get_class(parent.range):
            allowed = set(sv.class_descendants(parent.range, imports=True, mixins=True))
        else:
            allowed = {parent.range}
        if child.range not in allowed:
            relaxations.append(f"range {child.range}, which is not {parent.range} or a subclass of it")
    return relaxations


# ---------------------------------------------------------------------------
# Slots sharing a slot_uri
# ---------------------------------------------------------------------------


def merge_slots(slots: list[SlotDefinition]) -> SlotDefinition:
    """One slot standing in for several slots that share a predicate.

    Its range is the union of theirs (as ``any_of``). It is multivalued, without cardinalities or value
    constraints: those belong to the individual slots, and the generator emits them per slot (as qualified
    counts and in per-slot ``sh:or`` branches).

    Its title and description combine those of the slots, each marked with the slot it comes from (see
    :func:`slot_labels`). Metadata that cannot be combined (``annotations``, ``comments``, ``in_language``,
    ``owner``) is taken from the first slot.

    The slots themselves are left untouched: the result is a shallow copy of the first one, on which only
    attributes are reassigned.

    >>> doi = SlotDefinition("doi", owner="Thing", description="A DOI")
    >>> homepage = SlotDefinition("homepage", owner="Thing", title="Homepage", description="A web page")
    >>> merged = merge_slots([doi, homepage])
    >>> merged.title, merged.description
    ('doi / Homepage', 'doi: A DOI; homepage: A web page')
    """
    ranges = list(dict.fromkeys(r for slot in slots for r in slot_ranges(slot)))
    labels = slot_labels(slots)
    merged = copy.copy(slots[0])
    merged.name = " / ".join(slot.name for slot in slots)
    if any(slot.title for slot in slots):
        merged.title = " / ".join(slot.title or label for slot, label in zip(slots, labels))
    descriptions = [f"{label}: {slot.description}" for slot, label in zip(slots, labels) if slot.description]
    merged.description = "; ".join(descriptions) or None
    merged.range = None
    merged.any_of = [AnonymousSlotExpression(range=r) for r in ranges]
    merged.multivalued = True
    merged.required = False
    merged.identifier = False
    merged.minimum_cardinality = merged.maximum_cardinality = merged.exact_cardinality = None
    merged.ifabsent = None
    for attribute in SLOT_VALUE_CONSTRAINTS:
        setattr(merged, attribute, None)
    return merged


def slot_labels(slots: list[SlotDefinition]) -> list[str]:
    """Names telling *slots* apart: the slot name, prefixed with its class if the slots come from several classes.

    Slots merged in one shape normally belong to one class. With class_uri names, a shape can combine several
    classes, and their versions of a slot can share its name.

    >>> slot_labels([SlotDefinition("a", owner="C"), SlotDefinition("b", owner="C")])
    ['a', 'b']
    >>> slot_labels([SlotDefinition("s", owner="A"), SlotDefinition("s", owner="B")])
    ['A.s', 'B.s']
    """
    by_class = len({slot.owner for slot in slots}) > 1
    return [f"{slot.owner}.{slot.name}" if by_class else slot.name for slot in slots]


@dataclass
class PropertyGroup:
    """Slots emitted as one SHACL property shape, because they share a predicate (``sh:path``).

    Each slot is induced for one of the classes of the node shape; ``slot.owner`` names that class.
    """

    path: URIRef
    slots: list[SlotDefinition]

    @property
    def is_merged(self) -> bool:
        """Whether several slots share the predicate, so that their ranges are combined with ``sh:or``."""
        return len(self.slots) > 1

    def combined_slot(self) -> SlotDefinition:
        """The slot the property shape is built from: the only slot, or all slots merged (see :func:`merge_slots`)."""
        return merge_slots(self.slots) if self.is_merged else self.slots[0]


def group_slots_by_path(sv: SchemaView, slots: list[SlotDefinition]) -> list[PropertyGroup]:
    """Group *slots* by predicate, in the order in which each predicate first appears.

    Slots on ``rdf:type`` are never grouped and keep a property shape each: the generator treats ``rdf:type``
    specially (the node's own class assertion shares the predicate), and only does so for a single slot.
    """
    groups: list[PropertyGroup] = []
    by_path: dict[URIRef, PropertyGroup] = {}
    for slot in slots:
        path = slot_path(sv, slot)
        if path in by_path and path != RDF.type:
            by_path[path].slots.append(slot)
        else:
            by_path[path] = PropertyGroup(path, [slot])
            groups.append(by_path[path])
    return groups


def shape_slots(sv: SchemaView, class_names: list[str]) -> list[SlotDefinition]:
    """Induced slots of all classes emitted into one node shape, with each distinct slot once.

    A slot that several of the classes use unchanged is listed once. If ``slot_usage`` makes it differ between
    them, every version is kept, and the versions are then merged like slots sharing a predicate.
    """

    def ignoring_owner(slot: SlotDefinition) -> SlotDefinition:
        unowned = copy.copy(slot)
        unowned.owner = None
        return unowned

    slots: list[SlotDefinition] = []
    for name in class_names:
        for slot in sv.class_induced_slots(name):
            if not any(other.name == slot.name and ignoring_owner(other) == ignoring_owner(slot) for other in slots):
                slots.append(slot)
    return slots


@dataclass
class NodeShapeSpec:
    """What one node shape is built from: its IRI, the classes emitted into it, and their slots by predicate.

    With native names a shape has exactly one class. With class_uri names, all classes sharing a ``class_uri``
    are emitted into the same shape.
    """

    uri: URIRef
    classes: list[ClassDefinition]
    property_groups: list[PropertyGroup]


# ---------------------------------------------------------------------------
# Classes sharing a class_uri (role shapes, native names only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SharedClassUriGroup:
    """Classes that share one ``class_uri``. The *roots* are the classes without an ancestor in the group."""

    class_uri: str
    classes: tuple[str, ...]
    roots: tuple[str, ...]
    roles_relaxing_root: tuple[str, ...] = ()
    """Classes of a single-root group that accept more than the root for some slot (see
    :func:`relaxed_constraints`). Targeting the root would reject their valid nodes."""

    @property
    def needs_role_union(self) -> bool:
        """Whether no class can be targeted, so that a role union shape targets the ``class_uri`` instead.

        That is the case if the group has several roots, or if one of its classes relaxes the single root.
        """
        return len(self.roots) > 1 or bool(self.roles_relaxing_root)

    @property
    def role_classes(self) -> set[str]:
        """Classes without ``sh:targetClass``: all but the root, or all of them if a role union shape is needed."""
        return set(self.classes) if self.needs_role_union else set(self.classes) - set(self.roots)


@dataclass
class RolePlan:
    """How classes sharing a ``class_uri`` become role shapes. Only used with native-name shapes.

    Node shapes with the same ``sh:targetClass`` all apply to every instance of that class, and closed shapes
    with different properties can never all be satisfied. So in each group of classes sharing a ``class_uri``
    only the root keeps ``sh:targetClass``. The other classes become *role shapes*: shapes without a target,
    checked (with ``sh:node``) only where a slot's range names them.

    The root's shape then applies to every node of the ``class_uri``, which only works if the other classes
    refine it. If the group has several roots, or one of its classes relaxes the root (``slot_usage`` lets a
    subclass accept more than its parent), none of its classes is targeted. A *role union shape* targets the
    ``class_uri`` instead and requires a node to conform to at least one of the group's shapes.
    """

    groups: list[SharedClassUriGroup] = field(default_factory=list)
    recursive_references: set[tuple[str, str]] = field(default_factory=set)
    """``(referencing class, role class)`` pairs checked with ``sh:class`` only, as ``sh:node`` would make shapes
    recursive (see :func:`back_edges`)."""

    @cached_property
    def role_classes(self) -> set[str]:
        """Classes whose shapes get no ``sh:targetClass``."""
        return set().union(*(group.role_classes for group in self.groups))

    @cached_property
    def shared_classes(self) -> set[str]:
        """All classes that share their ``class_uri`` with another class."""
        return set().union(*(group.classes for group in self.groups))


def plan_role_shapes(sv: SchemaView, class_names: list[str]) -> RolePlan:
    """Find the classes among *class_names* that share a ``class_uri`` and decide which become role shapes."""
    by_class_uri: dict[str, list[str]] = {}
    for name in class_names:
        by_class_uri.setdefault(sv.get_uri(name, expand=True), []).append(name)
    groups = []
    for class_uri, names in by_class_uri.items():
        if len(names) < 2:
            continue
        roots = [
            name
            for name in names
            if not set(names) & set(sv.class_ancestors(name, imports=True, mixins=True, reflexive=False))
        ]
        roles_relaxing_root = _roles_relaxing_root(sv, roots[0], names) if len(roots) == 1 else []
        groups.append(
            SharedClassUriGroup(
                class_uri, tuple(sorted(names)), tuple(sorted(roots)), tuple(sorted(roles_relaxing_root))
            )
        )
    plan = RolePlan(groups)
    plan.recursive_references = _recursive_role_references(sv, class_names, plan.role_classes)
    return plan


def _roles_relaxing_root(sv: SchemaView, root: str, class_names: list[str]) -> list[str]:
    """Classes among *class_names* that accept more than *root* for a slot they share with it."""
    root_slots = {slot.name: slot for slot in sv.class_induced_slots(root)}
    return [
        name
        for name in class_names
        if name != root
        and any(
            relaxed_constraints(sv, root_slots[slot.name], slot)
            for slot in sv.class_induced_slots(name)
            if slot.name in root_slots
        )
    ]


def _recursive_role_references(sv: SchemaView, class_names: list[str], role_classes: set[str]) -> set[tuple[str, str]]:
    """References to role shapes that would make the generated ``sh:node`` shapes recursive.

    Role shapes are referenced with ``sh:node``, and SHACL leaves recursive shapes up to the implementation.
    The references from each class to the role classes its slots range over are searched depth-first, and
    each reference that closes a cycle is returned. Targeted classes are searched first, so that cycles are
    cut inside the role shapes rather than where validation enters them.
    """
    references = {
        name: list(
            dict.fromkeys(r for slot in sv.class_induced_slots(name) for r in slot_ranges(slot) if r in role_classes)
        )
        for name in class_names
    }
    targeted_first = sorted(class_names, key=lambda name: name in role_classes)
    return back_edges(targeted_first, references)


def back_edges(nodes: list[str], successors: dict[str, list[str]]) -> set[tuple[str, str]]:
    """Edges that close a cycle when the graph is searched depth-first from *nodes*, in that order.

    Removing them leaves the graph without cycles.

    >>> sorted(back_edges(["a"], {"a": ["b"], "b": ["a", "c"], "c": ["c"]}))
    [('b', 'a'), ('c', 'c')]
    """
    found: set[tuple[str, str]] = set()
    finished: set[str] = set()
    for start in nodes:
        if start in finished:
            continue
        # Iterative depth-first search: each stack entry is a node and the iterator over its successors.
        on_path = {start}
        stack = [(start, iter(successors.get(start, ())))]
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                stack.pop()
                on_path.discard(node)
                finished.add(node)
            elif child in on_path:
                found.add((node, child))
            elif child not in finished:
                on_path.add(child)
                stack.append((child, iter(successors.get(child, ()))))
    return found


def role_union_shape_name(sv: SchemaView, class_uri: str) -> str:
    """Local name of the role union shape for *class_uri*: the IRI's local name followed by ``Roles``.

    Underscores are appended while a class of the schema already has that name.
    """
    local_name = re.split(r"[#/:]", class_uri.rstrip("#/"))[-1]
    name = f"{local_name}Roles"
    class_names = {camelcase(cls) for cls in sv.all_classes()}
    while name in class_names:
        name += "_"
    return name
