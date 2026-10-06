type ViewBox = [number, number, number, number];
type Drag = { pointer: number; clientX: number; clientY: number; start: DOMPoint; inverse: DOMMatrix; box: ViewBox; moved: boolean };
export type DiagramViewport = { reset: () => void; setZoom: (scale: number) => void; setViewBox: (box: ViewBox) => void };
const navigating = new WeakMap<SVGSVGElement, () => boolean>();

export function diagramNavigationActive(node: Element): boolean {
  if (document.body.classList.contains("annotation-mode")) return true;
  const svg = node.closest("svg");
  return svg instanceof SVGSVGElement && (navigating.get(svg)?.() ?? false);
}

export function bindDiagramViewport(svg: SVGSVGElement, changed: () => void = () => {}): DiagramViewport {
  const original = svg.viewBox.baseVal;
  const initial: ViewBox = [original.x, original.y, original.width, original.height];
  let box: ViewBox = [...initial];
  let drag: Drag | null = null;
  let suppressClick = false;
  let quietUntil = 0;
  const suspended = (): boolean => document.body.classList.contains("annotation-mode");
  navigating.set(svg, () => drag !== null || performance.now() < quietUntil);
  svg.classList.add("diagram-viewport");
  svg.setAttribute("tabindex", "0");
  svg.setAttribute("aria-describedby", "diagram-navigation-help");
  svg.setAttribute("aria-keyshortcuts", "+ - 0 ArrowUp ArrowDown ArrowLeft ArrowRight");

  const apply = (next: ViewBox): void => {
    box = next;
    svg.setAttribute("viewBox", next.join(" "));
  };
  const point = (x: number, y: number): DOMPoint | null => {
    const matrix = svg.getScreenCTM();
    return matrix ? new DOMPoint(x, y).matrixTransform(matrix.inverse()) : null;
  };
  const zoom = (factor: number, anchor: DOMPoint): void => {
    const scale = Math.min(128, Math.max(.5, initial[2] / box[2] * factor));
    factor = scale / (initial[2] / box[2]);
    apply([anchor.x - (anchor.x - box[0]) / factor, anchor.y - (anchor.y - box[1]) / factor, box[2] / factor, box[3] / factor]);
  };
  const end = (cancelled: boolean): void => {
    if (!drag) return;
    const { pointer, moved } = drag;
    drag = null;
    svg.classList.remove("diagram-dragging");
    if (svg.hasPointerCapture(pointer)) svg.releasePointerCapture(pointer);
    if (moved) {
      suppressClick = true;
      quietUntil = performance.now() + 250;
      if (cancelled) changed();
    }
  };
  svg.addEventListener("pointerdown", event => {
    if (suspended() || event.button !== 0 || !event.isPrimary || drag) return;
    const matrix = svg.getScreenCTM(); if (!matrix) return;
    const inverse = matrix.inverse();
    suppressClick = false;
    drag = { pointer: event.pointerId, clientX: event.clientX, clientY: event.clientY, start: new DOMPoint(event.clientX, event.clientY).matrixTransform(inverse), inverse, box: [...box], moved: false };
  }, true);
  svg.addEventListener("pointermove", event => {
    if (!drag || event.pointerId !== drag.pointer) return;
    if (suspended()) { end(true); return; }
    // A press still means “select”; only a deliberate movement becomes a pan.
    event.stopPropagation();
    if (!drag.moved && Math.hypot(event.clientX - drag.clientX, event.clientY - drag.clientY) < 5) return;
    if (!drag.moved) {
      drag.moved = true;
      svg.setPointerCapture(event.pointerId);
      svg.classList.add("diagram-dragging");
    }
    event.preventDefault();
    const current = new DOMPoint(event.clientX, event.clientY).matrixTransform(drag.inverse);
    apply([drag.box[0] - (current.x - drag.start.x), drag.box[1] - (current.y - drag.start.y), drag.box[2], drag.box[3]]);
    changed();
  }, true);
  svg.addEventListener("pointerup", event => { if (event.pointerId === drag?.pointer) end(false); }, true);
  svg.addEventListener("pointercancel", event => { if (event.pointerId === drag?.pointer) end(true); }, true);
  svg.addEventListener("lostpointercapture", () => end(true));
  svg.addEventListener("pointerleave", () => { if (drag && !drag.moved) end(true); });
  svg.addEventListener("click", event => {
    if (suppressClick && event.detail !== 0) {
      event.preventDefault(); event.stopImmediatePropagation(); suppressClick = false;
    }
  }, true);
  svg.addEventListener("wheel", event => {
    if (suspended() || drag) return;
    const anchor = point(event.clientX, event.clientY); if (!anchor) return;
    event.preventDefault();
    const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? svg.clientHeight : 1);
    zoom(Math.exp(-Math.max(-200, Math.min(200, delta)) * .003), anchor);
    quietUntil = performance.now() + 250;
    changed();
  }, { passive: false });
  svg.addEventListener("keydown", event => {
    if (suspended() || event.altKey || event.ctrlKey || event.metaKey) return;
    const center = new DOMPoint(box[0] + box[2] / 2, box[1] + box[3] / 2);
    switch (event.key) {
      case "+": case "=": zoom(1.25, center); break;
      case "-": case "_": zoom(.8, center); break;
      case "0": apply([...initial]); break;
      case "ArrowLeft": apply([box[0] - box[2] * .1, box[1], box[2], box[3]]); break;
      case "ArrowRight": apply([box[0] + box[2] * .1, box[1], box[2], box[3]]); break;
      case "ArrowUp": apply([box[0], box[1] - box[3] * .1, box[2], box[3]]); break;
      case "ArrowDown": apply([box[0], box[1] + box[3] * .1, box[2], box[3]]); break;
      default: return;
    }
    event.preventDefault(); event.stopPropagation(); changed();
  });
  return {
    reset: () => apply([...initial]),
    setZoom: scale => {
      const factor = Math.min(128, Math.max(.5, scale));
      apply([initial[0] + initial[2] * (1 - 1 / factor) / 2, initial[1] + initial[3] * (1 - 1 / factor) / 2, initial[2] / factor, initial[3] / factor]);
    },
    setViewBox: next => apply([...next]),
  };
}
