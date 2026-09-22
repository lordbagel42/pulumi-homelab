import { sessionAnalytics } from "./analytics.js?v=night-1";
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = {
  authenticated: false,
  overview: null,
  routes: [],
  directories: [],
  view: "overview",
  loading: false,
  modalSession: null,
  settingsLoaded: false,
  settingsDirty: false,
  connected: false,
  audioURL: null,
};
const viewNames = {
  overview: "Overview",
  attention: "Attention inbox",
  tasks: "Saved tasks",
  people: "Speed dial",
  sessions: "Sessions",
  integrations: "Integrations",
  speech: "Speech studio",
  routes: "Phone routes",
  directories: "Directories",
  settings: "Settings",
  api: "Developer API",
};
const terminalStates = new Set([
  "completed",
  "complete",
  "done",
  "cancelled",
  "canceled",
  "failed",
  "error",
  "ended",
  "closed",
  "expired",
]);
const waitingStates = new Set([
  "waiting",
  "waiting_for_input",
  "awaiting_input",
  "needs_input",
  "callback_pending",
  "attention",
  "question",
]);
const paths = {
  overview: ["M3 3h7v7H3z", "M14 3h7v7h-7z", "M3 14h7v7H3z", "M14 14h7v7h-7z"],
  sessions: [
    "M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5Z",
  ],
  integrations: [
    "M12 3v6m0 6v6M3 12h6m6 0h6",
    "M9 9h6v6H9z",
    "M4 3v3H1m19-3v3h3M4 21v-3H1m19 3v-3h3",
  ],
  speech: ["M4 10v4m4-8v12m4-16v20m4-17v14m4-10v6"],
  routes: ["M5 3v12a4 4 0 0 0 4 4h10", "m16 16 3 3-3 3M5 7h14m-3-3 3 3-3 3"],
  directories: ["M5 3h15v18H5zM2 7h5M2 12h5M2 17h5", "M11 8h5m-5 4h5m-5 4h3"],
  settings: [
    "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8",
    "M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z",
  ],
  api: ["m8 5-7 7 7 7m8-14 7 7-7 7m-3-17-2 20"],
  refresh: ["M20 7v5h-5M4 17v-5h5", "M6 6a8 8 0 0 1 13 3M5 15a8 8 0 0 0 13 3"],
  search: ["M10.5 3a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15m5.5 13 5 5"],
  close: ["m6 6 12 12M6 18 18 6"],
  phone: [
    "M5 3H3v4c0 7.7 6.3 14 14 14h4v-4l-5-2-2 2a12 12 0 0 1-7-7l2-2-2-5H5Z",
  ],
  operator: ["m9 5-6 7 6 7m6-14 6 7-6 7M13 3l-2 18"],
  activity: ["M2 12h4l3-8 6 16 3-8h4"],
  check: ["m5 12 4 4L19 6"],
};
function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "" && text != null) node.textContent = String(text);
  return node;
}
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [k, v] of Object.entries({
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "1.6",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
  }))
    svg.setAttribute(k, v);
  for (const d of paths[name] || paths.integrations) {
    const path = document.createElementNS(svg.namespaceURI, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}
$$("[data-icon]").forEach((node) =>
  node.replaceChildren(icon(node.dataset.icon)),
);
function text(value, fallback = "") {
  return value == null
    ? fallback
    : typeof value === "object"
      ? JSON.stringify(value)
      : String(value);
}
function slugLabel(value) {
  return text(value).replaceAll("_", " ").replaceAll("-", " ");
}
function label(value) {
  const string = slugLabel(value);
  return string ? string[0].toUpperCase() + string.slice(1) : "Unknown";
}
function timestamp(value) {
  if (!value) return null;
  const time = new Date(
    typeof value === "number" && value < 1e12 ? value * 1000 : value,
  );
  return Number.isNaN(time.getTime()) ? null : time;
}
function relative(value) {
  const date = timestamp(value);
  if (!date) return "—";
  const seconds = Math.max(0, (Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
function time(value) {
  return (
    timestamp(value)?.toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }) || "—"
  );
}
function active(session) {
  return !terminalStates.has(session.state);
}
function needsAttention(session) {
  return (
    waitingStates.has(session.state) ||
    /waiting|question|attention|pending/.test(session.state || "")
  );
}
function statusClass(value) {
  const lower = text(value).toLowerCase();
  if (
    /error|failed|offline|unavailable|missing|not.configured|needs.configuration/.test(
      lower,
    )
  )
    return "error";
  if (/wait|pending|question|attention|degraded|unknown/.test(lower))
    return "waiting";
  if (
    /disabled|idle|complete|done|cancel|ended|closed|expired|inactive|unconfigured/.test(
      lower,
    )
  )
    return "neutral";
  return "";
}
function badge(value) {
  return el("span", `badge ${statusClass(value)}`, label(value || "unknown"));
}
function button(text, style = "secondary", action) {
  const node = el("button", `button ${style}`, text);
  node.type = "button";
  if (action) node.addEventListener("click", action);
  return node;
}
function empty(title, description, glyph = "sessions") {
  const node = el("div", "empty-state");
  const mark = el("div", "empty-icon");
  mark.append(icon(glyph));
  node.append(mark, el("h3", "", title), el("p", "", description));
  return node;
}
function toast(message, error = false) {
  const node = el("div", `toast${error ? " error" : ""}`, message);
  $("#toasts").append(node);
  setTimeout(() => node.remove(), error ? 8000 : 4500);
}
function errorMessage(error) {
  return error?.message || "Something went wrong. Try again.";
}
async function api(path, options = {}) {
  const { raw = false, ...fetchOptions } = options;
  const headers = new Headers(fetchOptions.headers || {});
  if (fetchOptions.body != null && !(fetchOptions.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
    fetchOptions.body = JSON.stringify(fetchOptions.body);
  }
  const response = await fetch(`/api/v1${path}`, {
    ...fetchOptions,
    headers,
    credentials: "same-origin",
  });
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith("/auth/"))
      showLogin("Your session expired. Enter your access token to reconnect.");
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      message =
        typeof body.detail === "string"
          ? body.detail
          : typeof body.error === "string"
            ? body.error
            : message;
    } catch {}
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  if (raw) return response;
  if (response.status === 204) return null;
  return response.json();
}
async function submit(form, work) {
  if (form.dataset.busy) return;
  form.dataset.busy = "true";
  const submitButton = $("button[type=submit]", form);
  const buttons = $$("button", form).map(node => [node, node.disabled]);
  buttons.forEach(([node]) => { node.disabled = true; });
  const error = $(".form-error", form);
  if (error) error.textContent = "";
  if (submitButton) {
    submitButton.disabled = true;
    submitButton.dataset.originalText = submitButton.textContent;
    submitButton.textContent = "Working…";
  }
  try {
    await work();
  } catch (e) {
    if (error) error.textContent = errorMessage(e);
    else toast(errorMessage(e), true);
  } finally {
    delete form.dataset.busy;
    buttons.forEach(([node, disabled]) => { node.disabled = disabled; });
    if (submitButton) {
      submitButton.textContent = submitButton.dataset.originalText;
    }
  }
}
function showLogin(message = "") {
  state.authenticated = false;
  state.connected = false;
  state.overview = null;
  state.settingsLoaded = false;
  state.settingsDirty = false;
  state.modalSession = null;
  $("#app").hidden = true;
  $("#login-screen").hidden = false;
  $("#login-error").textContent = message;
  if ($("#modal").open) $("#modal").close();
  if (state.audioURL) {
    URL.revokeObjectURL(state.audioURL);
    state.audioURL = null;
  }
}
async function showApp() {
  state.authenticated = true;
  $("#login-screen").hidden = true;
  $("#app").hidden = false;
  $("#access-token").value = "";
  navigate();
  await refresh();
}
$("#login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  submit(event.currentTarget, async () => {
    await api("/auth/login", {
      method: "POST",
      body: { token: $("#access-token").value },
    });
    await showApp();
  });
});
$$("[data-action=logout]").forEach((node) =>
  node.addEventListener("click", async () => {
    try {
      await api("/auth/logout", { method: "POST", body: {} });
      showLogin();
    } catch (error) {
      toast(errorMessage(error), true);
    }
  }),
);
$("#refresh").addEventListener("click", () => refresh(true));
window.addEventListener("hashchange", navigate);
function navigate() {
  if (location.hash.startsWith("#login=")) {
    consumeLoginFragment();
    return;
  }
  const name = location.hash.slice(1).split("/")[0] || "overview";
  state.view = Object.hasOwn(viewNames, name) ? name : "overview";
  $$("[data-page]").forEach(
    (node) => (node.hidden = node.dataset.page !== state.view),
  );
  $$("[data-view]").forEach((node) => {
    const selected = node.dataset.view === state.view;
    node.classList.toggle("active", selected);
    if (selected) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  });
  $("#breadcrumb-page").textContent = viewNames[state.view];
  document.title = `${viewNames[state.view]} · Switchboard`;
  if (state.overview) render();
  const target = location.hash.split("/")[1];
  if (state.authenticated && name === "attention" && target && state.modalSession?.id !== decodeURIComponent(target)) openSession(decodeURIComponent(target));
}
function connection(connected, message = "") {
  state.connected = connected;
  $("#connection-banner").hidden = connected;
  $("#connection-banner").textContent =
    message ||
    "Switchboard is temporarily unreachable. Showing the last received data; reconnecting automatically.";
  $("#service-status").textContent = connected
    ? "Switchboard connected"
    : "Connection interrupted";
  $("#live-text").textContent = connected ? "LIVE" : "RECONNECTING";
  ["#connection-dot", "#service-dot", "#live-dot"].forEach((selector) =>
    $(selector).classList.toggle("error", !connected),
  );
}
async function refresh(manual = false) {
  if (!state.authenticated || state.loading) return;
  state.loading = true;
  try {
    const results = await Promise.allSettled([
      api("/overview"),
      api("/routes"),
      api("/directories"),
    ]);
    if (!state.authenticated) return;
    if (results[0].status === "rejected") throw results[0].reason;
    state.overview = results[0].value;
    if (results[1].status === "fulfilled")
      state.routes = results[1].value.routes || [];
    if (results[2].status === "fulfilled")
      state.directories = results[2].value.directories || [];
    connection(true);
    await refreshAttention();
    await refreshPeople();
    render();
    $("#last-update").textContent = `Updated ${time(Date.now())}`;
    if (manual) toast("Switchboard is up to date.");
    if (state.modalSession && $("#modal").open) await refreshSession();
  } catch (error) {
    if (state.authenticated) {
      connection(
        false,
        `Connection interrupted: ${errorMessage(error)}. Reconnecting automatically.`,
      );
      if (manual) toast(errorMessage(error), true);
    }
  } finally {
    state.loading = false;
  }
}
function replaceDynamic(container, ...children) {
  if (
    container.contains(document.activeElement) &&
    document.activeElement !== container
  )
    return;
  container.replaceChildren(...children);
}
let analyticsHours = 24;
let analyticsSignature = "";
$$("[data-range]").forEach(node => node.addEventListener("click", () => {
  analyticsHours = Number(node.dataset.range);
  $$("[data-range]").forEach(item => item.setAttribute("aria-pressed", String(item === node)));
  renderAnalytics(state.overview?.sessions || []);
}));
function svgNode(tag, attrs = {}, content = "") {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (content) node.textContent = content;
  return node;
}
function renderAnalytics(sessions) {
  const {bins, total, previous, missing} = sessionAnalytics(sessions, analyticsHours);
  const signature = JSON.stringify([analyticsHours, bins, sessions.map(s => s.state), missing]);
  if (signature === analyticsSignature) return;
  analyticsSignature = signature;
  $("#activity-total").textContent = total.toLocaleString();
  $("#activity-comparison").textContent = `${previous} in prior ${analyticsHours === 24 ? "24 hours" : analyticsHours === 168 ? "7 days" : "30 days"}`;
  const chart = $("#activity-chart");
  const svg = svgNode("svg", {viewBox:"0 0 680 205", role:"group", "aria-label":`Session starts: ${total} in selected window. ${missing} sessions without creation timestamps.`});
  const defs = svgNode("defs"), gradient = svgNode("linearGradient", {id:"signal-fill", x1:0,y1:0,x2:0,y2:1});
  gradient.append(svgNode("stop", {offset:"0%", "stop-color":"#8bbcff", "stop-opacity":".22"}), svgNode("stop", {offset:"100%", "stop-color":"#8bbcff", "stop-opacity":"0"}));
  defs.append(gradient);svg.append(defs);
  const maximum = Math.max(3,...bins.map(b=>b.count));
  for (let i=0;i<4;i++) {
    const y=15+i*48;
    svg.append(svgNode("line", {x1:32,x2:667,y1:y,y2:y,class:"chart-grid"}),svgNode("text", {x:0,y:y+4,class:"chart-axis"},String(Math.round(maximum*(3-i)/3))));
  }
  const point = (b,i) => [32+i*635/(bins.length-1),159-b.count/maximum*144];
  const points=bins.map(point), line=points.map((p,i)=>`${i ? "L" : "M"}${p[0]},${p[1]}`).join(" ");
  svg.append(svgNode("path", {d:`${line} L667,159 L32,159 Z`,class:"chart-area"}),svgNode("path",{d:line,class:"chart-line",pathLength:1}));
  const shortDate = value => new Date(value).toLocaleString(undefined,analyticsHours===24 ? {hour:"2-digit",hour12:false} : {month:"short",day:"numeric"});
  [0,Math.floor((bins.length-1)/3),Math.floor(2*(bins.length-1)/3),bins.length-1].forEach((i,j)=>svg.append(svgNode("text",{x:points[i][0],y:190,class:"chart-axis","text-anchor":j===0?"start":j===3?"end":"middle"},shortDate(bins[i].start))));
  const detail=$("#chart-detail");
  detail.textContent = total ? "Hover or focus a point to inspect activity" : "No session starts in this window";
  bins.forEach((bin,i)=> {
    const description=`${new Date(bin.start).toLocaleString()} – ${new Date(bin.end).toLocaleString()}: ${bin.count} session${bin.count===1?"":"s"}`;
    const hit=svgNode("rect",{x:Math.max(32,points[i][0]-635/(bins.length-1)/2),y:8,width:i===0||i===bins.length-1?635/(bins.length-1)/2:635/(bins.length-1),height:158,class:"chart-hit",tabindex:0,role:"img","aria-label":description});
    hit.append(svgNode("title",{},description));
    hit.addEventListener("pointerenter",()=>detail.textContent=description);
    hit.addEventListener("focus",()=>detail.textContent=description);
    hit.addEventListener("keydown",event=>{ if(event.key === "ArrowRight" || event.key === "ArrowLeft") {event.preventDefault();const hits=[...svg.querySelectorAll(".chart-hit")];hits[Math.max(0,Math.min(hits.length-1,i+(event.key==="ArrowRight"?1:-1)))].focus();} });
    svg.append(hit);
  });
  chart.replaceChildren(svg);chart.classList.add("animate");
  const waiting=sessions.filter(s=>active(s)&&needsAttention(s)).length;
  const open=sessions.filter(s=>active(s)&&!needsAttention(s)).length;
  const closed=sessions.length-open-waiting;
  const main=el("div","balance-main"),ring=el("div","balance-ring"),center=el("div");
  ring.style.setProperty("--open",`${sessions.length ? open/sessions.length*100 : 0}%`);
  ring.style.setProperty("--waiting",`${sessions.length ? (open+waiting)/sessions.length*100 : 0}%`);
  center.append(el("strong","",sessions.length),el("span","","SESSIONS"));ring.append(center);
  const legend=el("div","balance-legend");
  [["Open",open,""],["Waiting",waiting,"waiting"],["Closed",closed,"closed"]].forEach(([name,n,kind])=>{const row=el("div","balance-item");row.append(el("span",`legend-dot ${kind}`),el("span","",name),el("b","",n));legend.append(row);});
  main.append(ring,legend);
  const note=el("p","balance-note",`Based on ${sessions.length} available sessions. ${missing ? `${missing} without a creation time are excluded from the activity chart.` : "Activity uses session creation times; balance shows current states."}`);
  $("#session-balance").replaceChildren(main,note);
}
function render() {
  const overview = state.overview;
  if (!overview) return;
  const sessions = Array.isArray(overview.sessions)
    ? [...overview.sessions].sort(
        (a, b) =>
          (timestamp(b.updated)?.getTime() || 0) -
          (timestamp(a.updated)?.getTime() || 0),
      )
    : [];
  const integrations = Array.isArray(overview.integrations)
    ? overview.integrations
    : [];
  $("#workspace-name").textContent = (
    overview.settings?.name || "Home network"
  ).toUpperCase();
  $("#version").textContent = overview.service?.version
    ? `v${overview.service.version}`
    : "SWITCHBOARD";
  $("#session-nav-count").textContent = sessions.filter(active).length;
  const healthy = integrations.filter(
    (item) =>
      item.enabled &&
      !["error", "waiting", "neutral"].includes(statusClass(item.status)),
  ).length;
  const uptime = Number(overview.service?.uptime || 0);
  const stats = [
    {
      name: "Open sessions",
      value: sessions.filter(active).length,
      note: sessions.filter(needsAttention).length
        ? `${sessions.filter(needsAttention).length} waiting for you`
        : "All conversations in one place",
      icon: "sessions",
    },
    {
      name: "Services ready",
      value: healthy,
      note: `${integrations.length} integrations registered`,
      icon: "integrations",
    },
    {
      name: "Phone extensions",
      value: state.routes.filter((route) => route.enabled).length,
      note: "Enabled routes on your switchboard",
      icon: "phone",
    },
    {
      name: "Service uptime",
      value:
        uptime >= 86400
          ? Math.floor(uptime / 86400)
          : uptime >= 3600
            ? Math.floor(uptime / 3600)
            : Math.floor(uptime / 60),
      unit: uptime >= 86400 ? "days" : uptime >= 3600 ? "hours" : "min",
      note: "Keeping the connections together",
      icon: "activity",
    },
  ];
  $("#stats").replaceChildren(
    ...stats.map((stat) => {
      const card = el("div", "stat-card"),
        top = el("div", "stat-label", stat.name),
        value = el("div", "stat-value", stat.value);
      top.append(icon(stat.icon));
      if (stat.unit) value.append(el("span", "stat-unit", stat.unit));
      card.append(top, value, el("div", "stat-caption", stat.note));
      return card;
    }),
  );
  replaceDynamic(
    $("#overview-routes"),
    ...(state.routes.length
      ? state.routes.map((route) => {
          const chip = el(
            "button",
            `route-chip${route.enabled ? "" : " disabled"}`,
          );
          chip.type = "button";
          chip.append(
            el("code", "", route.extension),
            el("span", "", route.name),
          );
          chip.addEventListener("click", () => editRoute(route));
          return chip;
        })
      : [el("span", "subtle", "No phone routes registered yet.")]),
  );
  $("#overview-session-count").textContent = sessions.length;
  renderSessions(
    $("#overview-sessions"),
    sessions.slice(0, 5),
    "Your switchboard is quiet.",
    "Start an operator or register a session from a connected tool.",
  );
  renderAnalytics(sessions);
  renderAllSessions();
  $("#overview-connections").replaceChildren(
    ...(integrations.length
      ? integrations.slice(0, 6).map((item) => {
          const row = el("div", "connection-row"),
            details = el("div", "connection-text");
          details.append(
            el("div", "connection-name", item.name),
            el("div", "connection-detail", item.detail || label(item.kind)),
          );
          row.append(
            el("span", "connection-icon", integrationMark(item)),
            details,
            badge(item.enabled ? item.status : "disabled"),
          );
          return row;
        })
      : [
          empty(
            "No connections yet",
            "Registered services will appear here.",
            "integrations",
          ),
        ]),
  );
  const events = Array.isArray(overview.events)
    ? [...overview.events].sort(
        (a, b) =>
          (timestamp(b.created)?.getTime() || 0) -
          (timestamp(a.created)?.getTime() || 0),
      )
    : [];
  $("#overview-events").replaceChildren(
    ...(events.length
      ? events.slice(0, 7).map((event) => {
          const row = el("div", "event-row");
          row.append(
            el("time", "event-time", time(event.created)),
            el("span", "event-dot"),
            el("span", "event-message", event.message || label(event.type)),
            el("span", "event-type", slugLabel(event.type)),
          );
          return row;
        })
      : [
          empty(
            "A fresh start.",
            "Activity appears here as sessions and services connect.",
            "activity",
          ),
        ]),
  );
  renderIntegrations(integrations);
  renderRoutes();
  renderDirectories();
  updateProviderSelects();
  if (!state.settingsLoaded && !state.settingsDirty) {
    const settings = overview.settings || {};
    for (const key of ["name", "default_stt", "default_tts", "default_host"]) {
      const field = $(`[name="${key}"]`, $("#settings-form"));
      if (field) setFieldValue(field, settings[key] ?? "");
    }
    state.settingsLoaded = true;
  }
}
function integrationMark(item) {
  const id = (item.id || item.name || "").toLowerCase();
  if (id === "amp") return "A";
  if (id.includes("codex")) return ">_";
  if (id.includes("slack")) return "#";
  if (id.includes("home")) return "⌂";
  if (id.includes("eleven")) return "Ⅱ";
  if (id.includes("whisper")) return "w";
  if (id.includes("piper")) return "p";
  return (item.name || item.id || "·").slice(0, 2).toUpperCase();
}
function sessionEngine(session) {
  return session.engine === "amp" ? "Amp" : "Codex";
}
function sessionContext(session) {
  const parts = [sessionEngine(session)];
  if (session.integration && !["amp", "codex"].includes(session.integration))
    parts.push(providerName(session.integration));
  return parts.join(" · ");
}
function renderSessions(
  container,
  sessions,
  title = "No sessions found.",
  description = "Try a different filter, or create an operator session.",
) {
  replaceDynamic(
    container,
    ...(sessions.length
      ? sessions.map((session) => {
          const row = el("button", "session-row");
          row.type = "button";
          const mark = el(
            "span",
            `session-symbol ${session.kind === "operator" ? "operator" : ""}`,
          );
          mark.append(
            icon(session.kind === "operator" ? "operator" : "sessions"),
          );
          const details = el("span", "session-text");
          details.append(
            el("span", "session-title", session.title || "Untitled session"),
            el(
              "span",
              "session-meta",
              [
                sessionContext(session),
                session.extension ? `Ext. ${session.extension}` : null,
                session.executor || (session.host ? label(session.host) : null),
              ]
                .filter(Boolean)
                .join(" · "),
            ),
          );
          const end = el("span", "session-row-end");
          end.append(
            badge(session.state),
            el("span", "session-time", relative(session.updated)),
          );
          row.append(mark, details, end);
          row.addEventListener("click", () => openSession(session.id));
          return row;
        })
      : [empty(title, description)]),
  );
}
function renderAllSessions() {
  let sessions = state.overview?.sessions || [];
  const query = $("#session-search").value.trim().toLowerCase(),
    filter = $("#session-filter").value;
  if (query)
    sessions = sessions.filter((item) =>
      [item.title, item.id, item.engine, item.integration, item.extension, item.host, item.executor].some(
        (v) => text(v).toLowerCase().includes(query),
      ),
    );
  if (filter === "active") sessions = sessions.filter(active);
  if (filter === "waiting") sessions = sessions.filter(needsAttention);
  if (filter === "completed")
    sessions = sessions.filter((item) => !active(item));
  renderSessions($("#all-sessions"), sessions);
}
$("#session-search").addEventListener("input", renderAllSessions);
$("#session-filter").addEventListener("change", renderAllSessions);
function isSpeech(item) {
  return /stt|tts|speech|whisper|elevenlabs|piper/.test(
    `${item.kind} ${item.id}`,
  );
}
function providerItems(kind) {
  return (state.overview?.integrations || []).filter((item) =>
    kind === "stt"
      ? /stt|whisper/.test(`${item.kind} ${item.id}`)
      : /tts|piper/.test(`${item.kind} ${item.id}`),
  );
}
function renderIntegrations(items) {
  const card = (item) => {
    const node = el("article", "integration-card"),
      top = el("div", "integration-top");
    top.append(
      el(
        "span",
        `integration-logo${/amp|codex|eleven/.test(item.id) ? " orange" : ""}`,
        integrationMark(item),
      ),
      badge(item.enabled ? item.status : "disabled"),
    );
    const bottom = el("div", "integration-footer");
    bottom.append(
      el(
        "span",
        "",
        item.key_configured
          ? "Credentials saved"
          : item.enabled
            ? "Integration enabled"
            : "Integration disabled",
      ),
      button("Configure ↗", "quiet small", () => editIntegration(item)),
    );
    if (item.id === "slack-huddles")
      bottom.prepend(button("Open huddles", "secondary small", openHuddles));
    node.append(
      top,
      el("h2", "", item.name || label(item.id)),
      el("span", "integration-kind", label(item.kind || "integration")),
      el(
        "p",
        "integration-description",
        item.detail || "Configure this connection to use it with your phone.",
      ),
      bottom,
    );
    return node;
  };
  replaceDynamic(
    $("#integration-cards"),
    ...(items.length
      ? items.map(card)
      : [
          empty(
            "No integrations registered",
            "Your installed integrations will appear here.",
            "integrations",
          ),
        ]),
  );
  const speech = items.filter(isSpeech);
  replaceDynamic(
    $("#speech-cards"),
    ...(speech.length
      ? speech.map(card)
      : [
          empty(
            "No speech providers registered",
            "Install a speech connector to transcribe recordings or generate audio.",
            "speech",
          ),
        ]),
  );
}
function options(select, items, blankLabel = "Use default") {
  const value = select.value;
  const desired = [
    ["", blankLabel],
    ...items.map((item) => [item.id, item.name || label(item.id)]),
  ];
  const key = JSON.stringify(desired);
  if (select.dataset.options === key) return;
  select.replaceChildren(
    ...desired.map(([value, label]) => {
      const option = el("option", "", label);
      option.value = value;
      return option;
    }),
  );
  select.dataset.options = key;
  setFieldValue(select, value);
}
function setFieldValue(field, value) {
  const string = text(value);
  if (
    field.tagName === "SELECT" &&
    string &&
    ![...field.options].some((option) => option.value === string)
  ) {
    const option = el("option", "", label(string));
    option.value = string;
    field.append(option);
  }
  field.value = string;
}
function updateProviderSelects() {
  for (const selector of ["#stt-provider", "#setting-stt"])
    options(
      $(selector),
      providerItems("stt"),
      selector.includes("setting") ? "Select a provider" : "Workspace default",
    );
  for (const selector of ["#tts-provider", "#setting-tts"])
    options(
      $(selector),
      providerItems("tts"),
      selector.includes("setting") ? "Select a provider" : "Workspace default",
    );
}
function providerName(id) {
  if (!id) return "Default";
  return (
    state.overview?.integrations?.find((item) => item.id === id)?.name ||
    label(id)
  );
}
function renderRoutes() {
  replaceDynamic(
    $("#route-table-body"),
    ...state.routes.map((route) => {
      const row = el("tr");
      const extension = el("td");
      extension.append(el("span", "extension-number", route.extension));
      const name = el("td");
      name.append(
        el("span", "route-name", route.name),
        el("span", "route-integration", providerName(route.integration)),
      );
      const status = el("td");
      status.append(badge(route.enabled ? "enabled" : "disabled"));
      const actions = el("td");
      actions.append(button("Edit ↗", "quiet small", () => editRoute(route)));
      row.append(
        extension,
        name,
        el("td", "", providerName(route.stt)),
        el("td", "", providerName(route.tts)),
        status,
        actions,
      );
      return row;
    }),
  );
  $("#routes-empty").replaceChildren(
    ...(state.routes.length
      ? []
      : [
          empty(
            "No routes registered",
            "Phone connectors can register the extensions available on your system.",
            "routes",
          ),
        ]),
  );
}
function modal(title, kicker = "SWITCHBOARD", wide = false) {
  state.modalSession = null;
  const dialog = $("#modal");
  dialog.classList.toggle("session-dialog", wide);
  const header = el("div", "modal-header"),
    heading = el("div");
  heading.append(el("span", "eyebrow", kicker), el("h2", "", title));
  const close = el("button", "icon-button");
  close.type = "button";
  close.setAttribute("aria-label", "Close dialog");
  close.append(icon("close"));
  close.addEventListener("click", () => dialog.close());
  header.append(heading, close);
  const body = el("div", "modal-body");
  $("#modal-content").replaceChildren(header, body);
  dialog.setAttribute("aria-label", title);
  if (!dialog.open) dialog.showModal();
  return body;
}
$("#modal").addEventListener("close", () => {
  state.modalSession = null;
});
$("#modal").addEventListener("click", (event) => {
  if (event.target === $("#modal")) {
    const rect = event.target.getBoundingClientRect();
    if (
      event.clientX < rect.left ||
      event.clientX > rect.right ||
      event.clientY < rect.top ||
      event.clientY > rect.bottom
    )
      event.target.close();
  }
});
function field(
  form,
  name,
  labelText,
  {
    value = "",
    type = "text",
    placeholder = "",
    required = false,
    rows = 4,
    items = null,
    hint = null,
  } = {},
) {
  const wrapper = el("div", "config-field"),
    caption = el("label", "", labelText),
    id = `field-${name.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
  caption.htmlFor = id;
  let input;
  if (items) {
    input = el("select");
    for (const item of items) {
      const option = el("option", "", item.label);
      option.value = item.value;
      input.append(option);
    }
  } else if (type === "textarea") {
    input = el("textarea");
    input.rows = rows;
  } else {
    input = el("input");
    input.type = type;
  }
  input.id = id;
  input.name = name;
  input.required = required;
  input.placeholder = placeholder;
  if (type === "checkbox") {
    input.checked = Boolean(value);
  } else setFieldValue(input, value);
  wrapper.append(caption, input);
  if (hint) wrapper.append(el("p", "field-hint", hint));
  form.append(wrapper);
  return input;
}
function formEnd(form, saveLabel = "Save changes") {
  const error = el("p", "form-error");
  error.setAttribute("role", "alert");
  const actions = el("div", "modal-actions");
  const cancel = button("Cancel", "quiet", () => $("#modal").close()),
    save = button(saveLabel, "primary");
  save.type = "submit";
  actions.append(cancel, save);
  form.append(error, actions);
}
function toggle(form, name, text, checked) {
  const wrapper = el("label", "toggle-field"),
    input = el("input");
  input.type = "checkbox";
  input.name = name;
  input.checked = Boolean(checked);
  wrapper.append(el("span", "", text), input);
  form.append(wrapper);
  return input;
}
$$('[data-action="create-operator"]').forEach((node) =>
  node.addEventListener("click", createOperator),
);
function createOperator() {
  const body = modal("Give Amp a task.", "AMP OPERATOR");
  body.append(
    el(
      "p",
      "subtle",
      "Talk to Amp through your phone or this dashboard. Follow progress here and open the thread in Amp to review its work.",
    ),
  );
  const form = el("form");
  field(form, "title", "Session title", {
    required: true,
    placeholder: "A short name for this task",
  });
  field(form, "prompt", "What should the operator do?", {
    type: "textarea",
    rows: 5,
    required: true,
    placeholder: "Describe the task and any details it needs.",
  });
  field(form, "integration", "Operator type", {
    value: "amp",
    items: [
      { value: "amp", label: "General Amp operator" },
      { value: "slack-huddles", label: "Slack huddle operator" },
    ],
  });
  const configuredBackends = (state.overview?.integrations?.find(item => item.id === "amp")?.backends || []).filter(item => item.configured);
  const hostItems = configuredBackends.length ? configuredBackends.map(item => ({value:item.host, label:item.executor || label(item.host)})) : [
      { value: "proxmox", label: "Homelab Amp runner" },
      { value: "workstation", label: "Workstation" },
    ];
  const preferredHost = state.overview?.settings?.default_host || "proxmox";
  field(form, "host", "Run on", {
    value: hostItems.some(item => item.value === preferredHost) ? preferredHost : hostItems[0].value,
    items: hostItems,
  });
  formEnd(form, "Start operator ↗");
  body.append(form);
  const operatorRequestKey = newRequestKey();
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submit(form, async () => {
      const values = Object.fromEntries(new FormData(form));
      const result = await api("/operators", { method: "POST", headers: {"Idempotency-Key": operatorRequestKey}, body: {...values, engine: "amp"} });
      toast("Operator session started.");
      await refresh();
      await openSession(result.id || result.session?.id);
    });
  });
}
function editIntegration(item) {
  const body = modal(item.name || label(item.id), "CONFIGURE INTEGRATION");
  body.append(
    el(
      "p",
      "subtle",
      item.detail || "Manage the settings for this connection.",
    ),
  );
  const form = el("form");
  toggle(form, "enabled", "Integration enabled", item.enabled);
  const config = item.config || {},
    types = new Map();
  for (const [key, value] of Object.entries(config)) {
    if (/^(api_key|token|password|secret|authorization)$/i.test(key)) continue;
    const type =
      typeof value === "boolean"
        ? "boolean"
        : typeof value === "number"
          ? "number"
          : typeof value === "object" && value !== null
            ? "json"
            : "string";
    types.set(key, type);
    if (type === "boolean") toggle(form, `config.${key}`, label(key), value);
    else
      field(form, `config.${key}`, label(key), {
        value: type === "json" ? JSON.stringify(value, null, 2) : value,
        type:
          type === "json" ? "textarea" : type === "number" ? "number" : "text",
      });
  }
  if (/eleven/.test(item.id) && !Object.hasOwn(config, "model_id")) {
    types.set("model_id", "string");
    field(form, "config.model_id", "Model ID", {
      value: "scribe_v2",
      hint: "Choose the model supported by your ElevenLabs connection.",
    });
  }
  if (/eleven|slack/.test(item.id)) {
    const key = field(
      form,
      "api_key",
      item.key_configured ? "Replace API key" : "API key",
      {
        type: "password",
        placeholder: item.key_configured
          ? "Leave blank to keep the saved key"
          : "Enter a key if this service requires one",
        hint: "Keys are saved on the server and are never returned to the dashboard.",
      },
    );
    key.autocomplete = "new-password";
  }
  formEnd(form);
  body.append(form);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submit(form, async () => {
      const values = new FormData(form),
        next = { ...config };
      for (const [key, type] of types) {
        const value = values.get(`config.${key}`);
        if (type === "boolean") next[key] = values.has(`config.${key}`);
        else if (type === "number") {
          next[key] = Number(value);
          if (!Number.isFinite(next[key]))
            throw new Error(`${label(key)} must be a number.`);
        } else if (type === "json") {
          try {
            next[key] = JSON.parse(value);
          } catch {
            throw new Error(`${label(key)} must contain valid JSON.`);
          }
        } else next[key] = value;
      }
      const payload = { enabled: values.has("enabled"), config: next };
      if (values.get("api_key")?.trim())
        payload.api_key = values.get("api_key").trim();
      await api(`/integrations/${encodeURIComponent(item.id)}`, {
        method: "PUT",
        body: payload,
      });
      $("#modal").close();
      toast(`${item.name} settings saved.`);
      await refresh();
    });
  });
}
function requestId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const value = [...bytes]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  return `${value.slice(0, 8)}-${value.slice(8, 12)}-${value.slice(12, 16)}-${value.slice(16, 20)}-${value.slice(20)}`;
}
function openHuddles() {
  const body = modal("Put a huddle on your phone.", "SLACK HUDDLES");
  body.append(
    el(
      "p",
      "subtle",
      "Find a Slack contact, then start a huddle on your handset.",
    ),
  );
  const form = el("form"),
    input = field(form, "query", "Find a contact", {
      required: true,
      placeholder: "Name or Slack member ID",
    }),
    actions = el("div", "modal-actions"),
    search = button("Find contacts ↗", "primary");
  search.type = "submit";
  actions.append(search);
  form.append(el("p", "form-error"), actions);
  body.append(form);
  const results = el("div", "huddle-results"),
    current = el("div", "huddle-current");
  body.append(results, current);
  const command = (command, params) =>
    api("/integrations/slack-huddles/control", {
      method: "POST",
      body: { command, ...params },
    });
  const showRequest = (request, message) => {
    current.replaceChildren(
      el("span", "eyebrow", "HUDDLE REQUEST"),
      el("h3", "", request.name),
      el("p", "session-copy muted", message),
      el("code", "session-id", request.id),
    );
    const controls = el("div", "session-controls"),
      status = button("Check status", "secondary small", async () => {
        status.disabled = true;
        try {
          const result = await command("status", { request_id: request.id });
          showRequest(
            request,
            `Status: ${label(result.status || result.phase || result.state || "received")}`,
          );
        } catch (error) {
          toast(errorMessage(error), true);
        } finally {
          status.disabled = false;
        }
      }),
      cancel = button("Cancel huddle", "danger small", async () => {
        cancel.disabled = true;
        try {
          const result = await command("cancel", { request_id: request.id });
          showRequest(
            request,
            `Status: ${label(result.status || result.phase || result.state || "cancellation requested")}`,
          );
        } catch (error) {
          toast(errorMessage(error), true);
        } finally {
          cancel.disabled = false;
        }
      });
    controls.append(status, cancel);
    current.append(controls);
  };
  try {
    const previous = JSON.parse(
      sessionStorage.getItem("switchboard-last-huddle") || "null",
    );
    if (previous?.id)
      showRequest(
        previous,
        "Previous huddle request. Check its status before starting another call.",
      );
  } catch {}
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submit(form, async () => {
      const result = await command("lookup", { query: input.value.trim() });
      const users = result.users || [];
      results.replaceChildren(
        ...(users.length
          ? users.map((user) => {
              const row = el("div", "huddle-contact"),
                name = el("div");
              name.append(
                el("h3", "", user.name || user.id),
                el(
                  "p",
                  "subtle",
                  user.username ? `@${user.username}` : user.id,
                ),
              );
              const dial = button(
                "Start huddle ↗",
                "secondary small",
                async () => {
                  dial.disabled = true;
                  const request = {
                    id: requestId(),
                    name: user.name || user.id,
                  };
                  try {
                    sessionStorage.setItem(
                      "switchboard-last-huddle",
                      JSON.stringify(request),
                    );
                  } catch {}
                  showRequest(request, "Requesting a huddle…");
                  try {
                    const reply = await command("dial", {
                      user_id: user.id,
                      request_id: request.id,
                      mode: "ring",
                    });
                    showRequest(
                      request,
                      `Status: ${label(reply.status || reply.phase || reply.state || "requested")}. Pick up your handset when it rings.`,
                    );
                  } catch (error) {
                    showRequest(
                      request,
                      `Could not confirm the result: ${errorMessage(error)}. Check status before trying again.`,
                    );
                  }
                  dial.textContent = "Request sent";
                },
              );
              row.append(name, dial);
              return row;
            })
          : [
              empty(
                "No contacts found",
                "Try another name or paste a Slack member ID.",
                "search",
              ),
            ]),
      );
      if (result.more)
        results.append(
          el(
            "p",
            "field-hint",
            "More contacts matched. Refine the name to narrow the results.",
          ),
        );
    });
  });
}
function editRoute(route) {
  const body = modal(`Extension ${route.extension}`, "PHONE ROUTE"),
    form = el("form");
  toggle(form, "enabled", "Route enabled", route.enabled);
  field(form, "extension", "Dial number", {
    value: route.extension,
    required: true,
    hint: "Changing this adds a dial alias. The original service number remains available.",
  });
  field(form, "name", "Route name", { value: route.name, required: true });
  field(form, "integration", "Integration", {
    value: route.integration,
    items: [{value: route.integration, label: providerName(route.integration)}],
  });
  for (const kind of ["stt", "tts"])
    field(
      form,
      kind,
      kind === "stt" ? "Speech recognition" : "Speech synthesis",
      {
        value: route[kind] || "",
        items: [
          { value: "default", label: "Use workspace default" },
          ...providerItems(kind).map((item) => ({
            value: item.id,
            label: item.name,
          })),
        ],
      },
    );
  const supportsEngine = ["codex", "slack-operator"].includes(route.id);
  const profile = {...(route.speech_engine || {})};
  const textOptions = ["engine_id", "voice_id", "model_id", "language", "greeting", "personality"];
  const numberOptions = ["speed", "stability", "similarity_boost"];
  const advancedOptions = ["tts", "asr", "turn", "conversation"];
  if (supportsEngine) {
    form.append(el("h3", "", "Continuous speech"));
    field(form, "speech_mode", "Phone conversation", {
      value: route.speech_mode || "legacy",
      items: [{value:"legacy", label:"Standard speech recognition and playback"}, {value:"speech-engine", label:"ElevenLabs Speech Engine"}],
      hint: "Continuous speech supports natural turn-taking and interruptions. Configure its connection under Integrations first.",
    });
    field(form, "profile.voice_id", "ElevenLabs voice ID", {value: profile.voice_id || "", hint:"Leave blank to use the Speech Engine integration's voice."});
    field(form, "profile.greeting", "Greeting", {value: profile.greeting || "", type:"textarea", rows:2, hint:"What the owner hears when a new call begins."});
    field(form, "profile.personality", "Operator instructions", {value: profile.personality || "", type:"textarea", rows:3, hint:"How the operator should speak and handle tasks on this route."});
    const advanced = el("details");
    advanced.append(el("summary", "small-link", "Advanced voice and turn settings"));
    field(advanced, "profile.engine_id", "Speech Engine ID", {value: profile.engine_id || "", hint:"Created by Provision below, or paste this route's existing seng_ resource ID."});
    field(advanced, "profile.model_id", "Voice model", {value: profile.model_id || "", placeholder:"Use integration default"});
    field(advanced, "profile.language", "Language", {value: profile.language || "", placeholder:"Use integration default"});
    for (const [name, title, minimum, maximum] of [["speed","Voice speed",.7,1.2],["stability","Voice stability",0,1],["similarity_boost","Voice similarity",0,1]]) {
      const input = field(advanced, `profile.${name}`, title, {type:"number",value:profile[name] ?? "",hint:`Optional, ${minimum}–${maximum}.`});
      input.min = minimum; input.max = maximum; input.step = .05;
    }
    for (const section of advancedOptions)
      field(advanced, `profile.${section}`, `${section.toUpperCase()} options (JSON)`, {type:"textarea",rows:2,value:JSON.stringify(profile[section] || {}, null, 2)});
    form.append(advanced);
    const speechActions = el("div", "modal-actions");
    speechActions.append(button("Save and provision engine", "secondary", () => submit(form, async () => {
      const saved = await saveRoute(true);
      Object.assign(route, saved);
      const result = await api(`/speech/engine/provision/${encodeURIComponent(route.id)}`, {method:"POST",body:{}});
      profile.engine_id = result.engine_id;
      form.elements["profile.engine_id"].value = result.engine_id;
      toast("Speech Engine configured. Choose continuous speech and save the route to use it.");
      await refresh();
    })), button("Preview saved voice", "quiet", () => submit(form, async () => {
      await saveRoute();
      const response = await api(`/speech/engine/preview/${encodeURIComponent(route.id)}`, {method:"POST",body:{text:form.elements["profile.greeting"].value.trim() || "Hello. Your Amp operator is ready."},raw:true});
      if (state.audioURL) URL.revokeObjectURL(state.audioURL);
      state.audioURL = URL.createObjectURL(await response.blob());
      voicePreview.src = state.audioURL;
      voicePreview.hidden = false;
      await voicePreview.play().catch(() => {});
    })));
    const voicePreview = el("audio");
    voicePreview.controls = true;
    voicePreview.hidden = true;
    form.append(speechActions, voicePreview);
  }
  async function saveRoute(provisioning = false) {
    const values = Object.fromEntries(new FormData(form));
    values.enabled = form.elements.enabled.checked;
    values.stt = values.stt || "default";
    values.tts = values.tts || "default";
    values.speech_engine = {...profile};
    if (supportsEngine) {
      for (const key of textOptions) {
        values.speech_engine[key] = values[`profile.${key}`].trim();
        delete values[`profile.${key}`];
      }
      for (const key of numberOptions) {
        const input = values[`profile.${key}`];
        values.speech_engine[key] = input === "" ? null : Number(input);
        delete values[`profile.${key}`];
      }
      for (const key of advancedOptions) {
        try { values.speech_engine[key] = JSON.parse(values[`profile.${key}`] || "{}"); }
        catch { throw new Error(`${key.toUpperCase()} options must contain valid JSON.`); }
        delete values[`profile.${key}`];
      }
      if (provisioning && !values.speech_engine.engine_id) values.speech_mode = "legacy";
    } else values.speech_mode = route.speech_mode || "legacy";
    return api(`/routes/${encodeURIComponent(route.id)}`, {method:"PUT",body:values});
  }
  formEnd(form, "Save route ↗");
  body.append(form);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submit(form, async () => {
      await saveRoute();
      $("#modal").close();
      toast("Phone route saved.");
      await refresh();
    });
  });
}
$("#settings-form").addEventListener(
  "input",
  () => (state.settingsDirty = true),
);
$("#settings-form").addEventListener(
  "change",
  () => (state.settingsDirty = true),
);
$("#settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  submit(form, async () => {
    await api("/settings", {
      method: "PATCH",
      body: Object.fromEntries(new FormData(form)),
    });
    state.settingsDirty = false;
    state.settingsLoaded = false;
    toast("Workspace settings saved.");
    await refresh();
  });
});
$("#transcribe-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  submit(form, async () => {
    const data = new FormData(form);
    if (!data.get("provider")) data.delete("provider");
    if (!data.get("language")) data.delete("language");
    const result = await api("/speech/transcribe", {
      method: "POST",
      body: data,
    });
    $("#transcript-result").hidden = false;
    $("#transcript-result p").textContent =
      result.text || "(No speech was detected.)";
  });
});
$("#synthesize-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  submit(form, async () => {
    const values = Object.fromEntries(new FormData(form));
    if (!values.provider) delete values.provider;
    const response = await api("/speech/synthesize", {
      method: "POST",
      body: values,
      raw: true,
    });
    const blob = await response.blob();
    if (state.audioURL) URL.revokeObjectURL(state.audioURL);
    state.audioURL = URL.createObjectURL(blob);
    $("#audio-result").hidden = false;
    $("#audio-result audio").src = state.audioURL;
    const link = $("#audio-result a");
    link.href = state.audioURL;
    link.download = `switchboard-speech.${blob.type.includes("mpeg") ? "mp3" : "wav"}`;
  });
});
async function openSession(id) {
  if (!id) {
    toast("The server did not return a session ID.", true);
    return;
  }
  const body = modal("Loading session…", "SESSION", true);
  body.append(
    el("div", "inline-loading", "Getting the latest session details…"),
  );
  state.modalSession = { id, data: null, questionKey: null };
  try {
    const result = await api(`/sessions/${encodeURIComponent(id)}`);
    if (state.modalSession?.id !== id) return;
    buildSession(result);
  } catch (error) {
    if (state.modalSession?.id === id) {
      body.replaceChildren(el("p", "form-error", errorMessage(error)));
    }
  }
}
function buildSession(session) {
  const modalState = state.modalSession;
  const body = $(".modal-body", $("#modal"));
  body.replaceChildren();
  $(".modal-header h2", $("#modal")).textContent =
    session.title || "Session details";
  const summary = el("div", "session-summary");
  summary.id = "detail-summary";
  const progress = el("div", "session-detail-section");
  progress.append(el("span", "eyebrow", "CURRENT ACTIVITY"));
  const progressText = el("p", "session-copy muted");
  progressText.id = "detail-progress";
  progress.append(progressText);
  const reply = el("div", "session-detail-section");
  reply.append(el("span", "eyebrow", "LATEST RESPONSE"));
  const replyText = el("div", "session-reply");
  replyText.id = "detail-reply";
  reply.append(replyText);
  const questions = el("div");
  questions.id = "detail-questions";
  const controls = el("div", "session-controls");
  const nativeThread = el("a", "button secondary", "Open thread in Amp ↗");
  nativeThread.id = "detail-native-thread";
  nativeThread.target = "_blank";
  nativeThread.rel = "noopener noreferrer";
  nativeThread.hidden = true;
  const callbackButton = button("Request phone input ↗", "secondary", () => {
    const callback = $("#detail-callback");
    callback.hidden = !callback.hidden;
    if (!callback.hidden) $("textarea", callback).focus();
  });
  const cancelButton = button("Cancel session", "danger", async () => {
    cancelButton.disabled = true;
    try {
      await api(`/sessions/${encodeURIComponent(session.id)}/cancel`, {
        method: "POST",
        body: {},
      });
      toast("Session cancellation requested.");
      await refreshSession();
      await refresh();
    } catch (error) {
      toast(errorMessage(error), true);
    } finally {
      cancelButton.disabled = false;
    }
  });
  cancelButton.id = "detail-cancel";
  controls.append(nativeThread, callbackButton, cancelButton);
  const callbackForm = el("form", "callback-form");
  callbackForm.id = "detail-callback";
  callbackForm.hidden = true;
  field(callbackForm, "question", "Ask something on the phone", {
    type: "textarea",
    rows: 3,
    placeholder:
      "What would you like to ask? Leave blank to use the session context.",
  });
  const callbackSubmit = button("Request callback ↗", "primary");
  callbackSubmit.type = "submit";
  callbackForm.append(callbackSubmit, el("p", "form-error"));
  callbackForm.addEventListener("submit", (event) => {
    event.preventDefault();
    submit(callbackForm, async () => {
      const question = callbackForm.elements.question.value.trim();
      await api(`/sessions/${encodeURIComponent(session.id)}/callback`, {
        method: "POST",
        body: { ...(question ? { question } : {}), options: [] },
      });
      callbackForm.hidden = true;
      toast("Phone input requested.");
      await refreshSession();
    });
  });
  const inputForm = el("form", "session-input");
  inputForm.id = "detail-input-form";
  field(inputForm, "text", "Continue the conversation", {
    type: "textarea",
    rows: 3,
    required: true,
    placeholder: "Send another instruction or add some context…",
  });
  const actions = el("div", "modal-actions"),
    send = button("Send input ↗", "primary");
  send.type = "submit";
  actions.append(send);
  inputForm.append(el("p", "form-error"), actions);
  inputForm.addEventListener("submit", (event) => {
    event.preventDefault();
    submit(inputForm, async () => {
      await api(`/sessions/${encodeURIComponent(session.id)}/input`, {
        method: "POST",
        body: { text: inputForm.elements.text.value },
      });
      inputForm.elements.text.value = "";
      toast("Input sent to the session.");
      await refreshSession();
    });
  });
  body.append(
    summary,
    progress,
    reply,
    questions,
    controls,
    callbackForm,
    inputForm,
    el("div", "session-id", `SESSION / ${session.id}`),
  );
  state.modalSession = modalState;
  updateSession(session);
}
async function refreshSession() {
  const id = state.modalSession?.id;
  if (!id) return;
  try {
    const result = await api(`/sessions/${encodeURIComponent(id)}`);
    if (state.modalSession?.id === id) updateSession(result);
  } catch (error) {
    if (state.modalSession?.id === id)
      toast(`Could not refresh this session: ${errorMessage(error)}`, true);
  }
}
function updateSession(session) {
  if (!state.modalSession || !$("#detail-summary")) return;
  state.modalSession.data = session;
  $(".modal-header h2", $("#modal")).textContent =
    session.title || "Session details";
  $("#detail-summary").replaceChildren(
    badge(session.state),
    ...[
      [sessionContext(session), ""],
      [session.extension, "Ext. "],
      [session.executor || session.host, ""],
    ]
      .filter(([value]) => value)
      .map(([value, prefix]) =>
        el("span", "session-meta-item", prefix + value),
      ),
    el("span", "", `Updated ${relative(session.updated)}`),
  );
  const threadLink = $("#detail-native-thread");
  threadLink.hidden = true;
  threadLink.removeAttribute("href");
  if (session.engine === "amp" && session.thread_url) {
    try {
      const url = new URL(session.thread_url);
      if (url.origin === "https://ampcode.com" && url.pathname.startsWith("/threads/") && !url.username && !url.password) {
        threadLink.href = url.href;
        threadLink.hidden = false;
      }
    } catch { /* A malformed link must not prevent session interaction. */ }
  }
  $("#detail-progress").textContent = text(
    session.progress,
    active(session)
      ? "Waiting for the next update."
      : "This session has ended.",
  );
  $("#detail-reply").textContent = text(
    session.last_reply,
    "No response yet. Replies will appear here.",
  );
  $("#detail-cancel").hidden = !active(session);
  const questions = (session.questions || []).filter(
    (question) =>
      !["answered", "cancelled", "canceled", "closed", "expired"].includes(
        question.state,
      ),
  );
  const questionKey = JSON.stringify(
    questions.map((question) => [
      question.id,
      question.state,
      question.questions,
    ]),
  );
  if (questionKey !== state.modalSession.questionKey) {
    state.modalSession.questionKey = questionKey;
    $("#detail-questions").replaceChildren(...questions.map(renderQuestion));
  }
}
function renderQuestion(question) {
  const card = el("section", "question-card");
  card.append(el("span", "eyebrow", "YOUR INPUT IS NEEDED"));
  for (const item of question.questions || [
    { id: question.id, question: question.question || "Your response" },
  ]) {
    card.append(el("h3", "", item.question));
    const choices = el("div", "question-options");
    for (const option of item.options || []) {
      const choice = button(option.label || option, "secondary small", () =>
        answerQuestion(question.id, item.id, option.label || option).catch(
          () => {},
        ),
      );
      choices.append(choice);
    }
    if (choices.children.length) card.append(choices);
    const form = el("form", "answer-form"),
      input = el("input");
    input.type = "text";
    input.name = "text";
    input.placeholder = "Or type a response…";
    input.required = true;
    input.setAttribute("aria-label", `Answer: ${item.question}`);
    const send = button("Send answer ↗", "secondary small");
    send.type = "submit";
    form.append(input, send, el("p", "form-error"));
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      submit(form, () => answerQuestion(question.id, item.id, input.value));
    });
    card.append(form);
  }
  return card;
}
async function answerQuestion(id, itemId, answer) {
  const activeCard = document.activeElement?.closest("#attention-cards .attention-card");
  // Let the owner move on immediately. Restore the card if delivery fails.
  if (activeCard) activeCard.hidden = true;
  try {
    await api(`/questions/${encodeURIComponent(id)}/answer`, {
      method: "POST",
      body: { item_id: itemId, text: answer },
    });
    if (activeCard) activeCard.remove();
    toast("Answer delivered.");
    await refreshSession();
    await refreshAttention();
  } catch (error) {
    if (activeCard) activeCard.hidden = false;
    toast(errorMessage(error), true);
    throw error;
  }
}
function renderDirectories() {
  replaceDynamic(
    $("#directory-cards"),
    ...(state.directories.length
      ? state.directories.map((directory) => {
          const card = el("article", "panel directory-card"),
            heading = el("div", "panel-heading"),
            title = el("div");
          title.append(
            el("span", "eyebrow", providerName(directory.integration)),
            el("h2", "", directory.name),
          );
          heading.append(
            title,
            badge(directory.enabled ? "enabled" : "disabled"),
          );
          card.append(heading);
          const entries = directory.entries || [];
          const list = el("div", "directory-entries");
          for (const entry of entries.slice(0, 5)) {
            const row = el("div", "directory-entry"),
              name = el("div");
            name.append(el("span", "directory-entry-name", entry.name));
            if (entry.description)
              name.append(
                el("span", "directory-entry-description", entry.description),
              );
            row.append(name, el("code", "", entry.number));
            list.append(row);
          }
          if (!entries.length)
            list.append(
              empty(
                "Nothing to dial yet",
                "Entries appear when the connected app publishes them.",
                "directories",
              ),
            );
          card.append(list);
          const footer = el("div", "directory-footer");
          footer.append(
            el(
              "span",
              "subtle",
              `${entries.length} ${entries.length === 1 ? "entry" : "entries"}`,
            ),
            button("Manage directory ↗", "quiet small", () =>
              editDirectory(directory),
            ),
          );
          card.append(footer);
          return card;
        })
      : [
          empty(
            "Your phone book starts here",
            "Register a directory or connect an app that publishes one.",
            "directories",
          ),
        ]),
  );
}
$$('[data-action="create-directory"]').forEach((node) =>
  node.addEventListener("click", () => editDirectory(null)),
);
function editDirectory(directory) {
  const creating = !directory,
    body = modal(
      creating ? "Add an app directory." : directory.name,
      "PHONE DIRECTORY",
    ),
    form = el("form");
  body.append(
    el(
      "p",
      "subtle",
      creating
        ? "Let an app publish dialable contacts to the phone’s Directories menu."
        : "Directory entries are the names and numbers shown on your phone. App-managed directories may update automatically.",
    ),
  );
  toggle(form, "enabled", "Show on the phone", directory?.enabled ?? true);
  if (creating)
    field(form, "id", "Directory ID", {
      required: true,
      placeholder: "my-app-contacts",
      hint: "A stable, unique identifier for the app to use.",
    });
  field(form, "name", "Directory name", {
    value: directory?.name || "",
    required: true,
    placeholder: "My app contacts",
  });
  if (creating)
    field(form, "integration", "Integration", {
      items: [
        { value: "custom", label: "Custom app" },
        ...(state.overview?.integrations || []).map((item) => ({
          value: item.id,
          label: item.name,
        })),
      ],
    });
  if (directory?.builtin)
    form.append(
      el(
        "p",
        "notice",
        "Entries are maintained by the connected app and update automatically.",
      ),
    );
  if (!creating && !directory.builtin)
    field(form, "entries", "Directory entries", {
      type: "textarea",
      rows: 9,
      value: JSON.stringify(directory.entries || [], null, 2),
      hint: "JSON array. Each entry needs id, name, and number. Description is optional.",
    });
  formEnd(form, creating ? "Create directory ↗" : "Save directory ↗");
  body.append(form);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submit(form, async () => {
      const values = new FormData(form),
        payload = { name: values.get("name"), enabled: values.has("enabled") };
      if (creating) {
        payload.id = values.get("id");
        payload.integration = values.get("integration");
        await api("/directories", { method: "POST", body: payload });
      } else {
        if (!directory.builtin) {
          let entries;
          try {
            entries = JSON.parse(values.get("entries"));
          } catch {
            throw new Error("Directory entries must contain valid JSON.");
          }
          if (
            !Array.isArray(entries) ||
            entries.some(
              (entry) =>
                !entry ||
                typeof entry !== "object" ||
                !entry.id ||
                !entry.name ||
                !entry.number,
            )
          )
            throw new Error("Each entry needs an id, name, and number.");
          await api(
            `/directories/${encodeURIComponent(directory.id)}/entries`,
            { method: "PUT", body: { entries } },
          );
        }
        await api(`/directories/${encodeURIComponent(directory.id)}`, {
          method: "PUT",
          body: payload,
        });
      }
      $("#modal").close();
      toast(creating ? "Directory created." : "Directory saved.");
      await refresh();
    });
  });
}
const base = `${location.origin}/api/v1`;
$("#api-base").textContent = base;
const curl = (path, body, method = "POST") =>
  `curl -X ${method} '${base}${path}' \\\n  -H "Authorization: Bearer $SWITCHBOARD_TOKEN" \\\n  -H 'Content-Type: application/json' \\\n  -d '${JSON.stringify(body, null, 2)}'`;
$("#operator-example").textContent = curl("/operators", {
  title: "My home operator",
  host: "proxmox",
  prompt: "Help me plan the next home automation.",
});
$("#input-example").textContent = curl("/sessions/SESSION_ID/input", {
  text: "Start with the living room lights.",
});
$("#callback-example").textContent = curl("/sessions/SESSION_ID/callback", {
  question: "Which room should we set up first?",
  options: ["Living room", "Kitchen"],
});
$("#register-example").textContent = curl("/sessions", {
  title: "My connected tool",
  integration: "my-tool",
  external_key: "my-stable-session-id",
  metadata: { source: "my-app" },
});
$("#directory-example").textContent =
  curl("/directories", {
    id: "my-app-contacts",
    name: "My app contacts",
    integration: "my-tool",
    enabled: true,
  }) +
  `\n\n` +
  curl(
    "/directories/my-app-contacts/entries",
    { entries: [{ id: "front-desk", name: "Front desk", number: "100" }] },
    "PUT",
  );
$$("[data-copy]").forEach((node) =>
  node.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(
        $(`#${node.dataset.copy}`).textContent,
      );
      toast("Example copied.");
    } catch {
      const range = document.createRange();
      range.selectNodeContents($(`#${node.dataset.copy}`));
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      toast("Example selected. Press Ctrl+C or ⌘C to copy.");
    }
  }),
);
function updateClock() {
  $("#clock").textContent =
    new Date()
      .toLocaleDateString(undefined, { month: "short", day: "numeric" })
      .toUpperCase() +
    " / " +
    new Date().toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
}
updateClock();
setInterval(updateClock, 30000);
setInterval(() => {
  refresh();
}, 5000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
async function consumeLoginFragment() {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const token = fragment.get("login");
  const destination = Object.hasOwn(viewNames, fragment.get("view")) ? fragment.get("view") : "overview";
  history.replaceState(
    null,
    "",
    location.pathname + location.search + "#" + destination,
  );
  try {
    await api("/auth/login", { method: "POST", body: { token } });
    await showApp();
  } catch (error) {
    showLogin(errorMessage(error));
  }
}
async function bootstrap() {
  if (location.hash.startsWith("#login=")) {
    await consumeLoginFragment();
  } else {
    try {
      await api("/auth/session");
      await showApp();
    } catch (error) {
      showLogin(error.status === 401 ? "" : errorMessage(error));
    }
  }
}
let favoriteData = {entries:[], revision:0};
async function refreshPeople() {
  try {
    favoriteData = await api("/favorites");
    renderPeople();
  } catch(error) { $("#favorite-status").textContent = errorMessage(error); }
}
function renderPeople() {
  const query = $("#favorite-search").value.toLowerCase();
  const entries = favoriteData.entries.filter(person => `${person.name} ${person.number} ${person.description}`.toLowerCase().includes(query));
  $("#favorite-status").textContent = favoriteData.entries.length ? `${favoriteData.entries.length} favorite people · reorder to match your phone directory` : "Start with the people you call most. Add a name and extension, or choose a recent huddle below.";
  replaceDynamic($("#favorite-cards"), ...entries.map(person => {
    const index = favoriteData.entries.findIndex(entry => entry.id === person.id);
    const card = el("article","panel attention-card");
    card.append(el("span","eyebrow",`FAVORITE / ${String(index+1).padStart(2,"0")}`),el("h2","",person.name),el("code","favorite-number",person.number));
    if (person.description) card.append(el("p","subtle",person.description));
    const actions = el("div","session-controls");
    actions.append(button("Edit","secondary",() => editFavorite(person)));
    for (const [offset,labelText] of [[-1,"Move up"],[1,"Move down"]]) {
      const move = button(labelText,"quiet small",async () => {
        move.disabled = true;
        const reordered = [...favoriteData.entries];
        [reordered[index],reordered[index+offset]] = [reordered[index+offset],reordered[index]];
        try { await savePeople(reordered); } catch(error) { toast(errorMessage(error),true); await refreshPeople(); }
      });
      move.disabled = index+offset < 0 || index+offset >= favoriteData.entries.length;
      actions.append(move);
    }
    card.append(actions); return card;
  }));
  if (query && !entries.length) $("#favorite-cards").append(el("p","subtle","No matching person. Try a name or extension."));
  const recent = state.directories.find(directory => directory.id === "slack-recent")?.entries || [];
  replaceDynamic($("#recent-people"), ...recent.map(person => {
    const card = el("article","panel attention-card"), saved = favoriteData.entries.some(entry=>entry.number===person.number);
    card.append(el("h3","",person.name),el("code","favorite-number",person.number));
    const add = button(saved ? "In favorites" : "Add to favorites ↗","secondary",() => editFavorite({...person,id:null}));
    add.disabled = saved; card.append(add); return card;
  }));
  if (!recent.length) $("#recent-people").append(el("p","subtle","People appear here after a huddle connects."));
}
async function savePeople(entries) {
  favoriteData = await api("/favorites", {method:"PUT",body:{entries,revision:favoriteData.revision}});
  // Reordering should be visible even while its button has focus.
  $("#favorite-cards").replaceChildren();
  renderPeople();
}
$("#favorite-search").addEventListener("input",renderPeople);
$("#new-favorite").addEventListener("click",() => editFavorite());
function editFavorite(person = {}) {
  const body = modal(person.id ? "Edit favorite." : "Keep someone close.","FAVORITE PERSON"), form = el("form");
  field(form,"name","Name",{value:person.name || "",required:true});
  field(form,"number","Phone number or extension",{value:person.number || "",required:true,hint:"Digits only. Use a number your phone system can already dial."});
  field(form,"description","A little context (optional)",{value:person.description || "",placeholder:"Team, location, or when to call"});
  formEnd(form,person.id ? "Save person" : "Add to favorites");
  if (person.id) form.append(button("Remove from favorites","danger",async () => {
    try { await savePeople(favoriteData.entries.filter(entry=>entry.id!==person.id)); $("#modal").close(); } catch(error) { toast(errorMessage(error),true); }
  }));
  body.append(form);
  form.addEventListener("submit",event => {event.preventDefault();submit(form,async () => {
    const values = {...Object.fromEntries(new FormData(form)),id:person.id || newRequestKey()};
    const entries = person.id ? favoriteData.entries.map(entry=>entry.id===person.id ? values : entry) : [...favoriteData.entries,values];
    await savePeople(entries); $("#modal").close(); toast("Favorite people saved.");
  });});
}
// Attention views reuse the same session and answer controls as the rest of the app.
let attentionData = null;
let notificationSeen = new Set();
try { notificationSeen = new Set(JSON.parse(localStorage.getItem("switchboard-notification-seen") || "[]")); } catch {}
async function refreshAttention() {
  const results = await Promise.allSettled([api("/attention"), api("/callbacks"), api("/task-templates")]);
  if (results[0].status === "fulfilled") {
    attentionData = results[0].value;
    $("#attention-status").textContent = attentionData.items.length ? `${attentionData.items.length} sessions need your attention` : "All caught up. New questions will appear here.";
    const quiet = attentionData.quiet_hours;
    $("#quiet-status").textContent = quiet.enabled ? `${quiet.active ? "Quiet hours active" : "Quiet hours scheduled"} · ${quiet.start}–${quiet.end} · ${quiet.timezone}` : "Quiet hours off · callbacks can ring anytime";
    replaceDynamic($("#attention-cards"), ...attentionData.items.map(item => {
      const card = el("article", "panel attention-card");
      card.append(badge(item.state), el("h2", "", item.title), el("p", "subtle", item.progress || item.last_reply || "Your input is needed."));
      for (const question of item.questions) card.append(renderQuestion(question));
      const actions = el("div", "session-controls");
      actions.append(button("Open session ↗", "secondary", () => openSession(item.id)), button(`Call me · ${item.extension}`, "quiet", async () => {
        try { await api(`/sessions/${encodeURIComponent(item.id)}/callback`, {method:"POST", headers:{"Idempotency-Key":newRequestKey()}, body:{}}); toast("Callback queued."); await refreshAttention(); } catch (error) { toast(errorMessage(error), true); }
      }));
      card.append(actions);
      return card;
    }));
    if ("Notification" in window && Notification.permission === "granted" && localStorage.getItem("switchboard-notifications") === "on") {
      for (const item of attentionData.items) {
        const key = item.id + ":" + (item.questions.map(q => q.id).join(",") || item.state + ":" + item.updated);
        if (notificationSeen.has(key)) continue;
        notificationSeen.add(key);
        const notice = new Notification("Switchboard needs your attention", {body:item.title, tag:key});
        notice.onclick = () => { window.focus(); location.hash = "attention/"+encodeURIComponent(item.id); openSession(item.id); notice.close(); };
      }
      localStorage.setItem("switchboard-notification-seen", JSON.stringify([...notificationSeen].slice(-500)));
    }
  } else $("#attention-status").textContent = `Inbox unavailable: ${errorMessage(results[0].reason)}`;
  if (results[1].status === "fulfilled") replaceDynamic($("#callback-cards"), ...results[1].value.callbacks.slice(0,30).map(item => {
    const card = el("article", "panel attention-card");
    card.append(badge(item.state), el("h3", "", item.title), el("p", "subtle", item.question || "Resume this session by phone"));
    if (item.detail) card.append(el("p", "notice", item.detail));
    if (item.state === "queued") card.append(button("Cancel callback", "quiet", async () => { try { await api(`/callbacks/${encodeURIComponent(item.id)}`, {method:"DELETE"}); await refreshAttention(); } catch(error) { toast(errorMessage(error),true); } }));
    return card;
  }));
  if (results[2].status === "fulfilled") replaceDynamic($("#template-cards"), ...results[2].value.templates.map(item => {
    const card = el("article", "panel attention-card");
    card.append(el("span", "eyebrow", `SPEED DIAL / ${item.number}`), el("h2", "", item.title), el("p", "subtle", item.prompt));
    const run = button("Start task ↗", "primary", async () => {
      run.disabled = true;
      try { const session = await api(`/task-templates/${item.number}/run`, {method:"POST", headers:{"Idempotency-Key":newRequestKey()}, body:{}}); await openSession(session.id); }
      catch(error) { toast(`${errorMessage(error)}. Check Sessions before starting again.`,true); }
      finally { run.disabled = !item.enabled; }
    });
    run.disabled = !item.enabled;
    card.append(run, button("Edit", "quiet", () => editTemplate(item)));
    return card;
  }));
}
function newRequestKey() {
  const bytes = new Uint8Array(16); crypto.getRandomValues(bytes);
  return Array.from(bytes, b => b.toString(16).padStart(2,"0")).join("");
}
$("#enable-notifications").addEventListener("click", async () => {
  if (!("Notification" in window) || !window.isSecureContext) { toast("Browser notifications need HTTPS or localhost. The native laptop notifier also works with the home-network address.", true); return; }
  const permission = await Notification.requestPermission();
  if (permission === "granted") { localStorage.setItem("switchboard-notifications","on"); toast("Notifications enabled while this dashboard is open."); await refreshAttention(); }
  else toast("Notifications were not enabled. You can change this in browser settings.",true);
});
$("#quiet-settings").addEventListener("click", () => {
  if (!attentionData) return;
  const body = modal("A quieter line.", "CALLBACK SCHEDULE"), form = el("form"), settings = attentionData.quiet_hours;
  body.append(el("p", "subtle", "Queues callback requests made through Switchboard. Direct bridge calls and incoming calls are unaffected."));
  toggle(form,"enabled","Enable quiet hours",settings.enabled);
  field(form,"start","Start",{type:"time",value:settings.start,required:true});
  field(form,"end","End",{type:"time",value:settings.end,required:true});
  field(form,"timezone","Time zone",{value:settings.timezone,required:true,hint:"For example, America/Boise. Daylight saving changes are automatic."});
  formEnd(form); body.append(form);
  form.addEventListener("submit", event => { event.preventDefault(); submit(form,async () => { const values = Object.fromEntries(new FormData(form)); values.enabled = form.elements.enabled.checked; await api("/quiet-hours",{method:"PUT",body:values}); $("#modal").close(); await refreshAttention(); }); });
});
$("#new-template").addEventListener("click", () => editTemplate());
function editTemplate(item = {}) {
  const body = modal(item.number ? "Edit saved task." : "A task worth keeping.", "SAVED TASK"), form = el("form");
  const number = field(form,"number","Speed-dial number",{value:item.number || "890001",required:true,hint:"Six digits, beginning with 89."});
  number.readOnly = !!item.number;
  field(form,"title","Name",{value:item.title || "",required:true});
  field(form,"prompt","Task",{type:"textarea",value:item.prompt || "",required:true});
  field(form,"instructions","Standing instructions",{type:"textarea",value:item.instructions || ""});
  field(form,"host","Run on",{value:item.host || state.overview?.settings?.default_host || "proxmox",items:[{value:"proxmox",label:"Homelab Amp runner"},{value:"workstation",label:"Workstation"}]});
  field(form,"cwd","Project folder (optional)",{value:item.cwd || ""});
  toggle(form,"enabled","Available to start",item.enabled ?? true);
  formEnd(form); body.append(form);
  form.addEventListener("submit",event => {event.preventDefault(); submit(form,async () => {const values = Object.fromEntries(new FormData(form)); values.enabled = form.elements.enabled.checked; values.cwd ||= null; values.integration = item.integration || "amp"; values.engine = item.engine || (values.integration === "codex" ? "codex" : "amp"); await api(item.number ? `/task-templates/${encodeURIComponent(values.number)}` : "/task-templates",{method:item.number ? "PUT" : "POST",body:values}); $("#modal").close(); await refreshAttention(); });});
}
bootstrap();
