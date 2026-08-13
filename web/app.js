/* ============================================================
   Deep Agent Web Chat — UI/UX Pro 版 v2
   左侧历史会话栏 · 用户右/Agent 左 · Markdown + 代码高亮
   停止生成 · 复制 · 重新生成 · 时间戳 · 智能滚动
   会话持久化于 localStorage
   ============================================================ */

(() => {
  "use strict";

  // ---------- DOM ----------
  const $ = (sel) => document.querySelector(sel);
  const messagesEl = $("#messages");
  const inputEl = $("#input");
  const sendBtn = $("#send-btn");
  const sendIcon = $("#send-icon");
  const clearBtn = $("#clear-btn");
  const themeBtn = $("#theme-btn");
  const themeIcon = $("#theme-icon");
  const sidebar = $("#sidebar");
  const sidebarMask = $("#sidebar-mask");
  const sidebarToggle = $("#sidebar-toggle");
  const newSessionBtn = $("#new-session-btn");
  const sessionListEl = $("#session-list");
  const modelSelect = $("#model-select");
  const attachBtn = $("#attach-btn");
  const fileInput = $("#file-input");
  const attachListEl = $("#attach-list");

  // ---------- 状态 ----------
  const STORE_KEY = "deepagents_sessions_v1";
  let sessions = [];          // [{ id, title, createdAt, updatedAt, messages: [{role, content, time}] }]
  let currentId = null;
  let streaming = false;
  let abortCtrl = null;
  let stickBottom = true;
  let lastRole = null;
  let lastRender = 0;

  const SUFFIX = ["时", "分", "秒"];
  const pad = (n) => String(n).padStart(2, "0");
  const fmtTime = (d = new Date()) =>
    [d.getHours(), d.getMinutes(), d.getSeconds()].map((n, i) => pad(n) + SUFFIX[i]).join("");

  // ---------- 主题 ----------
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("theme", theme);
    themeIcon.innerHTML =
      theme === "dark"
        ? '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9z"/>'
        : '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>';
  }
  applyTheme(localStorage.getItem("theme") || "light");
  themeBtn.addEventListener("click", () =>
    applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark")
  );

  // ---------- 会话管理 ----------
  function loadSessions() {
    try { sessions = JSON.parse(localStorage.getItem(STORE_KEY)) || []; }
    catch { sessions = []; }
    sessions.forEach((s) => {
      s.messages = s.messages || [];
      // 兼容旧数据: 补齐服务端会话 threadId
      if (!s.threadId) s.threadId = newId();
    });
  }
  function saveSessions() {
    localStorage.setItem(STORE_KEY, JSON.stringify(sessions));
  }
  function currentSession() {
    return sessions.find((s) => s.id === currentId) || null;
  }
  function touchSession(s) { s.updatedAt = Date.now(); }

  function newId() {
    return "s" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
  }

  // ---------- 服务端会话同步 (跨设备持续保存) ----------
  const api = {
    async list() {
      const r = await fetch("/api/sessions");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    },
    async get(id) {
      const r = await fetch(`/api/sessions/${encodeURIComponent(id)}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    },
    async sync(s) {
      const r = await fetch("/api/sessions/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: s.id,
          thread_id: s.threadId,
          title: s.title,
          created_at: s.createdAt,
          updated_at: s.updatedAt,
          messages: s.messages,
        }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    },
    async del(id) {
      const r = await fetch(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    },
    // 已删除会话 id 列表 (tombstone): 其他客户端据此清理本地缓存, 防复活
    async deleted() {
      const r = await fetch("/api/sessions/deleted");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    },
  };

  // 服务端字段 → 前端会话对象
  function normalizeSession(row) {
    return {
      id: row.id,
      threadId: row.thread_id,
      title: row.title || "新会话",
      createdAt: row.created_at,
      updatedAt: row.updated_at,
      messages: Array.isArray(row.messages) ? row.messages : [],
    };
  }

  // 启动引导: 服务端为主, 本地 localStorage 为缓存, 按更新时间双向合并
  async function bootstrapSessions() {
    loadSessions();
    let server, deleted;
    try {
      [server, deleted] = await Promise.all([api.list(), api.deleted()]);
    } catch { return; } // 服务端不可用则用本地缓存

    const delIds = new Set((deleted || []).map((d) => d.id));

    // 其他端已删除的会话: 本地同步清理（防止"复活"）
    if (delIds.size) {
      const before = sessions.length;
      sessions = sessions.filter((s) => !delIds.has(s.id));
      if (sessions.length !== before) {
        if (currentId && delIds.has(currentId)) {
          currentId = sessions.length ? sessions[0].id : null; // 正在看的会话被删 → 切换
        }
        saveSessions();
      }
    }

    const serverById = new Map(server.map((s) => [s.id, s]));
    const localById = new Map(sessions.map((s) => [s.id, s]));

    // 服务端有、本地没有 → 拉详情补进本地（跨设备恢复）
    for (const row of server) {
      if (localById.has(row.id)) continue;
      try {
        const detail = await api.get(row.id);
        if (detail && !detail.error) sessions.push(normalizeSession(detail));
      } catch { /* 忽略单个失败 */ }
    }
    // 两边都有 → 取更新时间较新的一方（服务端新则拉详情覆盖, 本地新则推）
    for (const [id, local] of localById) {
      const row = serverById.get(id);
      if (!row) continue;
      if ((row.updated_at || 0) > (local.updatedAt || 0)) {
        try {
          const detail = await api.get(id);
          if (detail && !detail.error) {
            Object.assign(local, normalizeSession(detail));
          }
        } catch { /* 拉取失败则保留本地 */ }
      } else if ((local.updatedAt || 0) > (row.updated_at || 0)) {
        try { await api.sync(local); } catch { /* 忽略 */ }
      }
    }
    // 本地有、服务端没有 → 上传迁移（已被删除的旧数据不推回, 防复活）
    for (const s of sessions) {
      if (serverById.has(s.id)) continue;
      if (delIds.has(s.id)) continue;
      try { await api.sync(s); } catch { /* 忽略 */ }
    }
    saveSessions();
  }

  // 同步单个会话到服务端（失败静默, 下次再试）
  function syncSession(s) {
    api.sync(s).catch(() => {});
  }

  function createSession() {
    const s = {
      id: newId(),
      threadId: newId(), // 服务端会话保存的恢复维度
      title: "新会话",
      createdAt: Date.now(),
      updatedAt: Date.now(),
      messages: [],
    };
    sessions.push(s);
    saveSessions();
    syncSession(s);
    currentId = s.id;
    renderSessionList();
    renderMessages();
    closeSidebarMobile();
  }

  async function switchSession(id) {
    if (id === currentId) return;
    if (streaming && abortCtrl) abortCtrl.abort();
    currentId = id;
    // 本地无消息快照（换设备/清缓存场景）→ 从服务端恢复
    const s = currentSession();
    if (s && !s.messages.length) {
      try {
        const detail = await api.get(s.id);
        if (detail && !detail.error) {
          const remote = normalizeSession(detail);
          if (remote.messages.length) {
            s.messages = remote.messages;
            saveSessions();
          }
        }
      } catch { /* 保持空会话 */ }
    }
    renderMessages();
    renderSessionList();
    closeSidebarMobile();
  }

  async function deleteSession(id) {
    const s = sessions.find((x) => x.id === id);
    if (!s) return;
    const userCount = s.messages.filter((m) => m.role === "user").length;
    if (userCount > 0 && !confirm(`删除会话「${s.title}」？此操作不可恢复。`)) return;
    sessions = sessions.filter((x) => x.id !== id);
    saveSessions();
    api.del(id).catch(() => {});
    if (currentId === id) {
      // 切换到最近更新的会话, 没有则新建
      const next = sessions.slice().sort((a, b) => b.updatedAt - a.updatedAt)[0];
      if (next) currentId = next.id;
      else { createSession(); return; }
    }
    renderSessionList();
    renderMessages();
  }

  function updateTitle(s) {
    const first = s.messages.find((m) => m.role === "user");
    if (first) {
      const t = first.content.replace(/\s+/g, " ").trim();
      s.title = t.length > 18 ? t.slice(0, 18) + "…" : t;
    }
  }

  function fmtSessionTime(ts) {
    const d = new Date(ts);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    return sameDay ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : `${d.getMonth() + 1}-${pad(d.getDate())}`;
  }

  function renderSessionList() {
    sessionListEl.innerHTML = "";
    const sorted = sessions.slice().sort((a, b) => b.updatedAt - a.updatedAt);
    if (!sorted.length) {
      const empty = document.createElement("div");
      empty.className = "session-empty";
      empty.textContent = "还没有会话\n点上方「新会话」开始聊天";
      sessionListEl.appendChild(empty);
      return;
    }
    sorted.forEach((s) => {
      const item = document.createElement("div");
      item.className = "session-item" + (s.id === currentId ? " active" : "");
      item.setAttribute("role", "listitem");
      item.innerHTML = `
        <div class="session-icon">
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
          </svg>
        </div>
        <div class="session-info">
          <div class="session-title">${escapeHtml(s.title)}</div>
          <div class="session-time">${fmtSessionTime(s.updatedAt)}</div>
        </div>
        <button class="session-del" title="删除会话" aria-label="删除会话">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round" stroke-linejoin="round">
            <path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6h14z"/>
          </svg>
        </button>`;
      item.addEventListener("click", (e) => {
        if (e.target.closest(".session-del")) return;
        switchSession(s.id);
      });
      item.querySelector(".session-del").addEventListener("click", (e) => {
        e.stopPropagation();
        deleteSession(s.id);
      });
      sessionListEl.appendChild(item);
    });
  }

  // ---------- 工具 ----------
  const escapeHtml = (s) =>
    s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); return true; }
      catch { return false; }
      finally { document.body.removeChild(ta); }
    }
  }

  function flashCopied(btn) {
    const old = btn.innerHTML;
    btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
    btn.dataset.state = "copied";
    setTimeout(() => { btn.innerHTML = old; delete btn.dataset.state; }, 1400);
  }

  // ---------- 语法高亮 ----------
  const KEYWORDS = new Set(`
    def class import from return if elif else for while in not and or is lambda with as try except
    finally raise yield pass break continue global nonlocal function const let var async await new
    typeof instanceof this export extends super interface type enum implements private public
    protected static readonly package void switch case default do goto struct union namespace using
    include define main self int float double char bool string str list dict set tuple print assert
    func go defer select range nil map chan fn mut pub use mod impl trait match move loop
    throw catch delete in of typeof null true false undefined NaN Infinity
    echo cd ls mkdir rm cp mv sudo export source set unset pwd cat grep sed awk curl wget
    sizeof extern typedef signed unsigned long short const volatile static register
    extends implements final synchronized native abstract volatile transient strictfp
  `.trim().split(/\s+/));

  // 组: 1=注释 2=字符串(含三引号) 3=数字 4=布尔 5=函数调用 6=类名 7=标识符(关键字判断)
  const RE_COMMON =
    /(\/\/[^\n]*|\/\*[\s\S]*?\*\/|<!--[\s\S]*?-->)|("(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)|(\b\d[\d_.]*(?:[eE][+-]?\d+)?\b)|(\b(?:true|false|True|False|None|null|undefined|nil)\b)|(\b[A-Za-z_][A-Za-z0-9_]*\b(?=\s*\())|(\b[A-Z][A-Za-z0-9_]*\b)|(\b[A-Za-z_][A-Za-z0-9_]*\b)/g;
  const RE_PY =
    /(#[^\n]*|\/\/[^\n]*|\/\*[\s\S]*?\*\/|<!--[\s\S]*?-->)|("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)|(\b\d[\d_.]*(?:[eE][+-]?\d+)?\b)|(\b(?:true|false|True|False|None|null|undefined|nil)\b)|(\b[A-Za-z_][A-Za-z0-9_]*\b(?=\s*\())|(\b[A-Z][A-Za-z0-9_]*\b)|(\b[A-Za-z_][A-Za-z0-9_]*\b)/g;

  function highlightCode(code, lang) {
    const re = /^(py|python)/i.test(lang || "") ? RE_PY : RE_COMMON;
    let out = "";
    let last = 0;
    let m;
    while ((m = re.exec(code)) !== null) {
      out += escapeHtml(code.slice(last, m.index));
      let cls = null;
      if (m[1]) cls = "tok-com";
      else if (m[2]) cls = "tok-str";
      else if (m[3]) cls = "tok-num";
      else if (m[4]) cls = "tok-bool";
      else if (m[5]) cls = "tok-fn";
      else if (m[6]) cls = "tok-cls";
      else if (m[7] && KEYWORDS.has(m[0])) cls = "tok-kw";
      out += cls ? `<span class="${cls}">${escapeHtml(m[0])}</span>` : escapeHtml(m[0]);
      last = m.index + m[0].length;
    }
    out += escapeHtml(code.slice(last));
    return out;
  }

  // ---------- Markdown 渲染 ----------
  function inline(s) {
    return s
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/~~([^~]+)~~/g, "<del>$1</del>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>");
  }

  function renderTable(lines) {
    const head = lines[0].split("|").map((c) => c.trim()).filter(Boolean);
    const body = lines.slice(2).map((ln) => ln.split("|").map((c) => c.trim()).filter(Boolean));
    const cells = (arr, tag) => arr.map((c) => `<${tag}>${inline(escapeHtml(c))}</${tag}>`).join("");
    return (
      `<div style="overflow-x:auto"><table><thead><tr>${cells(head, "th")}</tr></thead>` +
      `<tbody>${body.map((r) => `<tr>${cells(r, "td")}</tr>`).join("")}</tbody></table></div>`
    );
  }

  function renderList(lines, ordered, tasks) {
    const tag = ordered ? "ol" : "ul";
    const items = lines
      .map((ln) => {
        const item = ln.replace(ordered ? /^\d+\.\s+/ : /^[-*+]\s+/, "");
        if (tasks) {
          const chk = item.match(/^\[([ xX])\]\s*/);
          if (chk) {
            const done = chk[1].toLowerCase() === "x";
            return `<li><span class="task-check">${done ? "☑" : "☐"}</span>${inline(escapeHtml(item.replace(/^\[[ xX]\]\s*/, "")))}</li>`;
          }
        }
        return `<li>${inline(escapeHtml(item))}</li>`;
      })
      .join("");
    return `<${tag}${tasks ? ' class="task-list"' : ""}>${items}</${tag}>`;
  }

  function buildCodeBlock(lang, code) {
    const box = document.createElement("div");
    box.className = "code-block";
    const head = document.createElement("div");
    head.className = "code-head";
    head.innerHTML = `<span class="code-lang">${escapeHtml(lang)}</span>`;
    const copyBtn = document.createElement("button");
    copyBtn.className = "code-copy";
    copyBtn.type = "button";
    copyBtn.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>复制';
    copyBtn.addEventListener("click", async () => {
      if (await copyText(code)) flashCopied(copyBtn);
    });
    head.appendChild(copyBtn);
    box.appendChild(head);
    const pre = document.createElement("pre");
    const codeEl = document.createElement("code");
    codeEl.innerHTML = highlightCode(code, lang);
    pre.appendChild(codeEl);
    box.appendChild(pre);
    return box.outerHTML;
  }

  function renderMarkdown(md) {
    const blocks = [];
    const fenceRe = /```(\w*)\n([\s\S]*?)```/g;
    let last = 0, m;
    while ((m = fenceRe.exec(md)) !== null) {
      if (m.index > last) blocks.push({ t: "text", s: md.slice(last, m.index) });
      blocks.push({ t: "code", lang: m[1] || "code", code: m[2].replace(/\n$/, "") });
      last = m.index + m[0].length;
    }
    // 流式输出: 尾部未闭合的 ```lang\n... 也按代码块渲染, 避免流式中闪成纯文本
    const tail = md.slice(last);
    const openFence = tail.match(/^```(\w*)\n([\s\S]*)$/);
    if (openFence) {
      blocks.push({ t: "code", lang: openFence[1] || "code", code: openFence[2].replace(/\n$/, "") });
      last = md.length;
    }
    if (last < md.length) blocks.push({ t: "text", s: md.slice(last) });

    return blocks
      .map((b) => {
        if (b.t === "code") return buildCodeBlock(b.lang, b.code);
        return b.s
          .split(/\n{2,}/)
          .map((para) => {
            const lines = para.split("\n");
            const first = lines[0];
            const t = para.trim();
            if (!t) return "";
            const h = t.match(/^(#{1,4})\s+(.*)$/);
            if (h) return `<h${h[1].length}>${inline(escapeHtml(h[2]))}</h${h[1].length}>`;
            if (/^-{3,}$|^\*{3,}$/.test(t)) return "<hr>";
            if (first.startsWith(">")) {
              const q = lines.map((l) => l.replace(/^\s*>\s?/, "")).join(" ");
              return `<blockquote>${inline(escapeHtml(q))}</blockquote>`;
            }
            if (first.includes("|") && lines[1] && /^\|?[\s:|-]+\|?$/.test(lines[1].trim())) {
              return renderTable(lines);
            }
            if (/^[-*+]\s/.test(first)) {
              const tasks = lines.every((l) => /^[-*+]\s+\[[ xX]\]/.test(l));
              return renderList(lines, false, tasks);
            }
            if (/^\d+\.\s/.test(first)) return renderList(lines, true, false);
            const p = escapeHtml(t).split("\n").map(inline).join("<br>");
            return `<p>${p}</p>`;
          })
          .join("");
      })
      .join("");
  }

  // ---------- 消息 DOM (数据驱动) ----------
  function scrollToBottom() {
    if (stickBottom) messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function addRow(role, time) {
    const grouped = role === lastRole;
    lastRole = role;
    const row = document.createElement("div");
    row.className = `row ${role}${grouped ? " grouped" : ""}`;
    const avatar = document.createElement("div");
    avatar.className = "avatar " + (role === "ai" ? "ai" : "me");
    avatar.textContent = role === "ai" ? "✦" : "我";
    avatar.setAttribute("aria-hidden", "true");
    const wrap = document.createElement("div");
    wrap.className = "bubble-wrap";
    const bubble = document.createElement("div");
    bubble.className = `bubble ${role}`;
    const metaRow = document.createElement("div");
    metaRow.className = "meta-row";
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = time || fmtTime();
    metaRow.appendChild(meta);
    wrap.append(bubble, metaRow);
    row.append(avatar, wrap);
    messagesEl.appendChild(row);
    return { row, bubble, wrap, metaRow };
  }

  function addUserMessage(text, attaches = []) {
    const { bubble } = addRow("user");
    bubble.textContent = text;
    // 附件随消息显示在右侧气泡内
    if (attaches.length) {
      const list = document.createElement("div");
      list.className = "msg-attaches";
      attaches.forEach((a) => {
        const chip = document.createElement("span");
        chip.className = "msg-attach";
        chip.textContent = `📎 ${a.name}`;
        chip.title = `${a.name} (${fmtSize(a.size)})`;
        list.appendChild(chip);
      });
      bubble.appendChild(list);
    }
    // 写入会话
    const s = currentSession();
    if (s) {
      s.messages.push({ role: "user", content: text, time: fmtTime(), attachments: attaches });
      touchSession(s);
      updateTitle(s);
      saveSessions();
      syncSession(s); // 用户消息即时同步（即使中途停止也不丢）
      renderSessionList();
    }
    scrollToBottom();
  }

  function addAiThinking() {
    const { bubble, wrap } = addRow("ai");
    const tip = document.createElement("div");
    tip.className = "thinking";
    tip.innerHTML = "<span></span><span></span><span></span>";
    bubble.appendChild(tip);
    scrollToBottom();
    return { bubble, wrap };
  }

  function addAiActions(wrap, text, onRegen) {
    const metaRow = wrap.querySelector(".meta-row");
    const bar = document.createElement("div");
    bar.className = "msg-actions";
    bar.setAttribute("role", "toolbar");
    bar.setAttribute("aria-label", "消息操作");
    const copyBtn = document.createElement("button");
    copyBtn.className = "act-btn";
    copyBtn.type = "button";
    copyBtn.title = "复制回答";
    copyBtn.setAttribute("aria-label", "复制回答");
    copyBtn.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
    copyBtn.addEventListener("click", async () => {
      if (await copyText(text)) flashCopied(copyBtn);
    });
    const regenBtn = document.createElement("button");
    regenBtn.className = "act-btn";
    regenBtn.type = "button";
    regenBtn.title = "重新生成";
    regenBtn.setAttribute("aria-label", "重新生成");
    regenBtn.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>';
    regenBtn.addEventListener("click", () => onRegen && onRegen());
    bar.append(copyBtn, regenBtn);
    metaRow.appendChild(bar);
  }

  function removeLastAiRow() {
    const rows = messagesEl.querySelectorAll(".row.ai");
    const last = rows[rows.length - 1];
    if (last) last.remove();
    lastRole = null;
    const remaining = messagesEl.querySelectorAll(".row");
    const lastRow = remaining[remaining.length - 1];
    if (lastRow) lastRole = lastRow.classList.contains("user") ? "user" : "ai";
  }

  // 全量渲染当前会话消息 (切换会话 / 初始化 / 清空后)
  function renderMessages() {
    messagesEl.innerHTML = "";
    lastRole = null;
    const s = currentSession();
    if (!s || !s.messages.length) { renderWelcome(); return; }
    s.messages.forEach((msg) => {
      if (msg.role === "user") {
        const { bubble } = addRow("user", msg.time);
        bubble.textContent = msg.content;
        if (msg.attachments && msg.attachments.length) {
          const list = document.createElement("div");
          list.className = "msg-attaches";
          msg.attachments.forEach((a) => {
            const chip = document.createElement("span");
            chip.className = "msg-attach";
            chip.textContent = `📎 ${a.name}`;
            chip.title = `${a.name} (${fmtSize(a.size)})`;
            list.appendChild(chip);
          });
          bubble.appendChild(list);
        }
      } else {
        const { bubble, wrap } = addRow("ai", msg.time);
        const md = document.createElement("div");
        md.className = "md";
        md.innerHTML = renderMarkdown(msg.content);
        bubble.appendChild(md);
        addAiActions(wrap, msg.content, () => {
          // 重新生成: 移除该条并重发
          const s2 = currentSession();
          if (!s2) return;
          s2.messages.pop();
          saveSessions();
          syncSession(s2); // 同步删除服务端旧 AI 消息
          removeLastAiRow();
          sendMessage(lastUserText(s2), { isRegen: true });
        });
      }
    });
    scrollToBottom(false);
  }

  function lastUserText(s) {
    for (let i = s.messages.length - 1; i >= 0; i--) {
      if (s.messages[i].role === "user") return s.messages[i].content;
    }
    return "";
  }

  function renderWelcome() {
    const w = document.createElement("div");
    w.className = "welcome";
    w.innerHTML = `
      <div class="welcome-logo">✦</div>
      <h2>你好，我是 Deep Agent</h2>
      <p>基于 deepagents 构建的智能助手 —— 可以读写文件、执行命令、规划并拆解任务。<br>
        模型: <span class="model-badge">deepseek-v4-flash</span></p>
      <div class="suggestions">
        <button class="chip" data-q="帮我写一个 Python 快速排序">写个快速排序</button>
        <button class="chip" data-q="现在几点了？">现在几点了？</button>
        <button class="chip" data-q="你能做什么？介绍一下你的能力">你能做什么？</button>
      </div>`;
    messagesEl.appendChild(w);
    w.querySelectorAll(".chip").forEach((c) =>
      c.addEventListener("click", () => {
        inputEl.value = c.dataset.q;
        updateSendState();
        submit();
      })
    );
  }

  // ---------- 模型切换 ----------
  let currentModel = localStorage.getItem("model") || "deepseek-v4-flash";
  modelSelect.value = currentModel;
  modelSelect.addEventListener("change", () => {
    currentModel = modelSelect.value;
    localStorage.setItem("model", currentModel);
  });

  // ---------- 附件上传 ----------
  const attachments = []; // [{name, path, size, status, el}]
  const MAX_FILE_SIZE = 5 * 1024 * 1024;

  function fmtSize(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  function renderAttachments() {
    attachListEl.innerHTML = "";
    attachments.forEach((a) => {
      const chip = document.createElement("div");
      chip.className = "attach-chip" + (a.status === "uploading" ? " uploading" : "");
      const name = document.createElement("span");
      name.className = "ac-name";
      name.textContent = a.name;
      const meta = document.createElement("span");
      meta.className = "ac-meta";
      meta.textContent = a.status === "uploading" ? "上传中…" : (a.status === "error" ? "失败" : fmtSize(a.size));
      const del = document.createElement("button");
      del.className = "ac-del";
      del.type = "button";
      del.title = "移除附件";
      del.textContent = "✕";
      del.addEventListener("click", () => {
        const i = attachments.indexOf(a);
        if (i >= 0) attachments.splice(i, 1);
        renderAttachments();
      });
      chip.append(name, meta, del);
      a.el = chip;
      attachListEl.appendChild(chip);
    });
    attachListEl.style.display = attachments.length ? "" : "none";
  }

  attachBtn.addEventListener("click", () => fileInput.click());

  fileInput.addEventListener("change", async () => {
    const files = [...fileInput.files];
    fileInput.value = "";
    for (const f of files) {
      if (f.size > MAX_FILE_SIZE) {
        alert(`附件「${f.name}」超过 5MB 限制，已跳过`);
        continue;
      }
      const a = { name: f.name, path: "", size: f.size, status: "uploading" };
      attachments.push(a);
      renderAttachments();
      try {
        const fd = new FormData();
        fd.append("file", f);
        const resp = await fetch("/api/upload", { method: "POST", body: fd });
        const data = await resp.json();
        if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
        a.path = data.path;
        a.size = data.size;
        if (data.parsed_path) a.parsedPath = data.parsed_path;
        a.status = "done";
      } catch (err) {
        a.status = "error";
        a.meta = err.message;
      }
      renderAttachments();
    }
  });

  function attachmentsPayload() {
    return attachments
      .filter((a) => a.status === "done")
      .map((a) => ({ name: a.name, path: a.path, size: a.size, parsed_path: a.parsedPath || undefined }));
  }

  // ---------- 发送 / 流式接收 ----------
  // 服务端会话保存模式: 只传 thread_id, 完整上下文由 checkpointer 恢复
  function threadPayload() {
    const s = currentSession();
    return s ? s.threadId : "";
  }

  function pushAssistant(raw) {
    const s = currentSession();
    if (!s) return;
    s.messages.push({ role: "assistant", content: raw, time: fmtTime() });
    touchSession(s);
    saveSessions();
    renderSessionList();
    syncSession(s); // 消息快照同步到服务端（跨设备恢复用）
  }

  async function sendMessage(text, { isRegen = false } = {}) {
    if (streaming) return;
    streaming = true;
    abortCtrl = new AbortController();
    setSendUI(true);

    // 附件随消息发出: 快照 → 渲染进用户气泡 → 立即清空输入框上方列表
    const attachSnapshot = attachmentsPayload();
    if (!isRegen) addUserMessage(text, attachSnapshot);
    attachments.length = 0;
    renderAttachments();

    const { bubble, wrap } = addAiThinking();

    let raw = "";
    let hasOutput = false;
    let mdEl = null;
    let toolEl = null;
    const toolCalls = new Map(); // id -> {name, input, output, status, el}

    const render = () => {
      if (!hasOutput) return;
      if (!mdEl) {
        bubble.textContent = "";
        mdEl = document.createElement("div");
        mdEl.className = "md";
        bubble.appendChild(mdEl);
      }
      mdEl.innerHTML = renderMarkdown(raw);
      scrollToBottom();
    };

    // 工具图标映射
    const TOOL_ICONS = {
      read_file: "📖", write_file: "✏️", edit_file: "📝", delete: "🗑",
      ls: "📁", glob: "🔍", grep: "🔎", execute: "⚡", task: "🧩",
      save_memory: "🧠", delete_memory: "🧠", list_memories: "📋",
      get_current_time: "🕐",
    };

    // 渲染工具调用卡片（重建工具区）
    const renderTools = () => {
      if (!toolEl) return;
      toolEl.innerHTML = "";
      toolCalls.forEach((t) => {
        const item = document.createElement("div");
        item.className = "tool-call " + t.status;
        const icon = document.createElement("span");
        icon.className = "tc-icon";
        icon.textContent = TOOL_ICONS[t.name] || "🔧";
        const name = document.createElement("span");
        name.className = "tc-name";
        name.textContent = t.name;
        const input = document.createElement("span");
        input.className = "tc-input";
        input.textContent = t.input;
        const status = document.createElement("span");
        status.className = "tc-status " + t.status;
        status.textContent = t.status === "running" ? "运行中…" : "✓";
        item.append(icon, name, input, status);
        if (t.status === "done" && t.output) {
          const details = document.createElement("details");
          details.className = "tc-detail";
          const sum = document.createElement("summary");
          sum.textContent = "查看输出";
          const pre = document.createElement("pre");
          pre.textContent = t.output;
          details.append(sum, pre);
          item.appendChild(details);
        }
        toolEl.appendChild(item);
      });
      scrollToBottom();
    };

    // 注册工具调用（id 去重: 同名工具多次调用）
    const upsertTool = (id, patch) => {
      const t = toolCalls.get(id) || { name: "", input: "", output: null, status: "running" };
      Object.assign(t, patch);
      toolCalls.set(id, t);
      renderTools();
    };

    // 最终回答时移除工具过程展示（仅过程中可见）
    const clearTools = () => {
      if (toolEl) {
        toolEl.remove();
        toolEl = null;
        toolCalls.clear();
      }
    };

    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, thread_id: threadPayload(), model: currentModel, attachments: attachSnapshot }),
        signal: abortCtrl.signal,
      });
      if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop();
        for (const part of parts) {
          if (!part.startsWith("data: ")) continue;
          const data = JSON.parse(part.slice(6));
          if (data.type === "token") {
            if (!hasOutput) { hasOutput = true; if (!toolCalls.size) bubble.textContent = ""; }
            raw += data.content;
            const now = performance.now();
            if (now - lastRender > 80) { render(); lastRender = now; }
            else if (!mdEl) { bubble.textContent = raw + "▍"; }
          } else if (data.type === "tool_start") {
            if (!hasOutput) { hasOutput = true; bubble.textContent = ""; }
            if (!toolEl) {
              toolEl = document.createElement("div");
              toolEl.className = "tool-calls";
              bubble.appendChild(toolEl);
            }
            upsertTool(data.id, { name: data.name, input: data.input || "", status: "running" });
          } else if (data.type === "tool_end") {
            // 后端在 tool_end 里回传真实工具名 (ToolMessage.name), 覆盖占位名
            upsertTool(data.id, { name: data.name, status: "done", output: data.output || "" });
          } else if (data.type === "error") {
            throw new Error(data.content);
          }
        }
      }

      if (hasOutput) {
        render();
        clearTools(); // 最终回答时不展示工具调用过程
        pushAssistant(raw);
        addAiActions(wrap, raw, () => {
          const s = currentSession();
          if (!s) return;
          s.messages.pop();
          saveSessions();
          syncSession(s); // 同步删除服务端旧 AI 消息
          removeLastAiRow();
          sendMessage(text, { isRegen: true });
        });
      } else {
        bubble.textContent = "(未收到回复)";
      }
    } catch (err) {
      if (err.name === "AbortError") {
        if (hasOutput) {
          render();
          clearTools(); // 有部分输出时同样收起工具过程
          pushAssistant(raw);
          addAiActions(wrap, raw);
        } else {
          bubble.textContent = "⏹ 已停止";
        }
      } else {
        bubble.textContent = "";
        bubble.classList.add("err");
        const msg = document.createElement("p");
        msg.style.color = "var(--err-text)";
        msg.textContent = "⚠️ " + err.message;
        bubble.appendChild(msg);
        const retry = document.createElement("button");
        retry.className = "retry-btn";
        retry.type = "button";
        retry.textContent = "↻ 重试";
        retry.addEventListener("click", () => {
          removeLastAiRow();
          sendMessage(text, { isRegen: true });
        });
        bubble.appendChild(retry);
      }
    } finally {
      streaming = false;
      abortCtrl = null;
      setSendUI(false);
      inputEl.focus();
      scrollToBottom();
    }
  }

  // ---------- 发送按钮 UI ----------
  function setSendUI(streamingNow) {
    sendBtn.disabled = streamingNow ? false : !inputEl.value.trim();
    sendBtn.classList.toggle("stop", streamingNow);
    sendBtn.title = streamingNow ? "停止生成" : "发送";
    sendBtn.setAttribute("aria-label", streamingNow ? "停止生成" : "发送");
    sendIcon.innerHTML = streamingNow
      ? '<rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" stroke="none"/>'
      : '<path d="M22 2 11 13M22 2l-7 20-4-9-9-4 20-7z"/>';
    inputEl.disabled = streamingNow;
  }

  function stopStreaming() {
    if (streaming && abortCtrl) abortCtrl.abort();
  }

  // ---------- 输入框 ----------
  function autoResize() {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 150) + "px";
  }
  function updateSendState() {
    if (!streaming) sendBtn.disabled = !inputEl.value.trim();
  }
  inputEl.addEventListener("input", () => { autoResize(); updateSendState(); });
  inputEl.addEventListener("keydown", (e) => {
    // keyCode 229 = 输入法组合中 (某些浏览器/输入法下 isComposing 判断不准)
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      submit();
    }
  });
  sendBtn.addEventListener("click", () => {
    if (streaming) stopStreaming();
    else submit();
  });

  function submit() {
    const text = inputEl.value.trim();
    if (!text || streaming) return;
    inputEl.value = "";
    autoResize();
    updateSendState();
    sendMessage(text);
  }

  // ---------- 删除当前会话（右上角垃圾桶） ----------
  clearBtn.addEventListener("click", () => {
    const s = currentSession();
    if (!s) return;
    if (streaming) stopStreaming();
    deleteSession(s.id); // 确认弹窗在 deleteSession 内统一处理, 只弹一次
  });

  // ---------- 新会话 / 侧栏 ----------
  newSessionBtn.addEventListener("click", createSession);

  // 桌面端: 收起/展开 (collapsed); 移动端: 滑入 (open + 遮罩)
  const isMobileView = () => window.matchMedia("(max-width: 860px)").matches;

  sidebarToggle.addEventListener("click", () => {
    if (isMobileView()) {
      sidebar.classList.toggle("open");
      sidebarMask.classList.toggle("show", sidebar.classList.contains("open"));
    } else {
      sidebar.classList.toggle("collapsed");
      localStorage.setItem("sidebar_collapsed", sidebar.classList.contains("collapsed") ? "1" : "0");
      // 收起后让输入框保持聚焦, 避免聊天区布局跳动
      inputEl.focus();
    }
  });

  sidebarMask.addEventListener("click", closeSidebarMobile);
  function closeSidebarMobile() {
    sidebar.classList.remove("open");
    sidebarMask.classList.remove("show");
  }

  // 恢复桌面端收起状态
  if (!isMobileView() && localStorage.getItem("sidebar_collapsed") === "1") {
    sidebar.classList.add("collapsed");
  }

  // ---------- 智能滚动 ----------
  messagesEl.addEventListener("scroll", () => {
    const d = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight;
    stickBottom = d < 70;
  });

  // ---------- 启动 ----------
  (async () => {
    // 0) 查询 MCP 连接状态, 显示徽标 + 绑定点击弹窗
    try {
      const ms = await fetch("/api/mcp-status").then((r) => r.json());
      const badge = document.getElementById("mcp-badge");
      if (badge && ms && ms.enabled && ms.tools > 0) {
        badge.textContent = `MCP ${ms.tools} 工具`;
        badge.hidden = false;
        badge.addEventListener("click", () => showMcpModal(ms));
      }
    } catch { /* 忽略 */ }
    // 1) 从服务端恢复会话列表（本地缓存兜底 + 双向合并迁移）
    await bootstrapSessions();
    // 2) 没有任何会话则新建一个
    if (!sessions.length) {
      const s = {
        id: newId(),
        threadId: newId(),
        title: "新会话",
        createdAt: Date.now(),
        updatedAt: Date.now(),
        messages: [],
      };
      sessions.push(s);
      saveSessions();
      syncSession(s);
    }
    // 3) 默认进入最近更新的会话
    currentId = sessions.slice().sort((a, b) => b.updatedAt - a.updatedAt)[0].id;
    renderSessionList();
    renderMessages();
    updateSendState();
    inputEl.focus();
  })();

  // ---------- MCP 工具详情弹窗 ----------
  function showMcpModal(data) {
    const modal = document.getElementById("mcp-modal");
    const body = document.getElementById("mcp-modal-body");
    if (!modal || !body) return;
    if (data && Array.isArray(data.details) && data.details.length) {
      let html = "";
      for (const t of data.details) {
        const argChips = Object.entries(t.args || {})
          .map(([k, v]) =>
            `<span class="mcp-arg-chip"><b>${escapeHtml(k)}</b>` +
            `${v.required ? ' <span class="req">*</span>' : ""}</span>`
          )
          .join("");
        html +=
          `<div class="mcp-tool-card">` +
          `<div class="mcp-tool-name">${escapeHtml(t.name)}</div>` +
          (t.description ? `<div class="mcp-tool-desc">${escapeHtml(t.description)}</div>` : "") +
          (argChips ? `<div class="mcp-tool-args">${argChips}</div>` : "") +
          `</div>`;
      }
      body.innerHTML = html;
    } else {
      body.innerHTML = `<div class="mcp-modal-empty">没有已连接的工具</div>`;
    }
    modal.hidden = false;
    document.addEventListener("keydown", mcpModalKeyHandler);
  }

  function mcpModalKeyHandler(e) {
    if (e.key === "Escape") closeMcpModal();
  }

  function closeMcpModal() {
    const modal = document.getElementById("mcp-modal");
    if (modal) modal.hidden = true;
    document.removeEventListener("keydown", mcpModalKeyHandler);
  }

  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-mcp-close]")) closeMcpModal();
  });
})();
