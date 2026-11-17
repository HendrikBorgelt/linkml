"""
Tests for the SHACL generator's handling of the per-class ``extra_slots`` metaslot.

1. An explicit ``extra_slots`` on a class decides ``sh:closed``, whatever ``--closed``/``--non-closed`` say:
   ``allowed: true`` opens the shape; ``allowed: false``, or neither ``allowed`` nor ``range_expression``, closes it.
   Without ``extra_slots`` the generator option decides, and mixins and abstract classes stay open.
2. A ``range_expression`` cannot be enforced in SHACL Core: the shape is left open and a warning is logged.
3. ``extra_slots`` is not inherited (see linkml#3887, linkml#4145).
4. Only closed shapes get ``sh:ignoredProperties`` (linkml#3539).
5. Classes sharing a ``class_uri``: a shared shape is closed only if all of its classes are, and an open role
   under a closed root gets a role union shape, which is always open.
"""

import pytest
import rdflib
from pyshacl import validate
from rdflib import RDF, SH, Literal, URIRef

from linkml.generators.shaclgen import ShaclGenerator

EX = "http://example.org/"
PROV = "http://www.w3.org/ns/prov#"

HEADER = """
id: http://example.org/extra
name: extra
prefixes:
  ex: http://example.org/
  dcat: http://www.w3.org/ns/dcat#
  dcterms: http://purl.org/dc/terms/
  prov: http://www.w3.org/ns/prov#
  linkml: https://w3id.org/linkml/
default_prefix: ex
imports:
  - linkml:types
"""

DATA_PREFIXES = """
@prefix ex: <http://example.org/> .
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix prov: <http://www.w3.org/ns/prov#> .
"""

NAMING_MODES = pytest.mark.parametrize("use_class_uri_names", [True, False], ids=["class_uri_names", "native_names"])
CLOSED_OPTIONS = pytest.mark.parametrize("closed", [True, False], ids=["closed", "non_closed"])


def generate(tmp_path, schema: str, **kwargs) -> rdflib.Graph:
    """Generate the SHACL shapes graph for *schema*."""
    path = tmp_path / "schema.yaml"
    path.write_text(schema)
    return ShaclGenerator(str(path), mergeimports=True, **kwargs).as_graph()


def failing_nodes(shapes_graph: rdflib.Graph, data: str) -> set[str]:
    """Validate *data* against *shapes_graph* with pyshacl; return the local names of the nodes that fail."""
    data_graph = rdflib.Graph().parse(data=DATA_PREFIXES + data, format="turtle")
    _, report, _ = validate(data_graph, shacl_graph=shapes_graph, inference="none")
    return {
        str(report.value(result, SH.focusNode)).removeprefix(EX)
        for result in report.subjects(RDF.type, SH.ValidationResult)
    }


# ---------------------------------------------------------------------------
# 1.-4. closedness of single classes
# ---------------------------------------------------------------------------
EXTRA_SLOTS_SCHEMA = (
    HEADER
    + """
classes:
  Default:
    slots: [name]
  Allowed:
    slots: [name]
    extra_slots:
      allowed: true
  Forbidden:
    slots: [name]
    extra_slots:
      allowed: false
  Unspecified:
    description: Neither allowed nor range_expression, which the metamodel defines as closed.
    slots: [name]
    extra_slots: {}
  StringExtras:
    slots: [name]
    extra_slots:
      range_expression:
        range: string
  ForbiddenDespiteRange:
    description: allowed takes precedence over range_expression, as in the JSON Schema generator.
    slots: [name]
    extra_slots:
      allowed: false
      range_expression:
        range: string
  AbstractForbidden:
    abstract: true
    slots: [name]
    extra_slots:
      allowed: false
  MixinAllowed:
    mixin: true
    slots: [name]
    extra_slots:
      allowed: true
  OpenParent:
    slots: [name]
    extra_slots:
      allowed: true
  ChildOfOpenParent:
    is_a: OpenParent
  ClosedParent:
    slots: [name]
  OpenChild:
    is_a: ClosedParent
    extra_slots:
      allowed: true
slots:
  name:
    range: string
"""
)

EXPECTED_CLOSED = {
    # class: (sh:closed with --closed, sh:closed with --non-closed)
    "Default": (True, False),
    "Allowed": (False, False),
    "Forbidden": (True, True),
    "Unspecified": (True, True),
    "StringExtras": (False, False),
    "ForbiddenDespiteRange": (True, True),
    "AbstractForbidden": (True, True),
    "MixinAllowed": (False, False),
    # extra_slots is not inherited: the child falls back to the generator option
    "ChildOfOpenParent": (True, False),
    "ClosedParent": (True, False),
    "OpenChild": (False, False),
}


@NAMING_MODES
@CLOSED_OPTIONS
def test_extra_slots_decide_sh_closed_and_ignored_properties(tmp_path, closed, use_class_uri_names):
    g = generate(tmp_path, EXTRA_SLOTS_SCHEMA, closed=closed, use_class_uri_names=use_class_uri_names)
    for class_name, expected in EXPECTED_CLOSED.items():
        shape = URIRef(EX + class_name)
        is_closed = expected[0] if closed else expected[1]
        # an open shape says sh:closed false only with --closed; with --non-closed it says nothing
        statement = [Literal(True)] if is_closed else [Literal(False)] if closed else []
        assert list(g.objects(shape, SH.closed)) == statement, class_name
        assert len(list(g.objects(shape, SH.ignoredProperties))) == (1 if is_closed else 0), class_name


def node_of_each_class(extra: str) -> str:
    """Turtle with one node per class of :data:`EXPECTED_CLOSED`, named after its class, with *extra* triples."""
    return "\n".join(f'ex:{name} a ex:{name} ; ex:name "n" {extra} .' for name in EXPECTED_CLOSED)


@NAMING_MODES
@CLOSED_OPTIONS
def test_closed_shapes_reject_an_undeclared_property_and_open_ones_accept_it(tmp_path, closed, use_class_uri_names):
    g = generate(tmp_path, EXTRA_SLOTS_SCHEMA, closed=closed, use_class_uri_names=use_class_uri_names)
    expected_failures = {name for name, expected in EXPECTED_CLOSED.items() if expected[0 if closed else 1]}
    assert failing_nodes(g, node_of_each_class('; ex:undeclared "u"')) == expected_failures
    assert failing_nodes(g, node_of_each_class("")) == set()


@NAMING_MODES
def test_range_expression_leaves_the_shape_open_and_is_warned_about(tmp_path, caplog, use_class_uri_names):
    g = generate(tmp_path, EXTRA_SLOTS_SCHEMA, use_class_uri_names=use_class_uri_names)
    warnings = [r.getMessage() for r in caplog.records if "range_expression" in r.getMessage()]
    # ForbiddenDespiteRange is closed by allowed: false, so its range_expression does not matter
    assert len(warnings) == 1
    assert "StringExtras" in warnings[0] and "not enforced" in warnings[0]
    # the expression (range: string) is not enforced: an extra integer value is accepted as well
    data = 'ex:s a ex:StringExtras ; ex:name "n" ; ex:undeclared "u", 5 .'
    assert failing_nodes(g, data) == set()


# ---------------------------------------------------------------------------
# A mixin closed by extra_slots ignores the properties of the classes that use it
# ---------------------------------------------------------------------------
CLOSED_MIXIN_SCHEMA = (
    HEADER
    + """
classes:
  Flying:
    mixin: true
    slots: [altitude]
    extra_slots:
      allowed: false
  Bird:
    mixins: [Flying]
    slots: [name]
  Sighting:
    slots: [flyer]
slots:
  name:
    range: string
  altitude:
    range: integer
  flyer:
    range: Flying
"""
)


@pytest.mark.parametrize(
    ("use_class_uri_names", "bird", "failing"),
    [
        (True, 'ex:name "Robin" ; ex:altitude 3', set()),
        (False, 'ex:name "Robin" ; ex:altitude 3', set()),
        (True, 'ex:name "Robin" ; ex:undeclared "u"', {"bird"}),
        # with native names ex:flyer is checked with sh:node ex:Flying, so the sighting fails too
        (False, 'ex:name "Robin" ; ex:undeclared "u"', {"bird", "sighting"}),
    ],
    ids=[
        "class_uri_names-declared_properties",
        "native_names-declared_properties",
        "class_uri_names-undeclared_property",
        "native_names-undeclared_property",
    ],
)
def test_a_mixin_closed_by_extra_slots_accepts_the_classes_using_it(tmp_path, use_class_uri_names, bird, failing):
    g = generate(tmp_path, CLOSED_MIXIN_SCHEMA, use_class_uri_names=use_class_uri_names)
    assert (URIRef(EX + "Flying"), SH.closed, Literal(True)) in g
    data = f"ex:sighting a ex:Sighting ; ex:flyer ex:bird . ex:bird a ex:Bird, ex:Flying ; {bird} ."
    assert failing_nodes(g, data) == failing


# ---------------------------------------------------------------------------
# 5. classes sharing a class_uri
# ---------------------------------------------------------------------------
def shared_class_uri_schema(root_extra_slots: str = "", role_extra_slots: str = "") -> str:
    """``Activity`` and its subclass ``Run`` share ``prov:Activity``; ``Dataset.generated_by`` has range ``Run``."""
    return (
        HEADER
        + f"""
classes:
  Activity:
    class_uri: prov:Activity
    slots: [title]
    {root_extra_slots}
  Run:
    is_a: Activity
    class_uri: prov:Activity
    slots: [software]
    {role_extra_slots}
  Dataset:
    class_uri: dcat:Dataset
    slots: [generated_by]
slots:
  title:
    slot_uri: dcterms:title
    range: string
  software:
    range: string
  generated_by:
    slot_uri: prov:wasGeneratedBy
    range: Run
"""
    )


ALLOWED = "extra_slots: {allowed: true}"
SHARED_DATA = 'ex:d a dcat:Dataset ; prov:wasGeneratedBy ex:run . ex:run a prov:Activity ; dcterms:title "t" ; '


def test_class_uri_names_close_a_shared_shape_only_if_every_class_is_closed(tmp_path):
    activity = URIRef(PROV + "Activity")
    both_closed = generate(tmp_path, shared_class_uri_schema(), use_class_uri_names=True)
    assert (activity, SH.closed, Literal(True)) in both_closed
    assert (activity, SH.ignoredProperties, None) in both_closed

    g = generate(tmp_path, shared_class_uri_schema(role_extra_slots=ALLOWED), use_class_uri_names=True)
    assert list(g.objects(activity, SH.closed)) == [Literal(False)]
    assert (activity, SH.ignoredProperties, None) not in g
    assert failing_nodes(g, SHARED_DATA + 'ex:undeclared "u" .') == set()


def test_native_names_give_an_open_role_under_a_closed_root_a_role_union(tmp_path, caplog):
    g = generate(tmp_path, shared_class_uri_schema(role_extra_slots=ALLOWED), use_class_uri_names=False)
    union = URIRef(EX + "ActivityRoles")
    # the closed root would reject the role's undeclared properties, so neither class is targeted
    assert set(g.subjects(SH.targetClass, URIRef(PROV + "Activity"))) == {union}
    assert list(g.objects(union, SH.closed)) == [Literal(False)]
    assert (union, SH.ignoredProperties, None) not in g
    assert (URIRef(EX + "Activity"), SH.closed, Literal(True)) in g
    assert (URIRef(EX + "Run"), SH.closed, Literal(False)) in g
    assert "['Run'] accept more than their parent Activity" in caplog.text
    assert f"Class Run (role of Activity, class_uri {PROV}Activity) accepts properties" in caplog.text
    assert failing_nodes(g, SHARED_DATA + 'ex:software "s" ; ex:undeclared "u" .') == set()


@pytest.mark.parametrize(
    ("schema_args", "closed"),
    [
        ({"root_extra_slots": ALLOWED}, True),
        ({}, False),
    ],
    ids=["open root, closed role", "both open by --non-closed"],
)
def test_native_names_keep_the_root_targeted_unless_a_role_opens_a_closed_root(tmp_path, caplog, schema_args, closed):
    g = generate(tmp_path, shared_class_uri_schema(**schema_args), use_class_uri_names=False, closed=closed)
    assert set(g.subjects(SH.targetClass, URIRef(PROV + "Activity"))) == {URIRef(EX + "Activity")}
    assert "accepts properties" not in caplog.text
    assert failing_nodes(g, SHARED_DATA + 'ex:software "s" .') == set()
