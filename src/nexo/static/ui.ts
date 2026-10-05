type JsonRecord = Record<string, unknown>;
type Transaction = { id: string; network: string; currency: string; amount_minor: string; disputed: boolean };
type Chargeback = { id: string; transaction_id: string; amount_minor: string; currency: string; network: string; reason: string; status: string; batch_id: string | null; received_at: string };
type Batch = { id: string; slot: string; network: string; partition: number; state: string; row_count: number; object_key: string | null; sha256: string | null; byte_count: number | null; publication_started: boolean; last_error: string | null };
const element = <T extends HTMLElement>(id: string): T => {
  const found = document.getElementById(id);
  if (!found) throw new Error(`Missing element: ${id}`);
  return found as T;
};
const customer = document.body.dataset.page === "customer";
let token = "";
let transactions: Transaction[] = [];
let pendingSubmission: { body: string; key: string } | null = null;
let sessionVersion = 0;

function notice(message: string, error = false): void {
  const node = element("notice");
  node.textContent = message;
  node.className = error ? "error" : "";
}
async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Authorization", `Bearer ${token}`);
  if (options.body) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  const data: unknown = await response.json();
  if (!response.ok) {
    const message = typeof data === "object" && data && "error" in data ? String(data.error) : `Request failed (${response.status})`;
    throw new Error(message);
  }
  return data as T;
}
function table<T>(headers: string[], rows: T[], values: (row: T) => (string | Node)[]): HTMLElement {
  const table = document.createElement("table");
  const head = table.createTHead().insertRow();
  headers.forEach(label => { const th = document.createElement("th"); th.scope = "col"; th.textContent = label; head.append(th); });
  const body = table.createTBody();
  for (const item of rows) {
    const row = body.insertRow();
    values(item).forEach(value => { const cell = row.insertCell(); typeof value === "string" ? cell.textContent = value : cell.append(value); });
  }
  if (rows.length === 0) { const cell = body.insertRow().insertCell(); cell.colSpan = headers.length; cell.textContent = "No records yet."; }
  return table;
}
function json(value: unknown): HTMLPreElement {
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(value, null, 2);
  return pre;
}
function formatAmount(amount: string, currency: string): string { return `${amount} ${currency} minor units`; }
async function loadCustomer(version: number): Promise<void> {
  const [txs, cbs] = await Promise.all([
    api<{ transactions: Transaction[] }>("/api/transactions"),
    api<{ chargebacks: Chargeback[] }>("/api/chargebacks"),
  ]);
  if (version !== sessionVersion) return;
  transactions = txs.transactions;
  const select = element<HTMLSelectElement>("transaction");
  const previous = select.value;
  select.replaceChildren(new Option("Select a transaction", ""));
  for (const tx of transactions) {
    const option = new Option(`${tx.id} · ${tx.network} · ${formatAmount(tx.amount_minor, tx.currency)}${tx.disputed ? " · already disputed" : ""}`, tx.id);
    option.disabled = tx.disputed;
    select.add(option);
  }
  select.value = previous;
  element("chargebacks").replaceChildren(table(["Chargeback", "Amount", "Status", "Batch", "Received"], cbs.chargebacks, cb => [cb.id, formatAmount(cb.amount_minor, cb.currency), cb.status, cb.batch_id ?? "Unassigned", cb.received_at]));
  element<HTMLButtonElement>("submit-chargeback").disabled = false;
}
async function showBatch(id: string): Promise<void> {
  const version = sessionVersion;
  try {
    const detail = await api<{ batch: Batch; attempts: JsonRecord[]; events: JsonRecord[] }>(`/api/ops/batches/${encodeURIComponent(id)}`);
    if (version !== sessionVersion) return;
    element("batch-panel").hidden = false;
    element("batch-detail").replaceChildren(json(detail.batch));
    element("attempts").replaceChildren(table(["Started", "Finished", "Outcome", "Detail"], detail.attempts, a => [String(a.started_at ?? ""), String(a.finished_at ?? "In progress"), String(a.outcome ?? "In progress"), JSON.stringify(a.detail ?? null)]));
    element("events").replaceChildren(table(["Time", "Event", "Details"], detail.events, e => [String(e.created_at ?? ""), String(e.event ?? ""), JSON.stringify(e.details ?? {})]));
  } catch (error) { if (version === sessionVersion) notice(String(error), true); }
}
async function loadOps(version: number): Promise<void> {
  const [summary, batches] = await Promise.all([
    api<{ chargebacks: Record<string, number>; batches: Record<string, number>; oldest_pending_seconds: number | null }>("/api/ops/summary"),
    api<{ batches: Batch[] }>("/api/ops/batches"),
  ]);
  if (version !== sessionVersion) return;
  const metrics = document.createElement("div"); metrics.className = "metrics";
  for (const [name, value] of Object.entries({ "Oldest pending (seconds)": summary.oldest_pending_seconds ?? "No pending work", ...Object.fromEntries(Object.entries(summary.chargebacks).map(([state, count]) => [`Chargebacks · ${state}`, count])), ...Object.fromEntries(Object.entries(summary.batches).map(([state, count]) => [`Batches · ${state}`, count])) })) {
    const metric = document.createElement("div"); metric.className = "metric";
    const number = document.createElement("strong"); number.textContent = String(value);
    const label = document.createElement("span"); label.textContent = name; metric.append(number, label); metrics.append(metric);
  }
  element("summary").replaceChildren(metrics);
  element("batches").replaceChildren(table(["Batch", "Slot", "Network / partition", "State", "Rows", "Last error"], batches.batches, batch => {
    const button = document.createElement("button"); button.textContent = batch.id; button.addEventListener("click", () => void showBatch(batch.id));
    return [button, batch.slot, `${batch.network} / ${batch.partition}`, batch.state, String(batch.row_count), batch.last_error ?? "—"];
  }));
}
async function refresh(): Promise<void> {
  const version = sessionVersion;
  notice("Loading…");
  element<HTMLButtonElement>("refresh").disabled = true;
  try {
    if (customer) await loadCustomer(version); else await loadOps(version);
    if (version === sessionVersion) notice(`Updated ${new Date().toLocaleTimeString()}. Lists show the most recent records; batch details include delivery attempts and audit events.`);
  } catch (error) { if (version === sessionVersion) notice(String(error), true); }
  finally { if (version === sessionVersion && token) element<HTMLButtonElement>("refresh").disabled = false; }
}
element<HTMLFormElement>("session").addEventListener("submit", event => {
  event.preventDefault();
  token = element<HTMLInputElement>("token").value.trim();
  element<HTMLInputElement>("token").value = "";
  sessionVersion += 1;
  pendingSubmission = null;
  if (customer) {
    transactions = [];
    element("chargebacks").replaceChildren();
    element<HTMLSelectElement>("transaction").replaceChildren(new Option("Loading account…", ""));
    element("submission").textContent = "";
    element<HTMLButtonElement>("submit-chargeback").disabled = true;
  } else {
    element("summary").replaceChildren();
    element("batches").replaceChildren();
    element("batch-panel").hidden = true;
  }
  void refresh();
});
element("clear-session").addEventListener("click", () => { token = ""; sessionVersion += 1; location.reload(); });
element("refresh").addEventListener("click", () => void refresh());
if (customer) {
  element<HTMLSelectElement>("transaction").addEventListener("change", () => {
    const selected = transactions.find(tx => tx.id === element<HTMLSelectElement>("transaction").value);
    if (selected) element<HTMLInputElement>("amount").value = selected.amount_minor;
  });
  element<HTMLFormElement>("chargeback-form").addEventListener("submit", async event => {
    event.preventDefault();
    const body = JSON.stringify({ transaction_id: element<HTMLSelectElement>("transaction").value, amount_minor: element<HTMLInputElement>("amount").value, reason: element<HTMLSelectElement>("reason").value });
    // Keep the same key after a lost response so retrying cannot create another chargeback.
    if (!pendingSubmission || pendingSubmission.body !== body) pendingSubmission = { body, key: crypto.randomUUID() };
    const submission = pendingSubmission;
    const version = sessionVersion;
    const button = element<HTMLButtonElement>("submit-chargeback"); button.disabled = true;
    try {
      const chargeback = await api<Chargeback>("/api/chargebacks", { method: "POST", body: submission.body, headers: { "Idempotency-Key": submission.key } });
      if (version !== sessionVersion) return;
      pendingSubmission = null;
      element("submission").textContent = `Chargeback ${chargeback.id} accepted. Status: ${chargeback.status}.`;
      element("submission").className = "success";
      await refresh();
    } catch (error) {
      if (version === sessionVersion) { element("submission").textContent = `${String(error)}. If the response was lost, submit the same details again to retry safely.`; element("submission").className = "error"; }
    } finally { if (version === sessionVersion) button.disabled = false; }
  });
}
export {};
