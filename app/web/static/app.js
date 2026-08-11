"use strict";

const state = {
  view: "today",
  user: null,
  token: localStorage.getItem("token") || null,
  question: null,
  sessionId: null,
  kind: "chain",
  returnTo: "today",
  reviewTag: null,
  historyGroups: [],
  detailQuestionId: null,
  detailAttempts: [],
  detailSessionId: null,
  tagCategories: null,
  allTags: [],
  bankPage: 1,
  bankPageSize: 20,
  rangeFrom: null,
  rangeTo: null,
  todayDate: null,
  calendarMode: "today",
  uploadType: "direct",
  resumeToken: null,
  resumeCandidates: [],
  reviewRecommended: new Set(),
  calViews: {},
  calYear: null,
  calMonth: null,
  historyDates: new Set(),
  historyDone: {},
  stats: null,
  editingQuestion: null,
};

const $ = (sel) => document.querySelector(sel);

// 可刷新恢复的主视图（hash 路由，带状态参数）；answer/result/detail 为流程中间态（sessionStorage 恢复）
const HASHABLE = new Set(["today", "bank", "history", "review", "favorites", "upload"]);

// --- 主题：localStorage 持久化，body[data-theme] 切换（theme.css 6 套） ---

function applyTheme(theme) {
  document.body.dataset.theme = theme;
  localStorage.setItem("theme", theme);
  document.querySelectorAll(".theme-dot").forEach((btn) => {
    btn.classList.toggle("theme-active", btn.dataset.theme === theme);
  });
}

// --- 布局：body[data-layout] 切换（style.css 布局规则） ---

function applyLayout(layout) {
  document.body.dataset.layout = layout;
  localStorage.setItem("layout", layout);
  document.querySelectorAll(".layout-btn").forEach((btn) => {
    btn.classList.toggle("layout-active", btn.dataset.layout === layout);
  });
}

(function initThemeLayout() {
  const theme = localStorage.getItem("theme") || "retro";
  const layout = localStorage.getItem("layout") || "classic";
  document.body.dataset.theme = theme;
  document.body.dataset.layout = layout;
  document.querySelectorAll(".theme-dot").forEach((btn) => {
    btn.classList.toggle("theme-active", btn.dataset.theme === theme);
    btn.addEventListener("click", () => applyTheme(btn.dataset.theme));
  });
  document.querySelectorAll(".layout-btn").forEach((btn) => {
    btn.classList.toggle("layout-active", btn.dataset.layout === layout);
    btn.addEventListener("click", () => applyLayout(btn.dataset.layout));
  });
})();

if ("scrollRestoration" in history) history.scrollRestoration = "manual"; // 后退不恢复旧滚动位

function scrollTop() {
  window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  document.documentElement.scrollTop = 0;
  document.body.scrollTop = 0;
}

function show(view) {
  state.view = view;
  scrollTop(); // 切换页面回到顶部
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  $("#view-" + view).classList.remove("hidden");
  document.querySelectorAll("nav button[data-view]").forEach((btn) => {
    btn.classList.toggle("nav-active", btn.dataset.view === view);
  });
  const panel = $("#calendar-panel");
  if (view === "today" || view === "history") {
    state.calendarMode = view;
    panel.classList.toggle("hidden", window.innerWidth <= 768); // 移动端经「日历」按钮展开
    panel.classList.remove("panel-extra-only");
    if (!panel.classList.contains("hidden")) renderCalendar();
  } else if (view === "bank") {
    panel.classList.toggle("hidden", window.innerWidth <= 768);
    panel.classList.add("panel-extra-only"); // 题库页只留概览（无日历）
  } else {
    panel.classList.add("hidden");
    panel.classList.remove("panel-extra-only");
  }
  $("#calendar-toggle").classList.toggle("hidden", !(view === "today" || view === "history" || view === "bank"));
  if (view === "today") {
    loadToday(state.todayDate || undefined);
    loadHistory(); // 圆点数据
  }
  if (view === "bank") {
    loadBank();
    loadStats();
  }
  if (view === "history") {
    loadHistory();
    loadStats();
  }
  if (view === "favorites") {
    loadFavorites();
  }
  if (HASHABLE.has(view)) updateHash();
}

// --- hash 路由状态：主视图 + 筛选/页码/范围/标签（刷新与后退可恢复） ---

function currentViewParams() {
  const p = new URLSearchParams();
  const setIf = (key, el) => {
    const val = el.value;
    if (val && val !== "all") p.set(key, val);
  };
  if (state.view === "bank") {
    if (state.bankPage > 1) p.set("page", String(state.bankPage));
    if (state.bankPageSize !== 20) p.set("page_size", String(state.bankPageSize));
    setIf("q", $("#bank-search"));
    setIf("type", $("#filter-bank-type"));
    setIf("difficulty", $("#filter-bank-difficulty"));
    setIf("category", $("#filter-bank-category"));
  } else if (state.view === "history") {
    if (state.rangeFrom) p.set("from", state.rangeFrom);
    if (state.rangeTo) p.set("to", state.rangeTo);
    setIf("done", $("#filter-done"));
    setIf("score", $("#filter-score"));
    setIf("type", $("#filter-type"));
    setIf("difficulty", $("#filter-difficulty"));
    setIf("category", $("#filter-category"));
  } else if (state.view === "review") {
    if (state.reviewTag) p.set("tag", state.reviewTag);
  } else if (state.view === "today") {
    if (state.todayDate) p.set("date", state.todayDate);
  }
  return p;
}

function updateHash() {
  if (!HASHABLE.has(state.view)) return;
  const qs = currentViewParams().toString();
  const hash = "#" + state.view + (qs ? "?" + qs : "");
  if (location.hash !== hash) {
    // pushState：翻页/筛选等状态变化进入浏览器历史，后退可逐步回退（replaceState 会直接退出应用）
    history.pushState(null, "", location.pathname + location.search + hash);
  }
}

function applyViewParams(view, params) {
  if (view === "bank") {
    state.bankPage = params.has("page") ? Math.max(1, Number(params.get("page")) || 1) : 1;
    const ps = Number(params.get("page_size")) || 20;
    state.bankPageSize = [10, 20, 30, 50].includes(ps) ? ps : 20;
    $("#bank-page-size").value = String(state.bankPageSize);
    if (params.has("q")) $("#bank-search").value = params.get("q");
    if (params.has("type")) $("#filter-bank-type").value = params.get("type");
    if (params.has("difficulty")) $("#filter-bank-difficulty").value = params.get("difficulty");
    if (params.has("category")) $("#filter-bank-category").value = params.get("category");
  } else if (view === "history") {
    state.rangeFrom = params.get("from") || null;
    state.rangeTo = params.get("to") || null;
    $("#filter-date-from").value = state.rangeFrom || "";
    $("#filter-date-to").value = state.rangeTo || "";
    if (params.has("done")) $("#filter-done").value = params.get("done");
    if (params.has("score")) {
      $("#filter-score").value = params.get("score");
      $("#filter-score").disabled = false;
    }
    if (params.has("type")) $("#filter-type").value = params.get("type");
    if (params.has("difficulty")) $("#filter-difficulty").value = params.get("difficulty");
    if (params.has("category")) $("#filter-category").value = params.get("category");
  } else if (view === "review") {
    state.reviewTag = params.get("tag") || null;
  } else if (view === "today") {
    state.todayDate = params.get("date") || null;
  }
}

window.addEventListener("hashchange", () => {
  const raw = location.hash.slice(1);
  const [target, qs] = raw.split("?");
  if (!HASHABLE.has(target)) return;
  const params = new URLSearchParams(qs || "");
  const sameView = state.view === target;
  if (sameView && currentViewParams().toString() === params.toString()) return;
  applyViewParams(target, params);
  if (target === "review") {
    show("review");
    if (state.reviewTag) loadReview(state.reviewTag);
    else loadReviewHome();
  } else {
    show(target);
  }
});

// 全局轻量提示 / 确认弹层（替代原生 alert/confirm，保持纸感 UI 一致）
function uiToast(message, isError) {
  const el = $("#ui-toast");
  el.textContent = message;
  el.classList.toggle("toast-error", !!isError);
  el.classList.remove("hidden");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.add("hidden"), 4000);
}
function uiConfirm(message) {
  return new Promise((resolve) => {
    $("#ui-modal-text").textContent = message;
    $("#ui-modal").classList.remove("hidden");
    const ok = $("#ui-modal-ok");
    const cancel = $("#ui-modal-cancel");
    const done = (val) => {
      $("#ui-modal").classList.add("hidden");
      ok.removeEventListener("click", onOk);
      cancel.removeEventListener("click", onCancel);
      resolve(val);
    };
    const onOk = () => done(true);
    const onCancel = () => done(false);
    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onCancel);
  });
}

async function api(path, options) {
  const opts = options || {};
  opts.headers = Object.assign(
    {},
    opts.headers || {},
    state.token ? { Authorization: `Bearer ${state.token}` } : {}
  );
  const method = (opts.method || "GET").toUpperCase();
  const retries = method === "GET" ? 2 : 0; // 仅幂等 GET 重试（POST 重试会重复触发判分轮次）
  for (let attempt = 0; attempt <= retries; attempt++) {
    let resp;
    try {
      resp = await fetch(path, opts);
    } catch (e) {
      if (attempt < retries) {
        await sleep(500 * (attempt + 1));
        continue;
      }
      throw new Error("网络错误，请检查连接");
    }
    if (resp.status === 401) {
      state.token = null;
      localStorage.removeItem("token");
      showAuth();
      throw new Error("登录已过期，请重新登录");
    }
    if (!resp.ok && attempt < retries && resp.status >= 500) {
      await sleep(500 * (attempt + 1));
      continue;
    }
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || body.error || `HTTP ${resp.status}`);
    }
    return resp.json();
  }
}

function fillCategoryOptions(categorySelect) {
  const cats = state.tagCategories || [];
  categorySelect.innerHTML = '<option value="all">全部</option>';
  for (const c of cats) {
    const opt = document.createElement("option");
    opt.value = c.name;
    opt.textContent = c.name;
    categorySelect.appendChild(opt);
  }
}

function categoryTags(categoryFilter) {
  if (categoryFilter === "all") return null;
  return ((state.tagCategories || []).find((c) => c.name === categoryFilter) || {}).tags || [];
}

function filterByTypeCategory(items, typeFilter, categoryFilter, difficultyFilter) {
  const catTags = categoryTags(categoryFilter);
  return items.filter((i) => {
    if (typeFilter !== "all" && i.type !== typeFilter) return false;
    if (difficultyFilter !== "all" && i.difficulty !== Number(difficultyFilter)) return false;
    if (catTags && !(i.tags || []).some((t) => catTags.includes(t))) return false;
    return true;
  });
}

async function loadTagCategories() {
  if (state.tagCategories) return;
  const cats = await api("/api/tags");
  state.tagCategories = cats;
  state.allTags = cats.flatMap((c) => c.tags);
  fillCategoryOptions($("#filter-today-category"));
  fillCategoryOptions($("#filter-bank-category"));
  fillCategoryOptions($("#filter-category"));
  fillCategoryOptions($("#filter-review-category"));
}

async function loadBank() {
  const params = new URLSearchParams({
    page: state.bankPage,
    page_size: state.bankPageSize,
  });
  const typeFilter = $("#filter-bank-type").value;
  const categoryFilter = $("#filter-bank-category").value;
  const difficultyFilter = $("#filter-bank-difficulty").value;
  const keyword = $("#bank-search").value.trim();
  if (typeFilter !== "all") params.set("type", typeFilter);
  if (difficultyFilter !== "all") params.set("difficulty", difficultyFilter);
  if (categoryFilter !== "all") params.set("category", categoryFilter);
  if (keyword) params.set("q", keyword);
  let data;
  try {
    data = await api(`/api/bank?${params}`);
  } catch (err) {
    uiToast(err.message, true);
    return;
  }
  state.bankTotal = data.total;
  state.bankTotalPages = data.total_pages;
  $("#bank-summary").textContent = `共 ${data.total} 题`;
  const list = $("#bank-list");
  list.innerHTML = "";
  if (!data.items.length) {
    list.innerHTML = emptyState("暂无符合条件的题目", "调整筛选条件，或去「上传题目」扩充题库");
  }
  for (const q of data.items) {
    const card = document.createElement("div");
    card.className = "card question-card";
    const badge = q.done
      ? '<span class="badge badge-done">已答</span>'
      : '<span class="badge badge-todo">待做</span>';
    const isOwner = state.user && state.user.role === "owner";
    const adminBtns = isOwner
      ? `<button class="admin-btn" data-action="edit" data-id="${q.id}">编辑</button>
         <button class="admin-btn admin-delete" data-action="delete" data-id="${q.id}">删除</button>`
      : "";
    card.innerHTML = `
      <div class="question-head">
        <button class="fav-btn${q.favorited ? " active" : ""}" data-id="${q.id}" data-fav="${q.favorited ? "1" : "0"}" title="收藏">★</button>
        ${badge}
        <span class="badge">${q.type}</span>
        <span class="badge">难度 ${difficultyStars(q.difficulty)}</span>
        ${adminBtns}
      </div>
      <p>${escapeHtml(q.stem)}</p>`;
    card.addEventListener("click", (e) => {
      if (e.target.closest(".admin-btn") || e.target.closest(".fav-btn")) return;
      state.returnTo = "bank";
      startAnswer(q);
    });
    list.appendChild(card);
  }
  list.querySelectorAll(".fav-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = Number(btn.dataset.id);
      const fav = btn.dataset.fav === "1";
      await toggleFavorite(id, fav, (now) => {
        btn.classList.toggle("active", now);
        btn.dataset.fav = now ? "1" : "0";
      });
    });
  });
  list.querySelectorAll(".admin-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = Number(btn.dataset.id);
      if (btn.dataset.action === "edit") {
        openQuestionModal(id);
      } else if (await uiConfirm("删除该题？将同时删除其全部作答记录。")) {
        api(`/api/admin/questions/${id}`, { method: "DELETE" }).then(() => loadBank());
      }
    });
  });
  const totalPages = Math.max(1, data.total_pages);
  $("#bank-page-info").textContent = `第 ${data.page} / ${totalPages} 页`;
  $("#bank-prev").disabled = data.page <= 1;
  $("#bank-next").disabled = data.page >= totalPages;
  const goto = $("#bank-goto");
  goto.max = String(totalPages);
  goto.value = "";
  scrollTop();
  updateHash();
}

// --- 收藏 ---

const favState = { page: 1, pageSize: 20, totalPages: 1 };

async function toggleFavorite(id, favorited, after) {
  try {
    if (favorited) {
      await api(`/api/favorites/${id}`, { method: "DELETE" });
    } else {
      await api(`/api/favorites/${id}`, { method: "POST" });
    }
    if (after) after(!favorited);
  } catch (err) {
    uiToast(err.message, true);
  }
}

async function loadFavorites() {
  const params = new URLSearchParams({ page: favState.page, page_size: favState.pageSize });
  const keyword = $("#fav-search").value.trim();
  if (keyword) params.set("q", keyword);
  let data;
  try {
    data = await api(`/api/favorites?${params}`);
  } catch (err) {
    uiToast(err.message, true);
    return;
  }
  favState.totalPages = Math.max(1, data.total_pages);
  const list = $("#fav-list");
  list.innerHTML = "";
  if (!data.items.length) {
    list.innerHTML = emptyState("暂无收藏题目", "在题库或今日题目里点击 ★ 收藏想重点复习的题");
  }
  for (const q of data.items) {
    const card = document.createElement("div");
    card.className = "card question-card";
    card.innerHTML = `
      <div class="question-head">
        <button class="fav-btn active" data-id="${q.id}" title="取消收藏">★</button>
        <span class="badge">${q.type}</span>
        <span class="badge">难度 ${difficultyStars(q.difficulty)}</span>
        <span class="badge">${(q.tags || []).join("、") || "无标签"}</span>
      </div>
      <p>${escapeHtml(q.stem)}</p>`;
    card.addEventListener("click", (e) => {
      if (e.target.closest(".fav-btn")) return;
      state.returnTo = "favorites";
      startAnswer(q);
    });
    list.appendChild(card);
  }
  list.querySelectorAll(".fav-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = Number(btn.dataset.id);
      await toggleFavorite(id, true, () => loadFavorites());
    });
  });
  $("#fav-page-info").textContent = `第 ${data.page} / ${favState.totalPages} 页`;
  $("#fav-prev").disabled = data.page <= 1;
  $("#fav-next").disabled = data.page >= favState.totalPages;
}

$("#fav-prev").addEventListener("click", () => {
  if (favState.page > 1) { favState.page -= 1; loadFavorites(); }
});
$("#fav-next").addEventListener("click", () => {
  if (favState.page < favState.totalPages) { favState.page += 1; loadFavorites(); }
});
$("#fav-search").addEventListener("input", () => { favState.page = 1; loadFavorites(); });

function gotoBankPage() {  const input = $("#bank-goto");
  const n = Number(input.value);
  const total = Number(input.max) || 1;
  if (!Number.isInteger(n) || n < 1 || n > total) {
    uiToast(`请输入 1-${total} 的页码`, true);
    return;
  }
  if (n === state.bankPage) return;
  state.bankPage = n;
  loadBank();
}

async function loadToday(date) {
  state.todayDate = date || null;
  try {
    const items = state.todayDate
      ? await api(`/api/today?date=${state.todayDate}`)
      : await api("/api/today");
    state.todayItems = items;
    renderToday();
    renderPanelExtra();
    scrollTop();
    updateHash();
  } catch (err) {
    uiToast(err.message, true);
  }
}

// --- 右侧日历面板附加内容：当日完成情况 / 答题趋势 / 概览（随视图切换） ---

async function loadStats() {
  try {
    state.stats = await api("/api/stats");
    renderPanelExtra();
  } catch (e) {
    // 统计加载失败静默（不影响主内容）
  }
}

function renderPanelExtra() {
  const box = $("#panel-extra");
  if (state.view === "today") {
    renderTodayProgress(box);
  } else if (state.view === "history") {
    renderTrendChart(box);
  } else if (state.view === "bank") {
    renderStatsOverview(box);
  }
}

function renderTodayProgress(box) {
  const items = state.todayItems || [];
  const done = items.filter((q) => q.done).length;
  const pct = items.length ? Math.round((done / items.length) * 100) : 0;
  box.innerHTML = `
    <h4 class="panel-title">今日完成情况</h4>
    <p class="panel-line">${done} / ${items.length} 已答</p>
    <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>`;
}

function renderTrendChart(box) {
  const stats = state.stats;
  if (!stats) {
    box.innerHTML = '<h4 class="panel-title">答题趋势</h4><p class="meta">加载中…</p>';
    return;
  }
  const last14 = (stats.trend || []).slice(-14);
  const max = Math.max(1, ...last14.map((r) => r.answered));
  const cols = last14
    .map((r) => {
      const h = Math.round((r.answered / max) * 100);
      return `<div class="trend-col" title="${r.date} 答 ${r.answered} 题">
        <div class="trend-bar" style="height:${h}%">${r.answered ? `<span>${r.answered}</span>` : ""}</div>
        <span class="trend-day">${r.date.slice(8)}</span>
      </div>`;
    })
    .join("");
  box.innerHTML = `
    <h4 class="panel-title">答题趋势</h4>
    <div class="trend-chart">${last14.length ? cols : '<p class="meta">暂无答题数据</p>'}</div>
    <p class="meta">${stats.avg_score != null ? `近 30 天共答 ${stats.answered_total} 题 · 平均分 ${stats.avg_score}` : "暂无答题数据"}</p>`;
}

function renderStatsOverview(box) {
  const stats = state.stats;
  if (!stats) {
    box.innerHTML = '<h4 class="panel-title">概览</h4><p class="meta">加载中…</p>';
    return;
  }
  const items = [
    ["题库", stats.bank_total],
    ["已完成", stats.done_questions],
    ["已答", stats.answered_total],
    ["平均分", stats.avg_score != null ? stats.avg_score : "—"],
  ];
  box.innerHTML =
    '<h4 class="panel-title">概览</h4><div class="overview-grid">' +
    items
      .map(([k, v]) => `<div class="ov-item"><span class="ov-num">${v}</span><span class="ov-label">${k}</span></div>`)
      .join("") +
    "</div>";
}

function renderToday() {
  const items = filterByTypeCategory(
    state.todayItems || [],
    $("#filter-today-type").value,
    $("#filter-today-category").value,
    $("#filter-today-difficulty").value
  );
  const doneCount = items.filter((q) => q.done).length;
  const activeCount = items.filter((q) => !q.done && q.active_session_id).length;
  const prefix = state.todayDate ? `${state.todayDate} · ` : "";
  $("#today-summary").textContent = `${prefix}${items.length} 题待做 · 已完成 ${doneCount}`;
  if (activeCount) {
    $("#today-summary").textContent += ` · ${activeCount} 题进行中`;
  }
  const list = $("#today-list");
  list.innerHTML = "";
  if (!items.length) {
    list.innerHTML = state.todayDate
      ? emptyState("该日无题目", "换个日期看看，或点「立即更新」生成新题")
      : emptyState("无符合条件的题目", "调整筛选条件，或点「立即更新」获取今日新题");
    return;
  }
  for (const q of items) {
    const card = document.createElement("div");
    card.className = "card question-card";
    const statusBadge = q.done
      ? '<span class="badge badge-done">已答</span>'
      : q.active_session_id
        ? '<span class="badge badge-todo">进行中</span>'
        : '<span class="badge badge-todo">待做</span>';
    card.innerHTML = `
      <div class="question-head">
        ${statusBadge}
        <span class="badge">${q.type}</span>
        <span class="badge">难度 ${difficultyStars(q.difficulty)}</span>
      </div>
      <p>${escapeHtml(q.stem)}</p>`;
    card.addEventListener("click", () => startAnswer(q));
    list.appendChild(card);
  }
}

async function startAnswer(q) {
  state.question = q;
  state.kind = "chain";
  let resumed = false;
  if (q.active_session_id) {
    state.sessionId = q.active_session_id;
    resumed = true;
  } else {
    try {
      const created = await api("/api/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question_id: q.id, kind: "chain" }),
      });
      state.sessionId = created.session_id;
      resumed = !!created.resumed;
    } catch (e) {
      uiToast(e.message, true);
      return;
    }
  }
  const detail = await api(`/api/questions/${q.id}`); // today 列表不含 criteria，答题页需详情
  state.question = { ...q, ...detail };
  saveAnswerState();
  renderAnswer();
  if (resumed) {
    // 断点恢复：拉取已有轮次并渲染时间线，继续作答（D18）
    const s = await api(`/api/sessions/${state.sessionId}`);
    if (s.status === "active" && s.rounds_done > 0) {
      renderChat(s.transcript || [], s.followup);
      $("#answer-status").textContent = `已恢复上次对话（已答 ${s.rounds_done} 轮），请在下方继续回答`;
    }
  }
  history.pushState(null, "", `#answer?session=${state.sessionId}`); // 浏览器后退 → hash 回主视图
  show("answer");
}

// --- 答题中间页恢复：刷新后回到答题/结果页（sessionStorage） ---

function saveAnswerState() {
  if (!state.sessionId || !state.question) return;
  sessionStorage.setItem(
    "answer_state",
    JSON.stringify({
      question_id: state.question.id,
      session_id: state.sessionId,
      returnTo: state.returnTo || "today",
    })
  );
}

async function restoreAnswerSession() {
  let saved;
  try {
    saved = JSON.parse(sessionStorage.getItem("answer_state") || "");
  } catch (e) {
    sessionStorage.removeItem("answer_state");
    return false;
  }
  if (!saved || !saved.session_id) return false;
  let s;
  try {
    s = await api(`/api/sessions/${saved.session_id}`);
  } catch (e) {
    sessionStorage.removeItem("answer_state"); // 会话不存在/已删除：放弃恢复
    return false;
  }
  if (s.status === "active" || s.status === "judging") {
    let q;
    try {
      q = await api(`/api/questions/${saved.question_id}`);
    } catch (e) {
      sessionStorage.removeItem("answer_state");
      return false;
    }
    state.sessionId = saved.session_id;
    state.question = q;
    state.returnTo = saved.returnTo || "today";
    saveAnswerState();
    renderAnswer();
    history.pushState(null, "", `#answer?session=${state.sessionId}`); // 后退 → hash 回主视图
    if (s.status === "judging") {
      $("#answer-status").textContent = "判分进行中…";
      pollResult();
    } else if (s.rounds_done > 0) {
      renderChat(s.transcript || [], s.followup);
      $("#answer-status").textContent = `已恢复上次对话（已答 ${s.rounds_done} 轮），请在下方继续回答`;
    }
    show("answer");
    return true;
  }
  if (s.status === "done") {
    renderResult(s.judgment);
    show("result");
    return true;
  }
  if (s.status === "failed") {
    renderResult(null);
    show("result");
    return true;
  }
  return false;
}

function renderAnswer() {
  const q = state.question;
  $("#answer-meta").textContent = `题型：${q.type} · 难度 ${difficultyStars(q.difficulty)} · 标签：${(q.tags || []).join(", ") || "无"}`;
  $("#answer-stem").textContent = q.stem;
  $("#answer-chat").innerHTML = "";
  $("#answer-good").innerHTML = q.good_criteria.map((c) => `<li>${escapeHtml(c)}</li>`).join("");
  $("#answer-bad").innerHTML = q.bad_criteria.map((c) => `<li>${escapeHtml(c)}</li>`).join("");
  $("#answer-input").value = "";
  $("#answer-status").textContent = "";
}

async function submitAnswer() {
  const answer = $("#answer-input").value.trim();
  if (answer.length < 2) {
    $("#answer-status").textContent = "回答太短，请至少输入 2 个字符";
    return;
  }
  $("#answer-status").textContent = "判分中…";
  $("#answer-submit").disabled = true;
  try {
    await api(`/api/sessions/${state.sessionId}/answer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answer }),
    });
    await pollResult();
  } catch (e) {
    $("#answer-status").textContent = e.message;
    $("#answer-submit").disabled = false;
  }
}

async function pollResult() {
  let fails = 0;
  for (let i = 0; i < 120; i++) {
    await sleep(1000);
    let s;
    try {
      s = await api(`/api/sessions/${state.sessionId}`);
      fails = 0;
    } catch (e) {
      if (++fails >= 5) {
        $("#answer-status").textContent = `查询失败：${e.message}，请稍后刷新查看`;
        $("#answer-submit").disabled = false;
        return;
      }
      continue; // 瞬时错误（代理 502/网络抖动）不中断轮询
    }
    if (s.status === "done") {
      renderResult(s.judgment);
      show("result");
      return;
    }
    if (s.status === "failed") {
      renderResult(null);
      show("result");
      return;
    }
    if (s.status === "active") {
      renderChat(s.transcript || [], s.followup);
      $("#answer-input").value = ""; // 自动清空，直接回答新追问
      $("#answer-status").textContent = `请在下方回答面试官追问（已答 ${s.rounds_done} 轮）…`;
      $("#answer-submit").disabled = false;
      return; // 追问链：等待用户继续作答
    }
  }
  $("#answer-status").textContent = "判分超时，请稍后刷新查看";
  $("#answer-submit").disabled = false;
}

function renderResult(judgment) {
  const retry = $("#result-retry");
  if (judgment === null) {
    $("#result-title").textContent = "判分失败";
    $("#result-scores").innerHTML = "";
    $("#result-review").textContent = "可点击下方按钮重试判分";
    $("#result-reference").textContent = "";
    $("#result-tags").textContent = "";
    $("#result-ref").textContent = "";
    retry.classList.remove("hidden");
    retry.onclick = async () => {
      retry.disabled = true;
      await api(`/api/sessions/${state.sessionId}/answer`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answer: "重试判分" }),
      });
      await pollResult();
      retry.disabled = false;
    };
    return;
  }
  retry.classList.add("hidden");
  $("#result-title").textContent = `得分：${judgment.total_score ?? "—"}`;
  const dims = [
    ["accuracy", "准确性"],
    ["completeness", "完整性"],
    ["clarity", "条理性"],
    ["depth", "深度"],
  ];
  $("#result-scores").innerHTML = dims
    .map(
      ([k, label]) =>
        `<div class="score-row"><span>${label}</span><div class="score-bar"><div class="score-fill" style="width:${judgment.scores[k] ?? 0}%"></div></div><span>${judgment.scores[k] ?? 0}</span></div>`
    )
    .join("");
  $("#result-review").textContent = judgment.review || "";
  $("#result-reference").textContent = judgment.reference_answer || "（无）";
  $("#result-ref").textContent = judgment.reference_used
    ? "本判分参考了库内同类高分回答"
    : "（库内暂无同类高分回答可参考）";
  const weakTags = judgment.weak_tags || [];
  $("#result-tags").innerHTML =
    (weakTags.length ? "薄弱点： " : "") +
    weakTags
      .map(
        (t) =>
          `<button class="weak-tag" data-tag="${escapeHtml(t)}">复习：${escapeHtml(t)}</button>`
      )
      .join("");
  $("#result-tags").querySelectorAll(".weak-tag").forEach((btn) => {
    btn.addEventListener("click", () => loadReview(btn.dataset.tag));
  });
}

const LEVEL_NAMES = { 1: "概念", 2: "原理", 3: "权衡", 4: "边界", 5: "拓展" };

function renderChat(transcript, followup) {
  const chat = $("#answer-chat");
  const items = transcript.length
    ? transcript
    : followup
      ? [{ role: "interviewer", content: followup }]
      : [];
  chat.innerHTML = items
    .map((t, i) => {
      const isUser = t.role === "user";
      const latest = !isUser && i === items.length - 1;
      const cls = `chat-item ${isUser ? "chat-user" : "chat-interviewer"}${latest ? " chat-latest" : ""}`;
      const who = isUser ? "你" : "面试官";
      const levelBadge = !isUser && t.level
        ? ` <span class="chat-level">L${t.level} ${LEVEL_NAMES[t.level] || ""}</span>`
        : "";
      return `<div class="${cls}"><b>${who}：</b>${escapeHtml(t.content)}${levelBadge}</div>`;
    })
    .join("");
}

async function loadDetail(questionId) {
  state.detailQuestionId = questionId;
  sessionStorage.setItem("detail_state", String(questionId)); // 刷新后恢复详情页
  history.pushState(null, "", `#detail?q=${questionId}`); // 后退 → hash 回主视图
  const data = await api(`/api/questions/${questionId}/history`);
  state.detailAttempts = data.attempts;
  $("#detail-type").textContent = data.type;
  $("#detail-stem").textContent = data.stem;
  $("#detail-meta").textContent = "标签：" + ((data.tags || []).join(", ") || "无");
  const tabs = $("#attempt-tabs");
  tabs.innerHTML = "";
  if (!data.attempts.length) {
    state.detailSessionId = null;
    $("#detail-chat").innerHTML = '<p class="meta">暂无作答记录</p>';
    $("#detail-scores").innerHTML = "";
    $("#detail-review").textContent = "";
    $("#detail-reference").textContent = "";
    $("#detail-tags").textContent = "";
    $("#detail-delete").disabled = true;
  } else {
    $("#detail-delete").disabled = false;
    data.attempts.forEach((a, idx) => {
      const btn = document.createElement("button");
      btn.className = "attempt-tab" + (idx === 0 ? " active" : "");
      const time = (a.started_at || "").slice(5, 16).replace("T", " ");
      const label =
        a.status === "active"
          ? `${time} 未完成`
          : a.judgment_status === "failed"
            ? `${time} 判分失败`
            : `${time} · ${a.total_score ?? "—"}分`;
      btn.textContent = `第 ${data.attempts.length - idx} 次 ${label}`;
      btn.addEventListener("click", () => {
        tabs.querySelectorAll(".attempt-tab").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        state.detailSessionId = a.session_id;
        renderAttempt(a);
      });
      tabs.appendChild(btn);
    });
    state.detailSessionId = data.attempts[0].session_id;
    renderAttempt(data.attempts[0]);
  }
  show("detail");
}

async function restoreDetailSession() {
  const raw = sessionStorage.getItem("detail_state");
  if (!raw) return false;
  const qid = Number(raw);
  if (!Number.isInteger(qid)) {
    sessionStorage.removeItem("detail_state");
    return false;
  }
  try {
    await loadDetail(qid);
    return true;
  } catch (e) {
    sessionStorage.removeItem("detail_state"); // 题目已删除等：放弃恢复
    return false;
  }
}

async function deleteCurrentAttempt() {
  const idx = state.detailAttempts.findIndex(
    (a) => a.session_id === state.detailSessionId
  );
  const nth = state.detailAttempts.length - idx;
  if (!(await uiConfirm(`删除第 ${nth} 次作答记录？此操作不可恢复。`))) return;
  await api(`/api/sessions/${state.detailSessionId}`, { method: "DELETE" });
  loadDetail(state.detailQuestionId);
}

function renderAttempt(a) {
  const chat = $("#detail-chat");
  const items = a.transcript || [];
  chat.innerHTML = items
    .map((t) => {
      const isUser = t.role === "user";
      const cls = `chat-item ${isUser ? "chat-user" : "chat-interviewer"}`;
      const who = isUser ? "你" : "面试官";
      const levelBadge = !isUser && t.level
        ? ` <span class="chat-level">L${t.level} ${LEVEL_NAMES[t.level] || ""}</span>`
        : "";
      return `<div class="${cls}"><b>${who}：</b>${escapeHtml(t.content)}${levelBadge}</div>`;
    })
    .join("");
  $("#detail-scores").innerHTML = "";
  $("#detail-review").textContent = "";
  $("#detail-reference").textContent = "";
  $("#detail-tags").textContent = "";
  if (a.status === "active") {
    $("#detail-review").textContent = "（未完成，无判分）";
    return;
  }
  if (a.judgment_status === "failed" || !a.judgment) {
    $("#detail-review").textContent = "判分失败";
    return;
  }
  const j = a.judgment;
  const dims = [
    ["accuracy", "准确性"],
    ["completeness", "完整性"],
    ["clarity", "条理性"],
    ["depth", "深度"],
  ];
  $("#detail-scores").innerHTML = dims
    .map(
      ([k, label]) =>
        `<div class="score-row"><span>${label}</span><div class="score-bar"><div class="score-fill" style="width:${j.scores[k] ?? 0}%"></div></div><span>${j.scores[k] ?? 0}</span></div>`
    )
    .join("");
  $("#detail-review").textContent = `总分：${j.total_score ?? "—"}　评语：${j.review || ""}`;
  $("#detail-reference").textContent = j.reference_answer || "（无）";
  $("#detail-tags").textContent =
    "薄弱点：" + ((j.weak_tags || []).join(", ") || "无") +
    (j.reference_used ? "　（本判分参考了库内同类高分回答）" : "");
}

// --- 复习页主题自定义展示（岗位默认集 + 用户自定义 localStorage） ---

const REVIEW_VISIBLE_KEY = "review_visible_tags";

function focusDefaultVisibleTags() {
  const u = state.user || {};
  const visible = new Set();
  for (const c of state.tagCategories || []) {
    const roles = c.roles || [];
    if (!roles.length) {
      (c.tags || []).forEach((t) => visible.add(t)); // 通用分类：全岗位
      continue;
    }
    if (!u.focus) {
      (c.tags || []).forEach((t) => visible.add(t)); // 未设置岗位：全部
      continue;
    }
    if (u.focus === "backend") {
      if (c.lang && c.lang === u.focus_lang) {
        (c.tags || []).forEach((t) => visible.add(t)); // 我的语言分类
      } else if (c.lang === null && roles.includes("backend")) {
        (c.tags || []).forEach((t) => visible.add(t)); // 语言无关后端分类
      } else if (!u.focus_lang && roles.includes("backend")) {
        (c.tags || []).forEach((t) => visible.add(t)); // 未选语言：全部后端
      }
    } else if (roles.includes(u.focus)) {
      (c.tags || []).forEach((t) => visible.add(t));
    }
  }
  return visible;
}

function loadVisibleTags() {
  try {
    const raw = localStorage.getItem(REVIEW_VISIBLE_KEY);
    if (raw) return new Set(JSON.parse(raw));
  } catch (e) { /* 损坏存储忽略 */ }
  return focusDefaultVisibleTags();
}

function saveVisibleTags(set) {
  localStorage.setItem(REVIEW_VISIBLE_KEY, JSON.stringify([...set]));
}

function clearVisibleTags() {
  localStorage.removeItem(REVIEW_VISIBLE_KEY);
}

function focusLabel() {
  const u = state.user || {};
  const roleNames = { backend: "后端", ai_app: "AI应用", ai_infra: "AI基础设施", frontend: "前端", qa: "测试开发" };
  if (!u.focus) return "全部岗位";
  const base = roleNames[u.focus] || u.focus;
  return u.focus === "backend" && u.focus_lang ? `${base}·${u.focus_lang}` : base;
}

async function loadReviewHome() {
  sessionStorage.removeItem("review_paper_tag"); // 回到复习首页：不再自动恢复复习卷
  let tags;
  try {
    tags = await api("/api/review/tags");
  } catch (err) {
    uiToast(err.message, true);
    return;
  }
  const entry = $("#review-entry");
  const list = $("#review-list");
  list.innerHTML = "";
  $("#review-title").textContent = "薄弱点复习";
  $("#review-back").classList.add("hidden");
  $("#review-filter").classList.add("hidden");
  const weak = tags.filter((t) => t.count > 0); // 薄弱点全显示（不受可见集影响）
  const rest = tags.filter((t) => t.count === 0);
  const visible = loadVisibleTags();
  const shown = rest.filter((t) => visible.has(t.tag));
  const hiddenCount = rest.length - shown.length;
  let html = "";
  if (!weak.length) {
    html += '<div class="card"><p class="meta">暂无薄弱点数据，完成答题后这里会显示需要加强的题目标签</p></div>';
  } else {
    html += '<p class="meta">需要加强的主题：</p>';
    html += weak
      .map(
        (t) =>
          `<button class="weak-tag-entry" data-tag="${escapeHtml(t.tag)}">${escapeHtml(t.tag)} <span class="badge badge-todo">${t.count}</span></button>`
      )
      .join("");
  }
  const hasCustom = localStorage.getItem(REVIEW_VISIBLE_KEY) !== null;
  const scopeLabel = hasCustom ? "自定义主题" : `当前岗位 · ${focusLabel()}`;
  html += `<p class="meta" style="margin-top:12px">${scopeLabel}` +
    `<button id="rev-tags-open" class="rev-tags-open" title="自定义展示哪些主题">+</button>` +
    `</p>`;
  if (!shown.length) {
    html += '<p class="meta">未选择展示主题，点「+」选择要在复习页展示的主题</p>';
  } else {
    html += shown
      .map(
        (t) =>
          `<button class="weak-tag-entry" data-tag="${escapeHtml(t.tag)}">${escapeHtml(t.tag)}</button>`
      )
      .join("");
  }
  if (hiddenCount > 0) {
    html += `<p class="meta">已隐藏 ${hiddenCount} 个主题</p>`;
  }
  entry.innerHTML = html;
  entry.querySelectorAll(".weak-tag-entry").forEach((btn) => {
    btn.addEventListener("click", () => loadReview(btn.dataset.tag));
  });
  $("#rev-tags-open").addEventListener("click", openReviewTagsModal);
}

// --- 自定义主题弹窗 ---

function openReviewTagsModal() {
  const box = $("#rev-tags-list");
  const current = loadVisibleTags();
  box.innerHTML = "";
  for (const c of state.tagCategories || []) {
    const group = document.createElement("div");
    group.className = "rev-tags-group";
    const head = document.createElement("div");
    head.className = "rev-tags-group-head";
    head.textContent = c.name;
    group.appendChild(head);
    const wrap = document.createElement("div");
    wrap.className = "rev-tags-checks";
    for (const t of c.tags || []) {
      const label = document.createElement("label");
      label.className = "rev-tags-check";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = t;
      cb.checked = current.has(t);
      label.appendChild(cb);
      label.appendChild(document.createTextNode(t));
      wrap.appendChild(label);
    }
    group.appendChild(wrap);
    box.appendChild(group);
  }
  $("#rv-tags-modal2").classList.remove("hidden");
}

$("#rev-tags-save").addEventListener("click", () => {
  const chosen = new Set(
    [...document.querySelectorAll("#rev-tags-list input[type=checkbox]:checked")].map((cb) => cb.value)
  );
  saveVisibleTags(chosen);
  $("#rv-tags-modal2").classList.add("hidden");
  loadReviewHome();
});

$("#rev-tags-cancel").addEventListener("click", () => $("#rv-tags-modal2").classList.add("hidden"));

$("#rev-tags-reset").addEventListener("click", () => {
  clearVisibleTags();
  $("#rv-tags-modal2").classList.add("hidden");
  loadReviewHome();
  uiToast("已重置为当前岗位默认主题");
});

async function loadReview(tag) {
  state.reviewTag = tag;
  try {
    const data = await api(`/api/review?tag=${encodeURIComponent(tag)}`);
    state.reviewItems = data.items || [];
    const list = $("#review-list");
    const entry = $("#review-entry");
    entry.innerHTML = "";
    $("#review-title").textContent = `薄弱点复习：${tag}`;
    if (data.fallback) {
      entry.innerHTML = `<p class="meta">该标签暂无题目，展示「${escapeHtml(data.fallback_category || "")}」分类相关题目</p>`;
    }
    $("#review-back").classList.remove("hidden");
    $("#review-filter").classList.remove("hidden");
    $("#review-paper-card").classList.remove("hidden");
    $("#review-paper").innerHTML = "";
    $("#review-paper-btn").disabled = false;
    $("#review-paper-btn").textContent = "生成复习卷";
    renderReviewItems();
    scrollTop();
    updateHash();
    // 刷新后自动恢复已生成的复习卷（后端 user:tag 缓存 1h，命中则秒回）
    if (sessionStorage.getItem("review_paper_tag") === tag) {
      loadReviewPaper();
    }
  } catch (err) {
    uiToast(err.message, true);
  }
}

async function loadReviewPaper() {
  const btn = $("#review-paper-btn");
  btn.disabled = true;
  btn.textContent = "生成中…（约 10-30 秒）";
  try {
    const data = await api(`/api/review/paper?tag=${encodeURIComponent(state.reviewTag)}`);
    const box = $("#review-paper");
    if (data.error) {
      box.innerHTML = `<p class="meta">${escapeHtml(data.error)}（下方题目仍可练习）</p>`;
      state.reviewRecommended = new Set(data.recommended_ids || []);
    } else if (!data.paper_html) {
      box.innerHTML = '<p class="meta">该标签暂无题目，无法生成复习卷</p>';
    } else {
      box.innerHTML = data.paper_html;
      state.reviewRecommended = new Set(data.recommended_ids || []);
      sessionStorage.setItem("review_paper_tag", state.reviewTag); // 刷新后自动恢复
    }
  } catch (err) {
    $("#review-paper").innerHTML = `<p class="meta">${escapeHtml(err.message)}</p>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "重新生成复习卷";
  }
  renderReviewItems();
}

function renderReviewItems() {
  const items = filterByTypeCategory(
    state.reviewItems || [],
    $("#filter-review-type").value,
    $("#filter-review-category").value,
    $("#filter-review-difficulty").value
  );
  const recommended = state.reviewRecommended || new Set();
  items.sort((a, b) => {
    const ra = recommended.has(a.id) ? 0 : 1;
    const rb = recommended.has(b.id) ? 0 : 1;
    return ra - rb;
  });
  const list = $("#review-list");
  list.innerHTML = "";
  if (!items.length) {
    list.innerHTML = emptyState("无符合条件的题目", "换个标签或筛选条件试试");
    return;
  }
  for (const q of items) {
    const card = document.createElement("div");
    card.className = "card question-card";
    const badge = q.done
      ? `<span class="badge badge-done">得分 ${q.total_score ?? "—"}</span>`
      : '<span class="badge badge-todo">待做</span>';
    const recBadge = recommended.has(q.id)
      ? '<span class="badge badge-done">推荐</span>'
      : "";
    card.innerHTML = `
      <div class="question-head">
        ${recBadge}${badge}
        <span class="badge">${q.type}</span>
        <span class="badge">难度 ${difficultyStars(q.difficulty)}</span>
      </div>
      <p>${escapeHtml(q.stem)}</p>`;
    card.addEventListener("click", () => {
      state.returnTo = "review";
      startAnswer(q);
    });
    list.appendChild(card);
  }
  show("review");
}

async function loadHistory() {
  try {
    const groups = await api("/api/history");
    state.historyGroups = groups;
    state.historyDates = new Set(groups.map((g) => g.date));
    state.historyDone = {};
    for (const g of groups) {
      state.historyDone[g.date] = (g.items || []).some((i) => i.done);
    }
    renderCalendar();
    applyFilters();
  } catch (err) {
    uiToast(err.message, true);
  }
}

function applyFilters() {
  const doneFilter = $("#filter-done").value;
  const scoreFilter = $("#filter-score").value;
  const typeFilter = $("#filter-type").value;
  const difficultyFilter = $("#filter-difficulty").value;
  const categoryFilter = $("#filter-category").value;
  const from = state.rangeFrom;
  const to = state.rangeTo;
  const groups = [];
  for (const g of state.historyGroups || []) {
    if (from && g.date < from) continue;
    if (to && g.date > to) continue;
    let items = g.items;
    if (doneFilter !== "all") {
      items = items.filter((i) => i.done === (doneFilter === "done"));
    }
    if (doneFilter === "done" && scoreFilter !== "all") {
      items = items.filter((i) =>
        scoreFilter === "high" ? i.total_score >= 80 : (i.total_score ?? 0) < 80
      );
    }
    items = filterByTypeCategory(items, typeFilter, categoryFilter, difficultyFilter);
    if (items.length) groups.push({ date: g.date, items });
  }
  renderHistory(groups);
  scrollTop();
  updateHash();
}

function renderHistory(groups) {
  const list = $("#history-list");
  list.innerHTML = "";
  if (!groups.length) {
    list.innerHTML = emptyState("暂无符合条件的记录", "答过题之后，这里会按日期展示你的作答历史");
    return;
  }
  for (const g of groups) {
    const head = document.createElement("div");
    head.className = "card";
    head.innerHTML = `
      <div class="question-head">
        <span class="badge badge-done">${g.date}</span>
        <span class="badge">${g.items.length} 题</span>
        <span class="badge">${g.items.filter((i) => i.done).length} 已答</span>
      </div>`;
    list.appendChild(head);
    for (const r of g.items) {
      const card = document.createElement("div");
      card.className = "card question-card";
      const badge = r.done
        ? r.status === "failed"
          ? '<span class="badge badge-todo">判分失败</span>'
          : `<span class="badge badge-done">得分 ${r.total_score ?? "—"}</span>`
        : '<span class="badge badge-todo">未作答</span>';
      card.innerHTML = `
        <div class="question-head">
          ${badge}
          <span class="badge">${r.type}</span>
          <span class="badge">难度 ${difficultyStars(r.difficulty)}</span>
          <button class="history-delete" data-qid="${r.question_id}">删除</button>
        </div>
        <p>${escapeHtml(r.stem)}</p>`;
      card.addEventListener("click", () => loadDetail(r.question_id));
      list.appendChild(card);
    }
  }
  list.querySelectorAll(".history-delete").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const qid = Number(btn.dataset.qid);
      if (!(await uiConfirm("删除该题全部作答记录？题目将保留，可重新作答。"))) return;
      await api(`/api/questions/${qid}/history`, { method: "DELETE" });
      applyFilters();
    });
  });
}

function padDate(n) {
  return String(n).padStart(2, "0");
}

function emptyState(title, hint) {
  return `<div class="card empty-state"><span class="empty-mark">○</span><p>${title}</p>${hint ? `<p class="meta">${hint}</p>` : ""}</div>`;
}

function difficultyStars(n) {
  const c = Math.max(0, Math.min(5, Number(n) || 1));
  return "★".repeat(c) + "☆".repeat(5 - c);
}

function fmtDate(y, m, d) {
  return `${y}-${padDate(m)}-${padDate(d)}`;
}

function ensureCalMonth() {
  const view = state.calViews[state.calendarMode];
  if (view) {
    state.calYear = view.year;
    state.calMonth = view.month;
    return;
  }
  const now = new Date();
  setCalMonth(now.getFullYear(), now.getMonth());
}

function setCalMonth(year, month) {
  state.calYear = year;
  state.calMonth = month;
  state.calViews[state.calendarMode] = { year, month };
}

function renderCalendar() {
  ensureCalMonth();
  const y = state.calYear;
  const m = state.calMonth;
  $("#calendar-title").textContent = `${y}年${m + 1}月`;
  const grid = $("#calendar-grid");
  grid.innerHTML = "";
  const today = fmtDate(new Date().getFullYear(), new Date().getMonth() + 1, new Date().getDate());
  const daysInMonth = new Date(y, m + 1, 0).getDate();
  const mondayOffset = (new Date(y, m, 1).getDay() + 6) % 7;
  const mode = state.calendarMode;
  $("#calendar-clear").textContent = mode === "history" ? "清除范围" : "回到今日";
  $("#calendar-hint").textContent =
    mode === "history"
      ? "点击选择开始日期，再次点击选择结束日期"
      : "圆点 = 该日有题（实心 = 已答）；点击日期查看该日题目";
  for (let i = 0; i < mondayOffset; i++) {
    const blank = document.createElement("span");
    blank.className = "calendar-cell blank";
    grid.appendChild(blank);
  }
  for (let d = 1; d <= daysInMonth; d++) {
    const date = fmtDate(y, m + 1, d);
    const cell = document.createElement("button");
    cell.type = "button";
    cell.className = "calendar-cell";
    cell.dataset.date = date;
    const hasQ = date in state.historyDone;
    cell.innerHTML = `${d}${hasQ ? '<span class="cal-dot"></span>' : ""}`;
    if (hasQ && state.historyDone[date]) cell.classList.add("done");
    if (date === today) cell.classList.add("today");
    if (mode === "history") {
      const from = state.rangeFrom;
      const to = state.rangeTo;
      if (from && to && date >= from && date <= to) cell.classList.add("in-range");
      if (date === from) cell.classList.add("range-start");
      if (date === to) cell.classList.add("range-end");
    } else if (state.todayDate === date) {
      cell.classList.add("range-start");
    }
    if (state.historyDates.has(date)) cell.classList.add("has-history");
    grid.appendChild(cell);
  }
}

function setRange(from, to) {
  state.rangeFrom = from;
  state.rangeTo = to;
  $("#filter-date-from").value = from || "";
  $("#filter-date-to").value = to || "";
  renderCalendar();
  applyFilters();
}

function renderYearPick() {
  $("#pick-year").textContent = state.calYear;
  const months = $("#pick-months");
  months.innerHTML = "";
  for (let i = 0; i < 12; i++) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "pick-month";
    btn.dataset.month = i;
    btn.textContent = `${i + 1}月`;
    if (i === state.calMonth) btn.classList.add("pick-current");
    months.appendChild(btn);
  }
}

function toggleYearPick() {
  const pick = $("#calendar-pick");
  if (pick.classList.contains("hidden")) {
    renderYearPick();
    pick.classList.remove("hidden");
  } else {
    pick.classList.add("hidden");
  }
}

async function runDaily() {
  const btn = $("#daily-run");
  btn.disabled = true;
  btn.textContent = "运行中…";
  try {
    await api("/api/daily/run", { method: "POST" });
    // 首次同步需真实 LLM 生成题目（可能 10-20 分钟），持续轮询直到有当日题
    for (let i = 0; i < 240; i++) {
      await sleep(5000);
      btn.textContent = `运行中… ${Math.round(((i + 1) * 5) / 60 * 10) / 10} 分钟`;
      const items = await api("/api/today");
      if (items.length) {
        await loadToday();
        notifyNewQuestions();
        return;
      }
    }
    await loadToday();
  } catch (e) {
    uiToast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "立即更新";
  }
}

function notifyNewQuestions() {
  if (!("Notification" in window)) return;
  if (Notification.permission === "default") Notification.requestPermission();
  if (Notification.permission === "granted") {
    new Notification("面试助手", { body: "每日题目已更新，快去练习吧" });
  }
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

document.querySelectorAll("nav button[data-view]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.body.classList.remove("sidebar-open");
    const target = btn.dataset.view;
    if (target === "review") {
      state.reviewTag = null;
      show("review");
      loadReviewHome();
    } else if (target === "bank") {
      state.bankPage = 1; // 导航重新进入题库：回到第一页（刷新/后退走 hash 恢复不受影响）
      show("bank");
    } else {
      show(target);
    }
  });
});

// 移动端：汉堡开合抽屉侧边栏；日历按钮切换日历面板
$("#sidebar-hamburger").addEventListener("click", () => {
  document.body.classList.toggle("sidebar-open");
});
$("#calendar-toggle").addEventListener("click", () => {
  const panel = $("#calendar-panel");
  panel.classList.toggle("hidden");
  if (!panel.classList.contains("hidden")) renderCalendar();
});
$("#calendar-close").addEventListener("click", () => {
  $("#calendar-panel").classList.add("hidden");
});
$("#daily-run").addEventListener("click", runDaily);
$("#logout-btn").addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (e) {
    // 网络失败也照常本地登出（token 一并清除）
  }
  state.token = null;
  localStorage.removeItem("token");
  resetPerUserState();
  showAuth();
});

// 上传页（题目列表 / 面经文本 / 简历）
const UPLOAD_DESC = {
  direct: "以 Q: 或列表行形式提供题目，入库后自动补标签/难度，反问闲聊自动过滤。",
  facejing: "提供面试经验文本，后台解析生成知识题/设计题（LLM 生成）。",
  resume: "提供简历文本，后台解析项目经历生成 project 深挖题；生成后先展示候选，校对确认后入库。",
};
let uploadFile = null;

$("#upload-btn").addEventListener("click", () => show("upload"));
document.querySelectorAll(".upload-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".upload-tab").forEach((b) => b.classList.remove("upload-active"));
    btn.classList.add("upload-active");
    state.uploadType = btn.dataset.uploadType;
    $("#upload-desc").textContent = UPLOAD_DESC[state.uploadType] || "";
    $("#upload-count-row").classList.toggle("hidden", state.uploadType !== "resume");
    $("#upload-candidates").classList.add("hidden");
    resetUploadStatus();
  });
});
function resetUploadStatus() {
  const box = $("#upload-status");
  box.innerHTML = "";
  box.classList.remove("upload-ok", "upload-err");
}
function setUploadStatus(html, isError) {
  const box = $("#upload-status");
  box.innerHTML = html;
  box.classList.toggle("upload-err", !!isError);
  box.classList.toggle("upload-ok", !isError);
}
function clearUploadSelection() {
  uploadFile = null;
  $("#upload-filename").textContent = "未选择文件";
  $("#upload-submit").disabled = true;
}
function uploadWithProgress(payload, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.setRequestHeader("Authorization", `Bearer ${state.token}`);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onload = () => {
      let data = {};
      try {
        data = JSON.parse(xhr.responseText || "{}");
      } catch (_) {
        reject(new Error("服务器响应异常"));
        return;
      }
      if (xhr.status === 401) {
        state.token = null;
        localStorage.removeItem("token");
        showAuth();
        reject(new Error("登录已过期，请重新登录"));
        return;
      }
      if (xhr.status < 200 || xhr.status >= 400) {
        reject(new Error(data.detail || data.error || `HTTP ${xhr.status}`));
        return;
      }
      resolve(data);
    };
    xhr.onerror = () => reject(new Error("网络错误，上传中断"));
    xhr.send(JSON.stringify(payload));
  });
}
$("#upload-pick").addEventListener("click", () => $("#upload-file").click());
$("#upload-file").addEventListener("change", (e) => {
  uploadFile = e.target.files[0] || null;
  e.target.value = "";
  if (!uploadFile) return;
  if (!/\.(md|txt|pdf|docx?)$/i.test(uploadFile.name)) {
    setUploadStatus("仅支持 .md/.txt/.pdf/.doc/.docx 文件", true);
    uploadFile = null;
    $("#upload-filename").textContent = "未选择文件";
    $("#upload-submit").disabled = true;
    return;
  }
  if (uploadFile.size > 20 * 1024 * 1024) {
    setUploadStatus("文件超过 20MB 限制，请压缩或分段上传", true);
    uploadFile = null;
    $("#upload-filename").textContent = "未选择文件";
    $("#upload-submit").disabled = true;
    return;
  }
  $("#upload-filename").textContent = `${uploadFile.name}（${(uploadFile.size / 1024).toFixed(1)} KB）`;
  $("#upload-submit").disabled = false;
});
$("#upload-submit").addEventListener("click", async () => {
  if (!uploadFile) return;
  const type = state.uploadType;
  const isBinary = /\.(pdf|docx?)$/i.test(uploadFile.name);
  const payload = { filename: uploadFile.name, type };
  if (isBinary) {
    const buf = await uploadFile.arrayBuffer();
    let bin = "";
    const bytes = new Uint8Array(buf);
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    payload.content_base64 = btoa(bin);
  } else {
    payload.content = await uploadFile.text();
  }
  if (type === "resume") {
    payload.count = Number($("#upload-count").value) || 5;
  }
  $("#upload-submit").disabled = true;
  try {
    const resp = await uploadWithProgress(payload, (pct) => {
      setUploadStatus(`上传中… ${pct}%`);
    });
    if (resp.mode === "direct") {
      clearUploadSelection();
      setUploadStatus(`已入库 ${resp.count} 题。<a href="#" id="upload-go-bank">去题库查看</a>`);
      $("#upload-go-bank").addEventListener("click", (ev) => {
        ev.preventDefault();
        show("bank");
      });
    } else if (resp.mode === "facejing") {
      clearUploadSelection();
      setUploadStatus("已提交，后台生成中…（完成后可在题库查看）");
    } else if (resp.mode === "resume") {
      await pollResumeCandidates(resp.token);
    } else if (resp.mode === "parsing") {
      await pollUploadStatus(resp.token);
    }
  } catch (err) {
    setUploadStatus(err.message, true);
  } finally {
    $("#upload-submit").disabled = false;
  }
});
async function pollUploadStatus(token) {
  const start = Date.now();
  setUploadStatus("文件解析中…（PDF/Word 解析约 10-60 秒）");
  for (let i = 0; i < 300; i++) {
    await sleep(2000);
    let data;
    try {
      data = await api(`/api/upload/status/${token}`);
    } catch (err) {
      setUploadStatus(err.message, true);
      return;
    }
    if (data.status === "failed") {
      setUploadStatus(`解析失败：${data.error || "未知错误"}`, true);
      return;
    }
    if (data.status === "done") {
      const elapsed = Math.round((Date.now() - start) / 1000);
      if (data.mode === "direct") {
        clearUploadSelection();
        setUploadStatus(`解析完成，已入库 ${data.count} 题（耗时 ${elapsed} 秒）。<a href="#" id="upload-go-bank">去题库查看</a>`);
        $("#upload-go-bank").addEventListener("click", (ev) => {
          ev.preventDefault();
          show("bank");
        });
      } else if (data.mode === "facejing") {
        clearUploadSelection();
        setUploadStatus(`解析完成（耗时 ${elapsed} 秒），已提交后台生成中…`);
      } else if (data.mode === "resume") {
        await pollResumeCandidates(data.candidates_token);
      }
      return;
    }
    setUploadStatus(`文件解析中… 已用 ${Math.round((Date.now() - start) / 1000)} 秒（PDF/Word 约 10-60 秒）`);
  }
  setUploadStatus("解析超时，请重试", true);
}
async function pollResumeCandidates(token) {
  state.resumeToken = token;
  const start = Date.now();
  setUploadStatus("简历解析中…（约 10-60 秒）");
  for (let i = 0; i < 120; i++) {
    await sleep(3000);
    let data;
    try {
      data = await api(`/api/upload/candidates/${token}`);
    } catch (err) {
      setUploadStatus(err.message, true);
      return;
    }
    if (data.status === "failed") {
      setUploadStatus(`解析失败：${data.error || "未知错误"}`, true);
      return;
    }
    if (data.status === "done") {
      renderCandidates(data.items);
      return;
    }
    setUploadStatus(`简历解析中… 已用 ${Math.round((Date.now() - start) / 1000)} 秒（约 10-60 秒）`);
  }
  setUploadStatus("解析超时，请稍后在题库查看或重试", true);
}
function renderCandidates(items) {
  state.resumeCandidates = items;
  setUploadStatus(`解析完成，共 ${items.length} 题，请校对后确认入库。`);
  const list = $("#candidate-list");
  list.innerHTML = "";
  items.forEach((item, idx) => {
    const card = document.createElement("div");
    card.className = "card candidate-card";
    card.innerHTML = `
      <div class="question-head">
        <span class="badge">project</span>
        <span class="badge">难度 ${difficultyStars(item.difficulty)}</span>
        <span class="badge">${(item.tags || []).join("、") || "无标签"}</span>
      </div>
      <textarea data-idx="${idx}" rows="2" class="candidate-stem">${escapeHtml(item.stem)}</textarea>`;
    list.appendChild(card);
  });
  $("#upload-candidates").classList.remove("hidden");
}
$("#candidate-confirm").addEventListener("click", async () => {
  const textareas = Array.from(document.querySelectorAll(".candidate-stem"));
  const items = textareas.map((ta, idx) => ({
    stem: ta.value.trim(),
    tags: (state.resumeCandidates[idx] || {}).tags || [],
    difficulty: (state.resumeCandidates[idx] || {}).difficulty || 1,
  }));
  const valid = items.filter((i) => i.stem.length >= 6);
  if (!valid.length) {
    setUploadStatus("没有有效的题目（题干过短）", true);
    return;
  }
  try {
    const resp = await api("/api/upload/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: state.resumeToken, items: valid }),
    });
    $("#upload-candidates").classList.add("hidden");
    clearUploadSelection();
    setUploadStatus("已提交入库（后台去重中）…完成后可在题库查看");
  } catch (err) {
    setUploadStatus(err.message, true);
  }
});

// 数据备份：导出（fetch blob 带 Authorization，裸 <a href> 会 401）；导入为 zip 文件
$("#export-btn").addEventListener("click", async (e) => {
  e.preventDefault();
  try {
    const resp = await fetch("/api/export", {
      headers: state.token ? { Authorization: `Bearer ${state.token}` } : {},
    });
    if (resp.status === 401) {
      state.token = null;
      localStorage.removeItem("token");
      resetPerUserState();
      showAuth();
      return;
    }
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      uiToast(body.detail || body.error || `HTTP ${resp.status}`, true);
      return;
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const cd = resp.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename="?([^";]+)"?/);
    a.download = m ? m[1] : "interview_backup.zip";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    uiToast("导出失败：" + err.message, true);
  }
});
$("#import-pick").addEventListener("click", () => $("#import-file").click());
$("#import-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  if (!/\.zip$/i.test(file.name)) {
    uiToast("仅支持 .zip 备份文件", true);
    return;
  }
  $("#import-filename").textContent = file.name;
  if (!(await uiConfirm("导入将覆盖当前全部数据（导入前会自动备份当前库）。确认继续？"))) return;
  const buf = await file.arrayBuffer();
  let bin = "";
  const bytes = new Uint8Array(buf);
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  try {
    const resp = await api("/api/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content_base64: btoa(bin) }),
    });
    if (resp.new_token) {
      // 导入库含当前用户 → 用新 token 保持登录态（否则刷新后 401 退出）
      state.token = resp.new_token;
      localStorage.setItem("token", resp.new_token);
    }
    uiToast(`恢复成功：题库 ${resp.questions} 题，页面即将刷新`);
    setTimeout(() => location.reload(), 1200);
  } catch (err) {
    uiToast(`导入失败：${err.message}`, true);
  }
});
$("#answer-submit").addEventListener("click", submitAnswer);
function goBack() {
  sessionStorage.removeItem("answer_state"); // 离开答题流程：清除恢复点
  if (state.returnTo === "review") {
    show("review");
    loadReview(state.reviewTag);
  } else if (state.returnTo === "bank") {
    show("bank");
    loadBank();
  } else {
    show("today");
  }
}

$("#answer-back").addEventListener("click", goBack);
$("#result-back").addEventListener("click", goBack);
$("#review-paper-btn").addEventListener("click", loadReviewPaper);
$("#review-back").addEventListener("click", () => {
  state.reviewTag = null;
  show("review");
  loadReviewHome();
});
$("#filter-done").addEventListener("change", (e) => {
  $("#filter-score").disabled = e.target.value !== "done";
  if (e.target.value !== "done") $("#filter-score").value = "all";
  applyFilters();
});
$("#filter-date-from").addEventListener("change", (e) => {
  const from = e.target.value || null;
  if (from && state.rangeTo && from > state.rangeTo) {
    setRange(from, null);
    return;
  }
  state.rangeFrom = from;
  renderCalendar();
  applyFilters();
});
$("#filter-date-to").addEventListener("change", (e) => {
  const to = e.target.value || null;
  if (to && state.rangeFrom && to < state.rangeFrom) {
    setRange(to, null);
    return;
  }
  state.rangeTo = to;
  renderCalendar();
  applyFilters();
});
$("#calendar-prev").addEventListener("click", () => {
  ensureCalMonth();
  let y = state.calYear;
  let m = state.calMonth - 1;
  if (m < 0) {
    m = 11;
    y -= 1;
  }
  setCalMonth(y, m);
  renderCalendar();
});
$("#calendar-next").addEventListener("click", () => {
  ensureCalMonth();
  let y = state.calYear;
  let m = state.calMonth + 1;
  if (m > 11) {
    m = 0;
    y += 1;
  }
  setCalMonth(y, m);
  renderCalendar();
});
$("#calendar-clear").addEventListener("click", () => {
  const now = new Date();
  if (state.calendarMode === "history") {
    setRange(null, null);
  } else {
    loadToday();
  }
  setCalMonth(now.getFullYear(), now.getMonth());
  renderCalendar();
});
$("#calendar-grid").addEventListener("click", (e) => {
  const cell = e.target.closest(".calendar-cell");
  if (!cell || !cell.dataset.date) return;
  const date = cell.dataset.date;
  if (state.calendarMode === "today") {
    const todayStr = fmtDate(
      new Date().getFullYear(),
      new Date().getMonth() + 1,
      new Date().getDate()
    );
    // 点「今天」不带 date 参数（服务端按自身时区取今日，避免浏览器/服务器时区差导致空列表）
    loadToday(date === todayStr ? undefined : date);
    renderCalendar();
    return;
  }
  if (!state.rangeFrom || state.rangeTo) {
    setRange(date, null);
  } else {
    const from = state.rangeFrom < date ? state.rangeFrom : date;
    const to = state.rangeFrom < date ? date : state.rangeFrom;
    setRange(from, to);
  }
});
$("#calendar-title").addEventListener("click", toggleYearPick);
$("#pick-year-prev").addEventListener("click", () => {
  setCalMonth(state.calYear - 1, state.calMonth);
  renderYearPick();
});
$("#pick-year-next").addEventListener("click", () => {
  setCalMonth(state.calYear + 1, state.calMonth);
  renderYearPick();
});
$("#pick-months").addEventListener("click", (e) => {
  const btn = e.target.closest(".pick-month");
  if (!btn) return;
  setCalMonth(state.calYear, Number(btn.dataset.month));
  $("#calendar-pick").classList.add("hidden");
  renderCalendar();
});
document.addEventListener("click", (e) => {
  const pick = $("#calendar-pick");
  if (!pick.classList.contains("hidden") && !e.target.closest("#calendar-panel")) {
    pick.classList.add("hidden");
  }
});
$("#filter-score").addEventListener("change", applyFilters);
$("#filter-type").addEventListener("change", applyFilters);
$("#filter-difficulty").addEventListener("change", applyFilters);
$("#filter-today-type").addEventListener("change", renderToday);
$("#filter-today-difficulty").addEventListener("change", renderToday);
$("#filter-review-type").addEventListener("change", renderReviewItems);
$("#filter-review-difficulty").addEventListener("change", renderReviewItems);
$("#filter-today-category").addEventListener("change", renderToday);
$("#filter-category").addEventListener("change", applyFilters);
$("#filter-review-category").addEventListener("change", renderReviewItems);
$("#filter-bank-type").addEventListener("change", () => {
  state.bankPage = 1;
  loadBank();
});
$("#filter-bank-difficulty").addEventListener("change", () => {
  state.bankPage = 1;
  loadBank();
});
$("#filter-bank-category").addEventListener("change", () => {
  state.bankPage = 1;
  loadBank();
});
$("#bank-search").addEventListener("input", () => {
  state.bankPage = 1;
  loadBank();
});
$("#bank-prev").addEventListener("click", () => {
  if (state.bankPage > 1) {
    state.bankPage -= 1;
    loadBank();
  }
});
$("#bank-next").addEventListener("click", () => {
  if (state.bankPage < state.bankTotalPages) {
    state.bankPage += 1;
    loadBank();
  }
});
$("#bank-goto-btn").addEventListener("click", gotoBankPage);
$("#bank-goto").addEventListener("keydown", (e) => {
  if (e.key === "Enter") gotoBankPage();
});
$("#bank-page-size").addEventListener("change", (e) => {
  state.bankPageSize = Number(e.target.value) || 20;
  state.bankPage = 1; // 换条数回第一页
  loadBank();
});
$("#detail-back").addEventListener("click", () => {
  sessionStorage.removeItem("detail_state"); // 离开详情页：清除恢复点
  show("history");
  applyFilters();
});
$("#detail-delete").addEventListener("click", deleteCurrentAttempt);

// 切换用户时重置所有用户态（登录/登出/401 过期均调用，防跨用户残留）
function resetPerUserState() {
  sessionStorage.removeItem("answer_state");
  sessionStorage.removeItem("detail_state");
  sessionStorage.removeItem("review_paper_tag");
  state.reviewRecommended = new Set();
  state.reviewTag = null;
  state.resumeToken = null;
  state.resumeCandidates = [];
  state.bankPage = 1;
  state.rangeFrom = null;
  state.rangeTo = null;
  state.todayDate = null;
  state.editingQuestion = null;
  state.historyGroups = [];
  $("#review-paper").innerHTML = "";
  $("#upload-candidates").classList.add("hidden");
  $("#question-modal").classList.add("hidden");
}

// --- 认证：登录/注册 ---
function showAuth() {
  state.user = null;
  resetPerUserState();
  applyRoleUI();
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  $("#view-auth").classList.remove("hidden");
  $("#calendar-panel").classList.add("hidden");
  $("#calendar-toggle").classList.add("hidden");
  $("#auth-error").textContent = "";
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
}
let authMode = "login";
$("#auth-tab-login").addEventListener("click", () => {
  authMode = "login";
  $("#auth-tab-login").classList.add("upload-active");
  $("#auth-tab-register").classList.remove("upload-active");
  $("#auth-submit").textContent = "登录";
  $("#auth-hint").textContent = "登录后开始刷题";
  $("#auth-error").textContent = "";
  $("#auth-focus-row").classList.add("hidden");
  $("#auth-lang-row").classList.add("hidden");
});
["#auth-username", "#auth-password"].forEach((sel) => {
  $(sel).addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("#auth-submit").click(); // 回车提交
  });
});
$("#auth-tab-register").addEventListener("click", () => {
  authMode = "register";
  $("#auth-tab-register").classList.add("upload-active");
  $("#auth-tab-login").classList.remove("upload-active");
  $("#auth-submit").textContent = "注册";
  $("#auth-hint").textContent = "首个注册用户为管理员（owner）；开放注册共 20 个名额";
  $("#auth-error").textContent = "";
  $("#auth-focus-row").classList.remove("hidden");
  $("#auth-lang-row").classList.add("hidden"); // 默认岗位为空，语言行隐藏
});
$("#auth-focus").addEventListener("change", () => {
  $("#auth-lang-row").classList.toggle("hidden", $("#auth-focus").value !== "backend");
});
$("#auth-submit").addEventListener("click", async () => {
  const username = $("#auth-username").value.trim();
  const password = $("#auth-password").value;
  if (!username || !password) {
    $("#auth-error").textContent = "请输入用户名和密码";
    return;
  }
  const path = authMode === "login" ? "/api/auth/login" : "/api/auth/register";
  const payload = { username, password };
  if (authMode === "register") {
    payload.focus = $("#auth-focus").value || null;
    payload.focus_lang = $("#auth-focus-lang").value || null;
  }
  try {
    const body = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(async (r) => {
      if (!r.ok) {
        const b = await r.json().catch(() => ({}));
        throw new Error(b.detail || b.error || `HTTP ${r.status}`);
      }
      return r.json();
    });
    state.token = body.token;
    localStorage.setItem("token", body.token);
    state.user = body.user;
    resetPerUserState();
    applyRoleUI();
    $("#auth-username").value = "";
    $("#auth-password").value = "";
    loadTagCategories();
    show("today");
  } catch (err) {
    $("#auth-error").textContent = err.message;
  }
});

// --- 求职设置（岗位 × 语言，只影响推荐排序） ---

$("#focus-btn").addEventListener("click", () => {
  const u = state.user || {};
  $("#focus-sel").value = u.focus || "";
  $("#focus-lang-sel").value = u.focus_lang || "";
  $("#focus-lang-row").classList.toggle("hidden", (u.focus || "") !== "backend");
  $("#focus-error").textContent = "";
  $("#focus-modal").classList.remove("hidden");
});
$("#focus-cancel").addEventListener("click", () => $("#focus-modal").classList.add("hidden"));
$("#focus-sel").addEventListener("change", () => {
  $("#focus-lang-row").classList.toggle("hidden", $("#focus-sel").value !== "backend");
});
$("#focus-save").addEventListener("click", async () => {
  try {
    const body = await api("/api/auth/profile", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        focus: $("#focus-sel").value || null,
        focus_lang: $("#focus-lang-sel").value || null,
      }),
    });
    state.user.focus = body.focus;
    state.user.focus_lang = body.focus_lang;
    $("#focus-modal").classList.add("hidden");
    uiToast("求职设置已保存，推荐将按新岗位调整");
  } catch (err) {
    $("#focus-error").textContent = err.message;
  }
});

function applyRoleUI() {
  const isOwner = state.user && state.user.role === "owner";
  $("#daily-run").classList.toggle("hidden", !isOwner);
  $("#upload-btn").classList.toggle("hidden", !isOwner);
  $("#reviewer-link").style.display = isOwner ? "flex" : "none";
}

async function initAuth() {
  if (!state.token) {
    showAuth();
    return;
  }
  // 网络抖动/服务器繁忙时退避重试，不直接丢登录页（登录已过期由 api() 内 401 分支处理）
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const me = await api("/api/auth/me");
      state.user = me;
      applyRoleUI();
      loadTagCategories();
      renderCalendar();
      const initial = location.hash.slice(1);
      if (HASHABLE.has(initial)) {
        show(initial);
        if (initial === "review") loadReviewHome();
      } else {
        show("today");
      }
      return;
    } catch (e) {
      if (String(e.message || "").includes("登录已过期")) return;
      if (attempt === 2) {
        showAuth();
        $("#auth-error").textContent = "连接服务器失败，请检查网络后刷新重试";
        return;
      }
      await sleep(1500 * (attempt + 1));
    }
  }
}

initAuth();

// --- 管理员编辑题目（owner） ---
async function openQuestionModal(id) {
  state.editingQuestion = id;
  $("#qm-error").textContent = "";
  const detail = await api(`/api/questions/${id}`);
  $("#qm-stem").value = detail.stem;
  $("#qm-tags").value = (detail.tags || []).join(", ");
  $("#qm-difficulty").value = detail.difficulty;
  $("#qm-good").value = (detail.good_criteria || []).join("\n");
  $("#qm-bad").value = (detail.bad_criteria || []).join("\n");
  $("#question-modal").classList.remove("hidden");
}
$("#qm-cancel").addEventListener("click", () => {
  $("#question-modal").classList.add("hidden");
});
$("#qm-save").addEventListener("click", async () => {
  const id = state.editingQuestion;
  if (id == null) return;
  const payload = {
    stem: $("#qm-stem").value.trim(),
    tags: $("#qm-tags").value.split(/[,，]/).map((s) => s.trim()).filter(Boolean).slice(0, 5),
    difficulty: Number($("#qm-difficulty").value),
    good_criteria: $("#qm-good").value.split("\n").map((s) => s.trim()).filter(Boolean),
    bad_criteria: $("#qm-bad").value.split("\n").map((s) => s.trim()).filter(Boolean),
  };
  try {
    await api(`/api/admin/questions/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    $("#question-modal").classList.add("hidden");
    loadBank();
  } catch (err) {
    $("#qm-error").textContent = err.message;
  }
});

// 侧边栏收起/展开（localStorage 记忆）
(function initSidebar() {
  const sidebar = $("#sidebar");
  const toggle = $("#sidebar-toggle");
  const apply = (collapsed) => {
    sidebar.classList.toggle("collapsed", collapsed);
    toggle.textContent = collapsed ? "☰" : "◀";
  };
  try {
    apply(localStorage.getItem("sidebar_collapsed") === "1");
  } catch (e) {
    apply(false);
  }
  toggle.addEventListener("click", () => {
    const collapsed = !sidebar.classList.contains("collapsed");
    apply(collapsed);
    try {
      localStorage.setItem("sidebar_collapsed", collapsed ? "1" : "0");
    } catch (e) {
      /* localStorage 不可用时仅本次生效 */
    }
  });
})();
