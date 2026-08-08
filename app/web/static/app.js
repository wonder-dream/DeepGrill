"use strict";

const state = {
  view: "today",
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
};

const $ = (sel) => document.querySelector(sel);

function show(view) {
  state.view = view;
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  $("#view-" + view).classList.remove("hidden");
  document.querySelectorAll("nav button[data-view]").forEach((btn) => {
    btn.classList.toggle("nav-active", btn.dataset.view === view);
  });
  if (view === "today") loadToday();
  if (view === "bank") loadBank();
  if (view === "history") loadHistory();
}

async function api(path, options) {
  const resp = await fetch(path, options);
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

function filterByTypeCategory(items, typeFilter, categoryFilter) {
  const catTags = categoryTags(categoryFilter);
  return items.filter((i) => {
    if (typeFilter !== "all" && i.type !== typeFilter) return false;
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
  if (typeFilter !== "all") params.set("type", typeFilter);
  if (categoryFilter !== "all") params.set("category", categoryFilter);
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
    card.innerHTML = `
      <div class="question-head">
        ${badge}
        <span class="badge">${q.type}</span>
        <span class="badge">难度 ${q.difficulty}</span>
      </div>
      <p>${escapeHtml(q.stem)}</p>`;
    card.addEventListener("click", () => {
      state.returnTo = "bank";
      startAnswer(q);
    });
    list.appendChild(card);
  }
  const totalPages = Math.max(1, data.total_pages);
  $("#bank-page-info").textContent = `第 ${data.page} / ${totalPages} 页`;
  $("#bank-prev").disabled = data.page <= 1;
  $("#bank-next").disabled = data.page >= totalPages;
}

async function loadToday() {
  const items = await api("/api/today");
  state.todayItems = items;
  renderToday();
}

function renderToday() {
  const items = filterByTypeCategory(
    state.todayItems || [],
    $("#filter-today-type").value,
    $("#filter-today-category").value
  );
  const doneCount = items.filter((q) => q.done).length;
  const activeCount = items.filter((q) => !q.done && q.active_session_id).length;
  $("#today-summary").textContent = `${items.length} 题待做 · 已完成 ${doneCount}`;
  if (activeCount) {
    $("#today-summary").textContent += ` · ${activeCount} 题进行中`;
  }
  const list = $("#today-list");
  list.innerHTML = "";
  if (!items.length) {
    list.innerHTML = '<div class="card"><p class="meta">无符合条件的题目</p></div>';
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
        <span class="badge">难度 ${q.difficulty}</span>
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
  $("#answer-meta").textContent = `题型：${q.type} · 难度 ${q.difficulty} · 标签：${(q.tags || []).join(", ") || "无"}`;
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
  $("#detail-difficulty").textContent = "";
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
  renderReviewItems();
}

function renderReviewItems() {
  const items = filterByTypeCategory(
    state.reviewItems || [],
    $("#filter-review-type").value,
    $("#filter-review-category").value
  );
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
    card.innerHTML = `
      <div class="question-head">
        ${badge}
        <span class="badge">${q.type}</span>
        <span class="badge">难度 ${q.difficulty}</span>
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
  fillDateOptions(groups);
  applyFilters();
}

function fillDateOptions(groups) {
  const sel = $("#filter-date");
  sel.innerHTML = '<option value="all">全部</option>';
  for (const g of groups) {
    const opt = document.createElement("option");
    opt.value = g.date;
    opt.textContent = g.date;
    sel.appendChild(opt);
  }
}

function applyFilters() {
  const doneFilter = $("#filter-done").value;
  const dateFilter = $("#filter-date").value;
  const scoreFilter = $("#filter-score").value;
  const typeFilter = $("#filter-type").value;
  const categoryFilter = $("#filter-category").value;
  const groups = [];
  for (const g of state.historyGroups || []) {
    if (dateFilter !== "all" && g.date !== dateFilter) continue;
    let items = g.items;
    if (doneFilter !== "all") {
      items = items.filter((i) => i.done === (doneFilter === "done"));
    }
    if (doneFilter === "done" && scoreFilter !== "all") {
      items = items.filter((i) =>
        scoreFilter === "high" ? i.total_score >= 80 : (i.total_score ?? 0) < 80
      );
    }
    items = filterByTypeCategory(items, typeFilter, categoryFilter);
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
          <span class="badge">难度 ${r.difficulty}</span>
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
$("#filter-date").addEventListener("change", applyFilters);
$("#filter-score").addEventListener("change", applyFilters);
$("#filter-type").addEventListener("change", applyFilters);
$("#filter-today-type").addEventListener("change", renderToday);
$("#filter-review-type").addEventListener("change", renderReviewItems);
$("#filter-today-category").addEventListener("change", renderToday);
$("#filter-category").addEventListener("change", applyFilters);
$("#filter-review-category").addEventListener("change", renderReviewItems);
$("#filter-bank-type").addEventListener("change", () => {
  state.bankPage = 1;
  loadBank();
});
$("#filter-bank-category").addEventListener("change", () => {
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

loadTagCategories();
show("today");

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
