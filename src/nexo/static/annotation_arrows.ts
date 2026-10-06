export type AnnotationPanel = "architecture" | "sequence" | "code" | "community" | "glossary" | "page";
export type AnnotationTarget = { kind: "component" | "message" | "arrow"; id: string };
type Endpoint = { panel: AnnotationPanel; target?: AnnotationTarget; text: string; x: number; y: number };
export type AnnotationArrow = { id: number; from: Endpoint; to: Endpoint };
type Anchor = { element: Element; x: number; y: number; endpoint: Endpoint };
type Arrow = { id: number; from: Anchor; to: Anchor };
type Options = { enabled: () => boolean; panelFor: (element: Element) => AnnotationPanel; targetName: (target: AnnotationTarget) => string | undefined; changed: () => void; starting: () => void; insertMarker: (id: number) => boolean; removeMarker: (id: number) => void; status: (text: string) => void };
const ns = "http://www.w3.org/2000/svg";
const clamp = (value: number): number => Math.max(0, Math.min(1, value));

export function initializeAnnotationArrows(options: Options): { payload: () => AnnotationArrow[]; drawing: () => boolean; clear: () => void; refresh: () => void; markerError: (note: string) => string | null } {
  const button = document.getElementById("annotation-draw-arrow") as HTMLButtonElement;
  const list = document.getElementById("annotation-arrows")!;
  const composer = document.getElementById("annotation-panel")!;
  const overlay = document.createElementNS(ns, "svg"); overlay.classList.add("annotation-arrow-overlay"); overlay.setAttribute("aria-hidden", "true"); document.body.append(overlay);
  let arrows: Arrow[] = [];
  let nextID = 1;
  let armed = false;
  let quietUntil = 0;
  let drag: { pointer: number; from: Anchor; x: number; y: number } | null = null;
  let frame = 0;
  const targetFor = (node: Element): AnnotationTarget | undefined => {
    const participant = node.getAttribute("data-participant");
    if (participant && options.targetName({ kind: "component", id: participant })) return { kind: "component", id: participant };
    for (const kind of ["component", "message", "arrow"] as const) {
      const id = node.getAttribute(`data-${kind}`); if (id && options.targetName({ kind, id })) return { kind, id };
    }
    return undefined;
  };
  const anchorAt = (x: number, y: number): Anchor | null => {
    const hit = document.elementFromPoint(x, y); if (!hit || composer.contains(hit)) return null;
    const mapped = hit.closest("[data-component], [data-message], [data-arrow], [data-participant]");
    const semantic = hit.closest(".community-region, .code-line, .source-ref, button, a, input, select, textarea, label, p, li, h1, h2, h3, h4, summary, text, tspan");
    const panel = options.panelFor(hit);
    const panelElement = panel === "community" ? hit.closest(".community-card") : document.getElementById(({ architecture: "architecture", sequence: "sequence", code: "code-panel", glossary: "glossary", page: "" } as const)[panel]);
    const area = panelElement ?? document.body;
    const anchor = mapped ?? semantic ?? (hit.closest("svg") || area);
    const rect = anchor.getBoundingClientRect(); const region = area.getBoundingClientRect();
    if (!rect.width || !rect.height || !region.width || !region.height) return null;
    const target = mapped ? targetFor(mapped) : undefined;
    const source = hit.closest(".source-card")?.querySelector(".source-ref")?.textContent;
    const label = target ? options.targetName(target) : semantic?.querySelector("title")?.textContent || semantic?.getAttribute("aria-label") || semantic?.textContent || (panel === "community" ? hit.closest(".community-card")?.querySelector("h3")?.textContent : "");
    const text = [source, label].filter(Boolean).join(" · ").replace(/\s+/g, " ").trim().slice(0, 500);
    return { element: anchor, x: clamp((x - rect.left) / rect.width), y: clamp((y - rect.top) / rect.height), endpoint: { panel, ...(target ? { target } : {}), text, x: clamp((x - region.left) / region.width), y: clamp((y - region.top) / region.height) } };
  };
  const point = (anchor: Anchor): [number, number] | null => {
    const rect = anchor.element.getBoundingClientRect();
    if (!anchor.element.isConnected || !rect.width || !rect.height) return null;
    const x = rect.left + rect.width * anchor.x; const y = rect.top + rect.height * anchor.y;
    if (x < 0 || x > innerWidth || y < 0 || y > innerHeight) return null;
    for (const clip of [anchor.element.closest(".explorer-grid > section"), anchor.element.closest(".diagram, .community-picture")]) {
      if (!clip) continue;
      const bounds = clip.getBoundingClientRect();
      if (x < bounds.left || x > bounds.right || y < bounds.top || y > bounds.bottom) return null;
    }
    return [x, y];
  };
  const draw = (): void => {
    frame = 0; overlay.replaceChildren();
    if (!document.body.classList.contains("annotation-mode")) return;
    const line = (id: number, from: [number, number], to: [number, number]): void => {
      const angle = Math.atan2(to[1] - from[1], to[0] - from[0]);
      const path = document.createElementNS(ns, "path");
      path.setAttribute("d", `M${from[0]} ${from[1]} L${to[0]} ${to[1]} M${to[0] - 12 * Math.cos(angle - .45)} ${to[1] - 12 * Math.sin(angle - .45)} L${to[0]} ${to[1]} L${to[0] - 12 * Math.cos(angle + .45)} ${to[1] - 12 * Math.sin(angle + .45)}`);
      overlay.append(path);
      const label = document.createElementNS(ns, "text"); label.setAttribute("x", String((from[0] + to[0]) / 2 + 5)); label.setAttribute("y", String((from[1] + to[1]) / 2 - 5)); label.textContent = String(id); overlay.append(label);
    };
    for (const arrow of arrows) {
      const from = point(arrow.from); const to = point(arrow.to); if (from && to) line(arrow.id, from, to);
    }
    if (drag) { const from = point(drag.from); if (from) line(nextID, from, [drag.x, drag.y]); }
  };
  const redraw = (): void => { if (!frame) frame = requestAnimationFrame(draw); };
  const description = (endpoint: Endpoint): string => `${endpoint.panel}: ${endpoint.text || "blank space"} (${Math.round(endpoint.x * 100)}%, ${Math.round(endpoint.y * 100)}%)`;
  const refresh = (): void => {
    button.disabled = !options.enabled() || arrows.length >= 8;
    button.textContent = armed ? "Cancel drawing" : "Draw arrow"; button.setAttribute("aria-pressed", String(armed));
    document.body.classList.toggle("annotation-drawing", armed);
    list.replaceChildren();
    for (const arrow of arrows) {
      const item = document.createElement("li"); item.textContent = `[arrow ${arrow.id}] From ${description(arrow.from.endpoint)} → To ${description(arrow.to.endpoint)} `;
      const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "Remove"; remove.setAttribute("aria-label", `Remove arrow ${arrow.id}`); remove.disabled = !options.enabled();
      remove.addEventListener("click", () => { arrows = arrows.filter(value => value !== arrow); options.removeMarker(arrow.id); refresh(); options.changed(); });
      item.append(remove); list.append(item);
    }
    redraw();
  };
  const cancelDrawing = (): void => {
    const pointer = drag?.pointer; drag = null; armed = false;
    if (pointer !== undefined && document.documentElement.hasPointerCapture(pointer)) document.documentElement.releasePointerCapture(pointer);
    refresh();
  };
  button.addEventListener("click", () => {
    if (armed) { cancelDrawing(); return; }
    if (!options.enabled() || arrows.length >= 8) return;
    options.starting(); armed = true; window.getSelection()?.removeAllRanges(); refresh();
    options.status("Drag from the starting point to the arrow tip. Escape cancels drawing. The numbered marker will be inserted in your note.");
  });
  document.addEventListener("pointerdown", event => {
    if (!armed || !options.enabled() || event.button !== 0 || !event.isPrimary || drag) return;
    const from = anchorAt(event.clientX, event.clientY); if (!from) return;
    event.preventDefault(); event.stopImmediatePropagation();
    drag = { pointer: event.pointerId, from, x: event.clientX, y: event.clientY };
    document.documentElement.setPointerCapture(event.pointerId); redraw();
  }, true);
  document.addEventListener("pointermove", event => {
    if (!drag || event.pointerId !== drag.pointer) return;
    event.preventDefault(); event.stopImmediatePropagation(); drag.x = event.clientX; drag.y = event.clientY; redraw();
  }, true);
  document.addEventListener("pointerup", event => {
    if (!drag || event.pointerId !== drag.pointer) return;
    event.preventDefault(); event.stopImmediatePropagation();
    const from = drag.from; const start = point(from); const to = anchorAt(event.clientX, event.clientY);
    quietUntil = performance.now() + 300; cancelDrawing();
    if (!to || !start || Math.hypot(event.clientX - start[0], event.clientY - start[1]) < 8) { options.status("Draw a longer arrow between two points outside the note editor."); return; }
    const id = nextID++;
    arrows.push({ id, from, to });
    if (!options.insertMarker(id)) arrows.pop();
    refresh(); options.changed();
  }, true);
  document.addEventListener("pointercancel", event => { if (drag?.pointer === event.pointerId) { quietUntil = performance.now() + 300; cancelDrawing(); } }, true);
  document.addEventListener("lostpointercapture", event => { if (drag?.pointer === event.pointerId) { quietUntil = performance.now() + 300; cancelDrawing(); } }, true);
  window.addEventListener("blur", () => { if (armed) cancelDrawing(); });
  document.addEventListener("keydown", event => { if (armed && event.key === "Escape") { event.preventDefault(); event.stopImmediatePropagation(); cancelDrawing(); } }, true);
  document.addEventListener("scroll", redraw, true); window.addEventListener("resize", redraw); document.addEventListener("toggle", redraw, true);
  new ResizeObserver(redraw).observe(document.body);
  return {
    payload: () => arrows.map(({ id, from, to }) => ({ id, from: from.endpoint, to: to.endpoint })),
    drawing: () => armed || drag !== null || performance.now() < quietUntil,
    clear: () => { arrows = []; nextID = 1; quietUntil = 0; cancelDrawing(); },
    refresh,
    markerError: note => {
      for (const arrow of arrows) if (!note.includes(`[arrow ${arrow.id}]`)) return `Include [arrow ${arrow.id}] in your note at least once, or remove that arrow.`;
      for (const match of note.matchAll(/\[arrow (\d+)\]/g)) if (!arrows.some(arrow => String(arrow.id) === match[1])) return `No drawn arrow matches ${match[0]}. Remove the marker or draw an arrow.`;
      return null;
    },
  };
}
