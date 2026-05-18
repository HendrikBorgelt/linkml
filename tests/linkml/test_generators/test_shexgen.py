import json
import sys

import pytest
from pyshex.evaluate import evaluate

from linkml.generators.jsonldcontextgen import ContextGenerator
from linkml.generators.shexgen import ShExGenerator
from linkml_runtime.dumpers import json_dumper, rdf_dumper
from linkml_runtime.loaders import yaml_loader
from tests.linkml.test_generators.test_pythongen import make_python


@pytest.mark.skipif(sys.version_info < (3, 8), reason="ShEx has issues with python 3.7 at the moment")
def test_shex(kitchen_sink_path, input_path, tmp_path):
    """tests generation of shex and subsequent evaluation"""
    kitchen_module = make_python(kitchen_sink_path)
    data = input_path("kitchen_sink_inst_01.yaml")
    inst = yaml_loader.load(data, target_class=kitchen_module.Dataset)
    shexstr = ShExGenerator(kitchen_sink_path, mergeimports=True).serialize(collections=False)
    assert "<Person> CLOSED {" in shexstr
    assert "<has_familial_relationships> @<FamilialRelationship> * ;" in shexstr
    # re-enable below test when linkml/linkml#1914 is fixed
    # assert "<type> [ bizcodes:001 bizcodes:002 bizcodes:003 bizcodes:004 ] ?" in shexstr
    # validation
    # TODO: provide starting shape
    ctxt = ContextGenerator(kitchen_sink_path, mergeimports=True).serialize()
    inst = yaml_loader.load(data, target_class=kitchen_module.Dataset)

    # TODO: turn this into an actual test
    with open(tmp_path / "shexgen_log.txt", "w") as log:
        log.write(json_dumper.dumps(element=inst, contexts=ctxt))
        try:
            g = rdf_dumper.as_rdf_graph(element=inst, contexts=ctxt)
        except Exception as e:
            if "URL could not be dereferenced" in str(e):
                print("WARNING: non-modified version of pyld detected. RDF dumping test skipped")
                return
            raise e
        nodes = set()
        for s, p, o in g.triples((None, None, None)):
            nodes.add(s)
        for node in nodes:
            r = evaluate(g, shexstr, focus=node)

            log.write(f"Eval {node} = {r}\n")
            #               start="http://example.org/model/FriendlyPerson",
            #            focus="http://example.org/people/42")


def test_subproperty_of_generates_value_constraint():
    """Test that subproperty_of generates NodeConstraint with slot descendants."""
    schema_yaml = """
id: https://example.org/test
name: test

prefixes:
  ex: https://example.org/
  linkml: https://w3id.org/linkml/

imports:
  - linkml:types

default_prefix: ex

slots:
  related_to:
    description: Root predicate
    slot_uri: ex:related_to
  causes:
    is_a: related_to
    slot_uri: ex:causes
  treats:
    is_a: related_to
    slot_uri: ex:treats

  predicate:
    range: uriorcurie
    subproperty_of: related_to

classes:
  Association:
    slots:
      - predicate
"""
    gen = ShExGenerator(schema_yaml, format="json")
    shex_json = gen.serialize()
    shex_data = json.loads(shex_json)

    # Find the Association shape
    association_shape = None
    for shape in shex_data.get("shapes", []):
        shape_id = shape.get("id", "")
        if "Association" in shape_id:
            association_shape = shape
            break

    assert association_shape is not None, "Should find Association shape"

    # Navigate to find the predicate constraint
    # Shape structure: expression -> expressions -> [constraints]
    expression = association_shape.get("expression", {})
    expressions = expression.get("expressions", [expression])

    predicate_constraint = None
    for expr in expressions:
        if isinstance(expr, dict):
            predicate = expr.get("predicate", "")
            if "predicate" in predicate:
                predicate_constraint = expr
                break
            # Check nested expressions
            nested_exprs = expr.get("expressions", [])
            for nested in nested_exprs:
                if isinstance(nested, dict):
                    predicate = nested.get("predicate", "")
                    if "predicate" in predicate:
                        predicate_constraint = nested
                        break

    assert predicate_constraint is not None, "Should find predicate constraint"

    # Check that valueExpr has values list
    value_expr = predicate_constraint.get("valueExpr", {})
    values = value_expr.get("values", [])

    assert len(values) == 3, f"Should have 3 values, got {len(values)}"
    # Values should be the URIs of the slot hierarchy
    expected_uris = {
        "https://example.org/causes",
        "https://example.org/related_to",
        "https://example.org/treats",
    }
    assert set(values) == expected_uris


def test_subproperty_of_with_deeper_hierarchy():
    """Test that subproperty_of includes all descendants, not just direct children."""
    schema_yaml = """
id: https://example.org/test
name: test

prefixes:
  ex: https://example.org/
  linkml: https://w3id.org/linkml/

imports:
  - linkml:types

default_prefix: ex

slots:
  related_to:
    slot_uri: ex:related_to
  causes:
    is_a: related_to
    slot_uri: ex:causes
  directly_causes:
    is_a: causes
    slot_uri: ex:directly_causes
  treats:
    is_a: related_to
    slot_uri: ex:treats

  predicate:
    range: uriorcurie
    subproperty_of: related_to

classes:
  Association:
    slots:
      - predicate
"""
    gen = ShExGenerator(schema_yaml, format="json")
    shex_json = gen.serialize()
    shex_data = json.loads(shex_json)

    # Find predicate values in the generated ShEx
    values_found = []
    for shape in shex_data.get("shapes", []):
        shape_id = shape.get("id", "")
        if "Association" in shape_id:
            expression = shape.get("expression", {})
            for expr in expression.get("expressions", [expression]):
                if isinstance(expr, dict):
                    for nested in expr.get("expressions", [expr]):
                        if isinstance(nested, dict):
                            predicate = nested.get("predicate", "")
                            if "predicate" in predicate:
                                value_expr = nested.get("valueExpr", {})
                                values_found = value_expr.get("values", [])

    # Should include grandchild (directly_causes)
    expected_uris = {
        "https://example.org/causes",
        "https://example.org/directly_causes",
        "https://example.org/related_to",
        "https://example.org/treats",
    }
    assert set(values_found) == expected_uris


def test_subproperty_of_can_be_disabled():
    """Test that expand_subproperty_of=False disables value constraint generation."""
    schema_yaml = """
id: https://example.org/test
name: test

prefixes:
  ex: https://example.org/
  linkml: https://w3id.org/linkml/

imports:
  - linkml:types

default_prefix: ex

slots:
  related_to:
    slot_uri: ex:related_to
  causes:
    is_a: related_to
    slot_uri: ex:causes

  predicate:
    range: uriorcurie
    subproperty_of: related_to

classes:
  Association:
    slots:
      - predicate
"""
    gen = ShExGenerator(schema_yaml, format="json", expand_subproperty_of=False)
    shex_json = gen.serialize()
    shex_data = json.loads(shex_json)

    # Find the Association shape
    association_shape = None
    for shape in shex_data.get("shapes", []):
        shape_id = shape.get("id", "")
        if "Association" in shape_id:
            association_shape = shape
            break

    assert association_shape is not None

    # Navigate to find the predicate constraint
    expression = association_shape.get("expression", {})
    expressions = expression.get("expressions", [expression])

    predicate_constraint = None
    for expr in expressions:
        if isinstance(expr, dict):
            for nested in expr.get("expressions", [expr]):
                if isinstance(nested, dict):
                    predicate = nested.get("predicate", "")
                    if "predicate" in predicate:
                        predicate_constraint = nested
                        break

    assert predicate_constraint is not None

    # valueExpr should NOT have values list when disabled
    value_expr = predicate_constraint.get("valueExpr", {})
    assert "values" not in value_expr, "Should not have values list when expand_subproperty_of=False"


def test_subproperty_of_with_slot_usage():
    """Test that slot_usage subproperty_of narrows the constraint."""
    schema_yaml = """
id: https://example.org/test
name: test

prefixes:
  ex: https://example.org/
  linkml: https://w3id.org/linkml/

imports:
  - linkml:types

default_prefix: ex

slots:
  related_to:
    slot_uri: ex:related_to
  causes:
    is_a: related_to
    slot_uri: ex:causes
  directly_causes:
    is_a: causes
    slot_uri: ex:directly_causes
  treats:
    is_a: related_to
    slot_uri: ex:treats

  predicate:
    range: uriorcurie

classes:
  Association:
    slots:
      - predicate
  CausalAssociation:
    is_a: Association
    slot_usage:
      predicate:
        subproperty_of: causes
"""
    gen = ShExGenerator(schema_yaml, format="json")
    shex_json = gen.serialize()
    shex_data = json.loads(shex_json)

    # Find the CausalAssociation shape and its predicate values
    values_found = []
    for shape in shex_data.get("shapes", []):
        shape_id = shape.get("id", "")
        if "CausalAssociation" in shape_id and "_tes" not in shape_id:
            expression = shape.get("expression", {})
            for expr in expression.get("expressions", [expression]):
                if isinstance(expr, dict):
                    for nested in expr.get("expressions", [expr]):
                        if isinstance(nested, dict):
                            predicate = nested.get("predicate", "")
                            if "predicate" in predicate:
                                value_expr = nested.get("valueExpr", {})
                                values_found = value_expr.get("values", [])

    # Should only include causes and its descendants, not treats
    expected_uris = {
        "https://example.org/causes",
        "https://example.org/directly_causes",
    }
    assert set(values_found) == expected_uris


# ── class_closed tests ────────────────────────────────────────────────────────


def test_shex_explicitly_closed_is_closed(input_path):
    """ExplicitlyClosed (class_closed: true) → CLOSED keyword in ShEx output."""
    shexstr = ShExGenerator(input_path("class_closed_test.yaml"), mergeimports=True).serialize(collections=False)
    assert "<ExplicitlyClosed> CLOSED {" in shexstr, (
        "Expected '<ExplicitlyClosed> CLOSED {' in ShEx output for a class with class_closed: true. "
        "Check shexgen.py end_class() — it must call schemaview.is_class_closed() and set "
        "self.shape.closed = True for explicitly closed classes."
    )


def test_shex_explicitly_open_is_not_closed(input_path):
    """ExplicitlyOpen (class_closed: false) → no CLOSED keyword in ShEx output."""
    shexstr = ShExGenerator(input_path("class_closed_test.yaml"), mergeimports=True).serialize(collections=False)
    assert "<ExplicitlyOpen> CLOSED {" not in shexstr, (
        "ExplicitlyOpen has class_closed: false but CLOSED appeared in the ShEx output. "
        "Check shexgen.py end_class() — is_class_closed() must return False for this class."
    )
    assert "<ExplicitlyOpen> {" in shexstr, (
        "Expected '<ExplicitlyOpen> {' (open shape) in ShEx output for class_closed: false class."
    )


def test_shex_unannotated_defaults_to_closed(input_path):
    """Unannotated class (no class_closed annotation) → CLOSED by default."""
    shexstr = ShExGenerator(input_path("class_closed_test.yaml"), mergeimports=True).serialize(collections=False)
    assert "<Unannotated> CLOSED {" in shexstr, (
        "Unannotated class must default to CLOSED in ShEx output to preserve existing behaviour. "
        "Check that is_class_closed() uses default=True in shexgen.py end_class()."
    )


def test_shex_mixin_always_open_regardless_of_class_closed(input_path):
    """ClosedMixin has class_closed: true but is a mixin → must NOT be CLOSED in ShEx."""
    shexstr = ShExGenerator(input_path("class_closed_test.yaml"), mergeimports=True).serialize(collections=False)
    assert "<ClosedMixin> CLOSED {" not in shexstr, (
        "ClosedMixin is a mixin with class_closed: true, but CLOSED appeared in the ShEx output. "
        "Mixins must always be open in ShEx regardless of class_closed, because their shapes "
        "are structural only (they combine into concrete shapes that may themselves be closed)."
    )
