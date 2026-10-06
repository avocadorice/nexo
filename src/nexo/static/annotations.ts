import { initializeAnnotationArrows, type AnnotationPanel as Panel, type AnnotationTarget as Target } from "./annotation_arrows.js";
type Context = { flow_id: string; flow_name: string };
type ChosenTarget = Target & { panel: Panel; label: string };
const element = <T extends HTMLElement>(id: string): T => document.getElementById(id) as T;
export const annotationsActive = (): boolean => document.body.classList.contains("annotation-mode");

export function initializeAnnotations(context: () => Context, targetName: (target: Target) => string | undefined, starting: () => void): void {
  const toggle = element<HTMLButtonElement>("annotate");
  const composer = element("annotation-panel");
  const form = element<HTMLFormElement>("annotation-form");
  const note = element<HTMLTextAreaElement>("annotation-note");
  const save = element<HTMLButtonElement>("annotation-save");
  const status = element("annotation-status");
  const textPreview = element("annotation-text");
  const targetList = element("annotation-targets");
  const clearText = element<HTMLButtonElement>("annotation-clear-text");
  let current = context();
  let selectedText = "";
  let textPanel: Panel = "page";
  let targets: ChosenTarget[] = [];
  let busy = false;
  let markerCursor = 0;
  let request: { fingerprint: string; id: string } | null = null;
  const controls = ["flow", "diagram-zoom", "unpin"].map(id => element<HTMLSelectElement | HTMLButtonElement>(id));

  const panelFor = (node: Element): Panel => {
    if (node.closest(".community-card")) return "community";
    if (node.closest("#architecture")) return "architecture";
    if (node.closest("#sequence")) return "sequence";
    if (node.closest("#code-panel")) return "code";
    if (node.closest("#glossary")) return "glossary";
    return "page";
  };
  const viewPanel = (): Panel => {
    const panels = new Set(targets.map(target => target.panel));
    if (selectedText) panels.add(textPanel);
    arrowTool.payload().forEach(arrow => { panels.add(arrow.from.panel); panels.add(arrow.to.panel); });
    return panels.size === 1 ? [...panels][0]! : "page";
  };
  const render = (): void => {
    textPreview.textContent = selectedText || "No text selected.";
    clearText.disabled = busy || !selectedText;
    targetList.replaceChildren();
    if (!targets.length) {
      const item = document.createElement("li"); item.textContent = "No diagram items selected."; targetList.append(item);
    }
    for (const target of targets) {
      const item = document.createElement("li");
      item.append(document.createTextNode(`${target.label} `));
      const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "Remove";
      remove.setAttribute("aria-label", `Remove ${target.label}`); remove.disabled = busy;
      remove.addEventListener("click", () => { targets = targets.filter(value => value !== target); render(); });
      item.append(remove); targetList.append(item);
    }
    document.querySelectorAll<SVGGElement>("[data-component], [data-message], [data-arrow]").forEach(node => {
      node.classList.toggle("annotation-target", annotationsActive() && targets.some(target => node.dataset[target.kind] === target.id));
    });
    arrowTool.refresh();
    save.disabled = busy || (!selectedText.trim() && !targets.length && !arrowTool.payload().length) || !note.value.trim();
    note.readOnly = busy; toggle.disabled = busy;
    element("annotation-context").textContent = `${current.flow_name} · ${viewPanel()}`;
  };
  const arrowTool = initializeAnnotationArrows({
    enabled: () => annotationsActive() && !busy,
    panelFor, targetName, changed: render,
    starting: () => { markerCursor = note.selectionEnd; },
    insertMarker: id => {
      const marker = ` [arrow ${id}] `;
      if (note.value.length + marker.length > 4000) { status.textContent = "Shorten your note before adding an arrow marker."; return false; }
      note.setRangeText(marker, markerCursor, markerCursor, "end");
      status.textContent = `Arrow ${id} added. Type around its marker to explain what it means.`;
      note.focus(); return true;
    },
    removeMarker: id => { note.value = note.value.split(`[arrow ${id}]`).join(""); },
    status: text => { status.textContent = text; },
  });
  const exit = (): void => {
    document.body.classList.remove("annotation-mode");
    composer.hidden = true; toggle.textContent = "Annotate"; toggle.setAttribute("aria-expanded", "false"); toggle.setAttribute("aria-pressed", "false");
    controls.forEach(control => { control.disabled = false; });
    selectedText = ""; targets = []; note.value = ""; request = null; arrowTool.clear();
    render();
  };
  toggle.addEventListener("click", () => {
    if (busy) return;
    if (annotationsActive()) { exit(); return; }
    current = context(); starting();
    document.body.classList.add("annotation-mode");
    composer.hidden = false; toggle.textContent = "Cancel annotation"; toggle.setAttribute("aria-expanded", "true"); toggle.setAttribute("aria-pressed", "true");
    controls.forEach(control => { control.disabled = true; });
    status.textContent = "Select page text or a diagram item, then add your note. Cancel discards the unsaved draft.";
    render();
  });
  clearText.addEventListener("click", () => { selectedText = ""; render(); });
  note.addEventListener("input", render);

  const captureText = (): void => {
    if (!annotationsActive() || busy || arrowTool.drawing()) return;
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return;
    const range = selection.getRangeAt(0);
    if (range.intersectsNode(composer) || range.intersectsNode(toggle)) return;
    const text = selection.toString().replace(/\r\n?/g, "\n").trim();
    if (!text) return;
    if (text.length > 2000) { status.textContent = "Select up to 2,000 characters; the previous selection is kept."; return; }
    const anchor = selection.anchorNode instanceof Element ? selection.anchorNode : selection.anchorNode?.parentElement;
    selectedText = text; textPanel = anchor ? panelFor(anchor) : "page";
    render();
  };
  // Capture before focusing the note collapses the browser's page-text selection.
  document.addEventListener("mouseup", event => { if (!(event.target instanceof Node) || !composer.contains(event.target)) captureText(); });
  document.addEventListener("keyup", event => { if (event.shiftKey) captureText(); });
  element("annotation-use-selection").addEventListener("click", captureText);

  const chooseTarget = (node: Element): void => {
    if (busy) return;
    let target: Target;
    if (node.hasAttribute("data-component")) target = { kind: "component", id: node.getAttribute("data-component")! };
    else if (node.hasAttribute("data-message")) target = { kind: "message", id: node.getAttribute("data-message")! };
    else target = { kind: "arrow", id: node.getAttribute("data-arrow")! };
    const label = targetName(target); if (!label) return;
    const existing = targets.find(value => value.kind === target.kind && value.id === target.id);
    if (existing) targets = targets.filter(value => value !== existing);
    else if (targets.length < 8) targets.push({ ...target, label, panel: panelFor(node) });
    else status.textContent = "Use up to eight diagram items per note.";
    render();
  };
  document.addEventListener("click", event => {
    if (!annotationsActive() || !(event.target instanceof Element) || composer.contains(event.target) || event.target === toggle) return;
    if (arrowTool.drawing()) { event.preventDefault(); event.stopImmediatePropagation(); return; }
    const target = event.target.closest("[data-component], [data-message], [data-arrow]");
    if (target) { event.preventDefault(); event.stopImmediatePropagation(); chooseTarget(target); return; }
    // Keep the current view stable while text and diagram items are being selected.
    if (event.target.closest("a, button, select, input, textarea")) { event.preventDefault(); event.stopImmediatePropagation(); return; }
    if (busy || selectedText) return;
    const source = event.target.closest(".source-card")?.querySelector(".source-ref");
    const prose = event.target.closest("p, pre, blockquote, h1, h2, h3, h4, li, section, header");
    const text = (source?.textContent || prose?.textContent || "").trim();
    if (text && text.length <= 2000) {
      selectedText = text; textPanel = panelFor(event.target); render();
    } else if (text) status.textContent = "This area contains more than 2,000 characters. Highlight the specific text to attach.";
  }, true);
  document.addEventListener("keydown", event => {
    if (!annotationsActive() || !(event.target instanceof Element) || composer.contains(event.target) || event.target === toggle) return;
    if (event.key !== "Enter" && event.key !== " ") return;
    if (arrowTool.drawing()) { event.preventDefault(); event.stopImmediatePropagation(); return; }
    const target = event.target.closest("[data-component], [data-message], [data-arrow]");
    if (target) { event.preventDefault(); event.stopImmediatePropagation(); chooseTarget(target); }
  }, true);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || !annotationsActive() || (!selectedText.trim() && !targets.length && !arrowTool.payload().length) || !note.value.trim()) return;
    const markerError = arrowTool.markerError(note.value);
    if (markerError) { status.textContent = markerError; note.focus(); return; }
    const draft = {
      view: { flow_id: current.flow_id, panel: viewPanel() },
      selected_text: selectedText,
      targets: targets.map(({ kind, id }) => ({ kind, id })),
      note: note.value.replace(/\r\n?/g, "\n"),
      arrows: arrowTool.payload(),
    };
    const fingerprint = JSON.stringify(draft);
    if (request?.fingerprint !== fingerprint) request = { fingerprint, id: crypto.randomUUID() };
    busy = true; status.textContent = "Saving annotation…"; render();
    try {
      const response = await fetch("http://127.0.0.1:8765/annotations", {
        method: "POST", headers: { "Content-Type": "application/json", "X-Nexo-Editor": "1" },
        body: JSON.stringify({ request_id: request.id, ...draft }), signal: AbortSignal.timeout(12000),
      });
      const result = await response.json() as { id?: string; error?: string };
      if (!response.ok || !result.id || !/^A-\d+$/.test(result.id)) throw new Error(result.error || "The helper could not save this annotation.");
      busy = false; exit();
      element("annotation-result").textContent = `Saved ${result.id} to feedback/ANNOTATIONS.md. Answers will appear there.`;
      toggle.focus();
    } catch (error) {
      status.textContent = `Not saved or not yet confirmed. Your draft is kept; retrying it will not create a second entry. Run python3 scripts/editor_helper.py locally if needed. ${error instanceof Error ? error.message : ""}`;
    } finally { busy = false; render(); }
  });
  render();
}
