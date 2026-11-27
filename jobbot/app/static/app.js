"use strict";

/* ------------------------------------------------------------------ helpers */

const $ = (selector, root = document) => root.querySelector(selector);

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false || child === "") continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const fmt = (n) => Number(n || 0).toLocaleString();
const plural = (n, word) => `${fmt(n)} ${word}${Number(n) === 1 ? "" : "s"}`;
const MODES = { remote: "Remote", hybrid: "Hybrid", onsite: "On-site" };

function toDate(ts) {
  if (!ts) return null;
  return new Date(/^\d{4}-\d{2}-\d{2}$/.test(ts) ? `${ts}T12:00:00Z` : ts);
}

function since(ts) {
  const date = toDate(ts);
  if (!date || Number.isNaN(date.getTime())) return "";
  let minutes = (date.getTime() - Date.now()) / 60000;
  const future = minutes > 0;
  minutes = Math.abs(minutes);
  let span;
  if (minutes < 1) return future ? "now" : "just now";
  if (minutes < 60) span = `${Math.round(minutes)} min`;
  else if (minutes < 1440) span = `${Math.floor(minutes / 60)} h ${Math.round(minutes % 60)} min`;
  else if (minutes < 60 * 24 * 60) span = plural(Math.round(minutes / 1440), "day");
  else return date.toLocaleDateString();
  return future ? `in ${span}` : `${span} ago`;
}

function posted(ts) {
  if (!ts) return "";
  if (/^\d{4}-\d{2}-\d{2}$/.test(ts)) { // a date without a time: count calendar days
    const [y, m, d] = ts.split("-").map(Number);
    const today = new Date();
    const days = Math.round((Date.UTC(today.getFullYear(), today.getMonth(), today.getDate()) - Date.UTC(y, m - 1, d)) / 86400000);
    if (days <= 0) return "today";
    if (days === 1) return "yesterday";
    return days < 60 ? `${days} days ago` : new Date(Date.UTC(y, m - 1, d)).toLocaleDateString();
  }
  const text = since(ts);
  return text.startsWith("in ") ? "just now" : text;
}

function soon(ts) {
  const text = since(ts);
  return text.startsWith("in ") ? text : "soon";
}

function clock(ms) {
  return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function every(minutes) {
  if (minutes % 1440 === 0) return minutes === 1440 ? "every day" : `every ${minutes / 1440} days`;
  if (minutes % 60 === 0) return minutes === 60 ? "every hour" : `every ${minutes / 60} hours`;
  return `every ${minutes} minutes`;
}

function flag(code) {
  if (!/^[A-Z]{2}$/.test(code || "")) return "";
  return String.fromCodePoint(...[...code].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65));
}

async function api(path, { json, method, ...rest } = {}) {
  const init = { ...rest, headers: { Accept: "application/json" } };
  if (json !== undefined) {
    init.method = method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  } else if (method) {
    init.method = method;
  }
  const res = await fetch(path, init);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      if (data && data.detail) detail = typeof data.detail === "string" ? data.detail : data.detail.map((d) => d.msg).join("; ");
    } catch { /* not JSON */ }
    throw new Error(detail);
  }
  return res.json();
}

let toastTimer;
function toast(message, bad = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = bad ? "toast bad" : "toast";
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, bad ? 6000 : 3500);
}

const state = { names: {}, all: [], countries: null, status: null };
const countryName = (code) => state.names[code] || code;

/* ------------------------------------------------------------------- routing */

const TABS = ["chat", "jobs", "countries", "activity", "settings"];
let currentTab = null;

function route() {
  const [name, query] = location.hash.replace(/^#/, "").split("?");
  const tab = TABS.includes(name) ? name : "chat";
  currentTab = tab;
  for (const t of TABS) $(`#view-${t}`).hidden = t !== tab;
  document.querySelectorAll(".tabs a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
  if (tab === "chat") { renderChat(); $("#chat-input").focus(); }
  if (tab === "jobs") openJobs(new URLSearchParams(query || ""));
  if (tab === "countries") loadCountries();
  if (tab === "activity") loadActivity();
  if (tab === "settings") loadSettings();
}

/* -------------------------------------------------------------------- status */

async function refreshStatus() {
  let status;
  try {
    status = await api("/api/status");
  } catch {
    const el = $("#status");
    el.className = "status offline";
    $(".status-text", el).textContent = "Can't reach the bot — is it running?";
    $("#progress").hidden = true;
    return;
  }
  const previous = state.status;
  state.status = status;
  renderStatus(status);
  const finished = previous && previous.current && (!status.current || status.current.country !== previous.current.country);
  const started = status.current && (!previous || !previous.current);
  if (currentTab === "countries") {
    if (finished || started) loadCountries();
    else updateLive(status);
  }
  if (finished && currentTab === "activity") loadActivity();
}

function renderStatus(s) {
  const el = $("#status");
  el.className = `status ${s.state}`;
  let text;
  if (s.current) {
    const c = s.current;
    text = `Checking ${countryName(c.country)}${c.source ? ` · ${c.source}` : ""} · ${fmt(c.fetched)} jobs read`;
    if (c.new && !c.initial) text += ` · ${fmt(c.new)} new`;
  } else if (s.state === "paused") text = "Paused — scheduled checks are off";
  else if (s.state === "stopped") text = "The bot has stopped — restart the app";
  else if (s.next) text = `Idle · next check: ${countryName(s.next.country)} ${soon(s.next.at)}`;
  else text = "Idle · not watching any country yet";
  $(".status-text", el).textContent = text;
  $("#progress").hidden = !s.current;
  $("#stop-btn").hidden = !s.current;
  const pause = $("#pause-btn");
  pause.hidden = s.state === "stopped";
  pause.textContent = s.paused ? "Resume" : "Pause";
}

async function botAction(action) {
  try {
    renderStatus(await api(`/api/bot/${action}`, { method: "POST" }));
    toast({ pause: "Paused scheduled checks", resume: "Resumed scheduled checks", stop: "Stopping the current check" }[action]);
  } catch (err) { toast(err.message, true); }
}

/* ---------------------------------------------------------------------- chat */

const CHAT_KEY = "jobbot.chat.v1";
const WELCOME = "Hi! I'm jobbot. I run in the background and keep the job openings of the countries you choose up to date.\n\n" +
  "Tell me what to do — for example “watch Sweden” — or ask for jobs, like “nurse jobs in Berlin”. Say “help” to see more.";
const DEFAULT_SUGGESTIONS = ["Watch Sweden", "Watch Germany", "Status", "Help"];
let chatLog = [];

function loadChat() {
  try { chatLog = JSON.parse(localStorage.getItem(CHAT_KEY) || "[]"); } catch { chatLog = []; }
}

function saveChat() {
  const slim = chatLog.slice(-60).map((m) => ({
    ...m,
    jobs: (m.jobs || []).map(({ uid, title, company, location, city, work_mode, posted_at, first_seen, source, sources, new: isNew }) =>
      ({ uid, title, company, location, city, work_mode, posted_at, first_seen, source, sources, new: isNew })),
  }));
  try { localStorage.setItem(CHAT_KEY, JSON.stringify(slim)); } catch { /* storage full or blocked */ }
}

function jobsHash(search) {
  const p = new URLSearchParams();
  if (search.q) p.set("q", search.q);
  if (search.country) p.set("country", search.country);
  if (search.mode) p.set("mode", search.mode);
  if (search.days) p.set("days", search.days);
  if (search.new) { p.set("new", "1"); p.set("sort", "found"); }
  return `#jobs${p.toString() ? `?${p}` : ""}`;
}

function bubble(m) {
  if (m.from === "me") return h("div", { class: "msg me" }, m.text, m.at ? h("span", { class: "time" }, clock(m.at)) : null);
  const el = h("div", { class: "msg bot" }, m.text);
  if (m.jobs && m.jobs.length) el.append(h("div", { class: "jobs" }, m.jobs.map(jobCard)));
  if (m.search && m.total > (m.jobs || []).length) {
    el.append(h("div", { class: "see-all" }, h("a", { class: "btn small", href: jobsHash(m.search) }, `See all ${fmt(m.total)} in Jobs`)));
  }
  if (m.at) el.append(h("span", { class: "time" }, clock(m.at)));
  return el;
}

function renderChat() {
  const box = $("#messages");
  box.replaceChildren(bubble({ from: "bot", text: WELCOME }));
  for (const m of chatLog) box.append(bubble(m));
  const lastBot = [...chatLog].reverse().find((m) => m.from === "bot");
  setSuggestions(lastBot && lastBot.suggestions && lastBot.suggestions.length ? lastBot.suggestions : DEFAULT_SUGGESTIONS);
  scrollChat();
}

function setSuggestions(items) {
  $("#suggestions").replaceChildren(...items.map((text) => h("button", { class: "chip", type: "button", onclick: () => sendChat(text) }, text)));
}

function scrollChat() {
  requestAnimationFrame(() => window.scrollTo({ top: document.body.scrollHeight }));
}

async function sendChat(text) {
  text = (text || "").trim();
  if (!text) return;
  chatLog.push({ from: "me", text, at: Date.now() });
  renderChat();
  const typing = h("div", { class: "msg bot typing" }, "…");
  $("#messages").append(typing);
  scrollChat();
  try {
    const reply = await api("/api/chat", { json: { message: text } });
    chatLog.push({ from: "bot", ...reply, at: Date.now() });
  } catch (err) {
    chatLog.push({ from: "bot", text: `Sorry, that didn't work: ${err.message}`, at: Date.now() });
  }
  saveChat();
  renderChat();
  refreshStatus();
}

/* ---------------------------------------------------------------------- jobs */

const jobState = { page: 1, token: 0 };

function fillCountrySelect() {
  const select = $("#job-filters").elements.country;
  const current = select.value;
  const codes = new Map();
  for (const w of (state.countries && state.countries.watched) || []) codes.set(w.country, w.open);
  for (const c of (state.countries && state.countries.with_data) || []) codes.set(c.code, c.open);
  const options = [h("option", { value: "" }, "All countries")];
  for (const [code, open] of [...codes].sort((a, b) => countryName(a[0]).localeCompare(countryName(b[0])))) {
    options.push(h("option", { value: code }, `${flag(code)} ${countryName(code)} (${fmt(open)})`));
  }
  select.replaceChildren(...options);
  if (current && codes.has(current)) select.value = current;
}

async function openJobs(params) {
  if (!state.countries) {
    try { state.countries = await api("/api/countries"); } catch { /* shown below */ }
  }
  fillCountrySelect();
  const form = $("#job-filters");
  for (const name of ["q", "mode", "days"]) form.elements[name].value = params.get(name) || "";
  form.elements.sort.value = params.get("sort") || "posted";
  form.elements.new.checked = params.get("new") === "1";
  const country = params.get("country") || "";
  if (country && ![...form.elements.country.options].some((o) => o.value === country)) {
    form.elements.country.append(h("option", { value: country }, `${flag(country)} ${countryName(country)}`));
  }
  form.elements.country.value = country;
  loadJobs(true);
}

function filterParams() {
  const f = $("#job-filters").elements;
  const p = new URLSearchParams();
  if (f.q.value.trim()) p.set("q", f.q.value.trim());
  if (f.country.value) p.set("country", f.country.value);
  if (f.mode.value) p.set("mode", f.mode.value);
  if (f.days.value) p.set("days", f.days.value);
  if (f.sort.value !== "posted") p.set("sort", f.sort.value);
  if (f.new.checked) p.set("new", "1");
  return p;
}

async function loadJobs(reset) {
  const params = filterParams();
  if (reset) jobState.page = 1;
  const query = new URLSearchParams(params);
  if (query.get("new") === "1") query.set("new", "true");
  query.set("page", jobState.page);
  query.set("size", 30);
  const token = ++jobState.token;
  const list = $("#job-list");
  if (reset) list.replaceChildren(h("div", { class: "empty" }, "Searching…"));
  try {
    const data = await api(`/api/jobs?${query}`);
    if (token !== jobState.token) return; // a newer search has started
    if (reset) list.replaceChildren();
    if (!data.jobs.length && jobState.page === 1) list.append(emptyJobs(params));
    list.append(...data.jobs.map(jobCard));
    const shown = list.querySelectorAll(".job").length;
    $("#result-count").textContent = data.total
      ? `${plural(data.total, "open job")}${shown < data.total ? ` · showing ${fmt(shown)}` : ""}` : "";
    $("#more-jobs").hidden = shown >= data.total;
    const country = params.get("country");
    const link = $("#export-link");
    link.hidden = !country;
    if (country) link.href = `/api/export?country=${encodeURIComponent(country)}&format=csv`;
    const hash = `#jobs${params.toString() ? `?${params}` : ""}`;
    if (location.hash !== hash) history.replaceState(null, "", hash);
  } catch (err) {
    if (reset) list.replaceChildren();
    toast(err.message, true);
  }
}

function emptyJobs(params) {
  const watching = state.countries && state.countries.watched && state.countries.watched.length;
  if (!watching && !(state.countries && state.countries.with_data.length)) {
    return h("div", { class: "empty" }, "No jobs yet. ", h("a", { href: "#countries" }, "Watch a country"), " and the bot will start collecting them.");
  }
  return h("div", { class: "empty" }, params.toString() ? "No open jobs match these filters." : "No open jobs stored yet — the first check may still be running.");
}

function isRecent(ts, days = 7) {
  const date = toDate(ts);
  return date && Date.now() - date.getTime() < days * 86400000;
}

function jobCard(job) {
  const meta = [job.company, job.location || job.city, MODES[job.work_mode]].filter(Boolean).join(" · ");
  const when = job.posted_at ? `Posted ${posted(job.posted_at)}` : `Found ${since(job.first_seen)}`;
  return h("button", { class: "job", type: "button", onclick: () => openJob(job.uid) },
    h("div", { class: "title" }, h("span", {}, job.title), job.new && isRecent(job.first_seen) ? h("span", { class: "badge new" }, "New") : null),
    meta ? h("div", { class: "meta" }, meta) : null,
    h("div", { class: "meta" }, `${when} · ${job.sources || job.source}`),
    job.snippet ? h("div", { class: "snippet" }, job.snippet) : null);
}

function salary(job) {
  if (!job.salary_min && !job.salary_max) return job.salary_text || "";
  const range = job.salary_min === job.salary_max || !job.salary_max
    ? fmt(job.salary_min || job.salary_max) : `${fmt(job.salary_min)}–${fmt(job.salary_max)}`;
  return [range, job.salary_currency, job.salary_period ? `per ${job.salary_period}` : ""].filter(Boolean).join(" ");
}

async function openJob(uid) {
  let job;
  try { job = await api(`/api/jobs/${encodeURIComponent(uid)}`); } catch (err) { toast(err.message, true); return; }
  const facts = [
    ["Company", job.company], ["Location", job.location || job.city], ["Work mode", MODES[job.work_mode]],
    ["Employment", job.employment_type], ["Category", job.category], ["Salary", salary(job)],
    ["Posted", job.posted_at ? `${toDate(job.posted_at).toLocaleDateString()} (${posted(job.posted_at)})` : ""],
    ["Closes", job.expires_at ? toDate(job.expires_at).toLocaleDateString() : ""],
    ["First seen", `${since(job.first_seen)}`],
    ["Source", [job.source, ...(job.also_on || []).map((o) => o.source)].join(", ")],
  ].filter(([, v]) => v);
  $("#job-detail").replaceChildren(
    h("h3", { id: "job-title" }, job.title),
    h("dl", { class: "facts" }, facts.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])),
    job.url ? h("p", {}, h("a", { class: "btn primary", href: job.url, target: "_blank", rel: "noopener noreferrer" }, "Open the job listing ↗")) : null,
    job.description ? h("div", { class: "description" }, job.description) : h("p", { class: "muted" }, "This source gives no description; open the listing for details."));
  $("#job-dialog").showModal();
}

/* ----------------------------------------------------------------- countries */

async function loadCountries() {
  let data;
  try { data = await api("/api/countries"); } catch (err) { toast(err.message, true); return; }
  state.countries = data;
  for (const c of data.all) state.names[c.code] = c.name;
  const options = $("#country-options");
  if (!options.children.length) options.replaceChildren(...data.all.map((c) => h("option", { value: c.name }, c.code)));
  const list = $("#watch-list");
  if (!data.watched.length) {
    list.replaceChildren(h("div", { class: "card empty" }, "Not watching any country yet. Pick one above — Sweden and Germany work without any API key."));
  } else {
    list.replaceChildren(...data.watched.map((w) => watchCard(w, state.status)));
  }
  const others = $("#other-countries");
  others.replaceChildren();
  if (data.with_data.length) {
    others.append(h("h2", {}, "Stored but not watched"),
      h("div", { class: "other-list" }, data.with_data.map((c) =>
        h("a", { class: "chip", href: `#jobs?country=${c.code}` }, `${flag(c.code)} ${c.name} · ${fmt(c.open)}`))));
  }
}

function scheduleText(w) {
  const parts = [w.enabled ? `Checks ${every(w.every_minutes)}` : "Paused"];
  if (w.last_run_at) parts.push(`last ${since(w.last_run_at)}${w.last_status && w.last_status !== "ok" ? ` (${w.last_status})` : ""}`);
  else parts.push("not checked yet");
  if (w.enabled && w.next_run_at) parts.push(`next ${soon(w.next_run_at)}`);
  return parts.join(" · ");
}

function watchCard(w, status) {
  const running = status && status.current && status.current.country === w.country;
  const keyed = w.blocked.filter((b) => b.needs === "key");
  const boards = w.blocked.filter((b) => b.needs === "companies");
  const sources = h("div", { class: "sources" },
    w.ready.map((r) => h("span", { class: "badge ok", title: r.coverage || "" }, `✓ ${r.label}`)),
    keyed.map((b) => h("span", { class: "badge warn", title: b.reason }, `${b.label}: needs API key`)),
    boards.length === 4 ? h("span", { class: "badge", title: "Add companies in Settings" }, "Company boards: none listed") : null);
  const interval = h("select", { "aria-label": "How often", onchange: (e) => changeWatch(w.country, { every_hours: Number(e.target.value) }) },
    [1, 3, 6, 12, 24, 48].map((hours) => h("option", { value: hours, selected: hours * 60 === w.every_minutes }, every(hours * 60))));
  if (![1, 3, 6, 12, 24, 48].some((hrs) => hrs * 60 === w.every_minutes)) {
    interval.prepend(h("option", { value: w.every_minutes / 60, selected: true }, every(w.every_minutes)));
  }
  return h("div", { class: "card watch", dataset: { country: w.country } },
    h("div", { class: "watch-head" },
      h("div", {},
        h("div", { class: "watch-name" }, h("span", { class: "flag" }, flag(w.country)), w.name, " ",
          !w.enabled ? h("span", { class: "badge warn" }, "Paused") : null),
        h("div", { class: "watch-numbers" }, h("strong", {}, fmt(w.open)), " open jobs",
          w.new_this_week ? h("span", {}, " · ", h("span", { class: "badge new" }, `${fmt(w.new_this_week)} new this week`)) : null),
        h("div", { class: "muted" }, scheduleText(w))),
      h("button", { class: "btn small primary", type: "button", disabled: running, onclick: () => runNow(w.country) }, running ? "Checking…" : "Check now")),
    sources,
    !w.ready.length ? h("div", { class: "hint" }, "No source can check this country yet: add an API key or company boards in ", h("a", { href: "#settings" }, "Settings"), ".") : null,
    running ? liveBox(status.current) : null,
    h("div", { class: "watch-controls" },
      interval,
      h("button", { class: "btn small", type: "button", onclick: () => changeWatch(w.country, { enabled: !w.enabled }) }, w.enabled ? "Pause" : "Resume"),
      h("a", { class: "btn small ghost", href: `#jobs?country=${w.country}` }, "Browse jobs"),
      h("button", { class: "btn small ghost danger", type: "button", onclick: () => removeWatch(w) }, "Stop watching")));
}

function liveText(current) {
  const source = current.source ? `Reading ${current.source}` : "Starting";
  return `${source}: ${fmt(current.fetched)} jobs so far${current.new && !current.initial ? `, ${fmt(current.new)} new` : ""}`;
}

function liveBox(current) {
  return h("div", { class: "live" }, h("span", { class: "live-text" }, liveText(current)), h("div", { class: "progress" }, h("div", { class: "bar" })));
}

function updateLive(status) {
  if (!status.current) return;
  const card = document.querySelector(`.watch[data-country="${status.current.country}"] .live-text`);
  if (card) card.textContent = liveText(status.current);
}

function resolveTyped(value) {
  const text = value.trim().toLowerCase();
  if (!text || !state.countries) return null;
  const match = state.countries.all.find((c) => c.name.toLowerCase() === text || c.code.toLowerCase() === text);
  return match ? match.code : null;
}

async function showCountryHint() {
  const hint = $("#country-hint");
  const code = resolveTyped($("#country-input").value);
  if (!code) { hint.textContent = ""; return; }
  try {
    const sources = await api(`/api/countries/${code}/sources`);
    const ready = sources.filter((s) => s.ready);
    const keyed = sources.filter((s) => s.needs === "key");
    const parts = [];
    if (ready.length) parts.push(`Ready: ${ready.map((s) => s.label).join(", ")}.`);
    if (keyed.length) parts.push(`${keyed.map((s) => s.label).join(" and ")} need${keyed.length === 1 ? "s" : ""} a free API key (Settings).`);
    if (!ready.length) parts.push("Nothing can fetch this country yet: add an API key or company boards in Settings.");
    hint.textContent = `${flag(code)} ${countryName(code)} — ${parts.join(" ")}`;
  } catch { hint.textContent = ""; }
}

async function addCountry(event) {
  event.preventDefault();
  const code = resolveTyped($("#country-input").value);
  if (!code) { toast("Pick a country from the list", true); return; }
  try {
    await api("/api/watches", { json: { country: code, every_hours: Number($("#every-input").value) } });
    toast(`Watching ${countryName(code)} — the first check has started`);
    $("#country-input").value = "";
    $("#country-hint").textContent = "";
    await refreshStatus();
    loadCountries();
  } catch (err) { toast(err.message, true); }
}

async function changeWatch(country, changes) {
  try {
    await api(`/api/watches/${country}`, { json: changes, method: "PATCH" });
    loadCountries();
  } catch (err) { toast(err.message, true); }
}

async function removeWatch(w) {
  if (!confirm(`Stop watching ${w.name}? Its stored jobs stay searchable.`)) return;
  try {
    await api(`/api/watches/${w.country}`, { method: "DELETE" });
    toast(`Stopped watching ${w.name}`);
    loadCountries();
  } catch (err) { toast(err.message, true); }
}

async function runNow(country) {
  try {
    const result = await api(`/api/watches/${country}/run`, { method: "POST" });
    toast(result.state === "running" ? "Already checking it" : `Checking ${countryName(country)} now`);
    refreshStatus();
  } catch (err) { toast(err.message, true); }
}

/* ------------------------------------------------------------------ activity */

async function loadActivity() {
  let data;
  try { data = await api("/api/activity?limit=60"); } catch (err) { toast(err.message, true); return; }
  const KIND = { run: "ok", error: "bad", warning: "warn" };
  $("#events").replaceChildren(...(data.events.length ? data.events.map((e) => h("li", {},
    h("time", { datetime: e.at, title: new Date(e.at).toLocaleString() }, since(e.at)),
    h("span", {}, e.kind !== "run" && KIND[e.kind] ? h("span", { class: `badge ${KIND[e.kind]}` }, e.kind) : null, " ", e.message)))
    : [h("li", { class: "muted" }, "Nothing yet.")]));
  const STATUS = { ok: "ok", partial: "warn", error: "bad", running: "" };
  $("#crawls").replaceChildren(...(data.crawls.length ? data.crawls.map((c) => h("tr", { title: c.note || "" },
    h("td", {}, since(c.started_at)), h("td", {}, `${flag(c.country)} ${countryName(c.country)}`), h("td", {}, c.source),
    h("td", {}, c.mode), h("td", {}, h("span", { class: `badge ${STATUS[c.status] || ""}` }, c.status)),
    h("td", { class: "num" }, fmt(c.fetched)), h("td", { class: "num" }, fmt(c.new)), h("td", { class: "num" }, fmt(c.closed))))
    : [h("tr", {}, h("td", { colspan: 8, class: "muted" }, "No checks yet."))]));
}

/* ------------------------------------------------------------------ settings */

const KEY_LABELS = {
  ADZUNA_APP_ID: "Adzuna app ID", ADZUNA_APP_KEY: "Adzuna app key",
  USAJOBS_API_KEY: "USAJOBS API key", USAJOBS_EMAIL: "USAJOBS email (the one you registered with)",
};
const BOARD_LABELS = {
  greenhouse: ["Greenhouse", "boards.greenhouse.io/", "stripe"],
  lever: ["Lever", "jobs.lever.co/", "spotify"],
  ashby: ["Ashby", "jobs.ashbyhq.com/", "openai"],
  smartrecruiters: ["SmartRecruiters", "jobs.smartrecruiters.com/", "BoschGroup"],
};

async function saveSettings(patch, message = "Saved") {
  try {
    const data = await api("/api/settings", { json: patch, method: "PUT" });
    toast(message);
    renderSettings(data);
    state.countries = null;
  } catch (err) { toast(err.message, true); }
}

async function loadSettings() {
  try { renderSettings(await api("/api/settings")); } catch (err) { toast(err.message, true); }
}

function renderSettings(s) {
  const keyInputs = {};
  const keyFields = Object.entries(KEY_LABELS).map(([name, label]) => {
    const info = s.keys[name];
    const input = h("input", { id: `key-${name}`, type: name.endsWith("EMAIL") ? "email" : "password", autocomplete: "off",
      placeholder: info.set ? `${info.where === "app" ? "Saved" : "From the environment"} (${info.hint})` : "Not set" });
    keyInputs[name] = input;
    return h("div", { class: "field" }, h("label", { for: input.id }, label), input,
      info.where === "app" ? h("span", { class: "state" }, "Saved in the app. ",
        h("a", { href: "#settings", onclick: (e) => { e.preventDefault(); saveSettings({ keys: { [name]: null } }, "Key removed"); } }, "Remove")) : null);
  });
  const budget = h("input", { id: "adzuna-budget", type: "number", min: 1, value: s.adzuna_max_requests });
  const keysCard = h("form", { class: "card", onsubmit: (e) => {
    e.preventDefault();
    const keys = {};
    for (const [name, input] of Object.entries(keyInputs)) if (input.value.trim()) keys[name] = input.value.trim();
    saveSettings({ keys, adzuna_max_requests: Number(budget.value) || undefined }, "Keys saved");
  } },
  h("h2", {}, "API keys"),
  h("p", {}, "Sweden and Germany need no key. Adzuna covers 19 more countries (UK, US, Australia, New Zealand, Canada, India…) with a free key from ",
    h("a", { href: "https://developer.adzuna.com/", target: "_blank", rel: "noopener" }, "developer.adzuna.com"),
    "; USAJOBS lists US federal jobs (", h("a", { href: "https://developer.usajobs.gov/", target: "_blank", rel: "noopener" }, "developer.usajobs.gov"),
    "). Keys are stored in this app's database on this machine."),
  keyFields,
  h("div", { class: "field" }, h("label", { for: "adzuna-budget" }, "Adzuna requests per check (the free tier allows about 250 a day)"), budget),
  h("div", { class: "actions" }, h("button", { class: "btn primary", type: "submit" }, "Save keys")));

  const areas = {};
  const boards = Object.entries(BOARD_LABELS).map(([board, [label, prefix, example]]) => {
    const area = h("textarea", { id: `board-${board}`, rows: 3, placeholder: example });
    area.value = (s.companies[board] || []).join("\n");
    areas[board] = area;
    return h("div", { class: "field" }, h("label", { for: area.id }, label), area, h("span", { class: "state" }, `${prefix}`, h("b", {}, example)));
  });
  const boardsCard = h("form", { class: "card", onsubmit: (e) => {
    e.preventDefault();
    const companies = {};
    for (const [board, area] of Object.entries(areas)) companies[board] = area.value.split(/\n|,/).map((x) => x.trim()).filter(Boolean);
    saveSettings({ companies }, "Company boards saved");
  } },
  h("h2", {}, "Company career sites"),
  h("p", {}, "Add companies whose job boards the bot should read, for any country. Use the name from the board's web address, one per line. Add “=Name” to set how it's shown, e.g. “openai=OpenAI”."),
  h("div", { class: "boards" }, boards),
  h("div", { class: "actions" }, h("button", { class: "btn primary", type: "submit" }, "Save company boards")));

  const places = h("textarea", { id: "places", rows: 3, placeholder: "NZ: Hamilton, Nelson, Queenstown" });
  places.value = Object.entries(s.locations || {}).map(([code, words]) => `${code}: ${words.join(", ")}`).join("\n");
  const placesCard = h("form", { class: "card", onsubmit: (e) => {
    e.preventDefault();
    const locations = {};
    for (const line of places.value.split("\n")) {
      const [code, rest] = line.split(":");
      if (code && rest) locations[code.trim()] = rest.split(",").map((x) => x.trim()).filter(Boolean);
    }
    saveSettings({ locations }, "Place names saved");
  } },
  h("h2", {}, "Extra place names"),
  h("p", {}, "Company boards often give a city without the country. Add local towns here so their jobs are counted, one country per line."),
  h("div", { class: "field" }, places),
  h("div", { class: "actions" }, h("button", { class: "btn primary", type: "submit" }, "Save place names")));

  const hook = h("input", { id: "webhook", type: "url", placeholder: "https://hooks.slack.com/services/…", value: s.webhook_url || "" });
  const notifyCard = h("form", { class: "card", onsubmit: (e) => { e.preventDefault(); saveSettings({ webhook_url: hook.value.trim() }, "Webhook saved"); } },
    h("h2", {}, "Notifications"),
    h("p", {}, "Paste a Slack or Discord webhook URL and the bot posts new openings there after each check."),
    h("div", { class: "field" }, h("label", { for: "webhook" }, "Webhook URL"), hook),
    h("div", { class: "actions" },
      h("button", { class: "btn primary", type: "submit" }, "Save"),
      h("button", { class: "btn", type: "button", onclick: async () => {
        try { await api("/api/settings/test-webhook", { method: "POST" }); toast("Test message sent"); } catch (err) { toast(err.message, true); }
      } }, "Send a test message")));

  const sourcesCard = h("div", { class: "card" }, h("h2", {}, "Where jobs come from"),
    h("ul", {}, s.sources.map((src) => h("li", {}, h("b", {}, src.label), ` — ${src.coverage}`,
      src.countries && src.countries.length <= 3 ? ` (${src.countries.map(countryName).join(", ")})` : ""))));

  $("#view-settings").replaceChildren(h("div", { class: "settings" }, keysCard, boardsCard, placesCard, notifyCard, sourcesCard));
}

/* ---------------------------------------------------------------------- init */

async function init() {
  loadChat();
  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("#chat-input");
    const text = input.value;
    input.value = "";
    sendChat(text);
  });
  let debounce;
  $("#job-filters").addEventListener("input", (e) => {
    clearTimeout(debounce);
    debounce = setTimeout(() => loadJobs(true), e.target.name === "q" ? 350 : 0);
  });
  $("#job-filters").addEventListener("submit", (e) => { e.preventDefault(); loadJobs(true); });
  $("#more-jobs").addEventListener("click", () => { jobState.page += 1; loadJobs(false); });
  $("#add-country").addEventListener("submit", addCountry);
  $("#country-input").addEventListener("change", showCountryHint);
  $("#country-input").addEventListener("input", () => { if (resolveTyped($("#country-input").value)) showCountryHint(); });
  $("#pause-btn").addEventListener("click", () => botAction(state.status && state.status.paused ? "resume" : "pause"));
  $("#stop-btn").addEventListener("click", () => botAction("stop"));
  $("#job-dialog").addEventListener("click", (e) => { if (e.target.id === "job-dialog") e.target.close(); });
  window.addEventListener("hashchange", route);

  try {
    state.countries = await api("/api/countries");
    for (const c of state.countries.all) state.names[c.code] = c.name;
  } catch { /* the status line explains */ }
  await refreshStatus();
  route();
  setInterval(() => { if (document.visibilityState === "visible") refreshStatus(); }, 3000);
}

init();
