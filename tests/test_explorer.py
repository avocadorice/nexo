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
        assert source["url"].endswith(f":{source.get('focus_line', source['line'])}")
        original = (ROOT / source["file"]).read_text().splitlines()
        assert source["code"].splitlines()[0] == original[source["line"] - 1]
        if "focus" in source:
            assert source["focus"] in original[source["focus_line"] - 1]
            assert source["line"] <= source["focus_line"] <= source["focus_end_line"]
            assert source["focus_end_line"] < source["line"] + len(source["code"].splitlines())
    for svg in [tmp_path / "architecture.svg", *(tmp_path / "sequences").glob("*.svg")]:
        ElementTree.parse(svg)
    assert len(list((tmp_path / "sequences").glob("*.svg"))) == len(data["flows"])
    for flow in data["flows"]:
        for hop in flow["messages"]:
            assert all("focus_line" in source for source in hop["sources"])


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


def test_focus_range_tracks_source_edits_and_links_to_call_not_context(tmp_path):
    path = tmp_path / "sample.py"
    code = "def send():\n    prepare()\n    client.upload(\n        body,\n    )\n    done()\n"
    source = {
        "file": "sample.py",
        "symbol": "send",
        "focus": "client.upload(",
        "focus_end": "    )",
        "context": 1,
        "lines": 5,
    }
    for padding in (0, 7):
        path.write_text("# unrelated edit\n" * padding + code)
        result = builder.resolve_source(tmp_path, source, "/local checkout")
        assert result["line"] == padding + 2
        assert result["focus_line"] == padding + 3
        assert result["focus_end_line"] == padding + 5
        assert result["url"] == f"vscode://file/local%20checkout/sample.py:{padding + 3}"
        assert (
            result["code"] == "    prepare()\n    client.upload(\n        body,\n    )\n    done()"
        )


@pytest.mark.parametrize("focus", ["missing()", "repeat()"])
def test_focus_rejects_missing_or_ambiguous_call(tmp_path, focus):
    (tmp_path / "sample.py").write_text("def send():\n    repeat()\n    repeat()\n")
    with pytest.raises(ValueError, match="Focus must match once"):
        builder.resolve_source(
            tmp_path, {"file": "sample.py", "symbol": "send", "focus": focus}, str(tmp_path)
        )


@pytest.mark.parametrize(
    "options",
    [
        {"focus_end": "start()"},
        {"focus_end": "missing()"},
        {"focus_end": "finish()", "lines": 1},
    ],
)
def test_focus_range_cannot_reverse_or_disappear_from_excerpt(tmp_path, options):
    (tmp_path / "sample.py").write_text("def send():\n    start()\n    upload()\n    finish()\n")
    source = {"file": "sample.py", "symbol": "send", "focus": "upload()", "context": 0, **options}
    with pytest.raises(ValueError, match="Focus end|Excerpt must contain"):
        builder.resolve_source(tmp_path, source, str(tmp_path))


def test_sql_symbols_require_kind_when_index_and_function_share_name(tmp_path):
    path = tmp_path / "schema.sql"
    path.write_text(
        "CREATE INDEX audit_batch ON audit(batch_id);\n"
        "CREATE FUNCTION audit_batch() RETURNS trigger AS $$\n"
        "BEGIN\nRETURN NEW;\nEND\n$$ LANGUAGE plpgsql;\n"
    )
    with pytest.raises(ValueError, match="Expected one SQL symbol"):
        builder.extract_symbol(path, "audit_batch")
    line, code = builder.extract_symbol(path, "FUNCTION/audit_batch")
    assert line == 2
    assert code.startswith("CREATE FUNCTION")
    assert code.endswith("$$ LANGUAGE plpgsql;")


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


def test_create_response_is_drawn_after_durable_commit(tmp_path):
    builder.build(ROOT, tmp_path, compile_ts=False)
    document = ElementTree.parse(tmp_path / "sequences/create.svg")
    responses = [
        item.attrib["data-message"]
        for item in document.iter()
        if item.attrib.get("aria-label", "").endswith(" response") and "data-message" in item.attrib
    ]
    assert responses.index("create-commit") < responses.index("create-http")


def test_community_copies_stay_unchanged_and_boxes_are_selectable(tmp_path):
    data = builder.build(ROOT, tmp_path, compile_ts=False)
    for diagram in data["community_diagrams"]:
        original = (ROOT / "explorer/references" / diagram["file"]).read_bytes()
        assert (tmp_path / diagram["asset"]).read_bytes() == original
        workspace_original = ROOT.parent / "theory" / diagram["file"]
        if workspace_original.exists():
            assert workspace_original.read_bytes() == original
    document = ElementTree.parse(tmp_path / "architecture.svg")
    boxes = [node for node in document.iter() if "data-component" in node.attrib]
    assert {node.attrib["data-component"] for node in boxes} == {
        c["id"] for c in data["components"]
    }
    assert all(node.attrib["role"] == "button" and node.attrib["tabindex"] == "0" for node in boxes)


def test_community_map_rejects_missing_counterpart_or_out_of_bounds_region():
    mapping = json.loads((ROOT / "explorer/mapping.json").read_text())
    incomplete = copy.deepcopy(mapping)
    incomplete["components"][0]["community"].pop()
    with pytest.raises(ValueError, match="Missing community comparison"):
        builder.validate(incomplete)
    outside = copy.deepcopy(mapping)
    outside["components"][0]["community"][0]["regions"][0]["x"] = -1
    with pytest.raises(ValueError, match="Community region outside"):
        builder.validate(outside)
