import pytest
import rdflib
from click.testing import CliRunner
from rdflib import SH, URIRef

from linkml.generators.shaclgen import cli

RECOMMENDED_SCHEMA = """
id: http://example.org/recommended
name: recommended
prefixes:
  ex: http://example.org/
  linkml: https://w3id.org/linkml/
default_prefix: ex
imports:
  - linkml:types
classes:
  Dataset:
    slots: [title]
slots:
  title:
    range: string
    recommended: true
"""


def test_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert "Generate SHACL turtle from a LinkML model" in result.output


@pytest.mark.parametrize(
    ("args", "has_warnings"),
    [([], False), (["--no-recommended-warnings"], False), (["--recommended-warnings"], True)],
    ids=["default", "off", "on"],
)
def test_recommended_warnings_flag(tmp_path, args, has_warnings):
    schema = tmp_path / "schema.yaml"
    schema.write_text(RECOMMENDED_SCHEMA)
    result = CliRunner().invoke(cli, [str(schema), *args])
    assert result.exit_code == 0, result.output
    assert ("sh:Warning" in result.output) is has_warnings


EXTRA_SLOTS_SCHEMA = """
id: http://example.org/extra
name: extra
prefixes:
  ex: http://example.org/
  linkml: https://w3id.org/linkml/
default_prefix: ex
imports:
  - linkml:types
classes:
  Open:
    slots: [title]
    extra_slots:
      allowed: true
  Closed:
    slots: [title]
    extra_slots:
      allowed: false
  Default:
    slots: [title]
slots:
  title:
    range: string
"""


@pytest.mark.parametrize(
    ("args", "default_closed"),
    [([], True), (["--closed"], True), (["--non-closed"], False)],
    ids=["default", "closed", "non-closed"],
)
def test_extra_slots_override_the_closed_flag(tmp_path, args, default_closed):
    schema = tmp_path / "schema.yaml"
    schema.write_text(EXTRA_SLOTS_SCHEMA)
    result = CliRunner().invoke(cli, [str(schema), *args])
    assert result.exit_code == 0, result.output
    g = rdflib.Graph().parse(data=result.stdout, format="turtle")
    ex = "http://example.org/"
    # an open shape says sh:closed false with --closed, and nothing with --non-closed
    open_statement = rdflib.Literal(False) if default_closed else None
    assert g.value(URIRef(ex + "Open"), SH.closed) == open_statement
    assert g.value(URIRef(ex + "Closed"), SH.closed) == rdflib.Literal(True)
    assert g.value(URIRef(ex + "Default"), SH.closed) == (rdflib.Literal(True) if default_closed else None)
    assert set(g.subjects(SH.ignoredProperties, None)) == set(g.subjects(SH.closed, rdflib.Literal(True)))
