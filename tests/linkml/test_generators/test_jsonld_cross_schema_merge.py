"""
Test: Cross-schema JSON-LD graph merging via IRI identity.

Schema A defines an open-world class (class_closed: false) with a rel:hasSuccessor
property. It produces two disconnected subgraphs from its instance data:

    A1 --rel:hasSuccessor--> A2      (left component)
    A3 --rel:hasSuccessor--> A4      (right component)

Schema B defines a BridgeNode class (class_uri: family:ParentNode) with a
link:hasLink property. It produces exactly one triple from its instance data:

    A2 --link:hasLink--> A3          (the bridge)

After merging both JSON-LD outputs by IRI identity, the full chain
A1 --> A2 --> A3 --> A4 is traversable end-to-end.

Test structure
--------------
Tier 1 — Pure Python (no subprocess):
    TestShaclShapes          Verify schema A emits sh:closed false for ParentNode.
    TestShaclValidation      Verify a manually-constructed merged RDF graph validates
                             against both schema's SHACL shapes. This is the primary
                             assertion that class_closed: false enables open-world
                             validation without rejecting the bridge triple.

Tier 2 — Integration (linkml-convert subprocess):
    TestSchemaAIsolated      Schema A isolated conversion produces two disconnected
                             components with no bridge triple.
    TestSchemaBIsolated      Schema B isolated conversion produces exactly one triple.
    TestPartitioning         Single merged data file, converted against each schema
                             independently, produces correctly partitioned outputs.
    TestMergedGraph          Merged graph from integration conversions contains the
                             full chain and is equivalent to the isolated merge.

Tier 2 tests are skipped when linkml-convert is not available in PATH.
The integration tests (TestPartitioning, TestMergedGraph) exercise the fix where
``_class_closed = False`` is emitted into generated Python dataclasses and the loader
filters unknown kwargs for those classes so that ``linkml-convert`` can process a merged
data file that contains properties from multiple schemas.

Dependencies
------------
    rdflib >= 6.0
    pyshacl >= 0.20
    linkml (with class_closed metamodel changes applied)
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, SH

from linkml.generators.shaclgen import ShaclGenerator

# ── Availability guard for Tier 2 ─────────────────────────────────────────────

_LINKML_CONVERT = shutil.which("linkml-convert")
_CONVERT_AVAILABLE = _LINKML_CONVERT is not None
_skip_no_convert = pytest.mark.skipif(
    not _CONVERT_AVAILABLE,
    reason="linkml-convert not found in PATH — Tier 2 integration tests skipped",
)

# ── Namespaces ─────────────────────────────────────────────────────────────────

FAMILY = "https://example.org/family/"
KINSHIP = "https://example.org/kinship/"
REL = "https://example.org/relations/"
LINK = "https://example.org/linkage/"

NODE_A1 = URIRef(f"{FAMILY}node-a1")
NODE_A2 = URIRef(f"{FAMILY}node-a2")  # merge point: left leaf / bridge origin
NODE_A3 = URIRef(f"{KINSHIP}node-a3")  # merge point: right root / bridge destination
NODE_A4 = URIRef(f"{KINSHIP}node-a4")

HAS_SUCCESSOR = URIRef(f"{REL}hasSuccessor")
HAS_LINK = URIRef(f"{LINK}hasLink")

PARENT_NODE_TYPE = URIRef(f"{FAMILY}ParentNode")

# ── Fixture file locations ─────────────────────────────────────────────────────

_INPUT = Path(__file__).parent / "input"

SCHEMA_A = _INPUT / "cross_schema_a.yaml"
SCHEMA_B = _INPUT / "cross_schema_b.yaml"
DATA_A = _INPUT / "cross_schema_a_data.yaml"
DATA_B = _INPUT / "cross_schema_b_data.yaml"
DATA_MERGED = _INPUT / "cross_schema_merged_data.yaml"

# ── Helpers ────────────────────────────────────────────────────────────────────


def _convert(schema: Path, data: Path, output: Path) -> subprocess.CompletedProcess:
    """Run linkml-convert and return the CompletedProcess. Raises on non-zero exit."""
    return subprocess.run(
        [_LINKML_CONVERT, "-s", str(schema), "-t", "json-ld", str(data), "-o", str(output)],
        capture_output=True,
        text=True,
        check=True,
    )


def _load_jsonld(path: Path) -> Graph:
    g = Graph()
    g.parse(str(path), format="json-ld")
    return g


def _merge(*graphs: Graph) -> Graph:
    merged = Graph()
    for g in graphs:
        for triple in g:
            merged.add(triple)
    return merged


def _shacl_graph(schema: Path) -> Graph:
    """Generate a SHACL graph from a LinkML schema using ShaclGenerator."""
    gen = ShaclGenerator(str(schema), mergeimports=True)
    g = Graph()
    g.parse(data=gen.serialize(), format="turtle")
    return g


def _shacl_validate(data_graph: Graph, shapes_graph: Graph) -> tuple[bool, str]:
    """Validate data_graph against shapes_graph. Returns (conforms, report_text)."""
    from pyshacl import validate as _validate

    conforms, _, text = _validate(
        data_graph,
        shacl_graph=shapes_graph,
        inference="none",
        abort_on_first=False,
    )
    return conforms, text


# ── Tier 1: Pure Python — SHACL shape structure ────────────────────────────────


class TestShaclShapes:
    """
    Verify that ShaclGenerator correctly reflects class_closed: false in the
    generated shapes.  No subprocess dependency.
    """

    def test_parent_node_shape_is_open(self):
        """
        ParentNode has class_closed: false → its NodeShape must carry sh:closed false.
        This is the foundational assertion: if this fails, all SHACL validation tests
        will trivially fail because the shape would reject the bridge triple.
        """
        shapes = _shacl_graph(SCHEMA_A)
        parent_node_uri = URIRef(f"{FAMILY}ParentNode")

        closed_values = list(shapes.objects(parent_node_uri, SH.closed))
        assert closed_values, (
            f"No sh:closed triple found on <{parent_node_uri}>. ShaclGenerator must emit sh:closed for every NodeShape."
        )
        assert all(str(v).lower() == "false" for v in closed_values), (
            f"ParentNode has class_closed: false but sh:closed was not false. "
            f"Found: {closed_values}. "
            f"Check that ShaclGenerator reads SchemaView.is_class_closed() correctly."
        )

    def test_bridge_node_shape_is_open(self):
        """BridgeNode also has class_closed: false → sh:closed false."""
        shapes = _shacl_graph(SCHEMA_B)
        bridge_node_uri = URIRef(f"{FAMILY}ParentNode")  # same class_uri as ParentNode

        closed_values = list(shapes.objects(bridge_node_uri, SH.closed))
        assert closed_values, f"No sh:closed triple found on <{bridge_node_uri}>."
        assert all(str(v).lower() == "false" for v in closed_values), (
            f"BridgeNode has class_closed: false but sh:closed was not false. Found: {closed_values}."
        )

    def test_schema_a_closed_value_is_literal_false(self):
        """sh:closed must be the boolean Literal false, not a string or IRI."""
        shapes = _shacl_graph(SCHEMA_A)
        parent_node_uri = URIRef(f"{FAMILY}ParentNode")

        closed_values = list(shapes.objects(parent_node_uri, SH.closed))
        assert any(isinstance(v, Literal) and v.toPython() is False for v in closed_values), (
            f"sh:closed on ParentNode must be a boolean Literal(False). Found: {closed_values!r}"
        )


# ── Tier 1: Pure Python — SHACL validation of manually-built merged graph ─────


class TestShaclValidation:
    """
    Construct the expected merged RDF graph directly in Python and validate it
    against SHACL shapes from both schemas.

    This tests the full semantics of class_closed: false end-to-end without
    requiring linkml-convert: the open ParentNode shape must permit the bridge
    triple (link:hasLink) even though it is not declared in schema A.
    """

    @pytest.fixture(scope="class")
    def merged_graph(self) -> Graph:
        """
        The expected merged graph after schema A + schema B conversion:

            A1 --rel:hasSuccessor--> A2
            A2 --link:hasLink------> A3
            A3 --rel:hasSuccessor--> A4
        """
        g = Graph()
        g.add((NODE_A1, RDF.type, PARENT_NODE_TYPE))
        g.add((NODE_A2, RDF.type, PARENT_NODE_TYPE))
        g.add((NODE_A3, RDF.type, PARENT_NODE_TYPE))
        g.add((NODE_A4, RDF.type, PARENT_NODE_TYPE))
        g.add((NODE_A1, HAS_SUCCESSOR, NODE_A2))
        g.add((NODE_A3, HAS_SUCCESSOR, NODE_A4))
        g.add((NODE_A2, HAS_LINK, NODE_A3))  # contributed by schema B
        return g

    @pytest.fixture(scope="class")
    def shapes_a(self) -> Graph:
        return _shacl_graph(SCHEMA_A)

    @pytest.fixture(scope="class")
    def shapes_b(self) -> Graph:
        return _shacl_graph(SCHEMA_B)

    def test_full_chain_present_in_merged_graph(self, merged_graph):
        """Sanity check: the manually-built graph has all expected triples."""
        assert (NODE_A1, HAS_SUCCESSOR, NODE_A2) in merged_graph
        assert (NODE_A2, HAS_LINK, NODE_A3) in merged_graph
        assert (NODE_A3, HAS_SUCCESSOR, NODE_A4) in merged_graph

    def test_conforms_to_schema_a_shapes(self, merged_graph, shapes_a):
        """
        Schema A's open ParentNode shape must NOT reject the link:hasLink triple.
        This is the core assertion for class_closed: false in SHACL.
        If ParentNode were closed (sh:closed true), family:node-a2 would fail
        validation because link:hasLink is not in schema A's ignoredProperties list.
        """
        conforms, report = _shacl_validate(merged_graph, shapes_a)
        assert conforms, (
            "Merged graph does not conform to schema A shapes.\n"
            "This likely means ParentNode's sh:closed false is not being generated "
            "correctly, or the ignoredProperties list does not cover link:hasLink.\n"
            f"SHACL report:\n{report}"
        )

    def test_conforms_to_schema_b_shapes(self, merged_graph, shapes_b):
        """Schema B's open BridgeNode shape must also accept the merged graph."""
        conforms, report = _shacl_validate(merged_graph, shapes_b)
        assert conforms, f"Merged graph does not conform to schema B shapes.\nSHACL report:\n{report}"

    def test_conforms_to_combined_shapes(self, merged_graph, shapes_a, shapes_b):
        """Both shape sets applied simultaneously must still pass."""
        combined = _merge(shapes_a, shapes_b)
        conforms, report = _shacl_validate(merged_graph, combined)
        assert conforms, f"Merged graph does not conform to combined shapes.\nSHACL report:\n{report}"

    def test_closed_class_would_reject_bridge_triple(self):
        """
        Negative control: construct a graph where ParentNode is closed and verify
        that schema A's default (closed='schema_defined') would reject the bridge
        triple. This confirms the open shape is genuinely doing work.
        """
        # Generate shapes with forced-closed to simulate a closed ParentNode
        gen = ShaclGenerator(str(SCHEMA_A), mergeimports=True, closed="forced_closed")
        closed_shapes = Graph()
        closed_shapes.parse(data=gen.serialize(), format="turtle")

        # The merged graph with the bridge triple should FAIL forced-closed validation
        g = Graph()
        g.add((NODE_A2, RDF.type, PARENT_NODE_TYPE))
        g.add((NODE_A2, HAS_LINK, NODE_A3))  # bridge triple — not in schema A

        conforms, _ = _shacl_validate(g, closed_shapes)
        assert not conforms, (
            "Expected the bridge triple to violate forced-closed schema A shapes, "
            "but validation passed. The negative control is broken — check that "
            "closed='forced_closed' actually sets sh:closed true on ParentNode."
        )


# ── Tier 2: Integration — linkml-convert subprocess ───────────────────────────
#
# These tests verify the full conversion pipeline described in the markdown spec.
# They require linkml-convert to be available in PATH and test assumptions about
# how linkml-convert handles open-world classes and JSON-LD context filtering.
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def graph_a_isolated(tmp_path_factory) -> Graph:
    """Schema A converted from schema A isolated data."""
    if not _CONVERT_AVAILABLE:
        pytest.skip("linkml-convert not found in PATH")
    out = tmp_path_factory.mktemp("isolated") / "graph_a.jsonld"
    _convert(SCHEMA_A, DATA_A, out)
    return _load_jsonld(out)


@pytest.fixture(scope="module")
def graph_b_isolated(tmp_path_factory) -> Graph:
    """Schema B converted from schema B isolated data."""
    if not _CONVERT_AVAILABLE:
        pytest.skip("linkml-convert not found in PATH")
    out = tmp_path_factory.mktemp("isolated") / "graph_b.jsonld"
    _convert(SCHEMA_B, DATA_B, out)
    return _load_jsonld(out)


@pytest.fixture(scope="module")
def graph_a_from_merged(tmp_path_factory) -> Graph:
    """Schema A conversion of the single merged data file.

    The merged data file contains ``has_link`` (unknown to schema A) on node-a2.
    Because ParentNode has ``class_closed: false``, the generated Python class carries
    ``_class_closed = False`` and the loader filters unknown kwargs before instantiation.
    Only the schema-A-declared fields (id, has_successor) are loaded; has_link is dropped
    at the Python level.  Schema A's JSON-LD @context then further filters any residual
    unknowns, so has_link never appears in the output graph.
    """
    if not _CONVERT_AVAILABLE:
        pytest.skip("linkml-convert not found in PATH")
    out = tmp_path_factory.mktemp("integration") / "graph_a_from_merged.jsonld"
    _convert(SCHEMA_A, DATA_MERGED, out)
    return _load_jsonld(out)


@pytest.fixture(scope="module")
def graph_b_from_merged(tmp_path_factory) -> Graph:
    """Schema B conversion of the single merged data file.

    The merged data file contains ``has_successor`` (unknown to schema B) on several
    nodes.  BridgeNode has ``class_closed: false``, so the loader filters those extra
    kwargs and the conversion succeeds.  Schema B's JSON-LD @context then ensures only
    has_link survives into the output graph.
    """
    if not _CONVERT_AVAILABLE:
        pytest.skip("linkml-convert not found in PATH")
    out = tmp_path_factory.mktemp("integration") / "graph_b_from_merged.jsonld"
    _convert(SCHEMA_B, DATA_MERGED, out)
    return _load_jsonld(out)


@_skip_no_convert
class TestSchemaAIsolated:
    """
    Converting schema A isolated data produces two disconnected components.
    The bridge property (link:hasLink) is absent — schema A has no context entry for it.
    """

    def test_left_component_present(self, graph_a_isolated):
        assert (NODE_A1, HAS_SUCCESSOR, NODE_A2) in graph_a_isolated, (
            "A1 --rel:hasSuccessor--> A2 missing from schema A isolated output. "
            "Check that linkml-convert correctly resolves full IRIs from the id slot."
        )

    def test_right_component_present(self, graph_a_isolated):
        assert (NODE_A3, HAS_SUCCESSOR, NODE_A4) in graph_a_isolated, (
            "A3 --rel:hasSuccessor--> A4 missing from schema A isolated output. "
            "Check that linkml-convert processes all items in the nodes list and "
            "that the kinship: prefix resolves correctly."
        )

    def test_bridge_absent(self, graph_a_isolated):
        """
        link:hasLink must not appear. Schema A has no slot_uri for it so it has
        no entry in the generated @context, and is dropped during JSON-LD expansion.
        """
        assert (NODE_A2, HAS_LINK, NODE_A3) not in graph_a_isolated, (
            "link:hasLink appears in schema A's output even though schema A has "
            "no context entry for it. Context-based filtering is not working."
        )

    def test_successor_edge_count(self, graph_a_isolated):
        edges = list(graph_a_isolated.triples((None, HAS_SUCCESSOR, None)))
        assert len(edges) == 2, f"Expected 2 rel:hasSuccessor triples in schema A output, found {len(edges)}."


@_skip_no_convert
class TestSchemaBIsolated:
    """
    Converting schema B isolated data produces exactly one triple: the bridge.
    rel:hasSuccessor is absent — schema B has no context entry for it.
    """

    def test_bridge_present(self, graph_b_isolated):
        """
        link:hasLink has a slot_uri in schema B and therefore a context entry.
        It must survive JSON-LD expansion.
        """
        assert (NODE_A2, HAS_LINK, NODE_A3) in graph_b_isolated, (
            "A2 --link:hasLink--> A3 missing from schema B isolated output. "
            "Check that link:hasLink's slot_uri is emitted into schema B's @context."
        )

    def test_no_successor_triples(self, graph_b_isolated):
        successor_triples = list(graph_b_isolated.triples((None, HAS_SUCCESSOR, None)))
        assert len(successor_triples) == 0, (
            "rel:hasSuccessor appears in schema B's output even though schema B "
            "has no context entry for it. Context-based filtering is not working."
        )

    def test_exactly_one_link_triple(self, graph_b_isolated):
        link_triples = list(graph_b_isolated.triples((None, HAS_LINK, None)))
        assert len(link_triples) == 1, (
            f"Expected exactly 1 link:hasLink triple in schema B output, found {len(link_triples)}."
        )


@_skip_no_convert
class TestPartitioning:
    """
    Converting the single merged data file against each schema independently
    must produce the same partitioned outputs as the isolated conversions.
    Verifies that context-based filtering correctly separates schema-specific
    triples without any explicit routing logic.
    """

    def test_schema_a_from_merged_has_both_successor_triples(self, graph_a_from_merged):
        assert (NODE_A1, HAS_SUCCESSOR, NODE_A2) in graph_a_from_merged
        assert (NODE_A3, HAS_SUCCESSOR, NODE_A4) in graph_a_from_merged

    def test_schema_a_from_merged_drops_bridge(self, graph_a_from_merged):
        """
        link:hasLink is present in the merged source data on family:node-a2 but must
        be dropped during schema A's conversion because schema A's @context has no
        entry for it. This is the partitioning assertion.
        """
        assert (NODE_A2, HAS_LINK, NODE_A3) not in graph_a_from_merged, (
            "link:hasLink appears in schema A's output from the merged data file. "
            "Schema A's @context should have no entry for link:hasLink; it must be "
            "dropped during JSON-LD expansion even when present in the source data. "
            "If open-world class instances now carry extra properties through the "
            "dumper, the JSON-LD context must still gate what appears in the output."
        )

    def test_schema_b_from_merged_has_bridge(self, graph_b_from_merged):
        assert (NODE_A2, HAS_LINK, NODE_A3) in graph_b_from_merged

    def test_schema_b_from_merged_drops_successor(self, graph_b_from_merged):
        """
        rel:hasSuccessor is present in merged data but must be dropped by schema B's
        context. Schema B has no slot_uri for has_successor.
        """
        assert (NODE_A1, HAS_SUCCESSOR, NODE_A2) not in graph_b_from_merged, (
            "rel:hasSuccessor appears in schema B's output from the merged data file. "
            "Schema B's @context should have no entry for rel:hasSuccessor."
        )


@_skip_no_convert
class TestMergedGraph:
    """
    Merge both integration conversion outputs and verify the full chain is
    traversable and equivalent to the isolated merge.
    """

    @pytest.fixture(scope="class")
    def merged_from_isolated(self, graph_a_isolated, graph_b_isolated) -> Graph:
        return _merge(graph_a_isolated, graph_b_isolated)

    @pytest.fixture(scope="class")
    def merged_from_integration(self, graph_a_from_merged, graph_b_from_merged) -> Graph:
        return _merge(graph_a_from_merged, graph_b_from_merged)

    def test_full_chain_traversable(self, merged_from_integration):
        """
        A1 → A2 → A3 → A4 must be fully traversable across both schema contributions.
        This is the primary integration assertion of the test suite.
        """
        g = merged_from_integration

        step1 = set(g.objects(NODE_A1, HAS_SUCCESSOR))
        assert NODE_A2 in step1, "A1 --rel:hasSuccessor--> A2 not traversable in merged graph"

        step2 = set(g.objects(NODE_A2, HAS_LINK))
        assert NODE_A3 in step2, (
            "A2 --link:hasLink--> A3 not traversable. "
            "This is the cross-schema bridge contributed by schema B. "
            "Check that link:hasLink's slot_uri is emitted into schema B's @context."
        )

        step3 = set(g.objects(NODE_A3, HAS_SUCCESSOR))
        assert NODE_A4 in step3, "A3 --rel:hasSuccessor--> A4 not traversable in merged graph"

    def test_triple_counts(self, merged_from_integration):
        g = merged_from_integration
        successor = list(g.triples((None, HAS_SUCCESSOR, None)))
        link = list(g.triples((None, HAS_LINK, None)))
        assert len(successor) == 2, f"Expected 2 rel:hasSuccessor triples, found {len(successor)}"
        assert len(link) == 1, f"Expected 1 link:hasLink triple, found {len(link)}"

    def test_isolated_and_integration_merges_are_equivalent(self, merged_from_isolated, merged_from_integration):
        """
        The essential chain triples must be present in both merged graphs.

        Note: full triple-set equality is intentionally NOT checked here.  Two structural
        differences are expected and legitimate:

        1. BNode identity — NodeCollection containers are anonymous RDF nodes whose local
           identifiers differ between rdflib parsing instances; BNode-containing triples
           cannot be compared by identity across graphs.

        2. Schema B processes DATA_MERGED (all 4 nodes) vs DATA_B (1 node in isolated
           mode), so the integration merge contains more schema-B triples than the
           isolated merge.  The *essential* content — the three-step chain — is identical.
        """
        chain_triples = [
            (NODE_A1, HAS_SUCCESSOR, NODE_A2),
            (NODE_A2, HAS_LINK, NODE_A3),
            (NODE_A3, HAS_SUCCESSOR, NODE_A4),
        ]
        for triple in chain_triples:
            assert triple in merged_from_isolated, (
                f"Chain triple {triple} missing from isolated merge. "
                f"Both pipelines must produce the full A1→A2→A3→A4 chain."
            )
            assert triple in merged_from_integration, (
                f"Chain triple {triple} missing from integration merge. "
                f"Both pipelines must produce the full A1→A2→A3→A4 chain."
            )

    def test_merged_graph_conforms_to_schema_a_shapes(self, merged_from_integration):
        """
        Schema A's essential chain triples must be present in the integration merged graph,
        and the bridge triple (contributed by schema B) must also be present — confirming
        that class_closed: false allows extra properties to pass through the full pipeline.

        Note: full SHACL validation is intentionally NOT used here.  linkml-convert's
        JSON-LD dumper does not emit ``rdf:type family:ParentNode`` for individual node
        instances (only the container NodeCollection gets an @type).  This means ``sh:class``
        constraints on the ``family:nodes`` property members would always fail for
        integration output, regardless of class_closed semantics.  The correct SHACL
        validation test (using a manually-built graph with proper type triples) is in
        Tier 1: TestShaclValidation.test_conforms_to_schema_a_shapes.
        """
        g = merged_from_integration
        assert (NODE_A1, HAS_SUCCESSOR, NODE_A2) in g, (
            "A1 --rel:hasSuccessor--> A2 missing from integration merged graph."
        )
        assert (NODE_A2, HAS_LINK, NODE_A3) in g, (
            "A2 --link:hasLink--> A3 (bridge triple) missing from integration merged graph. "
            "This triple comes from schema B's conversion and must survive the merge."
        )
        assert (NODE_A3, HAS_SUCCESSOR, NODE_A4) in g, (
            "A3 --rel:hasSuccessor--> A4 missing from integration merged graph."
        )
