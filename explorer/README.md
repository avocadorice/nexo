# Nexo explorer

`mapping.json` is the single source for architecture boxes and arrows, ordered sequence messages, request/response descriptions, code symbols, languages, deployment objects and simplifications. `docs/glossary.json` supplies every tooltip definition. Generated SVG and JSON files are build output and must not be edited.

```sh
npm ci --prefix explorer
python scripts/build_explorer.py
```

Serve the Go API, then open `/static/explorer.html`. The customer and operations pages live beside it at `/static/index.html` and `/static/ops.html`. The explorer reads static build output and cannot call mutation endpoints.

Hover or focus previews an arrow's code; click or Enter pins it. The architecture, selected sequence and source panel stay linked. The selector exposes one sequence per use case. Requests and responses are shown on each diagram and in full in the source panel. Green diamonds identify durable transitions.

The builder resolves Python functions/methods, Go functions/methods/structs, TypeScript functions, SQL definitions and Kubernetes `Kind/name` objects. References use file plus symbol; optional `focus`, `context` and `lines` narrow an excerpt within that symbol. Missing symbols and unmapped architecture arrows fail the build. A genuinely future component must explicitly use `status: "not implemented"`; it is never supplied with invented code.

VS Code links use the build machine's checkout path by default. Container/cloud builds should pass the user's local path, for example `python scripts/build_explorer.py --source-root /Users/you/projects/nexo`, so those links open the matching local checkout. Rebuild after source changes; excerpts and line numbers are always extracted, never copied.

The browser source is TypeScript. `explorer/tsconfig.json` compiles it to `src/nexo/static/build/`. No framework, runtime dependency or animation is required. The API's content security policy permits these same-origin external scripts and styles.
