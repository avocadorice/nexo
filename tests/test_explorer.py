"""Traceability is checked against real symbols, not manually copied snippets."""

import copy
import importlib.util
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_explorer", ROOT / "scripts/build_explorer.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_all_diagram_hops_resolve_real_source_and_render(tmp_path):
    data = builder.build(ROOT, tmp_path, compile_ts=False)
    references = [source for box in data["components"] for source in box["sources"]]
    references += [
        source for flow in data["flows"] for hop in flow["messages"] for source in hop["sources"]
    ]
    assert references
    for source in references:
        assert source["status"] == "implemented"
        assert source["line"] > 0
        assert source["code"]
        assert source["url"].endswith(f":{source['line']}")
        original = (ROOT / source["file"]).read_text().splitlines()
        assert source["code"].splitlines()[0] == original[source["line"] - 1]
    for svg in [tmp_path / "architecture.svg", *(tmp_path / "sequences").glob("*.svg")]:
        ElementTree.parse(svg)
    assert len(list((tmp_path / "sequences").glob("*.svg"))) == len(data["flows"])


def test_missing_source_fails_build_instead_of_showing_stale_code(tmp_path):
    with pytest.raises(FileNotFoundError):
        builder.resolve_source(tmp_path, {"file": "missing.py", "symbol": "create"}, str(tmp_path))


def test_missing_symbol_is_rejected(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text("def other():\n    return 1\n")
    with pytest.raises(ValueError, match="Missing Python symbol"):
        builder.extract_symbol(path, "absent")


def test_source_cannot_escape_repository(tmp_path):
    with pytest.raises(ValueError, match="outside repository"):
        builder.resolve_source(
            tmp_path, {"file": "../secret.py", "symbol": "secret"}, str(tmp_path)
        )


def test_go_symbol_extraction_ignores_braces_in_strings_and_comments(tmp_path):
    path = tmp_path / "sample.go"
    path.write_text(
        "package sample\nfunc (s *Server) Run(ctx context.Context) error {\n"
        ' // }\n value := `}`\n if value == "{" { return nil }\n'
        " return nil\n}\nfunc After() {}\n"
    )
    line, code = builder.extract_symbol(path, "Run")
    assert line == 2
    assert code.endswith("return nil\n}")
    assert "After" not in code


def test_mapping_rejects_unmapped_architecture_arrows():
    mapping = json.loads((ROOT / "explorer/mapping.json").read_text())
    bad = copy.deepcopy(mapping)
    bad["arrows"].append(dict(bad["arrows"][0], id="orphan"))
    with pytest.raises(ValueError, match="Unmapped arrows"):
        builder.validate(bad)


def test_mapping_rejects_missing_request_or_response():
    mapping = json.loads((ROOT / "explorer/mapping.json").read_text())
    del mapping["flows"][0]["messages"][0]["response"]
    with pytest.raises(ValueError, match="Missing response"):
        builder.validate(mapping)


def test_explicit_unimplemented_reference_stays_honest(tmp_path):
    source = {"file": "future.py", "symbol": "future", "status": "not implemented"}
    result = builder.resolve_source(tmp_path, source, str(tmp_path))
    assert result["status"] == "not implemented"
    assert "code" not in result
