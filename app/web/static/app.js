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
  rangeFrom: null,
  rangeTo: null,
  todayDate: null,
  calendarMode: "today",
  uploadType: "direct",
  resumeToken: null,
  reviewRecommended: new Set(),
  calViews: {},
  calYear: null,
  calMonth: null,
  historyDates: new Set(),
  editingQuestion: null,
};

const $ = (sel) => document.querySelector(sel);

function show(view) {
  state.view = view;
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  $("#view-" + view).classList.remove("hidden");
  document.querySelectorAll("nav button[data-view]").forEach((btn) => {
    btn.classList.toggle("nav-active", btn.dataset.view === view);
  });
  const panel = $("#calendar-panel");
  if (view === "today" || view === "history") {
    state.calendarMode = view;
    panel.classList.remove("hidden");
    renderCalendar();
  } else {
    panel.classList.add("hidden");
  }
  if (view === "today") loadToday(state.todayDate || undefined);
  if (view === "bank") loadBank();
  if (view === "history") loadHistory();
}

async function api(path, options) {
  const opts = options || {};
  opts.headers = Object.assign(
    {},
    opts.headers || {},
    state.token ? { Authorization: `Bearer ${state.token}` } : {}
  );
  const resp = await fetch(path, opts);
  if (resp.status === 401) {
    state.token = null;
    localStorage.removeItem("token");
    showAuth();
    throw new Error("登录已过期，请重新登录");
  }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || body.error || `HTTP ${resp.status}`);
  }
  return resp.json();
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
    page_size: 20,
  });
  const typeFilter = $("#filter-bank-type").value;
  const categoryFilter = $("#filter-bank-category").value;
  const difficultyFilter = $("#filter-bank-difficulty").value;
  const keyword = $("#bank-search").value.trim();
  if (typeFilter !== "all") params.set("type", typeFilter);
  if (difficultyFilter !== "all") params.set("difficulty", difficultyFilter);
  if (categoryFilter !== "all") params.set("category", categoryFilter);
  if (keyword) params.set("q", keyword);
  const data = await api(`/api/bank?${params}`);
  state.bankTotal = data.total;
  state.bankTotalPages = data.total_pages;
  $("#bank-summary").textContent = `共 ${data.total} 题`;
  const list = $("#bank-list");
  list.innerHTML = "";
  if (!data.items.length) {
    list.innerHTML = '<div class="card"><p class="meta">无符合条件的题目</p></div>';
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
        ${badge}
        <span class="badge">${q.type}</span>
        <span class="badge">难度 ${difficultyStars(q.difficulty)}</span>
        ${adminBtns}
      </div>
      <p>${escapeHtml(q.stem)}</p>`;
    card.addEventListener("click", (e) => {
      if (e.target.closest(".admin-btn")) return;
      state.returnTo = "bank";
      startAnswer(q);
    });
    list.appendChild(card);
  }
  list.querySelectorAll(".admin-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const id = Number(btn.dataset.id);
      if (btn.dataset.action === "edit") {
        openQuestionModal(id);
      } else if (confirm("删除该题？将同时删除其全部作答记录。")) {
        api(`/api/admin/questions/${id}`, { method: "DELETE" }).then(() => loadBank());
      }
    });
  });
  const totalPages = Math.max(1, data.total_pages);
  $("#bank-page-info").textContent = `第 ${data.page} / ${totalPages} 页`;
  $("#bank-prev").disabled = data.page <= 1;
  $("#bank-next").disabled = data.page >= totalPages;
}

async function loadToday(date) {
  state.todayDate = date || null;
  const items = state.todayDate
    ? await api(`/api/today?date=${state.todayDate}`)
    : await api("/api/today");
  state.todayItems = items;
  renderToday();
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
    list.innerHTML = `<div class="card"><p class="meta">${state.todayDate ? "该日无题目" : "无符合条件的题目"}</p></div>`;
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
      alert(e.message);
      return;
    }
  }
  const detail = await api(`/api/questions/${q.id}`); // today 列表不含 criteria，答题页需详情
  state.question = { ...q, ...detail };
  renderAnswer();
  if (resumed) {
    // 断点恢复：拉取已有轮次并渲染时间线，继续作答（D18）
    const s = await api(`/api/sessions/${state.sessionId}`);
    if (s.status === "active" && s.rounds_done > 0) {
      renderChat(s.transcript || [], s.followup);
      $("#answer-status").textContent = `已恢复上次对话（已答 ${s.rounds_done} 轮），请在下方继续回答`;
    }
  }
  show("answer");
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
  for (let i = 0; i < 120; i++) {
    await sleep(1000);
    const s = await api(`/api/sessions/${state.sessionId}`);
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

async function deleteCurrentAttempt() {
  const idx = state.detailAttempts.findIndex(
    (a) => a.session_id === state.detailSessionId
  );
  const nth = state.detailAttempts.length - idx;
  if (!confirm(`删除第 ${nth} 次作答记录？此操作不可恢复。`)) return;
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

async function loadReviewHome() {
  const tags = await api("/api/review/tags");
  const entry = $("#review-entry");
  const list = $("#review-list");
  list.innerHTML = "";
  $("#review-title").textContent = "薄弱点复习";
  $("#review-back").classList.add("hidden");
  $("#review-filter").classList.add("hidden");
  const weak = tags.filter((t) => t.count > 0);
  const rest = tags.filter((t) => t.count === 0);
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
  html += '<p class="meta" style="margin-top:12px">全部主题：</p>';
  html += rest
    .map(
      (t) =>
        `<button class="weak-tag-entry" data-tag="${escapeHtml(t.tag)}">${escapeHtml(t.tag)}</button>`
    )
    .join("");
  entry.innerHTML = html;
  entry.querySelectorAll(".weak-tag-entry").forEach((btn) => {
    btn.addEventListener("click", () => loadReview(btn.dataset.tag));
  });
}

async function loadReview(tag) {
  state.reviewTag = tag;
  const items = await api(`/api/review?tag=${encodeURIComponent(tag)}`);
  state.reviewItems = items;
  const list = $("#review-list");
  const entry = $("#review-entry");
  entry.innerHTML = "";
  $("#review-title").textContent = `薄弱点复习：${tag}`;
  $("#review-back").classList.remove("hidden");
  $("#review-filter").classList.remove("hidden");
  $("#review-paper-card").classList.remove("hidden");
  $("#review-paper").innerHTML = "";
  $("#review-paper-btn").disabled = false;
  $("#review-paper-btn").textContent = "生成复习卷";
  renderReviewItems();
}

async function loadReviewPaper() {
  const btn = $("#review-paper-btn");
  btn.disabled = true;
  btn.textContent = "生成中…（约 10-30 秒）";
  try {
    const data = await api(`/api/review/paper?tag=${encodeURIComponent(state.reviewTag)}`);
    const box = $("#review-paper");
    if (!data.paper_html) {
      box.innerHTML = '<p class="meta">该标签暂无题目，无法生成复习卷</p>';
    } else {
      box.innerHTML = data.paper_html;
      state.reviewRecommended = new Set(data.recommended_ids || []);
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
    list.innerHTML = '<div class="card"><p class="meta">无符合条件的题目</p></div>';
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
  const groups = await api("/api/history");
  state.historyGroups = groups;
  state.historyDates = new Set(groups.map((g) => g.date));
  renderCalendar();
  applyFilters();
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
}

function renderHistory(groups) {
  const list = $("#history-list");
  list.innerHTML = "";
  if (!groups.length) {
    list.innerHTML = '<div class="card"><p class="meta">无符合条件的记录</p></div>';
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
      if (!confirm("删除该题全部作答记录？题目将保留，可重新作答。")) return;
      await api(`/api/questions/${qid}/history`, { method: "DELETE" });
      applyFilters();
    });
  });
}

function padDate(n) {
  return String(n).padStart(2, "0");
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
    mode === "history" ? "点击选择开始日期，再次点击选择结束日期" : "点击选择日期，查看该日题目";
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
    cell.textContent = d;
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
    alert(e.message);
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
    if (btn.dataset.view === "review") {
      state.reviewTag = null;
      show("review");
      loadReviewHome();
    } else {
      show(btn.dataset.view);
    }
  });
});
$("#daily-run").addEventListener("click", runDaily);

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
  const items = Array.from(document.querySelectorAll(".candidate-stem")).map((ta) => ({
    stem: ta.value.trim(),
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

// 数据备份：导入（导出为 <a download> 直链）
$("#import-pick").addEventListener("click", () => $("#import-file").click());
$("#import-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  if (!/\.zip$/i.test(file.name)) {
    alert("仅支持 .zip 备份文件");
    return;
  }
  $("#import-filename").textContent = file.name;
  if (!confirm("导入将覆盖当前全部数据（导入前会自动备份当前库）。确认继续？")) return;
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
    alert(`恢复成功：题库 ${resp.questions} 题，页面即将刷新`);
    location.reload();
  } catch (err) {
    alert(`导入失败：${err.message}`);
  }
});
$("#answer-submit").addEventListener("click", submitAnswer);
function goBack() {
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
    loadToday(date);
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
$("#detail-back").addEventListener("click", () => {
  show("history");
  applyFilters();
});
$("#detail-delete").addEventListener("click", deleteCurrentAttempt);

// --- 认证：登录/注册 ---
function showAuth() {
  state.user = null;
  applyRoleUI();
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  $("#view-auth").classList.remove("hidden");
  $("#calendar-panel").classList.add("hidden");
  $("#auth-error").textContent = "";
}
let authMode = "login";
$("#auth-tab-login").addEventListener("click", () => {
  authMode = "login";
  $("#auth-tab-login").classList.add("upload-active");
  $("#auth-tab-register").classList.remove("upload-active");
  $("#auth-submit").textContent = "登录";
  $("#auth-hint").textContent = "登录后开始刷题";
  $("#auth-error").textContent = "";
});
$("#auth-tab-register").addEventListener("click", () => {
  authMode = "register";
  $("#auth-tab-register").classList.add("upload-active");
  $("#auth-tab-login").classList.remove("upload-active");
  $("#auth-submit").textContent = "注册";
  $("#auth-hint").textContent = "首个注册用户为管理员（owner）；开放注册共 20 个名额";
  $("#auth-error").textContent = "";
});
$("#auth-submit").addEventListener("click", async () => {
  const username = $("#auth-username").value.trim();
  const password = $("#auth-password").value;
  if (!username || !password) {
    $("#auth-error").textContent = "请输入用户名和密码";
    return;
  }
  const path = authMode === "login" ? "/api/auth/login" : "/api/auth/register";
  try {
    const body = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
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
    applyRoleUI();
    $("#auth-username").value = "";
    $("#auth-password").value = "";
    loadTagCategories();
    show("today");
  } catch (err) {
    $("#auth-error").textContent = err.message;
  }
});

function applyRoleUI() {
  const isOwner = state.user && state.user.role === "owner";
  $("#daily-run").classList.toggle("hidden", !isOwner);
  $("#upload-btn").classList.toggle("hidden", !isOwner);
}

async function initAuth() {
  if (!state.token) {
    showAuth();
    return;
  }
  try {
    const me = await api("/api/auth/me");
    state.user = me;
    applyRoleUI();
    loadTagCategories();
    renderCalendar();
    show("today");
  } catch (e) {
    showAuth();
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
