# Nexo explorer

`mapping.json` is the single source for architecture boxes and arrows, ordered sequence messages, request/response descriptions, code symbols, languages, deployment objects and simplifications. `docs/glossary.json` supplies every tooltip definition. Generated SVG and JSON files are build output and must not be edited.

The glossary retains Nexo-specific meanings and the previously requested idempotency explanations. Other terms are opt-in: add them only when the user requests them, through feedback queued with `/nexo-add-glossary`. Languages, common acronyms and general computing vocabulary are left as plain text.

```sh
npm ci --prefix explorer
python scripts/build_explorer.py
```

Serve the Go API, then open `/` or `/static/explorer.html`. Explorer is the main navigation link. The collapsed System views menu opens the customer and operations pages at `/static/index.html` and `/static/ops.html`. The explorer reads static build output and cannot call chargeback mutation endpoints.

Hover or focus previews an arrow's code; click or Enter pins it. The architecture, selected sequence and source panel stay linked. The selected sequence step and the code that performs it are highlighted in yellow; related messages remain blue. The selector exposes one sequence per use case. Requests and responses are shown on each diagram and under “Request and response” in the source panel. Green diamonds identify durable transitions.

Drag directly on architecture, sequence or community diagrams to pan. Wheel or trackpad scrolling over a diagram zooms around the pointer; scroll outside the picture to move the page. Keyboard users can focus a diagram, use + / − to zoom, arrow keys to pan and 0 to reset. The existing Fit / 125% / 150% / 200% selector still controls architecture and sequence together; direct gestures show Custom. Community Whole diagram and region Zoom buttons still work. A drag does not pin an item or change the code preview. `diagram_viewport.ts` contains this DOM-only interaction; adding the `annotation-mode` class to the body suspends navigation so annotation tools can own pointer input.

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

Use **Annotate** to select text, a diagram box or message, or both, and write a note. The explorer pauses diagram navigation and previews while you compose. Save sends the note to the same local helper, which appends an entry to the workspace's `feedback/ANNOTATIONS.md`. It never calls a chargeback mutation endpoint. A failed save keeps the draft; retrying an unchanged draft cannot append it twice. This works only from the local explorer on port 8080 with the helper running. Notes are plain local files, not uploaded to a service.

The [annotation JSON contract](../contracts/explorer-annotation.schema.json) defines request and result shapes. The TypeScript client and Python helper share mapped component/message/arrow IDs; the helper resolves their names from the mapping and validates them before writing.

In annotation mode, **Draw arrow** lets you drag from a starting point to the place you mean. Each arrow inserts a numbered marker such as `[arrow 1]` at the note cursor. Write around those markers to connect each phrase to the right arrow. The queue records each arrow's start and tip as named elements or text, with panel coordinates for blank space, so an agent can understand it without a screenshot. Several arrows can accompany one note. Removing an arrow removes its marker; manually deleting a marker requires removing the corresponding arrow before saving.

The `nexo-implement-feedback-and-answer-questions` skill reads this queue alongside `feedback/QUEUE.md`. Answers and completion details are added to the annotation entry itself. The queue has no background watcher; invoke the skill when you want the waiting notes worked.

For agent updates while the helper can append, use the same queue lock: `python3 scripts/annotation_queue.py A-001 --status in-progress`, then `python3 scripts/annotation_queue.py A-001 --status done --answer-file /path/to/answer.txt` (and `--done-file` for implementation details). The command rereads the queue under the lock and changes only that entry's status and supplied response fields. It preserves other entries and the original note. The helper writes the complete new file durably before replacing it, so a crash cannot leave half a note. The lock coordinates participating writers; an unlocked whole-file rewrite by another program cannot be made safe by this helper.

The browser source is TypeScript. `explorer/tsconfig.json` compiles it to `src/nexo/static/build/`. No framework, runtime dependency or animation is required. The API's content security policy permits these same-origin external scripts and styles.

The UI and generated Nexo diagrams use a dark palette by default. The preserved community SVGs retain their original colors.

Hover or focus an architecture box to compare its role with both community designs. Click or Enter pins the comparison; the related sequence steps stay highlighted. Each picture keeps its original layout. Yellow outlines locate related boxes, and the Zoom buttons make their labels readable. The explanation calls out combined or partial matches: for example, Nexo's PostgreSQL rows cover roles shown as a database, Kafka, and a delivery queue in the references.

Each comparison's **Open original SVG in a new tab** link opens the unchanged reference without Nexo's highlight layer. The architecture and sequence retain their own full-diagram links.

`community_diagrams` and each component's `community` entries in `mapping.json` supply the reference assets, original SVG coordinates, and explanations. `explorer/references/` contains byte-for-byte copies of the reference SVGs in `theory/` so Docker builds are self-contained. The builder copies these unchanged; the browser draws highlights in a separate SVG layer. The reference files in `theory/` are never edited. Tests check reference bytes, coordinate bounds, comparison coverage, and keyboard-accessible boxes.
