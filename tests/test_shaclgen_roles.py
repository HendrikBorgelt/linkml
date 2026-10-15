"""
Tests for the SHACL generator's handling of shared ``class_uri`` / ``slot_uri`` and recommended slots.

1. Slots of one class sharing a ``slot_uri`` are merged into one property shape (``sh:or``) with
   per-slot cardinalities as qualified counts, instead of conflicting property shapes.
2. Classes sharing a ``class_uri`` get distinct shapes; only the group's root is targeted and the
   other classes are role shapes reached through the slots that name them.
3. ``recommended: true`` slots become ``sh:Warning`` constraints, scoped through the referencing slot.
"""

import logging

import pytest
import rdflib
from pyshacl import validate
from rdflib import RDF, SH, URIRef
from rdflib.collection import Collection

from linkml.generators.shaclgen import ShaclGenerator

EX = "http://example.org/"

HEADER = """
id: http://example.org/roles
name: roles
prefixes:
  ex: http://example.org/
  dcterms: http://purl.org/dc/terms/
  dcat: http://www.w3.org/ns/dcat#
  prov: http://www.w3.org/ns/prov#
  linkml: https://w3id.org/linkml/
default_prefix: ex
imports:
  - linkml:types
"""

SLOT_COLLISION_SCHEMA = (
    HEADER
    + """
classes:
  EvaluatedEntity:
    class_uri: ex:EvaluatedEntity
  EvaluatedActivity:
    class_uri: ex:EvaluatedActivity
  Dataset:
    class_uri: dcat:Dataset
    slots:
      - is_about_entity
      - is_about_activity
slots:
  is_about_entity:
    slot_uri: dcterms:subject
    range: EvaluatedEntity
    required: true
    multivalued: true
  is_about_activity:
    slot_uri: dcterms:subject
    range: EvaluatedActivity
    multivalued: true
    maximum_cardinality: 1
"""
)

ROLE_SCHEMA = (
    HEADER
    + """
classes:
  Thing:
    class_uri: ex:Thing
  Activity:
    class_uri: prov:Activity
    slots:
      - title
  DataGeneratingActivity:
    is_a: Activity
    class_uri: prov:Activity
    slots:
      - used
  Simulation:
    is_a: Activity
    class_uri: prov:Activity
    slots:
      - software
  Dataset:
    class_uri: dcat:Dataset
    slots:
      - was_generated_by
  Study:
    class_uri: ex:Study
    slots:
      - study_generated_by
slots:
  title:
    slot_uri: dcterms:title
    range: string
  used:
    slot_uri: prov:used
    range: Thing
    multivalued: true
    recommended: true
  software:
    slot_uri: ex:software
    range: string
    recommended: true
  was_generated_by:
    slot_uri: prov:wasGeneratedBy
    range: DataGeneratingActivity
    required: true
  study_generated_by:
    slot_uri: prov:wasGeneratedBy
    range: Simulation
"""
)

DATA_PREFIXES = """
@prefix ex: <http://example.org/> .
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix prov: <http://www.w3.org/ns/prov#> .
"""


def shapes(tmp_path, schema: str, **kwargs) -> rdflib.Graph:
    """Generate the SHACL graph for *schema*, with native names unless told otherwise (role shapes need them)."""
    kwargs.setdefault("use_class_uri_names", False)
    path = tmp_path / "schema.yaml"
    path.write_text(schema)
    return ShaclGenerator(str(path), mergeimports=True, **kwargs).as_graph()


def run(shapes_graph: rdflib.Graph, data: str) -> tuple[bool, set[tuple[str, str]]]:
    """Validate *data*; return conformance (warnings tolerated) and ``(focus, severity)`` of every result."""
    data_graph = rdflib.Graph().parse(data=DATA_PREFIXES + data, format="turtle")
    conforms, report, _ = validate(data_graph, shacl_graph=shapes_graph, inference="none", allow_warnings=True)
    results = {
        (str(report.value(r, SH.focusNode)).removeprefix(EX), str(report.value(r, SH.resultSeverity)).split("#")[-1])
        for r in report.subjects(RDF.type, SH.ValidationResult)
    }
    return conforms, results


# ---------------------------------------------------------------------------
# 1. slots sharing a slot_uri
# ---------------------------------------------------------------------------
def test_slots_sharing_slot_uri_become_one_property_shape(tmp_path):
    g = shapes(tmp_path, SLOT_COLLISION_SCHEMA, use_class_uri_names=True)
    dataset = URIRef("http://www.w3.org/ns/dcat#Dataset")
    subject = URIRef("http://purl.org/dc/terms/subject")
    on_subject = [p for p in g.objects(dataset, SH.property) if (p, SH.path, subject) in g]
    merged = [p for p in on_subject if (p, SH["or"], None) in g]
    assert len(merged) == 1
    branches = list(Collection(g, g.value(merged[0], SH["or"])))
    assert {g.value(b, SH["class"]) for b in branches} == {
        URIRef(EX + "EvaluatedEntity"),
        URIRef(EX + "EvaluatedActivity"),
    }
    # the merged shape itself has no counts; the slots' own cardinalities are qualified
    assert (merged[0], SH.maxCount, None) not in g
    qualified = [p for p in on_subject if p != merged[0]]
    assert {
        (g.value(p, SH.qualifiedMinCount) is not None, g.value(p, SH.qualifiedMaxCount) is not None) for p in qualified
    } == {
        (True, False),
        (False, True),
    }


AMBIGUOUS_SCHEMA = (
    HEADER
    + """
classes:
  A:
    class_uri: ex:Shared
  B:
    class_uri: ex:Shared
  C:
    is_a: B
    class_uri: ex:Shared
"""
)


def test_class_uri_names_keep_merging_classes_that_share_a_class_uri(tmp_path, caplog):
    """Merging classes into one shape is valid SHACL and stays as it was; nothing is warned about."""
    g = shapes(tmp_path, AMBIGUOUS_SCHEMA, use_class_uri_names=True)
    assert set(g.subjects(RDF.type, SH.NodeShape)) == {URIRef(EX + "Shared")}
    assert (URIRef(EX + "Shared"), SH.targetClass, URIRef(EX + "Shared")) in g
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


HIERARCHY_SCHEMA = (
    HEADER
    + """
classes:
  Single:
    class_uri: ex:Shared
    slots: [s]
  Parent:
    class_uri: ex:Shared
    slots: [s]
  Child:
    is_a: Parent
    class_uri: ex:Shared
    slots: [s]
slots:
  s:
    range: string
"""
)


@pytest.mark.parametrize("use_class_uri_names", [True, False])
def test_no_warning_without_shared_class_uri(tmp_path, caplog, use_class_uri_names):
    schema = HEADER + "classes:\n  A:\n    class_uri: ex:A\n    slots: [s]\nslots:\n  s:\n    range: string\n"
    shapes(tmp_path, schema, use_class_uri_names=use_class_uri_names)
    # other libraries (schemaview) log at INFO; only warnings from the generator matter here
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_warns_when_a_role_loosens_its_parent(tmp_path, caplog):
    schema = (
        HEADER
        + """
classes:
  Parent:
    class_uri: ex:Shared
    slots: [s]
    slot_usage:
      s:
        required: true
  Child:
    is_a: Parent
    class_uri: ex:Shared
    slot_usage:
      s:
        required: false
slots:
  s:
    range: string
"""
    )
    shapes(tmp_path, schema)
    assert "loosens slot s" in caplog.text


def test_warns_about_closed_unrelated_roots(tmp_path, caplog):
    shapes(tmp_path, AMBIGUOUS_SCHEMA)
    assert "are closed" in caplog.text
    caplog.clear()
    shapes(tmp_path, AMBIGUOUS_SCHEMA, closed=False)
    assert "are closed" not in caplog.text


def test_schema_without_collisions_is_unchanged(tmp_path):
    schema = (
        HEADER
        + """
classes:
  A:
    class_uri: ex:A
    slots: [s]
slots:
  s:
    range: string
"""
    )
    g = shapes(tmp_path, schema)
    assert (URIRef(EX + "A"), SH.targetClass, URIRef(EX + "A")) in g


# ---------------------------------------------------------------------------
# 3. recommended slots
# ---------------------------------------------------------------------------
RECOMMENDED_DATA = """
ex:d a dcat:Dataset ; prov:wasGeneratedBy ex:a1 .
ex:a1 a prov:Activity ; dcterms:title 'no inputs' .
ex:s a ex:Study ; prov:wasGeneratedBy ex:a2 .
ex:a2 a prov:Activity .
ex:a3 a prov:Activity .
"""


def test_recommended_is_ignored_by_default(tmp_path):
    g = shapes(tmp_path, ROLE_SCHEMA)
    assert (None, SH.severity, SH.Warning) not in g
    assert run(g, RECOMMENDED_DATA) == (True, set())


def test_recommended_warnings_follow_the_referencing_slot(tmp_path):
    g = shapes(tmp_path, ROLE_SCHEMA, recommended="warning")
    conforms, results = run(g, RECOMMENDED_DATA)
    # conforms: only warnings. The dataset's own activity lacks `used`; the study's simulation lacks `software`;
    # the unreferenced activity is nobody's business, and `used` is not asked of the simulation (same predicate
    # prov:wasGeneratedBy, different role).
    assert conforms
    assert {focus for focus, _ in results if "a1" in focus or focus == "d"} >= {"d"}
    assert {focus for focus, _ in results} == {"d", "a1", "s", "a2"}
    assert {severity for _, severity in results} == {"Warning"}


def test_recommendations_are_separate_shapes(tmp_path):
    g = shapes(tmp_path, ROLE_SCHEMA, recommended="warning")
    # nothing recommended leaks into the hard shapes
    for shape in (URIRef(EX + "DataGeneratingActivity"), URIRef("http://www.w3.org/ns/dcat#Dataset")):
        assert not any((p, SH.severity, SH.Warning) in g for p in g.objects(shape, SH.property))
    recommendations = URIRef(str(EX + "DataGeneratingActivity") + "Recommendations")
    assert (recommendations, RDF.type, SH.NodeShape) in g
    assert (recommendations, SH.targetClass, None) not in g


def test_invalid_recommended_mode_is_rejected(tmp_path):
    path = tmp_path / "schema.yaml"
    path.write_text(ROLE_SCHEMA)
    with pytest.raises(ValueError, match="recommended"):
        ShaclGenerator(str(path), recommended="error")


# ---------------------------------------------------------------------------
# 4. Native names (issue #3011)
#
# Issue #3011 introduced use_class_uri_names=False to get one shape per LinkML class instead of one merged
# shape per class_uri. Its test that every class keeps sh:targetClass is retired on purpose: several targeted
# shapes on one RDF class are conjoined and, being closed, can never all be satisfied. What the option was
# for - separate, LinkML-named shapes - is still checked below.
# ---------------------------------------------------------------------------


def test_issue_3011_distinct_shapes_with_native_names(tmp_path):
    """
    Test for issue #3011: Distinct shapes with native names.

    When using native names (use_class_uri_names=False), shapes are created per LinkML class
    even if class_uri is identical.
    """
    test_schema = """
id: http://example.org/nonmerge
name: distinct_shape_test
prefixes:
  prov: http://www.w3.org/ns/prov#
  linkml: https://w3id.org/linkml/
default_prefix: http://example.org/test#

imports:
  - linkml:types

classes:
  Entity:
    description: Base entity
    class_uri: prov:Entity
    slots: [a]

  EvaluatedEntity:
    description: Evaluated version
    class_uri: prov:Entity
    slots: [b]

  ThirdEntity:
    description: Third class
    class_uri: prov:Entity
    slots: [c]

slots:
  a:
    range: string
  b:
    range: string
  c:
    range: string
"""
    schema_path = tmp_path / "test_schema.yaml"
    schema_path.write_text(test_schema)

    shaclstr = ShaclGenerator(
        str(schema_path),
        mergeimports=True,
        use_class_uri_names=False,
    ).serialize()

    g = rdflib.Graph()
    g.parse(data=shaclstr, format="turtle")

    # the generated dummy shape of a group without a common parent is not a class shape
    shapes = [s for s in g.subjects(RDF.type, SH.NodeShape) if not str(s).endswith("Roles")]

    # EXPECTED: 3 distinct shapes (1 per LinkML class)
    assert len(shapes) == 3, f"Expected 3 separate shapes but found {len(shapes)}"

    # Check that each class name appears as the shape URI suffix (after the colon)
    for cname in ["Entity", "EvaluatedEntity", "ThirdEntity"]:
        matching = [s for s in shapes if str(s).endswith(f":{cname}")]
        assert len(matching) == 1, f"{cname}: expected 1 shape, found {matching}"


def test_issue_3011_inheritance_with_shared_class_uri(tmp_path):
    """
    Test that inheritance (is_a) works correctly with shared class_uri.

    When a child class inherits from a parent with a shared class_uri,
    native names mode should still produce distinct shapes.
    """
    test_schema = """
id: http://example.org/inheritance
name: inheritance_test
prefixes:
  schema: http://schema.org/
  linkml: https://w3id.org/linkml/
default_prefix: http://example.org/test#

imports:
  - linkml:types

classes:
  BaseThing:
    class_uri: schema:Thing
    slots: [name]

  ExtendedThing:
    class_uri: schema:Thing
    is_a: BaseThing
    slots: [description]

  DerivedFromExtended:
    is_a: ExtendedThing
    slots: [extra]

slots:
  name:
    range: string
  description:
    range: string
  extra:
    range: string
"""
    schema_path = tmp_path / "test_schema.yaml"
    schema_path.write_text(test_schema)

    shaclstr = ShaclGenerator(
        str(schema_path),
        mergeimports=True,
        use_class_uri_names=False,
    ).serialize()

    g = rdflib.Graph()
    g.parse(data=shaclstr, format="turtle")

    # the generated dummy shape of a group without a common parent is not a class shape
    shapes = [s for s in g.subjects(RDF.type, SH.NodeShape) if not str(s).endswith("Roles")]

    # Should have 3 distinct shapes
    assert len(shapes) == 3, f"Expected 3 shapes, found {len(shapes)}"

    # Verify each LinkML class has its own shape
    shape_names = {str(s) for s in shapes}
    assert any(s.endswith(":BaseThing") for s in shape_names)
    assert any(s.endswith(":ExtendedThing") for s in shape_names)
    assert any(s.endswith(":DerivedFromExtended") for s in shape_names)


def test_issue_3011_mixin_with_shared_class_uri(tmp_path):
    """
    Test that mixins work correctly with shared class_uri.

    Mixins that share a class_uri should still produce distinct shapes
    when using native names mode.
    """
    test_schema = """
id: http://example.org/mixin
name: mixin_test
prefixes:
  schema: http://schema.org/
  linkml: https://w3id.org/linkml/
default_prefix: http://example.org/test#

imports:
  - linkml:types

classes:
  Identifiable:
    mixin: true
    class_uri: schema:Thing
    slots: [id]

  Named:
    mixin: true
    class_uri: schema:Thing
    slots: [name]

  ConcreteEntity:
    mixins:
      - Identifiable
      - Named
    slots: [data]

slots:
  id:
    range: string
  name:
    range: string
  data:
    range: string
"""
    schema_path = tmp_path / "test_schema.yaml"
    schema_path.write_text(test_schema)

    shaclstr = ShaclGenerator(
        str(schema_path),
        mergeimports=True,
        use_class_uri_names=False,
    ).serialize()

    g = rdflib.Graph()
    g.parse(data=shaclstr, format="turtle")

    # the generated dummy shape of a group without a common parent is not a class shape
    shapes = [s for s in g.subjects(RDF.type, SH.NodeShape) if not str(s).endswith("Roles")]

    # Should have 3 distinct shapes (2 mixins + 1 concrete)
    assert len(shapes) == 3, f"Expected 3 shapes, found {len(shapes)}"

    # Verify each LinkML class has its own shape
    shape_names = {str(s) for s in shapes}
    assert any(s.endswith(":Identifiable") for s in shape_names)
    assert any(s.endswith(":Named") for s in shape_names)
    assert any(s.endswith(":ConcreteEntity") for s in shape_names)

    # Verify mixin shapes are marked as not closed (sh:closed false)
    for shape in shapes:
        shape_name = str(shape)
        closed_values = list(g.objects(shape, SH.closed))
        if ":Identifiable" in shape_name or ":Named" in shape_name:
            assert any(v.toPython() is False for v in closed_values), (
                f"Mixin shape {shape_name} should have sh:closed false"
            )


# ---------------------------------------------------------------------------
# 5. value constraints of merged slots stay with their own slot
# ---------------------------------------------------------------------------
PATTERN_SCHEMA = (
    HEADER
    + """
classes:
  Thing:
    class_uri: ex:Thing
    slots: [doi, homepage]
slots:
  doi:
    slot_uri: dcterms:identifier
    range: string
    pattern: '^10\\.'
  homepage:
    slot_uri: dcterms:identifier
    range: string
    pattern: '^https?://'
"""
)


@pytest.mark.parametrize(
    ("values", "violating"),
    [
        ('"10.1/a", "https://x.org"', set()),
        ('"10.1/a"', set()),
        ('"10.1/a", "hello"', {"t"}),
    ],
    ids=["each value matches one pattern", "one value", "value matching no pattern"],
)
def test_merged_slots_keep_their_patterns(tmp_path, caplog, values, violating):
    g = shapes(tmp_path, PATTERN_SCHEMA)
    _, results = run(g, f"ex:t a ex:Thing ; dcterms:identifier {values} .")
    assert {focus for focus, _ in results} == violating
    assert "not carried over" not in caplog.text


QUALIFIED_SCHEMA = (
    HEADER
    + """
classes:
  Quantitative:
    class_uri: ex:Quantitative
  Qualitative:
    class_uri: ex:Qualitative
  Sample:
    class_uri: ex:Sample
    slots: [has_quantitative, has_qualitative]
slots:
  has_quantitative:
    slot_uri: dcterms:relation
    range: Quantitative
    required: true
  has_qualitative:
    slot_uri: dcterms:relation
    range: Qualitative
    multivalued: true
"""
)


@pytest.mark.parametrize(
    ("values", "violating"),
    [
        ("ex:q1, ex:l1, ex:l2", set()),
        ("ex:l1", {"s"}),
        ("ex:q1, ex:q2", {"s"}),
        ("ex:q1, ex:other", {"s"}),
    ],
    ids=["valid", "missing quantitative", "two quantitative", "value of a foreign class"],
)
def test_merged_slots_with_different_object_ranges_keep_their_own_cardinality(tmp_path, values, violating):
    g = shapes(tmp_path, QUALIFIED_SCHEMA, use_class_uri_names=True)
    data = (
        "ex:q1 a ex:Quantitative . ex:q2 a ex:Quantitative . ex:l1 a ex:Qualitative . ex:l2 a ex:Qualitative . "
        f"ex:other a ex:Thing . ex:s a ex:Sample ; dcterms:relation {values} ."
    )
    _, results = run(g, data)
    assert {focus for focus, _ in results} == violating


# ---------------------------------------------------------------------------
# 6. groups with one parent, and groups with several parents (dummy shape)
# ---------------------------------------------------------------------------
SINGLE_ROOT_SCHEMA = (
    HEADER
    + """
classes:
  Parent:
    class_uri: ex:Shared
    slots: [a]
  Child:
    is_a: Parent
    class_uri: ex:Shared
    slots: [b]
slots:
  a:
    range: string
  b:
    range: string
"""
)


def test_single_parent_group_targets_the_parent_and_builds_no_dummy(tmp_path, caplog):
    g = shapes(tmp_path, SINGLE_ROOT_SCHEMA)
    assert set(g.subjects(SH.targetClass, None)) == {URIRef(EX + "Parent")}
    assert (URIRef(EX + "SharedRoles"), None, None) not in g
    assert "only Parent gets sh:targetClass" in caplog.text


DUMMY_SCHEMA = (
    HEADER
    + """
classes:
  A:
    class_uri: ex:Shared
    slots: [a]
  B:
    class_uri: ex:Shared
    slots: [b]
  C:
    is_a: B
    class_uri: ex:Shared
    slots: [c]
slots:
  a:
    range: string
    required: true
  b:
    range: string
    required: true
  c:
    range: string
"""
)


def test_several_parents_get_a_dummy_shape_that_lists_every_role(tmp_path, caplog):
    g = shapes(tmp_path, DUMMY_SCHEMA)
    dummy = URIRef(EX + "SharedRoles")
    assert set(g.subjects(SH.targetClass, None)) == {dummy}
    assert (dummy, SH.targetClass, URIRef(EX + "Shared")) in g
    assert (dummy, SH.closed, rdflib.Literal(False)) in g
    branches = list(Collection(g, g.value(dummy, SH["or"])))
    assert {g.value(b, SH.node) for b in branches} == {URIRef(EX + n) for n in "ABC"}
    assert "dummy shape" in caplog.text


@pytest.mark.parametrize(
    ("data", "conforms"),
    [
        ('ex:n a ex:Shared ; ex:a "x" .', True),
        ('ex:n a ex:Shared ; ex:b "x" .', True),
        # a node shaped like the subclass passes through the subclass role
        ('ex:n a ex:Shared ; ex:b "x" ; ex:c "y" .', True),
        # properties of both unrelated parents: each closed role rejects the other's property
        ('ex:n a ex:Shared ; ex:a "x" ; ex:b "y" .', False),
        ('ex:n a ex:Shared ; ex:other "x" .', False),
    ],
    ids=["role A", "role B", "subclass of B", "properties of both parents", "no role"],
)
def test_dummy_requires_at_least_one_role(tmp_path, data, conforms):
    assert run(shapes(tmp_path, DUMMY_SCHEMA), data)[0] is conforms


def test_dummy_shape_name_does_not_clash_with_a_class(tmp_path):
    schema = DUMMY_SCHEMA.replace("classes:\n", "classes:\n  SharedRoles:\n    slots: [c]\n", 1)
    g = shapes(tmp_path, schema)
    assert (URIRef(EX + "SharedRoles_"), SH.targetClass, URIRef(EX + "Shared")) in g
