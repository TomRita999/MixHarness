import {encodeVideos, videoFigure, VideoDrafts} from './video.js';

const $ = (selector) => document.querySelector(selector);
document.querySelectorAll("i.ic").forEach((glyph) => glyph.setAttribute("aria-hidden", "true"));
const content = $("#content");
const promptBox = $("#prompt");
const statusNames = {
  ready: "待执行", running: "执行中", waiting: "等待结果", done: "已完成",
  obsolete: "已跳过", failed: "失败", needs_key: "等待密钥",
};
const roleNames = {user: "你", gemini: "Gemini", openai: "OpenAI", system: "过程"};
const scopeNames = {engineering: "开发与研究", verification: "验证条件", report: "报告要求"};
let config, current, source, state;
let projects = [];
const catalogRequests = {gemini: 0, openai: 0};
let sessions = [], panelTab = "tasks", live = {}, epoch = 0, sending = false, savingModels = false, switching = false;
let toastTimer;
let messageRows = new Map();
const retryIds = new Map();
const videoDrafts = new VideoDrafts($('#composer'), () => current, updateSend, previewFile, showError);

function el(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function icon(name) {
  const element = el("i", "ic " + name);
  element.setAttribute("aria-hidden", "true");
  return element;
}
function button(label, className, action) {
  const control = el("button", className, label);
  control.type = "button";
  control.addEventListener("click", () => Promise.resolve(action()).catch(showError));
  return control;
}
function toast(text) {
  $("#toast").textContent = text;
  $("#toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 5500);
}
function showError(error) { toast(error.message); }
async function api(path, method = "GET", data) {
  const options = {method, cache: "no-store"};
  if (data !== undefined) {
    options.headers = {"Content-Type": "application/json"};
    options.body = JSON.stringify(data);
  }
  const response = await fetch("/api/" + path, options);
  if (!response.ok) {
    const problem = await response.json();
    throw new Error(typeof problem.detail === "string" ? problem.detail : "请求参数无效");
  }
  return response.json();
}
function sessionPath(suffix = "") { return "sessions/" + current + suffix; }
function localTime(stamp) {
  return new Date(stamp).toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"});
}
function rich(body, html, fallback) {
  if (!html) {
    body.textContent = fallback;
    body.classList.add("plain");
    return;
  }
  // HTML 由服务端的 Markdown 解析器生成，原始 HTML 已转义。
  body.innerHTML = html;
  body.querySelectorAll("a").forEach((a) => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
}
function modal(title, ...nodes) {
  $("#dialog-title").textContent = title;
  $("#dialog-body").replaceChildren(...nodes);
  if (!$("#dialog").open) $("#dialog").showModal();
}
function previewFile(file, mime) {
  const url = URL.createObjectURL(file);
  $('#dialog').addEventListener('close', () => URL.revokeObjectURL(url), {once: true});
  modal(file.name, videoFigure({name: file.name, mime, url}));
}
function connection(online) {
  $("#connection").classList.toggle("online", online);
  $("#connection span").textContent = online ? "已连接" : "连接中…";
}
const narrowScreen = matchMedia("(max-width: 760px)");
const systemTheme = matchMedia("(prefers-color-scheme: dark)");
function theme(mode) {
  document.documentElement.dataset.theme = mode;
  document.querySelector('meta[name="theme-color"]').content = mode === "dark" ? "#171717" : "#ffffff";
  const next = mode === "dark" ? "切换为浅色主题" : "切换为深色主题";
  $("#theme").setAttribute("aria-label", next);
  $("#theme").title = next;
  $("#theme").replaceChildren(icon(mode === "dark" ? "sun" : "moon"));
}
function hideSidebar() {
  $("#sidebar").classList.remove("shown");
  $("#scrim").hidden = true;
  $("#menu").setAttribute("aria-expanded", String(!narrowScreen.matches && !$("#app").classList.contains("rail-closed")));
}
function toggleSidebar(open) {
  if (narrowScreen.matches) {
    $("#sidebar").classList.toggle("shown", open);
    $("#scrim").hidden = !open;
  } else {
    $("#app").classList.toggle("rail-closed", !open);
    localStorage.setItem("mixharness-sidebar", open ? "open" : "closed");
  }
  $("#menu").setAttribute("aria-expanded", String(open));
  (open ? $("#hide-side") : $("#menu")).focus();
}
function historyAge(stamp) {
  const mins = Math.max(0, Math.floor((Date.now() - Date.parse(stamp)) / 60000));
  if (!mins) return "刚刚";
  if (mins < 60) return mins + " 分钟";
  if (mins < 1440) return Math.floor(mins / 60) + " 小时";
  return new Date(stamp).toLocaleDateString("zh-CN", {month: "numeric", day: "numeric"});
}
function drawHistory() {
  const list = $("#history");
  const query = $("#search").value.trim().toLowerCase();
  list.replaceChildren();
  for (const project of [...projects, {id: null, name: "独立会话"}]) {
    const chats = sessions.filter(s => s.project_id === project.id &&
      (project.name.toLowerCase().includes(query) || s.title.toLowerCase().includes(query)));
    if (!chats.length && (!project.id || !project.name.toLowerCase().includes(query))) continue;
    const group = el("details");
    const saved = "mixharness-expanded-" + (project.id || "local");
    group.open = Boolean(query) || localStorage.getItem(saved) !== "false";
    group.ontoggle = () => { if (!query && group.isConnected) localStorage.setItem(saved, String(group.open)); };
    const heading = el("summary", "project-toggle");
    heading.append(icon("folder"), el("span", "", project.name));
    group.append(heading);
    if (project.id) group.append(button("在此新建会话", "history-item", () => selectSession(null, project.id)));
    for (const s of chats) {
      const item = button("", "history-item" + (s.id === current ? " active" : ""), () => selectSession(s.id));
      item.title = s.title;
      if (s.id === current) item.setAttribute("aria-current", "page");
      const stamp = el("time", "", historyAge(s.updated));
      stamp.dateTime = s.updated;
      item.append(el("span", "", s.title), stamp);
      group.append(item);
    }
    list.append(group);
  }
  if (!list.childElementCount) list.append(el("p", "history-empty", "没有匹配的会话"));
}
function drawModels() {
  if (savingModels || !state) return;
  for (const provider of ["gemini", "openai"]) {
    const select = $("#" + provider);
    const model = state.session[provider];
    const data = config.catalogs[provider];
    const options = [model, ...data.models.filter(id => id !== model)];
    const signature = JSON.stringify([options, data.source, data.configured, data.error]);
    if (select.value !== model || select.dataset.catalog !== signature) {
      const group = el("optgroup");
      group.label = data.source === "account" ? "可用模型" : "常用模型";
      group.append(...data.models.map(id => new Option(id, id)));
      select.replaceChildren(group);
      if (!data.models.includes(model)) select.prepend(new Option(model + " · 当前", model));
      if (data.configured) select.append(new Option("刷新列表", "__refresh"));
      select.append(new Option("自定义 ID…", "__custom"));
      select.value = model;
      select.dataset.catalog = signature;
    }
    select.title = data.error || model;
    const level = $("#" + provider + "-effort");
    const levels = Object.keys(data.groups.find(group => group.id === model)?.choices || {});
    if (level.dataset.model !== model || level.options.length !== levels.length + 1) {
      level.replaceChildren(new Option("默认", ""), ...levels.map(e => new Option(e, e)));
      level.dataset.model = model;
    }
    level.value = state.session.efforts?.[provider] || "";
    level.hidden = !levels.length || state.session.backend === "clients";
  }
}
async function refreshCatalog(provider) {
  const version = ++catalogRequests[provider];
  const result = await api("models/" + provider + "/refresh", "POST");
  if (version !== catalogRequests[provider]) return;
  config.catalogs[provider] = result;
  drawModels();
  if (result.error) toast(result.error);
}
function syncCatalogs() {
  for (const id of ["gemini", "openai"]) {
    ++catalogRequests[id];
    if (config.keys[id]) refreshCatalog(id).catch(showError);
  }
}
function drawConfig() {
  const ready = config.keys.gemini && config.keys.openai;
  $("#key-state").classList.toggle("ready", ready);
  $("#key-state b").textContent = ready ? "API 密钥已配置" : "配置 API 密钥";
  videoDrafts.configure(config.video_upload);
  drawModels();
}
function settings() {
  const text = el("p", "", "在项目目录的 .env 中填入密钥后重新加载。");
  const path = el("code", "", config.project);
  const rows = el("div");
  for (const [id, name, env, url] of [
    ["gemini", "Google AI Studio", "GEMINI_API_KEY", "https://aistudio.google.com/apikey"],
    ["openai", "OpenAI Platform", "OPENAI_API_KEY", "https://platform.openai.com/api-keys"],
  ]) {
    const row = el("div", "row"), main = el("div", "row-main");
    const a = el("a", "", name);
    a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer";
    main.append(a, el("small", "", env));
    row.append(main, el("span", "badge " + (config.keys[id] ? "done" : "needs_key"), config.keys[id] ? "已配置" : "待配置"));
    rows.append(row);
  }
  const directory = el("p", "", "当前工作目录");
  const projectPath = el("code", "", state.session.workspace);
  const actions = el("div", "dialog-actions");
  actions.append(button("重新加载", "send", async () => {
    config = await api("config/reload", "POST");
    drawConfig(); syncCatalogs(); settings(); toast("已重新加载配置");
  }));
  modal("设置", text, path, rows, directory, projectPath, actions);
}
function showBrief() {
  const parts = [];
  for (const [field, label] of Object.entries(scopeNames)) {
    const heading = el("h3", "", label);
    const version = state.session.versions[field];
    if (version) heading.append(el("small", "badge", " v" + version));
    parts.push(heading, el("p", "", state.session.brief[field] || "尚未指定"));
  }
  modal("当前需求", ...parts);
}
function showModels(provider) {
  const input = el("input");
  input.placeholder = "输入模型 ID";
  input.value = state.session[provider];
  input.setAttribute("aria-label", "模型 ID");
  const apply = button("确定", "send", async () => {
    const id = input.value.trim();
    if (!/^[A-Za-z0-9._:/-]{1,150}$/.test(id)) throw new Error("请输入有效的模型 ID");
    await saveModel(provider, id); $("#dialog").close();
  });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") apply.click(); });
  modal(provider === "gemini" ? "选择 Gemini 模型" : "选择 OpenAI 模型", input, apply);
  input.focus();
}
async function saveModel(provider, value, effort) {
  const sid = current;
  savingModels = true;
  $("#gemini").disabled = true; $("#openai").disabled = true;
  $("#gemini-effort").disabled = true; $("#openai-effort").disabled = true;
  try {
    const updated = await api(sessionPath("/models"), "PATCH", {provider, model: value, effort});
    if (sid === current) applyState(updated);
  } finally {
    savingModels = false; $("#gemini").disabled = false; $("#openai").disabled = false;
    $("#gemini-effort").disabled = false; $("#openai-effort").disabled = false;
    drawModels();
  }
}
function updateSend() {
  const disabled = !current || sending || switching;
  videoDrafts.add.disabled = disabled;
  $("#send").disabled = disabled || (!promptBox.value.trim() && !videoDrafts.files(current).length);
}
function fillPrompt(value) {
  promptBox.value = value;
  promptBox.dispatchEvent(new Event("input"));
  promptBox.focus();
}
function empty(title, subtitle, mark) {
  const group = el("div", "empty");
  group.append(icon(mark), el("h2", "", title), el("p", "", subtitle));
  return group;
}
function buildSuggestions() {
  for (const [label, mark, text] of [
    ["梳理结构", "book", "看看这个项目，讲一下它主要做什么、目录怎么分、怎么跑起来。"],
    ["运行测试", "code", "跑一下项目里的测试，看看哪些能通过、哪些有问题。"],
    ["撰写说明", "file-text", "根据项目内容写份说明，缺数据或拿不准的地方让 GPT 测一下。"],
  ]) {
    const b = button(label, "", () => fillPrompt(text));
    b.prepend(icon(mark)); $("#suggestions").append(b);
  }
}
function chat() {
  const transcript = [];
  const nextRows = new Map();
  function appendTrace(parent, event) {
    if (!event.trace?.length) return;
    const details = el("dl", "trace");
    for (const [label, value] of event.trace) {
      details.append(el("dt", "", label), el("dd", "", value));
    }
    parent.append(details);
  }
  for (const event of state.events) {
    if (event.kind === "message") {
      const key = current + '/' + event.seq;
      const signature = JSON.stringify(event);
      const saved = messageRows.get(key);
      if (saved?.signature === signature) {
        nextRows.set(key, saved);
        transcript.push(saved.article);
        continue;
      }
      const article = el("article", "entry " + event.actor);
      const header = el("div", "entry-head");
      header.append(el("span", "", roleNames[event.actor]), el("time", "", localTime(event.at)));
      article.append(header);
      appendTrace(article, event);
      if (event.body || event.html) {
        const body = el("div", "body markdown");
        rich(body, event.html, event.body);
        article.append(body);
      }
      for (const item of event.attachments || []) {
        if (item.kind === 'video') {
          const url = '/api/' + sessionPath('/attachments/' + encodeURIComponent(item.id));
          article.append(videoFigure({name: item.name, mime: item.mime, url}));
        }
      }
      for (const video of event.videos || []) article.append(videoFigure(video));
      nextRows.set(key, {signature, article});
      transcript.push(article);
    } else if (["verification", "failure", "progress", "requirements"].includes(event.kind)) {
      const line = el("div", "notice" + (event.kind === "failure" ? " error" : ""));
      if (event.kind === "requirements") {
        line.append(el("strong", "", "Gemini · 需求整理"));
      } else if (event.notice) {
        line.append(el("strong", "", event.notice));
        const error = el("details");
        error.append(el("summary", "", "原始错误"), el("p", "", event.body));
        line.append(error);
      } else {
        const owner = event.kind === "failure" ? roleNames[event.actor] + " · 失败 · " : "";
        line.append(el("span", "", (event.kind === "verification" ? "Gemini 请求 GPT 验证 · " : owner) + event.body));
      }
      appendTrace(line, event);
      if (event.retryable) {
        const url = sessionPath("/tasks/" + event.meta.job + "/retry");
        if (["engineering", "verification"].includes(event.meta.kind)) line.append(button("只读重试 GPT", "quiet", () => api(url + "?read_only=true", "POST")));
        line.append(button("重试", "quiet", () => api(url, "POST")));
      }
      transcript.push(line);
    }
  }
  messageRows = nextRows;
  for (const request of state.approvals || []) {
    const path = sessionPath("/approvals/" + request.id);
    const detail = el("details", "notice");
    detail.append(el("summary", "", "等待批准 · " + request.source + " · " + request.title),
      el("pre", "artifact-text", request.detail),
      button("允许", "quiet", () => api(path, "POST", {allow: true})),
      button("拒绝", "quiet", () => api(path, "POST", {allow: false})));
    transcript.push(detail);
  }
  if (transcript.length) {
    const waiting = state.tasks.filter((t) => t.status === "needs_key");
    if (waiting.length) transcript.push(el("div", "notice", "等待配置 API 密钥后继续执行。"));
    const active = state.tasks.filter((t) => t.status === "running" || t.status === "waiting");
    if (active.length) transcript.push(el("div", "notice", active.map((t) => t.title + " · " + statusNames[t.status]).join("　")));
  }
  const preview = el("div", "live"); preview.id = "live"; transcript.push(preview);
  return transcript;
}
function drawLive() {
  const target = $("#live");
  if (!target) return;
  const atBottom = content.scrollHeight - content.scrollTop - content.clientHeight < 90;
  target.replaceChildren();
  for (const [jid, value] of Object.entries(live)) {
    const task = state.tasks.find((t) => t.id === jid && t.status === "running");
    if (task && value) {
      target.append(el("div", "entry-head", task.title + " · 正在生成"), el("div", "live", value));
    }
  }
  if (atBottom && Object.keys(live).length) content.scrollTop = content.scrollHeight;
}
function tasks() {
  if (!state.tasks.length) return empty("暂无任务", "发送消息后会显示任务进度。", "workflow");
  const group = el("div");
  for (const task of [...state.tasks].reverse()) {
    const wrapper = el("div");
    const row = el("div", "row " + task.status);
    const main = el("div", "row-main");
    main.append(el("strong", "", task.title), el("small", "", task.model + " · " + localTime(task.at)));
    row.append(icon(task.kind === "report" ? "file-text" : "activity"), main, el("span", "badge " + task.status, statusNames[task.status]));
    if (task.status === "needs_key") row.append(button("配置", "quiet", settings));
    if (task.status === "failed") row.append(button("重试", "quiet", () => api(sessionPath("/tasks/" + task.id + "/retry"), "POST")));
    wrapper.append(row);
    if (task.error) wrapper.append(el("p", "detail", task.error));
    if (task.draft && task.kind === "report") wrapper.append(button("查看草稿", "quiet", () => modal("报告草稿", el("pre", "artifact-text", task.draft))));
    group.append(wrapper);
  }
  return group;
}
async function showArtifact(id) {
  const sid = current;
  const path = sessionPath("/artifacts/" + id);
  const item = await api(path);
  if (sid !== current) return;
  const text = el("div", item.html ? "markdown" : "artifact-text");
  rich(text, item.html, item.content);
  const link = el("a", "quiet", "下载文件");
  link.href = "/api/" + path + "?download=true"; link.download = item.name;
  const actions = el("div", "dialog-actions"); actions.append(link);
  const details = [];
  if (item.meta.argv) details.push(el("p", "", "验证命令　" + item.meta.argv.join(" ") + "　·　退出码 " + item.meta.exit_code));
  if (item.meta.source) details.push(el("p", "", "项目版本　" + item.meta.source.slice(0, 16)));
  modal(item.name, ...details, text, ...(item.videos || []).map(videoFigure), actions);
}
function files() {
  if (!state.artifacts.length) return empty("暂无成果", "生成的文件与验证日志会显示在这里。", "files");
  const group = el("div");
  for (const item of [...state.artifacts].reverse()) {
    const row = button("", "row row-link", () => showArtifact(item.id));
    const main = el("div", "row-main");
    main.append(el("strong", "", item.name), el("small", "", localTime(item.at) + " · SHA256 " + item.digest.slice(0, 10)));
    row.append(icon("file-text"), main, icon("chevron-right")); group.append(row);
  }
  return group;
}
function drawContent(forceScroll = false) {
  const starting = !state.events.some((event) => event.kind === "message" && event.actor === "user");
  const atBottom = content.scrollHeight - content.scrollTop - content.clientHeight < 90;
  const oldScroll = content.scrollTop;
  $("#conversation").classList.toggle("empty-session", starting);
  $("#welcome").hidden = !starting;
  $("#suggestions").hidden = !starting;
  promptBox.placeholder = starting ? "输入消息…" : "继续输入或补充要求…";
  // 会话更新时让原有视频留在文档中，保留播放位置和浏览器解码状态。
  const rows = chat();
  const keep = new Set(rows);
  for (const child of Array.from(content.childNodes)) {
    if (!keep.has(child)) child.remove();
  }
  let next = content.firstChild;
  for (const row of rows) {
    if (row === next) next = next.nextSibling;
    else content.insertBefore(row, next);
  }
  drawLive();
  content.scrollTop = forceScroll || atBottom ? content.scrollHeight : oldScroll;
  if (!$("#inspector").hidden) drawInspector();
}
function applyState(next, initial = false) {
  if (!initial && state?.session.id === next.session.id &&
      (next.events.at(-1)?.seq || 0) < (state.events.at(-1)?.seq || 0)) return;
  state = next;
  const s = state.session;
  const project = projects.find(p => p.id === s.project_id);
  $("#header-project").textContent = project?.name || "独立会话";
  $("#header-project").title = project?.folders.map(f => f.path).join("\n") || s.workspace;
  localStorage.setItem("mixharness-recent-" + (s.project_id || "local"), s.id);
  sessions = [s, ...sessions.filter((item) => item.id !== s.id)].sort((a,b) => b.updated.localeCompare(a.updated));
  $("#session-title").textContent = s.title;
  $("#session-title").title = s.title;
  document.title = s.title === "新会话" ? "MixHarness" : s.title + " · MixHarness";
  $("#task-count").textContent = state.tasks.filter((t) => t.status !== "obsolete").length;
  $("#file-count").textContent = state.artifacts.length;
  drawModels(); drawHistory(); drawContent(initial); updateSend();
}
function drawInspector() {
  document.querySelectorAll("[data-tab]").forEach((b) => {
    const selected = b.dataset.tab === panelTab;
    b.setAttribute("aria-selected", String(selected)); b.tabIndex = selected ? 0 : -1;
  });
  $("#panel-content").setAttribute("aria-labelledby", "tab-" + panelTab);
  if (state) $("#panel-content").replaceChildren(panelTab === "tasks" ? tasks() : files());
}
function toggleInspector(open) {
  $("#inspector").hidden = !open;
  $("#open-inspector").setAttribute("aria-expanded", String(open));
  if (open) { drawInspector(); $("#tab-" + panelTab).focus(); }
  else $("#open-inspector").focus();
}
async function selectSession(id, projectId = state?.session.project_id) {
  const request = ++epoch;
  switching = true;
  $(".desk").inert = true;
  $("#new-session").disabled = true;
  source?.close();
  hideSidebar();
  try {
    if (!id) {
      const models = state ? {gemini: state.session.gemini, openai: state.session.openai} : {};
      if (projectId !== undefined) Object.assign(models, {project_id: projectId, standalone: projectId === null});
      id = (await api("sessions", "POST", models)).id;
    }
    const next = await api("sessions/" + id);
    if (request !== epoch) return;
    if (id !== current && $('#dialog').open) $('#dialog').close();
    current = id; live = {};
    videoDrafts.render();
    localStorage.setItem("mixharness-session", id);
    promptBox.value = sessionStorage.getItem("mixharness-draft-" + id) || "";
    applyState(next, true); resizePrompt();
    source = new EventSource("/api/" + sessionPath("/events"));
    source.onopen = () => { if (request === epoch) connection(true); };
    source.onerror = () => { if (request === epoch) connection(false); };
    source.addEventListener("state", (e) => { if (request === epoch) applyState(JSON.parse(e.data)); });
    source.addEventListener("preview", (e) => { if (request === epoch) { live = JSON.parse(e.data); drawLive(); } });
    source.addEventListener("deleted", async () => {
      if (request !== epoch) return;
      source.close();
      videoDrafts.set(id, []);
      retryIds.delete(id);
      sessionStorage.removeItem("mixharness-draft-" + id);
      try {
        const sessions = await api("sessions");
        if (request === epoch) await selectSession(sessions[0]?.id, projectId);
      } catch (error) {
        showError(error);
      }
    });
  } finally {
    if (request === epoch) {
      switching = false; $(".desk").inert = false; $("#new-session").disabled = false; updateSend();
    }
  }
}
async function newSession() {
  if (switching) return;
  await selectSession(null);
  promptBox.focus();
}
$("#composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = promptBox.value.trim(), sid = current;
  const files = videoDrafts.files(sid);
  if ((!text && !files.length) || sending || switching || !sid) return;
  let attempt = retryIds.get(sid);
  if (!attempt || attempt.text !== text || attempt.files !== files) {
    attempt = {text, files, client_id: crypto.randomUUID()};
  }
  retryIds.set(sid, attempt);
  sending = true; updateSend();
  try {
    const message = {text, client_id: attempt.client_id};
    if (files.length) message.videos = await encodeVideos(files);
    await api('sessions/' + sid + '/messages', 'POST', message);
    retryIds.delete(sid);
    videoDrafts.remove(sid, files);
    if (sid === current && promptBox.value.trim() === text) fillPrompt("");
    else if ((sessionStorage.getItem("mixharness-draft-" + sid) || "").trim() === text) sessionStorage.removeItem("mixharness-draft-" + sid);
  } catch (error) { showError(error); }
  finally { sending = false; updateSend(); }
});
promptBox.addEventListener("input", () => {
  if (current) sessionStorage.setItem("mixharness-draft-" + current, promptBox.value);
  resizePrompt(); updateSend();
});
function resizePrompt() {
  promptBox.style.height = "auto";
  promptBox.style.height = Math.min(promptBox.scrollHeight, 170, innerHeight * 0.24) + "px";
}
promptBox.addEventListener("keydown", (e) => {
  if (!e.isComposing && e.keyCode !== 229 && e.key === "Enter" && !e.shiftKey) {
    e.preventDefault(); $("#composer").requestSubmit();
  }
});
for (const name of ["gemini", "openai"]) {
  $("#" + name + "-effort").addEventListener("change", e => saveModel(name, state.session[name], e.target.value).catch(showError));
  $("#" + name).addEventListener("change", (e) => {
    if (e.target.value === "__custom") { drawModels(); showModels(name); }
    else if (e.target.value === "__refresh") { drawModels(); refreshCatalog(name).catch(showError); }
    else saveModel(name, e.target.value).catch(showError);
  });
}
document.querySelectorAll("[data-tab]").forEach((b) => {
  b.addEventListener("click", () => { panelTab = b.dataset.tab; drawInspector(); });
  b.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault(); panelTab = panelTab === "tasks" ? "files" : "tasks";
      drawInspector(); $("#tab-" + panelTab).focus();
    }
  });
});
$("#open-inspector").onclick = () => toggleInspector($("#inspector").hidden);
$("#close-inspector").onclick = () => toggleInspector(false);
$("#new-session").onclick = () => newSession().catch(showError);
$("#settings").onclick = $("#key-state").onclick = () => { if (state) settings(); };
$("#brief").onclick = () => { if (state) showBrief(); };
$("#open-project").onclick = () => {
  const path = el("input");
  path.placeholder = "项目文件夹的完整路径";
  path.setAttribute("aria-label", "项目文件夹");
  const open = button("打开", "send", async () => {
    const project = await api("projects", "POST", {directory: path.value.trim()});
    [projects, sessions] = await Promise.all([api("projects"), api("sessions")]);
    const chats = sessions.filter(s => s.project_id === project.id);
    const recent = localStorage.getItem("mixharness-recent-" + project.id);
    $("#dialog").close();
    await selectSession(chats.find(s => s.id === recent)?.id || chats[0]?.id, project.id);
  });
  modal("打开项目", path, open);
  path.focus();
};
$("#search-toggle").onclick = () => {
  const input = $("#search"); input.hidden = !input.hidden; input.value = "";
  $("#search-toggle").setAttribute("aria-expanded", String(!input.hidden));
  drawHistory();
  if (!input.hidden) input.focus();
};
$("#search").oninput = drawHistory;
$("#close-dialog").onclick = () => $("#dialog").close();
$('#dialog').addEventListener('close', () => { if (!$('#dialog').open) $('#dialog-body').replaceChildren(); });
$("#dialog").addEventListener("click", (e) => {
  if (e.target !== $("#dialog")) return;
  const rect = e.target.getBoundingClientRect();
  if (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom) e.target.close();
});
$("#menu").onclick = () => toggleSidebar(true);
$("#hide-side").onclick = $("#scrim").onclick = () => toggleSidebar(false);
if (localStorage.getItem("mixharness-sidebar") === "closed") $("#app").classList.add("rail-closed");
narrowScreen.addEventListener("change", hideSidebar);
theme(document.documentElement.dataset.theme);
$("#theme").onclick = () => {
  const mode = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  localStorage.setItem("mixharness-theme", mode); theme(mode);
};
systemTheme.addEventListener("change", e => { if (!localStorage.getItem("mixharness-theme")) theme(e.matches ? "dark" : "light"); });
buildSuggestions();
$("#new-shortcut").textContent = /Mac/.test(navigator.platform) ? "⌘ K" : "Ctrl K";
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k" && !$("#dialog").open) { e.preventDefault(); newSession().catch(showError); }
  if (e.key === "Escape" && !$("#dialog").open) {
    if ($("#sidebar").classList.contains("shown")) toggleSidebar(false);
    else if (!$("#inspector").hidden) toggleInspector(false);
  }
});
async function start() {
  [config, sessions, projects] = await Promise.all([api("config"), api("sessions"), api("projects")]);
  drawConfig(); syncCatalogs();
  const saved = localStorage.getItem("mixharness-session");
  if (sessions.length) await selectSession(sessions.find((s) => s.id === saved)?.id || sessions[0].id);
  else await newSession();
}
start().catch(showError);
