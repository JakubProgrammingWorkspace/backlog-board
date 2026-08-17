// Backlog board — vanilla JS, no build step. Read-only client: renders /api/state,
// re-renders on SSE "change" events from /api/events. No fetch ever mutates a task;
// tasks are edited by the agent (or by hand) as markdown files under backlog/.
(() => {
  "use strict";

  const STATUSES = ["investigate", "todo", "doing", "review", "done"];
  const PRIORITY_ORDER = { P1: 0, P2: 1, P3: 2 };

  const state = {
    data: null,
    search: "",
    epicFilter: "",
    priFilter: new Set(),
    groupByEpic: false,
    openId: null, // "T-014" or "E-002" currently shown in the drawer
  };

  const el = (sel) => document.querySelector(sel);
  const boardEl = el("#board");
  const epicsListEl = el("#epics-list");
  const adrBodyEl = el("#adr-body");
  const searchEl = el("#search");
  const epicFilterEl = el("#epic-filter");
  const connDot = el("#conn-dot");
  const projectNameEl = el("#project-name");
  const drawer = el("#drawer");
  const drawerBackdrop = el("#drawer-backdrop");
  const drawerContent = el("#drawer-content");
  const toastEl = el("#toast");
  const themeToggle = el("#theme-toggle");

  // ---- theme (light/dark switch; inline script in index.html already set the
  // initial data-theme before paint, this just wires the toggle + keeps it in sync) ----

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    themeToggle.textContent = theme === "dark" ? "☀️" : "🌙";
    themeToggle.setAttribute("aria-pressed", String(theme === "dark"));
    themeToggle.title = theme === "dark" ? "switch to light theme" : "switch to dark theme";
  }
  applyTheme(document.documentElement.dataset.theme || "light");
  themeToggle.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    localStorage.setItem("backlog-theme", next);
    applyTheme(next);
  });

  function escapeHtml(s) {
    return (s || "").replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  // path is backlog-dir-relative, e.g. "tasks/archive/T-003-x.md" vs "tasks/T-002-x.md" —
  // archived items still show wherever their status puts them (done column, Epics tab)
  // per the skill's "not a delete, just tidies the listing" design; this just makes that
  // otherwise-invisible fact visible instead of requiring someone to notice "archive/" in
  // a raw path string.
  function isArchived(path) {
    return (path || "").split("/").includes("archive");
  }

  // Working-days-since, mirroring the server's _business_days_since (serve.py) — used
  // only to render the auto-accept countdown chip; the server's own sweep is the one
  // that actually promotes review -> done, this is just making that clock visible.
  function businessDaysSince(isoDate, today) {
    const start = new Date(`${isoDate}T00:00:00`);
    if (Number.isNaN(start.getTime())) return null;
    let days = 0;
    const cur = new Date(start);
    while (cur < today) {
      cur.setDate(cur.getDate() + 1);
      const dow = cur.getDay();
      if (dow !== 0 && dow !== 6) days++;
    }
    return days;
  }

  // "review" chip: how long until the server's auto-accept sweep promotes this task to
  // done, counting from `updated:` (a human editing the review notes bumps that and
  // buys another day — see SKILL.md). null when auto-accept is off or the clock hasn't
  // started (no parseable `updated:`).
  function reviewCountdownLabel(t) {
    if (t.status !== "review" || !state.data.review_auto_accept) return null;
    const elapsed = businessDaysSince(t.updated, new Date());
    if (elapsed === null) return null;
    const remaining = state.data.review_after_days - elapsed;
    return remaining <= 0 ? "auto-accepts today" : `auto-accepts in ${remaining}d`;
  }

  function renderMarkdown(text) {
    if (window.marked) {
      return window.marked.parse(text || "", { breaks: true });
    }
    // vendor/marked.min.js missing — fall back to preformatted text rather than fail
    return `<pre>${escapeHtml(text)}</pre>`;
  }

  function showToast(msg) {
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => toastEl.classList.remove("show"), 1800);
  }

  // ---- filtering ----

  function taskMatches(t) {
    if (state.epicFilter && t.epic !== state.epicFilter) return false;
    if (state.priFilter.size && !state.priFilter.has(t.priority)) return false;
    if (state.search) {
      const hay = (t.id + " " + t.title + " " + t.body).toLowerCase();
      if (!hay.includes(state.search)) return false;
    }
    return true;
  }

  // ---- board tab ----

  function cardHtml(t) {
    const pct = t.total ? Math.round((100 * t.done) / t.total) : 0;
    const archived = isArchived(t.path);
    const sessionTag = t.status === "doing" && t.session_id
      ? ` · <span class="session-tag" title="session ${escapeHtml(t.session_id)}">${escapeHtml(t.session_id.slice(0, 8))}</span>` : "";
    const reviewLabel = reviewCountdownLabel(t);
    const reviewTag = reviewLabel
      ? ` · <span class="review-tag" title="auto-promotes to done unless reviewed first">${reviewLabel}</span>` : "";
    return `
      <div class="card" data-kind="task" data-id="${t.id}" tabindex="0" role="button"
           aria-label="${escapeHtml(t.id)}: ${escapeHtml(t.title)}${archived ? " (archived)" : ""}">
        <div class="card-top">
          <span class="card-id">${t.id}</span>
          <span class="pri-badge pri-${t.priority}">${t.priority}</span>
        </div>
        <div class="card-title">${escapeHtml(t.title)}</div>
        <div class="card-meta">${t.epic}${t.total ? ` · ${t.done}/${t.total}` : ""}${sessionTag}${reviewTag}${archived
          ? ` · <span class="archived-tag" title="moved to archive/ by retention — still counted, not deleted">archived</span>` : ""}</div>
        ${t.total ? `<div class="bar"><div style="width:${pct}%"></div></div>` : ""}
      </div>`;
  }

  function renderColumns(tasks, container) {
    for (const status of STATUSES) {
      const col = document.createElement("div");
      col.className = "col";
      const inStatus = tasks.filter((t) => t.status === status)
        .sort((a, b) => (PRIORITY_ORDER[a.priority] ?? 3) - (PRIORITY_ORDER[b.priority] ?? 3));
      col.innerHTML = `<h2>${status}<span>${inStatus.length}</span></h2>` +
        (inStatus.length ? inStatus.map(cardHtml).join("") : `<div class="empty">—</div>`);
      container.appendChild(col);
    }
  }

  function renderBoard() {
    const tasks = state.data.tasks.filter(taskMatches);
    boardEl.innerHTML = "";

    if (!state.groupByEpic) {
      renderColumns(tasks, boardEl);
      return;
    }
    const epicIds = [...new Set(tasks.map((t) => t.epic))];
    const titleOf = (id) => state.data.epics.find((e) => e.id === id)?.title || id;
    for (const epicId of epicIds) {
      const lane = document.createElement("div");
      lane.className = "lane";
      lane.innerHTML = `<div class="lane-title">${epicId} — ${escapeHtml(titleOf(epicId))}</div>`;
      const grid = document.createElement("div");
      grid.className = "board";
      renderColumns(tasks.filter((t) => t.epic === epicId), grid);
      lane.appendChild(grid);
      boardEl.appendChild(lane);
    }
  }

  // ---- epics tab ----

  function renderEpics() {
    epicsListEl.innerHTML = state.data.epics.map((e) => {
      const own = state.data.tasks.filter((t) => t.epic === e.id);
      const done = own.filter((t) => t.status === "done").length;
      const archived = isArchived(e.path);
      return `
        <div class="epic-card" data-kind="epic" data-id="${e.id}" tabindex="0" role="button"
             aria-label="${escapeHtml(e.id)}: ${escapeHtml(e.title)}${archived ? " (archived)" : ""}">
          <h3>${e.id} <span class="status-badge ${e.status}">${e.status}</span></h3>
          <div class="card-title">${escapeHtml(e.title)}</div>
          <div class="card-meta">${done}/${own.length} tasks done${archived
            ? ` · <span class="archived-tag" title="moved to archive/ by retention — still counted, not deleted">archived</span>` : ""}</div>
        </div>`;
    }).join("") || `<div class="empty">No epics yet.</div>`;
  }

  // ---- ADR tab ----

  function renderAdr() {
    adrBodyEl.innerHTML = renderMarkdown(state.data.adr);
  }

  // ---- epic filter <select> ----

  function renderEpicFilterOptions() {
    const current = epicFilterEl.value;
    epicFilterEl.innerHTML = `<option value="">all epics</option>` +
      state.data.epics.map((e) => `<option value="${e.id}">${e.id} — ${escapeHtml(e.title)}</option>`).join("");
    epicFilterEl.value = current && state.data.epics.some((e) => e.id === current) ? current : "";
    state.epicFilter = epicFilterEl.value;
  }

  // ---- drawer ----

  function openDrawer(kind, id) {
    const item = kind === "task"
      ? state.data.tasks.find((t) => t.id === id)
      : state.data.epics.find((e) => e.id === id);
    if (!item) return;
    state.openId = id;

    const archivedChip = isArchived(item.path)
      ? `<span class="drawer-chip archived-tag" title="moved to archive/ by retention — still counted, not deleted">archived</span>` : "";
    const sessionChip = kind === "task" && item.status === "doing" && item.session_id
      ? `<span class="drawer-chip session-tag" title="${escapeHtml(item.session_id)}">session ${escapeHtml(item.session_id.slice(0, 8))}</span>` : "";
    const reviewLabel = kind === "task" ? reviewCountdownLabel(item) : null;
    const reviewChip = reviewLabel
      ? `<span class="drawer-chip review-tag" title="auto-promotes to done unless reviewed first">${reviewLabel}</span>` : "";
    const chips = (kind === "task"
      ? `<span class="drawer-chip">${item.status}</span>
         <span class="drawer-chip pri-${item.priority}">${item.priority}</span>
         <span class="drawer-chip">${item.epic}</span>
         ${item.total ? `<span class="drawer-chip">${item.done}/${item.total} subtasks</span>` : ""}`
      : `<span class="drawer-chip status-badge ${item.status}">${item.status}</span>`) + sessionChip + reviewChip + archivedChip;

    const docsLink = item.docs
      ? `<div class="drawer-path">docs: <code>${escapeHtml(item.docs)}</code>
           <button type="button" id="drawer-docs-copy">copy path</button></div>` : "";
    const viewOnBoard = kind === "epic"
      ? `<button type="button" id="drawer-view-board" class="drawer-path-button">view on board →</button>` : "";

    drawerContent.innerHTML = `
      <h2>${item.id} ${escapeHtml(item.title)}</h2>
      <div class="drawer-chips">${chips}</div>
      <div class="drawer-path">
        <code id="drawer-file-path">${escapeHtml(item.path)}</code>
        <button type="button" id="drawer-copy">copy path</button>
      </div>
      ${docsLink}
      ${viewOnBoard}
      <div class="markdown-body">${renderMarkdown(item.body)}</div>
    `;
    el("#drawer-copy")?.addEventListener("click", () => {
      navigator.clipboard?.writeText(`${state.data.root}/backlog/${item.path}`);
      showToast("path copied");
    });
    // docs: is already project-root-relative (e.g. "docs/foo.md#anchor"), unlike
    // item.path which is backlog-dir-relative — no "backlog/" prefix here.
    el("#drawer-docs-copy")?.addEventListener("click", () => {
      navigator.clipboard?.writeText(`${state.data.root}/${item.docs}`);
      showToast("docs path copied");
    });
    el("#drawer-view-board")?.addEventListener("click", () => {
      state.epicFilter = item.id;
      epicFilterEl.value = item.id;
      renderBoard();
      el('.tab[data-tab="board"]').click();
      closeDrawer();
    });

    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    drawerBackdrop.classList.add("open");
  }

  function closeDrawer() {
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    drawerBackdrop.classList.remove("open");
    state.openId = null;
  }

  el("#drawer-close").addEventListener("click", closeDrawer);
  drawerBackdrop.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

  document.addEventListener("click", (e) => {
    const card = e.target.closest("[data-kind]");
    if (!card) return;
    openDrawer(card.dataset.kind, card.dataset.id);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const card = e.target.closest("[data-kind]");
    if (!card) return;
    e.preventDefault(); // stop Space from scrolling the page
    openDrawer(card.dataset.kind, card.dataset.id);
  });

  // ---- tabs ----

  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      el(`#tab-${btn.dataset.tab}`).classList.add("active");
    });
  });

  // ---- controls ----

  searchEl.addEventListener("input", () => {
    state.search = searchEl.value.trim().toLowerCase();
    renderBoard();
  });
  epicFilterEl.addEventListener("change", () => {
    state.epicFilter = epicFilterEl.value;
    renderBoard();
  });
  document.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const pri = chip.dataset.pri;
      if (state.priFilter.has(pri)) { state.priFilter.delete(pri); chip.classList.remove("active"); }
      else { state.priFilter.add(pri); chip.classList.add("active"); }
      renderBoard();
    });
  });
  el("#group-by-epic").addEventListener("change", (e) => {
    state.groupByEpic = e.target.checked;
    renderBoard();
  });

  // ---- render + fetch ----

  function renderAll() {
    projectNameEl.textContent = state.data.project || "Backlog";
    renderEpicFilterOptions();
    renderBoard();
    renderEpics();
    renderAdr();
    if (state.openId) {
      const kind = state.openId.startsWith("E-") ? "epic" : "task";
      const stillExists = (kind === "task" ? state.data.tasks : state.data.epics)
        .some((i) => i.id === state.openId);
      if (stillExists) openDrawer(kind, state.openId);
      else closeDrawer();
    }
  }

  // ---- move animation ----
  // FLIP technique: record each card's on-screen position before a re-render, then after
  // the DOM is rebuilt, invert the delta into a transform and let it transition to zero.
  // Only used for the live (SSE-triggered) reload — that's the one case where a card can
  // silently jump to a different column while the user's already looking at it; a plain
  // filter/search re-render doesn't need it.

  function captureCardRects() {
    const rects = new Map();
    document.querySelectorAll(".card[data-id]").forEach((el) => {
      rects.set(el.dataset.id, el.getBoundingClientRect());
    });
    return rects;
  }

  function animateCardMoves(prevRects) {
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    document.querySelectorAll(".card[data-id]").forEach((el) => {
      const before = prevRects.get(el.dataset.id);
      if (!before) return; // new card — nothing to animate from
      const after = el.getBoundingClientRect();
      const dx = before.left - after.left;
      const dy = before.top - after.top;
      if (!dx && !dy) return;
      el.style.transition = "none";
      el.style.transform = `translate(${dx}px, ${dy}px)`;
      requestAnimationFrame(() => {
        el.style.transition = "transform 320ms cubic-bezier(.2,.8,.2,1)";
        el.style.transform = "";
      });
    });
  }

  let lastVersion = -1;
  async function refresh({ silent } = {}) {
    const res = await fetch("/api/state", { cache: "no-store" });
    const data = await res.json();
    if (data.version === lastVersion) return;
    lastVersion = data.version;
    state.data = data;
    const prevRects = silent ? captureCardRects() : null;
    renderAll();
    if (prevRects) animateCardMoves(prevRects);
    if (silent) showToast("board updated");
  }

  function connectEvents() {
    const src = new EventSource("/api/events");
    src.onopen = () => {
      connDot.classList.add("live"); connDot.classList.remove("down");
      connDot.title = "live — updates push automatically";
    };
    src.onerror = () => {
      connDot.classList.remove("live"); connDot.classList.add("down");
      connDot.title = "disconnected — reconnecting…";
    };
    src.addEventListener("change", () => refresh({ silent: true }));
  }

  refresh().then(connectEvents).catch((err) => {
    connDot.classList.add("down");
    boardEl.innerHTML = `<div class="empty">Failed to load /api/state: ${escapeHtml(String(err))}</div>`;
  });
})();
