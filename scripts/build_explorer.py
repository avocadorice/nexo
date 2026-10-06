"""Build read-only diagrams and source excerpts from the one traceability mapping."""

from __future__ import annotations

import argparse
import ast
import copy
import json
import math
import re
import subprocess
import textwrap
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]


def brace_end(text: str, start: int) -> int:
    """Find a Go/TypeScript body, ignoring braces in strings and comments."""
    depth = 0
    parentheses = 0
    body = False
    token = re.compile(
        r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`[^`]*`|[(){}]'
    )
    for match in token.finditer(text, start):
        value = match.group()
        if value == "(":
            parentheses += 1
        elif value == ")":
            parentheses -= 1
        elif value == "{" and (body or parentheses == 0):
            body = True
            depth += 1
        elif value == "}" and body:
            depth -= 1
            if depth == 0:
                return match.end()
    raise ValueError("Unclosed source symbol")


def extract_symbol(path: Path, symbol: str) -> tuple[int, str]:
    text = path.read_text()
    lines = text.splitlines()
    if path.suffix == ".py":
        names = symbol.split(".")
        nodes = ast.parse(text).body
        node = None
        for name in names:
            node = next(
                (
                    item
                    for item in nodes
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and item.name == name
                ),
                None,
            )
            if node is None:
                raise ValueError(f"Missing Python symbol {symbol} in {path}")
            nodes = node.body
        assert node is not None and node.end_lineno is not None
        return node.lineno, "\n".join(lines[node.lineno - 1 : node.end_lineno])
    if path.suffix in {".go", ".ts"}:
        if path.suffix == ".go":
            pattern = (
                rf"^func\s+(?:\([^\n]*\)\s+)?{re.escape(symbol)}\b"
                rf"|^type\s+{re.escape(symbol)}\s+struct\b"
            )
        else:
            pattern = rf"^(?:export\s+)?(?:async\s+)?function\s+{re.escape(symbol)}\b"
        matches = list(re.finditer(pattern, text, re.MULTILINE))
        if len(matches) != 1:
            raise ValueError(f"Expected one symbol {symbol} in {path}, found {len(matches)}")
        start = matches[0].start()
        end = brace_end(text, start)
        return text[:start].count("\n") + 1, text[start:end]
    if path.suffix == ".sql":
        kind, name = symbol.split("/", 1) if "/" in symbol else (None, symbol)
        matches = list(
            re.finditer(
                r"^CREATE\s+(?:UNIQUE\s+)?(TABLE|INDEX|FUNCTION|TRIGGER)\s+"
                rf"(?:IF NOT EXISTS\s+)?{re.escape(name)}\b",
                text,
                re.MULTILINE | re.IGNORECASE,
            )
        )
        if kind:
            matches = [match for match in matches if match[1].lower() == kind.lower()]
        if len(matches) != 1:
            raise ValueError(f"Expected one SQL symbol {symbol} in {path}, found {len(matches)}")
        match = matches[0]
        start = match.start()
        end = text.find(";", start) + 1
        if "$$" in text[start:end]:
            close = text.find("$$", text.find("$$", start) + 2)
            end = text.find(";", close) + 1
        return text[:start].count("\n") + 1, text[start:end]
    if path.suffix in {".yaml", ".yml"}:
        kind, name = symbol.split("/", 1)
        for match in re.finditer(r"(?:\A|(?<=\n)---\n)([\s\S]*?)(?=\n---\n|\Z)", text):
            document = match.group(1)
            kind_match = re.search(r"^kind:\s*(\S+)\s*$", document, re.MULTILINE)
            metadata = re.search(r"^metadata:\n((?:[ \t].*\n)+)", document, re.MULTILINE)
            name_match = (
                re.search(r"^  name:\s*(\S+)\s*$", metadata[1], re.MULTILINE) if metadata else None
            )
            if kind_match and name_match and (kind_match[1], name_match[1]) == (kind, name):
                return text[: match.start(1)].count("\n") + 1, document.rstrip()
        raise ValueError(f"Missing Kubernetes object {symbol} in {path}")
    raise ValueError(f"Unsupported source type: {path}")


def resolve_source(root: Path, source: dict, source_root: str) -> dict:
    result = copy.deepcopy(source)
    if result.get("status") == "not implemented":
        return result
    relative = Path(source["file"])
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Source outside repository: {relative}")
    line, code = extract_symbol(path, source["symbol"])
    if "focus_end" in source and "focus" not in source:
        raise ValueError(f"Focus end requires focus: {relative}:{source['symbol']}")
    if "focus" in source:
        parts = code.splitlines()
        matches = [index for index, text in enumerate(parts) if source["focus"] in text]
        if len(matches) != 1:
            raise ValueError(f"Focus must match once: {relative}:{source['symbol']}")
        start = end = matches[0]
        if "focus_end" in source:
            ends = [index for index, text in enumerate(parts) if source["focus_end"] in text]
            if len(ends) != 1 or ends[0] < start:
                raise ValueError(
                    f"Focus end must match once after focus: {relative}:{source['symbol']}"
                )
            end = ends[0]
        result.update(focus_line=line + start, focus_end_line=line + end)
        offset = max(0, start - source.get("context", 2))
        excerpt_end = min(len(parts), offset + source.get("lines", 25))
        if not offset <= start <= end < excerpt_end:
            raise ValueError(f"Excerpt must contain focused lines: {relative}:{source['symbol']}")
        line += offset
        code = "\n".join(parts[offset:excerpt_end])
    location = source_root.rstrip("/") + "/" + str(relative)
    target = quote(location, safe="/")
    result.update(
        status="implemented",
        line=line,
        code=code,
        location=f"{location}:{result.get('focus_line', line)}",
        url=f"vscode://file{target}:{result.get('focus_line', line)}",
    )
    return result


def validate_explanation_links(message: dict) -> None:
    links = message.get("explanation_links", [])
    if not isinstance(links, list):
        raise ValueError(f"Explanation links must be a list: {message['id']}")
    spans = []
    explanation = message["explanation"]
    for link in links:
        text = link.get("text") if isinstance(link, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Explanation link needs nonempty text: {message['id']}")
        start = explanation.find(text)
        if start < 0 or explanation.find(text, start + 1) >= 0:
            raise ValueError(f"Explanation text must match once: {message['id']}: {text!r}")
        if not any(text in paragraph for paragraph in explanation.split("\n\n")):
            raise ValueError(f"Explanation text must stay in one paragraph: {message['id']}")
        indices = link.get("sources")
        if (
            not isinstance(indices, list)
            or not indices
            or any(
                type(index) is not int or not 0 <= index < len(message["sources"])
                for index in indices
            )
            or len(indices) != len(set(indices))
        ):
            raise ValueError(f"Explanation sources must be unique valid indices: {message['id']}")
        for index in indices:
            source = message["sources"][index]
            if (
                source.get("status", "implemented") != "implemented"
                or not isinstance(source.get("focus"), str)
                or not source["focus"].strip()
            ):
                raise ValueError(
                    f"Explanation source must be implemented and focused: {message['id']}"
                )
        spans.append((start, start + len(text)))
    ordered = sorted(spans)
    if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:], strict=False)):
        raise ValueError(f"Explanation links must not overlap: {message['id']}")


def validate(mapping: dict) -> None:
    component_ids = [item["id"] for item in mapping["components"]]
    arrow_ids = [item["id"] for item in mapping["arrows"]]
    flow_ids = [item["id"] for item in mapping["flows"]]
    message_ids: list[str] = []
    used_arrows: set[str] = set()
    for kind, ids in [("component", component_ids), ("arrow", arrow_ids), ("flow", flow_ids)]:
        if len(ids) != len(set(ids)):
            raise ValueError(f"Duplicate {kind} IDs")
    for arrow in mapping["arrows"]:
        if arrow["from"] not in component_ids or arrow["to"] not in component_ids:
            raise ValueError(f"Unknown component in arrow {arrow['id']}")
    for flow in mapping["flows"]:
        if not flow["messages"]:
            raise ValueError(f"Empty flow: {flow['id']}")
        ordered_ids = [message["id"] for message in flow["messages"]]
        for message in flow["messages"]:
            for field in ("request", "response", "protocol", "explanation", "sources", "mode"):
                if not message.get(field):
                    raise ValueError(f"Missing {field} in {message['id']}")
            validate_explanation_links(message)
            if message["from"] not in component_ids or message["to"] not in component_ids:
                raise ValueError(f"Unknown component in message {message['id']}")
            if message["arrow"] not in arrow_ids:
                raise ValueError(f"Unknown arrow in message {message['id']}")
            after = message.get("response_after")
            if after and (
                after not in ordered_ids
                or ordered_ids.index(after) <= ordered_ids.index(message["id"])
            ):
                raise ValueError(f"Invalid response ordering in {message['id']}")
            message_ids.append(message["id"])
            used_arrows.add(message["arrow"])
    if len(message_ids) != len(set(message_ids)):
        raise ValueError("Duplicate message IDs")
    if used_arrows != set(arrow_ids):
        raise ValueError(f"Unmapped arrows: {set(arrow_ids) - used_arrows}")
    diagrams = {item["id"]: item for item in mapping["community_diagrams"]}
    if len(diagrams) != len(mapping["community_diagrams"]):
        raise ValueError("Duplicate community diagram IDs")
    for component in mapping["components"]:
        comparisons = component["community"]
        if len(comparisons) != len(diagrams) or {c["diagram"] for c in comparisons} != set(
            diagrams
        ):
            raise ValueError(f"Missing community comparison for {component['id']}")
        for comparison in comparisons:
            if not comparison["explanation"] or comparison["relationship"] not in {
                "equivalent",
                "combined",
                "partial",
                "not shown",
            }:
                raise ValueError("Community comparison needs an explanation and relationship")
            if bool(comparison["regions"]) == (comparison["relationship"] == "not shown"):
                raise ValueError("Only a 'not shown' comparison may have no regions")
            left, top, width, height = diagrams[comparison["diagram"]]["view_box"]
            for region in comparison["regions"]:
                x, y, w, h = (region[key] for key in ("x", "y", "width", "height"))
                if not all(math.isfinite(n) for n in (x, y, w, h)) or not (
                    w > 0
                    and h > 0
                    and x >= left
                    and y >= top
                    and x + w <= left + width
                    and y + h <= top + height
                ):
                    raise ValueError(f"Community region outside diagram: {region['label']}")


def svg_start(width: int, height: int, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="group" aria-label="{escape(title)}"><title>{escape(title)}</title>'
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#a0b6c7"/></marker></defs>'
        '<rect width="100%" height="100%" fill="#18232e"/>'
    )


def label(
    text: str,
    x: float,
    y: float,
    glossary: dict,
    size: int = 12,
    anchor: str = "middle",
    color: str = "#e0e8ef",
) -> str:
    definitions = [
        f"{term}: {meaning}"
        for term, meaning in glossary.items()
        if re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE)
    ]
    title = f"<title>{escape(chr(10).join(definitions))}</title>" if definitions else ""
    return (
        f'<text x="{x}" y="{y}" font-family="system-ui,sans-serif" font-size="{size}" '
        f'text-anchor="{anchor}" fill="{color}">{title}{escape(text)}</text>'
    )


def architecture(mapping: dict, glossary: dict) -> str:
    geometry = mapping["diagram"]
    width, height = geometry["width"], geometry["height"]
    box_width, box_height = geometry["box_width"], geometry["box_height"]
    parts = [svg_start(width, height, "Nexo architecture")]
    boxes = {item["id"]: item for item in mapping["components"]}
    for arrow in mapping["arrows"]:
        path = arrow["path"]
        dash = ' stroke-dasharray="7 5"' if arrow["mode"] == "asynchronous" else ""
        parts.append(
            f'<g class="hop" data-arrow="{arrow["id"]}" tabindex="0" role="button" '
            f'aria-label="{escape(arrow["label"])}"><path class="hit" d="{path}" '
            'stroke="transparent" stroke-width="20" fill="none"/>'
            f'<path class="ink" d="{path}" fill="none" stroke="#a0b6c7" '
            f'stroke-width="1.6" marker-end="url(#arrow)"{dash}/>'
        )
        x, y = arrow["label_at"]
        parts.append(label(arrow.get("short_label", arrow["label"]), x, y, glossary, 11))
        parts.append("</g>")
    for box in boxes.values():
        x, y = box["position"]
        fill = "#382f26" if box.get("external") else "#243544"
        parts.append(
            f'<g class="component" data-component="{box["id"]}" tabindex="0" role="button" '
            f'aria-label="{escape(box["name"])}: compare community diagrams">'
            f'<rect x="{x}" y="{y}" width="{box_width}" height="{box_height}" rx="5" '
            f'fill="{fill}" stroke="#a0b6c7" stroke-width="1.5"/>'
        )
        center = x + box_width / 2
        for index, line in enumerate(textwrap.wrap(box["name"], 21)):
            parts.append(label(line, center, y + 22 + index * 15, glossary, 12))
        parts.append(label(box["language"], center, y + 57, glossary, 10))
        parts.append(label(box["deployment_label"], center, y + 75, glossary, 9))
        if box["status"] == "not implemented":
            parts.append(label("NOT IMPLEMENTED", center, y - 7, glossary, 11, color="#ff9b9b"))
        parts.append("</g>")
    parts.append(
        label("PostgreSQL is the durable work queue.", width / 2, height - 12, glossary, 11)
    )
    return "".join(parts) + "</svg>"


def sequence(flow: dict, mapping: dict, glossary: dict) -> str:
    ids = list(
        dict.fromkeys(
            component
            for message in flow["messages"]
            for component in [message["from"], message["to"]]
        )
    )
    positions = {component: 60 + index * 120 for index, component in enumerate(ids)}
    width = max(360, len(ids) * 120)
    events = []
    pending: dict[str, list] = {}

    def respond(index, message):
        events.append(("response", index, message))
        for original_index, original in pending.pop(message["id"], []):
            respond(original_index, original)

    for index, message in enumerate(flow["messages"]):
        events.append(("request", index + 1, message))
        if message.get("response_after"):
            pending.setdefault(message["response_after"], []).append((index + 1, message))
        else:
            respond(index + 1, message)
    if pending:
        raise ValueError(f"Unresolved sequence response ordering in {flow['id']}")
    height = 100 + sum(
        158 if kind == "response" and msg.get("durable") else 116 for kind, _, msg in events
    )
    parts = [svg_start(width, height, flow["name"])]
    for component in ids:
        box = next(item for item in mapping["components"] if item["id"] == component)
        x = positions[component]
        parts.append(
            f'<path d="M {x} 77 V {height - 12}" '
            'stroke="#425869" stroke-dasharray="4 5"/>'
            f'<g data-participant="{component}">'
            f'<rect x="{x - 56}" y="12" width="112" height="65" rx="4" '
            'fill="#243544" stroke="#7891a5"/>'
        )
        for index, line in enumerate(textwrap.wrap(box["name"], 17)):
            parts.append(label(line, x, 30 + index * 13, glossary, 11))
        parts.append(label(box["language"], x, 67, glossary, 9))
        parts.append("</g>")
    y = 100
    for kind, index, message in events:
        is_response = kind == "response"
        event_height = 158 if is_response and message.get("durable") else 116
        left, right = positions[message["from"]], positions[message["to"]]
        end = right if left != right else right + 70
        if is_response:
            left, end = end, left
        dashed = is_response or message["mode"] == "asynchronous"
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        parts.append(
            f'<g class="hop" data-message="{message["id"]}" tabindex="0" role="button" '
            f'aria-label="{escape(message["label"])} {kind}"><rect class="background" '
            f'x="4" y="{y - 15}" width="{width - 8}" height="{event_height - 5}" '
            'rx="4" fill="#18232e" fill-opacity=".7"/>'
        )
        heading = f"{index}. {message['label']} · {kind}"
        if not is_response:
            heading += f" · {message['mode']}"
        for offset, line in enumerate(textwrap.wrap(heading, width // 7)[:2]):
            parts.append(label(line, width / 2, y + offset * 14, glossary, 12))
        detail = message["response_label" if is_response else "request_label"]
        for offset, line in enumerate(textwrap.wrap(detail, width // 6)[:2]):
            parts.append(label(line, width / 2, y + 40 + offset * 14, glossary, 11))
        parts.append(
            f'<path class="ink" d="M {left} {y + 76} H {end}" fill="none" '
            f'stroke="#a0b6c7" marker-end="url(#arrow)"{dash}/>'
        )
        if is_response and message.get("durable"):
            lines = textwrap.wrap("◆ " + message["durable"], width // 6)[:3]
            for offset, line in enumerate(lines):
                parts.append(
                    label(line, width / 2, y + 101 + offset * 14, glossary, 11, color="#8cdeb0")
                )
        parts.append("</g>")
        y += event_height
    return "".join(parts) + "</svg>"


def build(
    root: Path = ROOT,
    output: Path | None = None,
    source_root: str | None = None,
    compile_ts: bool = True,
) -> dict:
    output = output or root / "src/nexo/static"
    mapping = json.loads((root / "explorer/mapping.json").read_text())
    glossary = json.loads((root / "docs/glossary.json").read_text())
    validate(mapping)
    data = copy.deepcopy(mapping)
    for component in data["components"]:
        component["sources"] = [
            resolve_source(root, source, source_root or str(root))
            for source in component["sources"]
        ]
    for flow in data["flows"]:
        for message in flow["messages"]:
            message["sources"] = [
                resolve_source(root, source, source_root or str(root))
                for source in message["sources"]
            ]
    data["glossary"] = glossary
    output.mkdir(parents=True, exist_ok=True)
    (output / "sequences").mkdir(exist_ok=True)
    for diagram in mapping["community_diagrams"]:
        source = (root / "explorer/references" / diagram["file"]).resolve()
        target = (output / diagram["asset"]).resolve()
        if not source.is_relative_to(
            (root / "explorer/references").resolve()
        ) or not target.is_relative_to(output.resolve()):
            raise ValueError("Community diagram paths must stay within their directories")
        content = source.read_bytes()
        view_box = [float(n) for n in ElementTree.fromstring(content).attrib["viewBox"].split()]
        if view_box != diagram["view_box"]:
            raise ValueError(f"Community viewBox differs from mapping: {diagram['id']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (output / "explorer-data.json").write_text(json.dumps(data, indent=2) + "\n")
    (output / "architecture.svg").write_text(architecture(mapping, glossary))
    for flow in mapping["flows"]:
        (output / "sequences" / f"{flow['id']}.svg").write_text(sequence(flow, mapping, glossary))
    if compile_ts:
        compiler = root / "explorer/node_modules/typescript/bin/tsc"
        if not compiler.exists():
            raise RuntimeError("TypeScript is missing. Run npm ci --prefix explorer first.")
        subprocess.run(
            [
                "node",
                str(compiler),
                "--project",
                str(root / "explorer/tsconfig.json"),
                "--outDir",
                str(output / "build"),
            ],
            check=True,
        )
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-root", help="Local checkout path used by VS Code links")
    parser.add_argument("--skip-typescript", action="store_true")
    args = parser.parse_args()
    data = build(
        output=args.output, source_root=args.source_root, compile_ts=not args.skip_typescript
    )
    print(f"Built {len(data['components'])} components and {len(data['flows'])} mapped flows.")


if __name__ == "__main__":
    main()
