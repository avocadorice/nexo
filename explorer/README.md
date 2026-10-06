# Nexo explorer

`mapping.json` is the single source for architecture boxes and arrows, ordered sequence messages, request/response descriptions, code symbols, languages, deployment objects and simplifications. `docs/glossary.json` supplies every tooltip definition. Generated SVG and JSON files are build output and must not be edited.

```sh
npm ci --prefix explorer
python scripts/build_explorer.py
```

Serve the Go API, then open `/static/explorer.html`. The customer and operations pages live beside it at `/static/index.html` and `/static/ops.html`. The explorer reads static build output and cannot call mutation endpoints.

Hover or focus previews an arrow's code; click or Enter pins it. The architecture, selected sequence and source panel stay linked. The selected sequence step and the code that performs it are highlighted in yellow; related messages remain blue. The selector exposes one sequence per use case. Requests and responses are shown on each diagram and under “Request and response” in the source panel. Green diamonds identify durable transitions.

The builder resolves Python functions/methods, Go functions/methods/structs, TypeScript functions, SQL definitions and Kubernetes `Kind/name` objects. SQL names shared by different objects need a kind, such as `FUNCTION/audit_batch`. References use file plus symbol. `focus` identifies a unique line in that symbol; optional `focus_end` identifies the last line of the highlighted call. `context` and `lines` control the surrounding excerpt. Highlighted line numbers and VS Code targets are resolved from source at build time. The link opens the first highlighted line, not the start of the surrounding excerpt. Optional `label` explains a source excerpt in plain language.

Missing or ambiguous symbols and focus anchors, reversed or clipped highlight ranges, and unmapped architecture arrows fail the build. A genuinely future component must explicitly use `status: "not implemented"`; it is never supplied with invented code.

VS Code links use the build machine's checkout path by default. Container/cloud builds should pass the user's local path, for example `python scripts/build_explorer.py --source-root /Users/you/projects/nexo`, so those links open the matching local checkout. Rebuild after source changes; excerpts and line numbers are always extracted, never copied.

The browser source is TypeScript. `explorer/tsconfig.json` compiles it to `src/nexo/static/build/`. No framework, runtime dependency or animation is required. The API's content security policy permits these same-origin external scripts and styles.

Hover or focus an architecture box to compare its role with both community designs. Click or Enter pins the comparison; the related sequence steps stay highlighted. Each picture keeps its original layout. Yellow outlines locate related boxes, and the Zoom buttons make their labels readable. The explanation calls out combined or partial matches: for example, Nexo's PostgreSQL rows cover roles shown as a database, Kafka, and a delivery queue in the references.

`community_diagrams` and each component's `community` entries in `mapping.json` supply the reference assets, original SVG coordinates, and explanations. `explorer/references/` contains byte-for-byte copies of the reference SVGs in `theory/` so Docker builds are self-contained. The builder copies these unchanged; the browser draws highlights in a separate SVG layer. The reference files in `theory/` are never edited. Tests check reference bytes, coordinate bounds, comparison coverage, and keyboard-accessible boxes.
