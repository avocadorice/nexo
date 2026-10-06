# Nexo explorer

`mapping.json` is the single source for architecture boxes and arrows, ordered sequence messages, request/response descriptions, code symbols, languages, deployment objects and simplifications. `docs/glossary.json` supplies every tooltip definition. Generated SVG and JSON files are build output and must not be edited.

The glossary retains Nexo-specific meanings and the previously requested idempotency explanations. Other terms are opt-in: add them only when the user requests them, through feedback queued with `/nexo-add-glossary`. Languages, common acronyms and general computing vocabulary are left as plain text.

```sh
npm ci --prefix explorer
python scripts/build_explorer.py
```

Serve the Go API, then open `/` or `/static/explorer.html`. Explorer is the main navigation link. The collapsed System views menu opens the customer and operations pages at `/static/index.html` and `/static/ops.html`. The explorer reads static build output and cannot call chargeback mutation endpoints.

Hover or focus previews an arrow's code; click or Enter pins it. The architecture, selected sequence and source panel stay linked. The selected sequence step and the code that performs it are highlighted in yellow; related messages remain blue. The selector exposes one sequence per use case. Requests and responses are shown on each diagram and under “Request and response” in the source panel. Green diamonds identify durable transitions.

The builder resolves Python functions/methods, Go functions/methods/structs, TypeScript functions, SQL definitions and Kubernetes `Kind/name` objects. SQL names shared by different objects need a kind, such as `FUNCTION/audit_batch`. References use file plus symbol. `focus` identifies a unique line in that symbol; optional `focus_end` identifies the last line of the highlighted call. `context` and `lines` control the surrounding excerpt. Highlighted line numbers and VS Code targets are resolved from source at build time. The link opens the first highlighted line, not the start of the surrounding excerpt. Optional `label` explains a source excerpt in plain language.

Missing or ambiguous symbols and focus anchors, reversed or clipped highlight ranges, and unmapped architecture arrows fail the build. A genuinely future component must explicitly use `status: "not implemented"`; it is never supplied with invented code.

Underlined explanation sentences or clauses reveal the code that implements those words when hovered or keyboard-focused. Click or Enter pins that explanation's code; Escape or Show all code restores the whole hop. Unlinked text remains plain when it does not correspond cleanly to the extracted lines. Optional `explanation_links` annotations in the same mapping pair exact explanation text with source indices. The builder rejects stale, overlapping or ambiguous text, invalid indices and links to unfocused or unimplemented code. Hover only changes which existing source cards are shown; it does not rebuild the explanation or steal keyboard focus.

VS Code links use the build machine's checkout path by default. Container/cloud builds should pass the user's local path, for example `python scripts/build_explorer.py --source-root /Users/you/projects/nexo`, so those links open the matching local checkout. Rebuild after source changes; excerpts and line numbers are always extracted, never copied.

Each excerpt also has **Copy location**, which copies the absolute `file:line` for VS Code's Quick Open (Cmd+P). The location stays visible for manual copying when a browser denies clipboard access. The original `vscode://` link remains available in browsers that support it.

For embedded browsers that do not launch `vscode://` links, run this in your local checkout and leave it running while studying:

```sh
python3 scripts/editor_helper.py
```

Then use **Open via local helper** from `http://localhost:8080/static/explorer.html` or `http://127.0.0.1:8080/static/explorer.html`. Stop the helper with Ctrl+C. It listens only on `127.0.0.1:8765` and runs `code --reuse-window --goto` for a mapped source file inside this checkout. It checks the requesting origin, host, JSON content type, custom header, path and line; arbitrary commands and files outside Nexo are rejected. It is a local study tool, not a cloud workload. Public deployments use Copy location or the original link. The API permits helper connections only from the explorer page's content security policy.

The browser source is TypeScript. `explorer/tsconfig.json` compiles it to `src/nexo/static/build/`. No framework, runtime dependency or animation is required. The API's content security policy permits these same-origin external scripts and styles.

Hover or focus an architecture box to compare its role with both community designs. Click or Enter pins the comparison; the related sequence steps stay highlighted. Each picture keeps its original layout. Yellow outlines locate related boxes, and the Zoom buttons make their labels readable. The explanation calls out combined or partial matches: for example, Nexo's PostgreSQL rows cover roles shown as a database, Kafka, and a delivery queue in the references.

`community_diagrams` and each component's `community` entries in `mapping.json` supply the reference assets, original SVG coordinates, and explanations. `explorer/references/` contains byte-for-byte copies of the reference SVGs in `theory/` so Docker builds are self-contained. The builder copies these unchanged; the browser draws highlights in a separate SVG layer. The reference files in `theory/` are never edited. Tests check reference bytes, coordinate bounds, comparison coverage, and keyboard-accessible boxes.
