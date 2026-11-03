import pytest
from click.testing import CliRunner

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
