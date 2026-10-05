type Source = { file: string; symbol: string; label?: string; line?: number; focus_line?: number; focus_end_line?: number; code?: string; url?: string; status: string; plumbing?: boolean };
type Component = { id: string; name: string; language: string; deployment: string; scaling: string; status: string; sources: Source[] };
type Message = { id: string; arrow: string; label: string; from: string; to: string; mode: string; request: string; response: string; durable?: string; protocol: string; explanation: string; simplification?: string; sources: Source[] };
type Flow = { id: string; name: string; note: string; messages: Message[] };
type Data = { components: Component[]; flows: Flow[]; glossary: Record<string, string> };
const get = <T extends HTMLElement>(id: string): T => document.getElementById(id) as T;
let data: Data;
let currentFlow: Flow;
let pinned: string | null = null;
let selected: string | null = null;
let flowRequest = 0;
let selectionRequest = 0;
const messages = new Map<string, Message>();
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
  document.querySelectorAll<SVGGElement>(".hop").forEach(node => {
    const active = node.dataset.message ? messages.get(node.dataset.message)?.arrow === message?.arrow : node.dataset.arrow === message?.arrow;
    const isPin = node.dataset.message ? node.dataset.message === pinMessage?.id : node.dataset.arrow === pinMessage?.arrow;
    node.classList.toggle("active", active); node.classList.toggle("pinned", isPin);
    node.classList.toggle("selected", node.dataset.message === message?.id);
  });
}
function preview(id: string): void {
  const message = messages.get(id); if (!message) return;
  selected = id;
  get("hop-title").textContent = message.label;
  glossary(get("hop-title"));
  get("selection-state").textContent = pinned === id ? "Pinned" : pinned ? "Previewing · pinned selection returns on exit" : "Preview";
  const panel = get("code-panel"); panel.replaceChildren();
  for (const paragraph of message.explanation.split("\n\n")) appendText(panel, "p", paragraph);
  appendText(panel, "p", `${message.protocol} · ${message.mode}`, "muted hop-protocol");
  if (message.durable) appendText(panel, "p", `◆ Durable transition: ${message.durable}`, "success");
  appendText(panel, "h3", "Where this happens");
  if (message.sources.some(source => source.focus_line !== undefined)) appendText(panel, "p", "Highlighted lines perform this step. The surrounding code gives context.", "muted code-legend");
  message.sources.forEach(source => sourcePanel(panel, source));
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
  root.querySelectorAll<SVGGElement>(".hop").forEach(node => {
    const resolve = (): Message | undefined => node.dataset.message ? messages.get(node.dataset.message) : currentFlow.messages.find(m => m.arrow === node.dataset.arrow) ?? data.flows.flatMap(flow => flow.messages).find(m => m.arrow === node.dataset.arrow);
    const choose = async (pin: boolean): Promise<void> => {
      const request = ++selectionRequest;
      const message = resolve(); if (!message) return;
      if (pin) pinned = message.id;
      const flow = data.flows.find(flow => flow.messages.some(item => item.id === message.id));
      if (flow && currentFlow.id !== flow.id) await renderFlow(flow);
      if (request === selectionRequest) preview(message.id);
    };
    node.addEventListener("pointerenter", () => void choose(false));
    node.addEventListener("focus", () => void choose(false));
    node.addEventListener("click", () => void choose(true));
    node.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); void choose(true); } });
    const restore = async (): Promise<void> => {
      const request = ++selectionRequest;
      const id = pinned;
      if (!id) return;
      const flow = data.flows.find(f => f.messages.some(m => m.id === id));
      if (flow && flow.id !== currentFlow.id) await renderFlow(flow);
      if (request === selectionRequest) preview(id);
    };
    node.addEventListener("pointerleave", () => void restore());
    node.addEventListener("blur", () => void restore());
  });
}
async function loadSvg(file: string, target: HTMLElement): Promise<void> {
  const response = await fetch(file); if (!response.ok) throw new Error(`Could not load ${file}`);
  const xml = new DOMParser().parseFromString(await response.text(), "image/svg+xml");
  if (xml.querySelector("parsererror")) throw new Error(`Invalid diagram: ${file}`);
  target.replaceChildren(document.importNode(xml.documentElement, true)); bindHops(target);
}
async function renderFlow(flow: Flow): Promise<void> {
  currentFlow = flow; const request = ++flowRequest;
  get<HTMLSelectElement>("flow").value = flow.id;
  get("flow-title").textContent = flow.name;
  glossary(get("flow-title"));
  get("flow-note").textContent = flow.note;
  const link = get<HTMLAnchorElement>("sequence-file"); link.href = `sequences/${flow.id}.svg`;
  const temporary = document.createElement("div"); await loadSvg(link.href, temporary);
  if (request !== flowRequest) return;
  get("sequence").replaceChildren(...Array.from(temporary.childNodes));
  glossary(get("flow-note")); highlight();
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
    document.body.classList.remove("zoom-125", "zoom-150", "zoom-200");
    const zoom = (event.target as HTMLSelectElement).value;
    if (zoom !== "100") document.body.classList.add(`zoom-${zoom}`);
  });
  get("unpin").addEventListener("click", () => { selectionRequest += 1; pinned = null; get("selection-state").textContent = "Selection unpinned"; highlight(); });
  glossary(document.body);
}
void main().catch(error => { get("notice").textContent = String(error); get("notice").className = "error"; });
export {};
