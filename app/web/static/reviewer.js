"use strict";

// 题库人工审核页：AI 参考（现有标签快照）+ 人工编辑 + 采用建议 + 批量操作 + 进度
const state = {
  token: localStorage.getItem("token") || null,
  page: 1,
  pageSize: 20,
  totalPages: 1,
  questions: [],
  categories: [],
  tags: [],
  reviewed: new Set(JSON.parse(localStorage.getItem("rv_reviewed") || "[]")),
};

const $ = (s) => document.querySelector(s);

async function api(path, options) {
  const opts = options || {};
  opts.headers = Object.assign(
    {},
    opts.headers || {},
    state.token ? { Authorization: `Bearer ${state.token}` } : {}
  );
  const resp = await fetch(path, opts);
  if (resp.status === 401) {
    localStorage.removeItem("token");
    location.href = "/";
    throw new Error("登录已过期");
  }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || body.error || `HTTP ${resp.status}`);
  }
  return resp.json();
}

function toast(msg, isError) {
  const el = $("#rv-toast");
  el.textContent = msg;
  el.classList.toggle("rv-toast-error", !!isError);
  el.classList.remove("hidden");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), 4000);
}

async function init() {
  if (!state.token) {
    location.href = "/";
    return;
  }
  try {
    await Promise.all([loadTags(), loadQuestions()]);
  } catch (e) {
    toast(e.message, true);
  }
}

async function loadTags() {
  const cats = await api("/api/tags");
  state.categories = cats;
  state.tags = cats.flatMap((c) => c.tags);
  const sel = $("#rv-filter-cat");
  sel.innerHTML = '<option value="all">全部</option>';
  for (const c of cats) {
    const opt = document.createElement("option");
    opt.value = c.name;
    opt.textContent = c.name;
    sel.appendChild(opt);
  }
}

function reviewedOf(q) {
  return state.reviewed.has(q.id);
}

async function loadQuestions() {
  const params = new URLSearchParams({
    page: state.page,
    page_size: state.pageSize,
  });
  const cat = $("#rv-filter-cat").value;
  const diff = $("#rv-filter-diff").value;
  const kw = $("#rv-search").value.trim();
  if (cat !== "all") params.set("category", cat);
  if (diff !== "all") params.set("difficulty", diff);
  if (kw) params.set("q", kw);
  const data = await api(`/api/bank?${params}`);
  state.totalPages = Math.max(1, data.total_pages);
  // 已审/未审：当前页前端过滤（进度存本地）
  const status = $("#rv-filter-status").value;
  let items = data.items;
  if (status === "done") items = items.filter((i) => reviewedOf(i));
  if (status === "todo") items = items.filter((i) => !reviewedOf(i));
  // 拉取建议与已审状态
  const ids = items.map((i) => i.id).join(",");
  const sugg = ids ? await api(`/api/review/suggestions?ids=${ids}`) : {};
  renderList(items, sugg.suggestions || {});
  updatePager(data);
  updateProgress();
}

function renderList(items, suggestions) {
  const list = $("#rv-list");
  list.innerHTML = "";
  for (const q of items) {
    const s = suggestions[q.id] || { suggested_tags: [], suggested_difficulty: 1, suggested_category: "" };
    const card = document.createElement("div");
    card.className = "card rv-card" + (reviewedOf(q) ? " rv-reviewed" : "");
    card.innerHTML = `
      <div class="rv-card-head">
        <input type="checkbox" class="rv-check" data-id="${q.id}">
        <span class="rv-id">#${q.id}</span>
        <span class="badge">${q.type}</span>
        <span class="badge">${reviewedOf(q) ? "已审核" : "未审核"}</span>
      </div>
      <p class="rv-stem">${escapeHtml(q.stem)}</p>
      <div class="rv-ref">AI 建议：${escapeHtml(s.suggested_category || "—")} ｜ ${(s.suggested_tags || []).map(escapeHtml).join("、") || "无"} ｜ 难度 ${"★".repeat(Math.max(1, Math.min(5, s.suggested_difficulty || 1)))}</div>
      <div class="rv-edit">
        <label>分类 <select class="rv-cat" data-id="${q.id}"></select></label>
        <label>标签 <input class="rv-tags-in" data-id="${q.id}" placeholder="逗号分隔，可自定义"></label>
        <label>难度 <select class="rv-diff" data-id="${q.id}">
          ${[1,2,3,4,5].map(d => `<option value="${d}">${"★".repeat(d)}</option>`).join("")}
        </select></label>
        <button class="rv-use-sugg" data-id="${q.id}">采用AI建议</button>
        <button class="rv-save" data-id="${q.id}">保存</button>
        <button class="rv-del rv-danger" data-id="${q.id}">删除</button>
      </div>`;
    list.appendChild(card);
    fillCatSelect(card.querySelector(".rv-cat"), s.suggested_category);
    card.querySelector(".rv-tags-in").value = (s.suggested_tags || []).join(", ");
    card.querySelector(".rv-diff").value = String(s.suggested_difficulty || 1);
  }
  bindCardEvents();
}

function fillCatSelect(sel, selected) {
  sel.innerHTML = '<option value="">（未选）</option>';
  for (const c of state.categories) {
    const opt = document.createElement("option");
    opt.value = c.name;
    opt.textContent = c.name;
    sel.appendChild(opt);
  }
  if (selected) sel.value = selected;
}

function bindCardEvents() {
  document.querySelectorAll(".rv-use-sugg").forEach((btn) => {
    btn.addEventListener("click", () => {
      const card = btn.closest(".rv-card");
      // AI 建议已在编辑区预填（打开页面时），采用即刷新为建议值
      toast("已采用 AI 建议（可再调整后保存）");
    });
  });
  document.querySelectorAll(".rv-save").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".rv-card");
      const id = Number(btn.dataset.id);
      const cat = card.querySelector(".rv-cat").value;
      const tagsRaw = card.querySelector(".rv-tags-in").value;
      const difficulty = Number(card.querySelector(".rv-diff").value);
      const tags = tagsRaw.split(/[,，]/).map((t) => t.trim()).filter(Boolean).slice(0, 5);
      const saveTags = cat ? tags.filter((t) => t) : tags;
      try {
        await api(`/api/admin/questions/${id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tags: saveTags, difficulty, reviewed: true }),
        });
        markReviewed(id);
        toast("已保存 #" + id);
      } catch (e) {
        toast(e.message, true);
      }
    });
  });
  document.querySelectorAll(".rv-del").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = Number(btn.dataset.id);
      if (!(await uiConfirm(`删除题目 #${id}？将连带删除其作答记录。`))) return;
      try {
        await api(`/api/admin/questions/${id}`, { method: "DELETE" });
        toast("已删除 #" + id);
        loadQuestions();
      } catch (e) {
        toast(e.message, true);
      }
    });
  });
}

function markReviewed(id) {
  state.reviewed.add(id);
  localStorage.setItem("rv_reviewed", JSON.stringify([...state.reviewed]));
  const card = document.querySelector(`.rv-card .rv-save[data-id="${id}"]`);
  if (card) card.closest(".rv-card").classList.add("rv-reviewed");
  updateProgress();
}

function updateProgress() {
  const total = state.totalPages ? state.totalPages * state.pageSize : 0;
  $("#rv-progress").textContent = `已审 ${state.reviewed.size} 题 / 题库 ${total} 题`;
}

function updatePager(data) {
  $("#rv-page-info").textContent = `第 ${data.page} / ${state.totalPages} 页（本页 ${data.items.length} 题）`;
  $("#rv-prev").disabled = data.page <= 1;
  $("#rv-next").disabled = data.page >= state.totalPages;
}

function uiConfirm(msg) {
  return window.confirm(msg);
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

$("#rv-prev").addEventListener("click", () => { if (state.page > 1) { state.page -= 1; loadQuestions(); } });
$("#rv-next").addEventListener("click", () => { if (state.page < state.totalPages) { state.page += 1; loadQuestions(); } });
["#rv-filter-cat", "#rv-filter-diff", "#rv-filter-status"].forEach((s) => {
  $(s).addEventListener("change", () => { state.page = 1; loadQuestions(); });
});
$("#rv-search").addEventListener("input", debounce(() => { state.page = 1; loadQuestions(); }, 400));

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// 批量
$("#rv-batch-apply").addEventListener("click", async () => {
  const ids = checkedIds();
  if (!ids.length) return toast("请先勾选题目", true);
  // 采用建议 = 编辑区已预填建议值，直接批量保存
  for (const id of ids) {
    const card = document.querySelector(`.rv-save[data-id="${id}"]`).closest(".rv-card");
    const tags = card.querySelector(".rv-tags-in").value.split(/[,，]/).map(t => t.trim()).filter(Boolean).slice(0, 5);
    const difficulty = Number(card.querySelector(".rv-diff").value);
    await api(`/api/admin/questions/${id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tags, difficulty, reviewed: true }) });
    markReviewed(id);
  }
  toast(`已批量采用并保存 ${ids.length} 题`);
  loadQuestions();
});

$("#rv-batch-save").addEventListener("click", async () => {
  const ids = checkedIds();
  if (!ids.length) return toast("请先勾选题目", true);
  for (const id of ids) {
    const card = document.querySelector(`.rv-save[data-id="${id}"]`).closest(".rv-card");
    const tags = card.querySelector(".rv-tags-in").value.split(/[,，]/).map(t => t.trim()).filter(Boolean).slice(0, 5);
    const difficulty = Number(card.querySelector(".rv-diff").value);
    await api(`/api/admin/questions/${id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tags, difficulty, reviewed: true }) });
    markReviewed(id);
  }
  toast(`已批量保存 ${ids.length} 题`);
  loadQuestions();
});

$("#rv-batch-delete").addEventListener("click", async () => {
  const ids = checkedIds();
  if (!ids.length) return toast("请先勾选题目", true);
  if (!uiConfirm(`确认删除勾选的 ${ids.length} 题？不可恢复。`)) return;
  let ok = 0;
  for (const id of ids) {
    try {
      await api(`/api/admin/questions/${id}`, { method: "DELETE" });
      state.reviewed.add(id);
      ok += 1;
    } catch (e) {
      toast(`#${id} 删除失败：${e.message}`, true);
    }
  }
  toast(`已删除 ${ok}/${ids.length} 题`);
  loadQuestions();
});

function checkedIds() {
  return [...document.querySelectorAll(".rv-check:checked")].map((c) => Number(c.dataset.id));
}

// 标签/分类管理
$("#rv-open-tags").addEventListener("click", async () => {
  $("#rv-tags-modal").classList.remove("hidden");
  await renderTagsTree();
});
$("#rv-tags-close").addEventListener("click", () => $("#rv-tags-modal").classList.add("hidden"));

const ROLE_LABELS = { backend: "后端", ai_app: "AI应用", ai_infra: "AI基础设施", frontend: "前端", qa: "测开" };

async function renderTagsTree() {
  const tree = $("#rv-tags-tree");
  const data = await api("/api/admin/tags/tree");
  const cats = await api("/api/tags");  // 带 roles/lang 标注
  const meta = new Map(cats.map((c) => [c.name, c]));
  const newCatSel = $("#rv-new-tag-cat");
  newCatSel.innerHTML = "";
  let html = "";
  for (const c of data) {
    newCatSel.innerHTML += `<option value="${c.id}">${c.name}</option>`;
    const m = meta.get(c.name) || {};
    const badge = (m.roles && m.roles.length ? ` [${m.roles.map((r) => ROLE_LABELS[r] || r).join("/")}]` : " [全岗位]")
      + (m.lang ? ` · ${m.lang}` : "");
    html += `<div class="rv-cat-node"><b>${escapeHtml(c.name)}</b><span class="rv-role-badge">${escapeHtml(badge)}</span> <button class="rv-del-cat" data-id="${c.id}">删分类</button>`;
    html += `<div class="rv-tag-node">${c.tags.map(t => `<span class="rv-tag-chip">${escapeHtml(t.name)} <button class="rv-del-tag" data-id="${t.id}">✕</button></span>`).join("")}</div></div>`;
  }
  tree.innerHTML = html;
  tree.querySelectorAll(".rv-del-tag").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const item = data.flatMap((c) => c.tags).find((t) => t.id === Number(btn.dataset.id));
      const name = item ? item.name : String(btn.dataset.id);
      if (!uiConfirm(`删除标签「${name}」？将连带清除题上该标签。`)) return;
      await api(`/api/admin/tags/${btn.dataset.id}`, { method: "DELETE" });
      toast(`已删除标签「${name}」`);
      loadTags();
      renderTagsTree();
    });
  });
  tree.querySelectorAll(".rv-del-cat").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const cat = data.find((c) => c.id === Number(btn.dataset.id));
      const name = cat ? cat.name : String(btn.dataset.id);
      if (!uiConfirm(`删除分类「${name}」？（非空分类将被拒绝）`)) return;
      const r = await api(`/api/admin/categories/${btn.dataset.id}`, { method: "DELETE" }).catch((e) => ({ error: e.message }));
      if (r.error) return toast(r.error, true);
      toast(`已删除分类「${name}」`);
      loadTags();
      renderTagsTree();
    });
  });
}

$("#rv-add-cat").addEventListener("click", async () => {
  const name = $("#rv-new-cat").value.trim();
  if (!name) return toast("请输入分类名", true);
  await api("/api/admin/categories", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
  toast(`已新增分类「${name}」`);
  $("#rv-new-cat").value = "";
  loadTags();
  renderTagsTree();
});

$("#rv-add-tag").addEventListener("click", async () => {
  const name = $("#rv-new-tag").value.trim();
  const catId = Number($("#rv-new-tag-cat").value);
  if (!name) return toast("请输入标签名", true);
  if (!catId) return toast("请先新增分类或选择分类", true);
  await api("/api/admin/tags", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, category_id: catId }) });
  toast(`已新增标签「${name}」`);
  $("#rv-new-tag").value = "";
  loadTags();
  renderTagsTree();
});

init();
