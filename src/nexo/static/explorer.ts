import { annotationsActive, initializeAnnotations } from "./annotations.js";
import { bindDiagramViewport, diagramNavigationActive, type DiagramViewport } from "./diagram_viewport.js";
type Source = { file: string; symbol: string; label?: string; line?: number; focus_line?: number; focus_end_line?: number; code?: string; url?: string; location?: string; status: string; plumbing?: boolean };
type Region = { x: number; y: number; width: number; height: number; label: string };
type Comparison = { diagram: string; regions: Region[]; explanation: string; relationship: string };
type CommunityDiagram = { id: string; title: string; asset: string; view_box: [number, number, number, number] };
type Component = { id: string; name: string; language: string; deployment: string; scaling: string; status: string; sources: Source[]; community: Comparison[] };
type ExplanationLink = { text: string; sources: number[] };
type Message = { id: string; arrow: string; label: string; from: string; to: string; mode: string; request: string; response: string; durable?: string; protocol: string; explanation: string; explanation_links?: ExplanationLink[]; simplification?: string; sources: Source[] };
type Flow = { id: string; name: string; note: string; messages: Message[] };
type Data = { components: Component[]; flows: Flow[]; glossary: Record<string, string>; community_diagrams: CommunityDiagram[] };
const get = <T extends HTMLElement>(id: string): T => document.getElementById(id) as T;
let data: Data;
let currentFlow: Flow;
let pinned: string | null = null;
let selected: string | null = null;
let flowRequest = 0;
let selectionRequest = 0;
const messages = new Map<string, Message>();
const diagramViewports = new WeakMap<SVGSVGElement, DiagramViewport>();
function appendText(parent: HTMLElement, tag: string, content: string, className = ""): HTMLElement {
  const node = document.createElement(tag); node.textContent = content; node.className = className; parent.append(node); return node;
}
function glossary(root: HTMLElement): void {
  const terms = Object.keys(data.glossary).sort((a, b) => b.length - a.length);
  const pattern = new RegExp(`\\b(${terms.map(term => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})\\b`, "gi");
  const lookup = new Map(terms.map(term => [term.toLowerCase(), data.glossary[term] ?? ""]));
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  while (walker.nextNode()) {
    const node = walker.currentNode as Text;
    if (node.parentElement?.closest("abbr, svg, a, button, select, script, style")) continue;
    if (node.textContent?.trim()) nodes.push(node);
  }
  for (const node of nodes) {
    const text = node.textContent ?? "";
    const fragment = document.createDocumentFragment(); let previous = 0;
    for (const match of text.matchAll(pattern)) {
      const index = match.index ?? 0; fragment.append(document.createTextNode(text.slice(previous, index)));
      const term = document.createElement("abbr"); term.textContent = match[0]; term.title = lookup.get(match[0].toLowerCase()) ?? "";
      fragment.append(term); previous = index + match[0].length;
    }
    if (previous) { fragment.append(document.createTextNode(text.slice(previous))); node.replaceWith(fragment); }
  }
}
function sourcePanel(parent: HTMLElement, source: Source): void {
  if (source.status !== "implemented") { appendText(parent, "p", `${source.file} · ${source.symbol}: not implemented.`, "error"); return; }
  if (source.label) appendText(parent, "h4", source.label, "source-label");
  const link = document.createElement("a"); link.className = "source-ref"; link.href = source.url ?? "#";
  link.textContent = `${source.file}:${source.focus_line ?? source.line} · ${source.symbol} · Open in VS Code${source.plumbing ? " · plumbing" : ""}`;
  parent.append(link);
  const controls = appendText(parent, "div", "", "source-controls");
  const copy = appendText(controls, "button", "Copy location") as HTMLButtonElement;
  copy.type = "button";
  const open = appendText(controls, "button", "Open via local helper") as HTMLButtonElement;
  open.type = "button";
  const status = appendText(parent, "p", "", "source-status muted"); status.setAttribute("role", "status");
  const location = document.createElement("input"); location.type = "text"; location.readOnly = true;
  location.value = source.location ?? `${source.file}:${source.focus_line ?? source.line}`;
  location.setAttribute("aria-label", "Source location to copy"); location.hidden = true; parent.append(location);
  copy.addEventListener("click", () => {
    location.hidden = false;
    void navigator.clipboard?.writeText(location.value).then(() => {
      status.textContent = "Copied. Paste into VS Code’s Quick Open (Cmd+P).";
    }).catch(() => {
      location.focus(); location.select(); status.textContent = "Press Cmd+C to copy, then paste into VS Code’s Quick Open (Cmd+P).";
    });
    if (!navigator.clipboard) {
      location.focus(); location.select(); status.textContent = "Press Cmd+C to copy, then paste into VS Code’s Quick Open (Cmd+P).";
    }
  });
  open.addEventListener("click", async () => {
    open.disabled = true; status.textContent = "Opening source…";
    try {
      const response = await fetch("http://127.0.0.1:8765/open", {
        method: "POST", headers: { "Content-Type": "application/json", "X-Nexo-Editor": "1" },
        body: JSON.stringify({ file: source.file, line: source.focus_line ?? source.line }),
        signal: AbortSignal.timeout(12000),
      });
      if (!response.ok) throw new Error("Editor helper could not open this location.");
      status.textContent = "Opened in VS Code.";
    } catch {
      status.textContent = "Run python3 scripts/editor_helper.py in your local Nexo checkout, then try again from localhost:8080. Copy location also works without the helper.";
    } finally { open.disabled = false; }
  });
  const pre = document.createElement("pre"); pre.className = "source-code";
  pre.setAttribute("aria-label", `${source.symbol}${source.focus_line ? `; highlighted lines ${source.focus_line} to ${source.focus_end_line}` : ""}`);
  const code = document.createElement("code");
  (source.code ?? "").split("\n").forEach((text, index) => {
    const line = (source.line ?? 1) + index;
    const row = document.createElement("span"); row.className = "code-line";
    const focused = source.focus_line !== undefined && line >= source.focus_line && line <= (source.focus_end_line ?? source.focus_line);
    row.classList.toggle("code-focus", focused);
    const number = appendText(row, "span", String(line), "line-number"); number.setAttribute("aria-hidden", "true");
    appendText(row, "span", text || " ", "line-text"); code.append(row);
  });
  pre.append(code); parent.append(pre);
}
function highlight(): void {
  const message = selected ? messages.get(selected) : undefined;
  const pinMessage = pinned ? messages.get(pinned) : undefined;
  const componentID = selected?.startsWith("component:") ? selected.slice(10) : undefined;
  document.querySelectorAll<SVGGElement>(".hop").forEach(node => {
    const related = node.dataset.message ? [messages.get(node.dataset.message)] : data.flows.flatMap(f => f.messages).filter(m => m.arrow === node.dataset.arrow);
    const active = componentID ? related.some(m => m?.from === componentID || m?.to === componentID) : node.dataset.message ? messages.get(node.dataset.message)?.arrow === message?.arrow : node.dataset.arrow === message?.arrow;
    const isPin = node.dataset.message ? node.dataset.message === pinMessage?.id : node.dataset.arrow === pinMessage?.arrow;
    node.classList.toggle("active", active); node.classList.toggle("pinned", isPin);
    node.classList.toggle("selected", Boolean(message && node.dataset.message === message.id));
  });
  document.querySelectorAll<SVGGElement>(".component").forEach(node => {
    node.classList.toggle("active", componentID === node.dataset.component);
    node.classList.toggle("pinned", pinned === `component:${node.dataset.component}`);
  });
}
function comparisonPanel(parent: HTMLElement, comparison: Comparison): void {
  const diagram = data.community_diagrams.find(d => d.id === comparison.diagram); if (!diagram) return;
  const card = appendText(parent, "div", "", "community-card");
  appendText(card, "h3", diagram.title);
  const original = appendText(card, "a", "Open original SVG in a new tab") as HTMLAnchorElement;
  original.href = diagram.asset; original.target = "_blank"; original.rel = "noopener";
  appendText(card, "p", `${comparison.relationship} · ${comparison.explanation}`);
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg"); svg.classList.add("community-picture");
  svg.setAttribute("viewBox", diagram.view_box.join(" ")); svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${diagram.title}: ${comparison.regions.map(r => r.label).join(", ") || "no matching box"}`);
  const image = document.createElementNS(ns, "image"); image.setAttribute("href", diagram.asset);
  for (const [i, attr] of ["x", "y", "width", "height"].entries()) image.setAttribute(attr, String(diagram.view_box[i]));
  svg.append(image);
  for (const region of comparison.regions) {
    const rect = document.createElementNS(ns, "rect");
    for (const attr of ["x", "y", "width", "height"] as const) rect.setAttribute(attr, String(region[attr]));
    rect.setAttribute("class", "community-region");
    const title = document.createElementNS(ns, "title"); title.textContent = region.label; rect.append(title); svg.append(rect);
  }
  card.append(svg);
  const viewport = bindDiagramViewport(svg);
  const controls = appendText(card, "div", "", "community-controls");
  const overview = appendText(controls, "button", "Whole diagram") as HTMLButtonElement;
  overview.type = "button";
  overview.addEventListener("click", () => viewport.reset());
  for (const region of comparison.regions) {
    const zoom = appendText(controls, "button", `Zoom: ${region.label}`) as HTMLButtonElement;
    zoom.type = "button";
    zoom.addEventListener("click", () => {
      const padding = Math.max(region.width, region.height) * .25;
      viewport.setViewBox([region.x - padding, region.y - padding, region.width + 2 * padding, region.height + 2 * padding]);
    });
  }
}
function previewComponent(id: string): void {
  if (annotationsActive()) return;
  const component = data.components.find(c => `component:${c.id}` === id); if (!component) return;
  selected = id;
  get("hop-title").textContent = component.name;
  get("selection-state").textContent = pinned === id ? "Pinned" : pinned ? "Previewing · pinned selection returns on exit" : "Preview";
  const panel = get("code-panel"); panel.replaceChildren();
  panel.onkeydown = null;
  appendText(panel, "p", "Highlighted areas show the closest roles in the two community designs. They do not mean the implementations are identical.");
  appendText(panel, "p", "Click a Nexo box to keep this comparison open. Use Zoom to read a highlighted box in its original picture.", "muted");
  component.community.forEach(comparison => comparisonPanel(panel, comparison));
  appendText(panel, "h3", "Nexo implementation");
  appendText(panel, "p", `${component.language} · ${component.deployment}. ${component.scaling}`);
  component.sources.forEach(source => sourcePanel(panel, source));
  glossary(panel); glossary(get("hop-title")); highlight();
}
function preview(id: string): void {
  if (annotationsActive()) return;
  if (id.startsWith("component:")) { previewComponent(id); return; }
  const message = messages.get(id); if (!message) return;
  selected = id;
  get("hop-title").textContent = message.label;
  glossary(get("hop-title"));
  get("selection-state").textContent = pinned === id ? "Pinned" : pinned ? "Previewing · pinned selection returns on exit" : "Preview";
  const panel = get("code-panel"); panel.replaceChildren();
  const links = message.explanation_links ?? [];
  const explanationControls: HTMLElement[] = [];
  const sourceCards: HTMLElement[] = [];
  let pinnedExplanation: number | null = null;
  let hoveredExplanation: number | null = null;
  let focusedExplanation: number | null = null;
  const showSources = (index: number | null): void => {
    if (annotationsActive()) return;
    const sourceIndices = index === null ? undefined : links[index]?.sources;
    sourceCards.forEach((card, i) => {
      card.hidden = sourceIndices !== undefined && !sourceIndices.includes(i);
      card.classList.toggle("source-selected", sourceIndices?.includes(i) ?? false);
    });
    explanationControls.forEach(control => {
      const i = Number(control.dataset.explanation);
      control.classList.toggle("explanation-active", i === index);
      control.setAttribute("aria-pressed", String(i === pinnedExplanation));
    });
  };
  const refreshSources = (): void => showSources(hoveredExplanation ?? focusedExplanation ?? pinnedExplanation);
  const resetSources = (): void => {
    if (annotationsActive()) return;
    pinnedExplanation = hoveredExplanation = focusedExplanation = null; showSources(null);
  };
  panel.onkeydown = event => { if (event.key === "Escape") { event.preventDefault(); resetSources(); } };
  if (links.length) appendText(panel, "p", "Hover or focus an underlined explanation to show its code below. Click or Enter keeps it shown; Escape shows all code.", "muted explanation-help");
  for (const paragraph of message.explanation.split("\n\n")) {
    const p = appendText(panel, "p", "", "hop-explanation");
    const matches = links.map((link, index) => ({ link, index, start: paragraph.indexOf(link.text) })).filter(match => match.start >= 0).sort((a, b) => a.start - b.start);
    let previous = 0;
    for (const { link, index, start } of matches) {
      p.append(document.createTextNode(paragraph.slice(previous, start)));
      const control = appendText(p, "button", link.text, "explanation-link") as HTMLButtonElement;
      control.type = "button"; control.setAttribute("aria-pressed", "false");
      control.setAttribute("aria-controls", link.sources.map(i => `source-${message.id}-${i}`).join(" "));
      control.dataset.explanation = String(index); explanationControls.push(control);
      const pin = (): void => { pinnedExplanation = pinnedExplanation === index ? null : index; refreshSources(); };
      control.addEventListener("pointerenter", () => { hoveredExplanation = index; refreshSources(); });
      control.addEventListener("focus", () => { focusedExplanation = index; refreshSources(); });
      control.addEventListener("pointerleave", () => { hoveredExplanation = null; refreshSources(); });
      control.addEventListener("blur", () => { focusedExplanation = null; refreshSources(); });
      control.addEventListener("click", pin);
      previous = start + link.text.length;
    }
    p.append(document.createTextNode(paragraph.slice(previous)));
  }
  appendText(panel, "p", `${message.protocol} · ${message.mode}`, "muted hop-protocol");
  if (message.durable) appendText(panel, "p", `◆ Durable transition: ${message.durable}`, "success");
  appendText(panel, "h3", "Where this happens");
  if (message.sources.some(source => source.focus_line !== undefined)) appendText(panel, "p", "Highlighted lines perform this step. The surrounding code gives context.", "muted code-legend");
  if (links.length) {
    const reset = appendText(panel, "button", "Show all code", "show-all-code") as HTMLButtonElement; reset.type = "button";
    reset.addEventListener("click", resetSources);
  }
  message.sources.forEach((source, index) => {
    const card = appendText(panel, "div", "", "source-card"); card.id = `source-${message.id}-${index}`;
    sourceCards.push(card); sourcePanel(card, source);
  });
  const contract = document.createElement("details"); appendText(contract, "summary", "Request and response");
  appendText(contract, "h3", "Request"); appendText(contract, "pre", message.request);
  appendText(contract, "h3", "Response"); appendText(contract, "pre", message.response); panel.append(contract);
  if (message.simplification) appendText(panel, "p", message.simplification, "simplification");
  for (const id of new Set([message.from, message.to])) {
    const component = data.components.find(c => c.id === id);
    if (component) appendText(panel, "p", `${component.name}: ${component.language}; ${component.deployment}. Scaling: ${component.scaling}`, "muted");
  }
  glossary(panel); highlight();
}
function bindHops(root: HTMLElement): void {
  const flowFor = (id: string): Flow | undefined => {
    const belongs = (flow: Flow): boolean => flow.messages.some(m => id.startsWith("component:") ? m.from === id.slice(10) || m.to === id.slice(10) : m.id === id);
    return belongs(currentFlow) ? currentFlow : data.flows.find(belongs);
  };
  root.querySelectorAll<SVGGElement>(".hop, .component").forEach(node => {
    const resolve = (): string | undefined => node.dataset.component ? `component:${node.dataset.component}` : node.dataset.message ?? (currentFlow.messages.find(m => m.arrow === node.dataset.arrow) ?? data.flows.flatMap(flow => flow.messages).find(m => m.arrow === node.dataset.arrow))?.id;
    const choose = async (pin: boolean): Promise<void> => {
      if (diagramNavigationActive(node)) return;
      const request = ++selectionRequest;
      const id = resolve(); if (!id) return;
      if (pin) pinned = id;
      const flow = flowFor(id);
      if (flow && currentFlow.id !== flow.id) await renderFlow(flow);
      if (request === selectionRequest && !diagramNavigationActive(node)) preview(id);
    };
    // Scrolling code into keyboard focus can move a diagram beneath a stationary pointer.
    node.addEventListener("pointermove", () => { if (resolve() !== selected) void choose(false); });
    node.addEventListener("focus", () => void choose(false));
    node.addEventListener("click", () => void choose(true));
    node.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); void choose(true); } });
    const restore = async (): Promise<void> => {
      if (diagramNavigationActive(node)) return;
      const request = ++selectionRequest;
      const id = pinned;
      if (!id) return;
      const flow = flowFor(id);
      // Keep pinned controls in place when focus moves from the diagram into the panel.
      if (id === selected && flow?.id === currentFlow.id) return;
      if (flow && flow.id !== currentFlow.id) await renderFlow(flow);
      if (request === selectionRequest && !diagramNavigationActive(node)) preview(id);
    };
    node.addEventListener("pointerleave", () => void restore());
    node.addEventListener("blur", () => void restore());
  });
}
async function loadSvg(file: string, target: HTMLElement): Promise<void> {
  const response = await fetch(file); if (!response.ok) throw new Error(`Could not load ${file}`);
  const xml = new DOMParser().parseFromString(await response.text(), "image/svg+xml");
  if (xml.querySelector("parsererror")) throw new Error(`Invalid diagram: ${file}`);
  const svg = document.importNode(xml.documentElement, true) as unknown as SVGSVGElement;
  target.replaceChildren(svg);
  const viewport = bindDiagramViewport(svg, () => { get<HTMLSelectElement>("diagram-zoom").value = "custom"; });
  diagramViewports.set(svg, viewport);
  const zoom = Number(get<HTMLSelectElement>("diagram-zoom").value);
  if (Number.isFinite(zoom)) viewport.setZoom(zoom / 100);
  bindHops(target);
}
async function renderFlow(flow: Flow): Promise<void> {
  if (annotationsActive()) return;
  const request = ++flowRequest;
  const annotate = get<HTMLButtonElement>("annotate"); annotate.disabled = true;
  try {
    const temporary = document.createElement("div"); await loadSvg(`sequences/${flow.id}.svg`, temporary);
    if (request !== flowRequest) return;
    currentFlow = flow;
    get<HTMLSelectElement>("flow").value = flow.id;
    get("flow-title").textContent = flow.name;
    glossary(get("flow-title"));
    get("flow-note").textContent = flow.note;
    get<HTMLAnchorElement>("sequence-file").href = `sequences/${flow.id}.svg`;
    get("sequence").replaceChildren(...Array.from(temporary.childNodes));
    glossary(get("flow-note")); highlight();
  } finally {
    if (request === flowRequest) {
      // A draft must describe the diagram that finished loading, including after a failed fetch.
      get<HTMLSelectElement>("flow").value = currentFlow.id;
      annotate.disabled = false;
    }
  }
}
async function main(): Promise<void> {
  const response = await fetch("explorer-data.json"); if (!response.ok) throw new Error("Explorer data is missing. Run python scripts/build_explorer.py.");
  data = await response.json() as Data;
  for (const flow of data.flows) {
    get<HTMLSelectElement>("flow").add(new Option(flow.name, flow.id));
    flow.messages.forEach(message => messages.set(message.id, message));
  }
  for (const [term, definition] of Object.entries(data.glossary)) { appendText(get("glossary"), "dt", term); appendText(get("glossary"), "dd", definition); }
  for (const component of data.components) {
    appendText(get("components"), "h3", component.name);
    appendText(get("components"), "p", `${component.language} · ${component.deployment} · ${component.status}`);
    appendText(get("components"), "p", component.scaling);
    const details = document.createElement("details"); appendText(details, "summary", "Component source"); component.sources.forEach(source => sourcePanel(details, source)); get("components").append(details);
  }
  const first = data.flows[0]; if (!first) throw new Error("No mapped flows");
  currentFlow = first;
  await Promise.all([loadSvg("architecture.svg", get("architecture")), renderFlow(first)]);
  if (first.messages[0]) { pinned = first.messages[0].id; preview(pinned); }
  get<HTMLSelectElement>("flow").addEventListener("change", async event => {
    const flow = data.flows.find(item => item.id === (event.target as HTMLSelectElement).value); if (!flow) return;
    selectionRequest += 1;
    pinned = flow.messages[0]?.id ?? null; await renderFlow(flow); if (pinned) preview(pinned);
  });
  get<HTMLSelectElement>("diagram-zoom").addEventListener("change", event => {
    const scale = Number((event.target as HTMLSelectElement).value) / 100;
    if (!Number.isFinite(scale)) return;
    document.querySelectorAll<SVGSVGElement>(".diagram > svg").forEach(svg => diagramViewports.get(svg)?.setZoom(scale));
  });
  get("unpin").addEventListener("click", () => { selectionRequest += 1; pinned = null; get("selection-state").textContent = "Selection unpinned"; highlight(); });
  glossary(document.body);
  initializeAnnotations(() => ({ flow_id: currentFlow.id, flow_name: currentFlow.name }), target => {
    if (target.kind === "component") return data.components.find(component => component.id === target.id)?.name;
    if (target.kind === "message") return messages.get(target.id)?.label;
    return [...messages.values()].find(message => message.arrow === target.id)?.label;
  }, () => { selectionRequest += 1; });
}
void main().catch(error => { get("notice").textContent = String(error); get("notice").className = "error"; });
export {};
