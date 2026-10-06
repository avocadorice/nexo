import { annotationsActive } from "./annotations.js";

export type Requirement = {
  id: string;
  kind: "functional" | "non-functional";
  title: string;
  components: string[];
  explanation: string;
  limitation?: string;
  status?: "partial";
};

export function initializeRequirements(requirements: Requirement[], components: { id: string; name: string }[], glossary: (root: HTMLElement) => void): void {
  const section = document.getElementById("requirements")!;
  const detail = document.getElementById("requirement-detail")!;
  const clear = document.getElementById("clear-requirement") as HTMLButtonElement;
  const state = document.getElementById("requirement-state")!;
  const names = new Map(components.map(component => [component.id, component.name]));
  const buttons = new Map<string, HTMLButtonElement>();
  let hovered: string | null = null;
  let focused: string | null = null;
  let pinned: string | null = null;
  let shown: string | null | undefined;

  const paragraph = (text: string, className = ""): void => {
    const node = document.createElement("p"); node.textContent = text; node.className = className; detail.append(node);
  };
  const render = (): void => {
    const active = requirements.find(requirement => requirement.id === (hovered ?? focused ?? pinned));
    document.querySelectorAll<SVGGElement>("#architecture [data-component]").forEach(node => {
      node.classList.toggle("requirement-match", !!active?.components.includes(node.dataset.component ?? ""));
    });
    for (const [id, button] of buttons) {
      button.classList.toggle("requirement-active", id === active?.id);
      button.setAttribute("aria-pressed", String(id === pinned));
    }
    clear.disabled = !pinned;
    state.textContent = pinned ? `Pinned ${pinned}` : "";
    const next = active?.id ?? null;
    if (shown === next) return;
    shown = next; detail.replaceChildren(); detail.scrollTop = 0;
    if (!active) {
      paragraph("Hover or focus a requirement to see its matching boxes. Click to pin those outlines while you read the diagrams.");
      for (const requirement of requirements.filter(item => item.status === "partial")) {
        paragraph(`${requirement.id} · Partially implemented: ${requirement.limitation ?? requirement.explanation}`, "simplification");
      }
    } else {
      paragraph(`${active.id} · Boxes: ${active.components.map(id => names.get(id) ?? id).join(" · ")}`, "requirement-boxes");
      paragraph(active.explanation);
      if (active.limitation) paragraph(active.limitation, "simplification");
    }
    glossary(detail);
  };
  for (const requirement of requirements) {
    const list = document.getElementById(requirement.kind === "functional" ? "functional-requirements" : "nonfunctional-requirements")!;
    const item = document.createElement("li");
    const button = document.createElement("button"); button.type = "button"; button.className = "requirement-button";
    button.dataset.requirement = requirement.id;
    const id = document.createElement("strong"); id.textContent = requirement.id;
    button.append(id, document.createTextNode(` ${requirement.title}`));
    if (requirement.status === "partial") {
      const badge = document.createElement("span"); badge.className = "requirement-partial"; badge.textContent = "Partial"; button.append(badge);
    }
    button.setAttribute("aria-controls", "architecture requirement-detail");
    button.setAttribute("aria-describedby", "requirements-help");
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("pointerenter", () => { if (!annotationsActive()) { hovered = requirement.id; render(); } });
    button.addEventListener("pointerleave", () => { if (!annotationsActive()) { hovered = null; render(); } });
    button.addEventListener("focus", () => { if (!annotationsActive()) { hovered = null; focused = requirement.id; render(); } });
    button.addEventListener("blur", () => { if (!annotationsActive()) { focused = null; render(); } });
    button.addEventListener("click", () => { if (!annotationsActive()) { pinned = pinned === requirement.id ? null : requirement.id; render(); } });
    buttons.set(requirement.id, button); item.append(button); list.append(item);
  }
  clear.addEventListener("click", () => {
    if (annotationsActive()) return;
    pinned = null; hovered = null; focused = null; render();
  });
  section.addEventListener("keydown", event => {
    if (event.key !== "Escape" || annotationsActive()) return;
    pinned = null; hovered = null; focused = null; render();
  });
  // Leave the captured view still during annotations, then resume from the current pointer and focus.
  new MutationObserver(() => {
    if (annotationsActive()) return;
    hovered = [...buttons].find(([, button]) => button.matches(":hover"))?.[0] ?? null;
    focused = [...buttons].find(([, button]) => button === document.activeElement)?.[0] ?? null;
    render();
  }).observe(document.body, { attributes: true, attributeFilter: ["class"] });
  render();
}
