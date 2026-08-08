/* W-Term 프론트엔드: 프로젝트 목록 + 탭별 xterm.js 터미널 + WebSocket 연동 */
(() => {
  "use strict";

  const sidebarEl = document.getElementById("sidebar");
  const sidebarToggleEl = document.getElementById("sidebar-toggle");
  const terminalPaneEl = document.getElementById("terminal-pane");
  const tabBarEl = document.getElementById("tab-bar");
  const terminalStackEl = document.getElementById("terminal-stack");
  const projectListEl = document.getElementById("project-list");
  const connStatusEl = document.getElementById("conn-status");
  const loginOverlayEl = document.getElementById("login-overlay");
  const loginFormEl = document.getElementById("login-form");
  const loginPasswordEl = document.getElementById("login-password");
  const loginErrorEl = document.getElementById("login-error");
  const logoutBtnEl = document.getElementById("logout-btn");

  const AGENT_LABEL = { claude: "Claude", codex: "Codex", shell: "셸" };

  // 탭 하나 = 세션 하나(`<project>#<agent>`) = WebSocket 하나.
  //
  // 서버는 세션 키가 다르면 서로 완전히 독립이므로 탭 여러 개가 동시에 붙어 있어도
  // "라이브 세션당 WS 하나"라는 불변조건은 그대로다. 반대로 같은 키를 두 번 열면
  // 서버가 먼저 붙어 있던 소켓을 4000으로 끊는다 — 그래서 openTab은 키로 기존 탭을
  // 찾아 재사용하고, 새 탭을 만드는 것은 그 키가 아직 없을 때뿐이다.
  const tabs = [];
  let activeTab = null;
  let lastProjects = [];

  function setStatus(cls, text) {
    connStatusEl.className = cls;
    connStatusEl.textContent = text;
  }

  function sendJson(tab, obj) {
    if (tab.ws && tab.ws.readyState === WebSocket.OPEN) tab.ws.send(JSON.stringify(obj));
  }

  function showLogin(message) {
    loginErrorEl.textContent = message || "";
    loginOverlayEl.classList.add("visible");
    loginPasswordEl.focus();
  }

  loginFormEl.addEventListener("submit", async (e) => {
    e.preventDefault();
    loginErrorEl.textContent = "";
    let res;
    try {
      res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: loginPasswordEl.value }),
      });
    } catch {
      loginErrorEl.textContent = "서버에 연결할 수 없습니다.";
      return;
    }
    if (res.ok) {
      loginPasswordEl.value = "";
      loginOverlayEl.classList.remove("visible");
      loadProjects();
      return;
    }
    if (res.status === 429) {
      // 서버가 시도 제한으로 막은 상태. 남은 시간을 알려주지 않으면 사용자는
      // 패스워드가 틀린 줄 알고 계속 눌러 차단을 더 늘린다.
      let seconds = 0;
      try {
        seconds = (await res.json()).retry_after || 0;
      } catch {
        seconds = Number(res.headers.get("Retry-After")) || 0;
      }
      loginErrorEl.textContent = `시도가 너무 많습니다. ${seconds}초 후 다시 시도하세요.`;
    } else if (res.status === 401) {
      loginErrorEl.textContent = "패스워드가 올바르지 않습니다.";
    } else if (res.status === 403) {
      loginErrorEl.textContent = "허용되지 않은 주소로 접속했습니다.";
    } else if (res.status === 413) {
      // 서버가 본문 크기로 먼저 끊은 것. 일반 실패 문구로 두면 패스워드가 틀린
      // 줄 알고 같은 것을 계속 붙여넣게 된다.
      loginErrorEl.textContent = "입력이 너무 깁니다.";
    } else {
      loginErrorEl.textContent = "로그인에 실패했습니다.";
    }
    loginPasswordEl.select();
  });

  logoutBtnEl.addEventListener("click", async () => {
    try {
      await fetch("/api/logout", { method: "POST" });
    } catch {
      /* 서버에 못 닿아도 아래에서 화면은 잠근다 */
    }
    // 로그아웃은 이 브라우저의 접근을 끊는 것이므로 열린 탭도 전부 닫는다.
    // 서버 쪽 세션은 grace 동안 살아 있어 다시 로그인하면 재접속된다.
    for (const tab of tabs.slice()) closeTab(tab, { refresh: false });
    setStatus("disconnected", "연결 안 됨");
    projectListEl.replaceChildren();
    lastProjects = [];
    showLogin("로그아웃되었습니다.");
  });

  // ── 크기 맞추기 ────────────────────────────────────────────────────
  //
  // 비활성 탭은 `display:none`이 아니라 `visibility:hidden`으로 숨긴다. 레이아웃은
  // 그대로 잡히므로 숨은 터미널도 폭·높이를 알 수 있고, 덕분에 백그라운드 세션의
  // PTY 크기도 창 크기를 따라간다 — display:none이면 clientWidth가 0이 되고,
  // fit 애드온이 그 값을 최소치(2열)로 잘라 PTY를 2열로 줄여버린다. 아래 가드는
  // 그래도 0이 나오는 순간(탭 생성 직후 등)을 위한 것이다.

  function fitTab(tab) {
    if (!tab.hostEl.clientWidth || !tab.hostEl.clientHeight) return;
    tab.fitAddon.fit();
    sendJson(tab, { type: "resize", cols: tab.term.cols, rows: tab.term.rows });
  }

  let fitFrame = null;
  function scheduleFit() {
    if (fitFrame !== null) cancelAnimationFrame(fitFrame);
    fitFrame = requestAnimationFrame(() => {
      fitFrame = null;
      for (const tab of tabs) fitTab(tab);
    });
  }

  window.addEventListener("resize", scheduleFit);
  if ("ResizeObserver" in window) {
    const paneResizeObserver = new ResizeObserver(scheduleFit);
    paneResizeObserver.observe(terminalPaneEl);
  }

  // ── 사이드바 접기 ──────────────────────────────────────────────────

  const sidebarStorageKey = "wterm.sidebar.collapsed";

  function setSidebarCollapsed(collapsed, persist = false) {
    document.body.classList.toggle("sidebar-collapsed", collapsed);
    sidebarEl.toggleAttribute("inert", collapsed);
    sidebarToggleEl.setAttribute("aria-expanded", String(!collapsed));
    const label = collapsed ? "사이드바 펼치기" : "사이드바 접기";
    sidebarToggleEl.setAttribute("aria-label", label);
    sidebarToggleEl.title = label;

    if (persist) {
      try {
        localStorage.setItem(sidebarStorageKey, String(collapsed));
      } catch {
        // 저장소가 차단된 브라우저에서도 현재 탭의 접기 기능은 계속 동작한다.
      }
    }
    scheduleFit();
  }

  let sidebarCollapsed = false;
  try {
    sidebarCollapsed = localStorage.getItem(sidebarStorageKey) === "true";
  } catch {
    // localStorage 사용 불가 시 기본값(펼침)을 유지한다.
  }
  setSidebarCollapsed(sidebarCollapsed);

  sidebarToggleEl.addEventListener("click", () => {
    sidebarCollapsed = !sidebarCollapsed;
    setSidebarCollapsed(sidebarCollapsed, true);
  });

  // ── 탭 ─────────────────────────────────────────────────────────────

  function findTab(name, agent) {
    return tabs.find((t) => t.name === name && t.agent === agent) || null;
  }

  function setTabStatus(tab, cls, text) {
    tab.statusCls = cls;
    tab.statusText = text;
    tab.dotEl.className = `tab-dot ${cls}`;
    tab.el.title = `${tab.name} (${AGENT_LABEL[tab.agent]}) — ${text}`;
    if (tab === activeTab) setStatus(cls, text);
  }

  function paintActive() {
    for (const t of tabs) {
      const on = t === activeTab;
      t.el.classList.toggle("active", on);
      t.el.setAttribute("aria-selected", String(on));
      t.el.tabIndex = on ? 0 : -1;
      t.hostEl.classList.toggle("active", on);
    }
    document.body.classList.toggle("session-open", tabs.length > 0);
  }

  function activateTab(tab) {
    activeTab = tab;
    tab.unread = false;
    tab.el.classList.remove("unread");
    paintActive();
    tab.el.scrollIntoView({ block: "nearest", inline: "nearest" });
    setStatus(tab.statusCls, tab.statusText);
    fitTab(tab);
    tab.term.focus();
    renderProjects();
  }

  function createTab(name, agent) {
    const term = new Terminal({
      fontFamily: '"Cascadia Code", "D2Coding", Menlo, monospace',
      fontSize: 14,
      cursorBlink: true,
      scrollback: 5000,
      theme: {
        background: "#1e1e2e",
        foreground: "#cdd6f4",
        cursor: "#cba6f7",
      },
    });
    const fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);

    const hostEl = document.createElement("div");
    hostEl.className = "term-host";

    const el = document.createElement("div");
    el.className = "tab";
    el.setAttribute("role", "tab");

    const dotEl = document.createElement("span");
    dotEl.className = "tab-dot disconnected";

    const nameEl = document.createElement("span");
    nameEl.className = "tab-name";
    nameEl.textContent = name;

    const agentEl = document.createElement("span");
    agentEl.className = `tab-agent ${agent}`;
    agentEl.textContent = AGENT_LABEL[agent];

    const closeEl = document.createElement("button");
    closeEl.className = "tab-close";
    closeEl.type = "button";
    closeEl.textContent = "×";
    closeEl.setAttribute("aria-label", `${name} ${AGENT_LABEL[agent]} 탭 닫기`);
    // 탭을 닫아도 세션은 죽지 않는다. 소켓만 떨어지므로 서버는 grace 동안
    // 프로세스를 붙들고, 그 안에 다시 열면 화면째 복원된다.
    closeEl.title = "탭 닫기 (세션은 유예 시간 동안 서버에 남아 있습니다)";

    el.append(dotEl, nameEl, agentEl, closeEl);

    const tab = {
      name, agent, term, fitAddon, hostEl, el, dotEl,
      ws: null,
      reconnectTimer: null,
      reconnectAttempts: 0,
      intentionalClose: false,
      statusCls: "disconnected",
      statusText: "연결 안 됨",
      unread: false,
    };

    el.addEventListener("click", (e) => {
      if (e.target === closeEl) return;
      activateTab(tab);
    });
    el.addEventListener("auxclick", (e) => {
      if (e.button === 1) {
        e.preventDefault();
        closeTab(tab);
      }
    });
    closeEl.addEventListener("click", () => closeTab(tab));
    term.onData((data) => sendJson(tab, { type: "input", data }));

    tabs.push(tab);
    tabBarEl.appendChild(el);
    terminalStackEl.appendChild(hostEl);
    // term.open 전에 활성 표시를 걸어 둔다. 문자 크기를 재는 것은 open 시점이라,
    // 숨은 상태에서 열면 첫 fit이 엉뚱한 크기로 잡힌다.
    activeTab = tab;
    paintActive();
    term.open(hostEl);
    setTabStatus(tab, "disconnected", "연결 안 됨");
    return tab;
  }

  function closeTab(tab, { refresh = true } = {}) {
    const index = tabs.indexOf(tab);
    if (index < 0) return;
    tab.intentionalClose = true;
    clearTimeout(tab.reconnectTimer);
    if (tab.ws) tab.ws.close();
    tab.ws = null;
    tab.term.dispose();
    tab.el.remove();
    tab.hostEl.remove();
    tabs.splice(index, 1);

    if (activeTab === tab) {
      activeTab = null;
      const next = tabs[index] || tabs[index - 1] || null;
      if (next) {
        activateTab(next);
      } else {
        paintActive();
        setStatus("disconnected", "연결 안 됨");
      }
    } else {
      paintActive();
    }
    if (refresh) renderProjects();
  }

  // 탭 전환 단축키. xterm은 textarea에서 keydown을 받아 그 자리에서 입력을 보내므로
  // 캡처 단계에서 가로채야 터미널로 흘러 들어가지 않는다. Ctrl+Alt 조합은 Claude
  // TUI와 셸이 쓰는 키(Esc, Ctrl-C, 방향키, Alt+방향키)와 겹치지 않는 자리다.
  document.addEventListener("keydown", (e) => {
    if (!e.ctrlKey || !e.altKey || e.shiftKey || e.metaKey) return;
    let delta = 0;
    if (e.key === "ArrowRight") delta = 1;
    else if (e.key === "ArrowLeft") delta = -1;
    else return;
    if (tabs.length < 2) return;
    e.preventDefault();
    e.stopPropagation();
    const index = tabs.indexOf(activeTab);
    activateTab(tabs[(index + delta + tabs.length) % tabs.length]);
  }, true);

  // ── 연결 ───────────────────────────────────────────────────────────

  function connect(tab, mode) {
    tab.intentionalClose = true;
    if (tab.ws) tab.ws.close();
    clearTimeout(tab.reconnectTimer);

    tab.term.reset();
    setTabStatus(tab, "connecting", "연결 중…");

    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url =
      `${proto}://${location.host}/ws/${encodeURIComponent(tab.name)}` +
      `?mode=${mode}&agent=${encodeURIComponent(tab.agent)}`;
    const sock = new WebSocket(url);
    sock.binaryType = "arraybuffer";
    tab.ws = sock;
    tab.intentionalClose = false;

    sock.onopen = () => {
      tab.reconnectAttempts = 0;
      setTabStatus(tab, "connected", `연결됨: ${tab.name} (${AGENT_LABEL[tab.agent]})`);
      fitTab(tab);
      loadProjects();
    };

    sock.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        const msg = JSON.parse(ev.data);
        if (msg.type === "status") {
          tab.term.write(`\r\n\x1b[38;5;183m[W-Term] ${msg.message}\x1b[0m\r\n`);
        } else if (msg.type === "exit") {
          tab.intentionalClose = true;
          tab.term.write(
            `\r\n\x1b[38;5;210m[W-Term] 세션이 종료되었습니다 (exit=${msg.code}).\x1b[0m\r\n`
          );
        }
      } else {
        tab.term.write(new Uint8Array(ev.data));
        // 백그라운드 탭이 무언가 출력했다는 표시. 화면 전환 없이도 "저쪽에서
        // 뭔가 움직였다"를 알 수 있어야 탭을 여러 개 열어둘 값어치가 생긴다.
        if (tab !== activeTab && !tab.unread) {
          tab.unread = true;
          tab.el.classList.add("unread");
        }
      }
    };

    sock.onclose = (ev) => {
      if (tab.ws !== sock) return; // 이미 다른 연결로 교체됨
      tab.ws = null;
      loadProjects();
      // 오리진 거절(4403)은 여기로 오지 않는다. 서버가 accept 전에 닫아 핸드셰이크가
      // HTTP 403으로 끝나고, 브라우저는 그것을 1006으로만 알려준다. 아래 재연결
      // 경로를 타고 "재연결 실패"로 끝나며, 원인은 서버 로그에 남는다.
      if (
        tab.intentionalClose || ev.code === 4000 || ev.code === 4401 ||
        ev.code === 4404 || ev.code === 4400 || ev.code === 4408
      ) {
        setTabStatus(tab, "disconnected", "연결 종료");
        if (ev.code === 4000)
          tab.term.write("\r\n\x1b[38;5;210m[W-Term] 다른 클라이언트가 연결하여 종료되었습니다.\x1b[0m\r\n");
        if (ev.code === 4401) showLogin("인증이 만료되었습니다. 다시 로그인하세요.");
        // 4408(유휴 종료)에서 재연결하면 방금 정리한 세션이 곧바로 다시 뜬다.
        // 사유는 서버가 닫기 직전 보낸 status 메시지에 이미 찍혀 있다.
        // 나머지는 재연결해봐야 같은 이유로 거절당하는 설정 문제다.
        if (ev.code === 4404 || ev.code === 4400)
          tab.term.write(`\r\n\x1b[38;5;210m[W-Term] 연결이 거절되었습니다: ${ev.reason || ev.code}\x1b[0m\r\n`);
        return;
      }
      // 비정상 단절: 자동 재연결 (라이브 세션이면 재접속, 아니면 claude -c로 최근 세션 이어하기)
      if (tab.reconnectAttempts < 20) {
        tab.reconnectAttempts += 1;
        const delay = Math.min(1000 * tab.reconnectAttempts, 5000);
        setTabStatus(tab, "connecting", `재연결 시도 중… (${tab.reconnectAttempts})`);
        tab.reconnectTimer = setTimeout(() => connect(tab, "continue"), delay);
      } else {
        setTabStatus(tab, "disconnected", "재연결 실패");
      }
    };
  }

  /** 해당 세션의 탭을 앞으로 가져온다. mode가 있으면 (재)연결까지 한다. */
  function openTab(name, agent, mode) {
    const tab = findTab(name, agent) || createTab(name, agent);
    activateTab(tab);
    if (mode) connect(tab, mode);
    return tab;
  }

  // ── 프로젝트 목록 ──────────────────────────────────────────────────

  async function loadProjects() {
    try {
      const res = await fetch("/api/projects");
      if (res.status === 401) {
        showLogin();
        return;
      }
      lastProjects = await res.json();
    } catch {
      projectListEl.textContent = "프로젝트 목록을 불러오지 못했습니다.";
      return;
    }
    renderProjects();
  }

  function renderProjects() {
    projectListEl.replaceChildren();
    for (const p of lastProjects) {
      const card = document.createElement("div");
      const cardActive = activeTab !== null && activeTab.name === p.name;
      card.className = "project" + (cardActive ? " active" : "");

      const nameEl = document.createElement("div");
      nameEl.className = "name";
      nameEl.textContent = p.name;
      if (p.live) {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = "CLAUDE";
        nameEl.appendChild(badge);
      }
      if (p.codex_live) {
        const badge = document.createElement("span");
        badge.className = "badge codex";
        badge.textContent = "CODEX";
        nameEl.appendChild(badge);
      }
      if (p.shell_live) {
        const badge = document.createElement("span");
        badge.className = "badge shell";
        badge.textContent = "SHELL";
        nameEl.appendChild(badge);
      }

      const pathEl = document.createElement("div");
      pathEl.className = "path";
      pathEl.textContent = p.ssh ? `${p.ssh}:${p.path}` : p.path;

      const actions = document.createElement("div");
      actions.className = "actions";

      function makeAgentRow(label, agent, live, hasHistory) {
        const row = document.createElement("div");
        row.className = "agent-row";
        const labelEl = document.createElement("span");
        labelEl.className = `agent-label ${agent}`;
        labelEl.textContent = label;

        const open = findTab(p.name, agent);
        const resumeBtn = document.createElement("button");
        resumeBtn.className = "primary";
        if (open && open.ws) {
          // 이미 탭이 붙어 있는 세션. 여기서 다시 연결하면 서버가 그 탭의 소켓을
          // 4000으로 끊는다 — 그러니 연결은 건드리지 않고 탭만 앞으로 가져온다.
          resumeBtn.textContent = "보기";
          resumeBtn.title = "열려 있는 탭으로 이동";
          resumeBtn.onclick = () => activateTab(open);
        } else {
          resumeBtn.textContent = live ? "재접속" : "이어하기";
          resumeBtn.title = live
            ? `실행 중인 ${label} 세션에 재접속`
            : `${label}의 이전 세션 목록에서 선택해 이어하기`;
          resumeBtn.disabled = !live && !hasHistory;
          resumeBtn.onclick = () => openTab(p.name, agent, "resume");
        }

        const newBtn = document.createElement("button");
        newBtn.textContent = "새 세션";
        newBtn.onclick = () => {
          if (live && !confirm(`'${p.name}'의 실행 중인 ${label} 세션을 종료하고 새로 시작할까요?`)) return;
          openTab(p.name, agent, "new");
        };
        row.append(labelEl, resumeBtn, newBtn);
        return row;
      }
      actions.append(
        makeAgentRow("Claude", "claude", p.live, p.has_history),
        makeAgentRow("Codex", "codex", p.codex_live, p.codex_has_history)
      );


      const openShell = findTab(p.name, "shell");
      const shellBtn = document.createElement("button");
      if (openShell && openShell.ws) {
        shellBtn.textContent = "셸 보기";
        shellBtn.title = "열려 있는 셸 탭으로 이동";
        shellBtn.onclick = () => activateTab(openShell);
      } else {
        shellBtn.textContent = p.shell_live ? "셸 재접속" : "셸";
        shellBtn.title = p.ssh
          ? `원격 셸 열기 (ssh ${p.ssh})`
          : "이 디렉터리에서 로컬 셸 열기";
        shellBtn.onclick = () => openTab(p.name, "shell", "attach");
      }

      const shellRow = document.createElement("div");
      shellRow.className = "agent-row shell-row";
      const shellLabel = document.createElement("span");
      shellLabel.className = "agent-label shell";
      shellLabel.textContent = "Shell";
      shellRow.append(shellLabel, shellBtn);
      actions.append(shellRow);
      card.append(nameEl, pathEl, actions);
      projectListEl.appendChild(card);
    }
  }

  loadProjects();
  // 붙어 있는 소켓이 없는 탭이 하나라도 있으면(또는 탭이 없으면) 배지가 바뀌어도
  // 알 방법이 없다. 소켓이 전부 살아 있을 때는 그쪽 이벤트로 갱신되므로 쉰다.
  setInterval(() => {
    if (tabs.length === 0 || tabs.some((t) => !t.ws)) loadProjects();
  }, 10000);
})();
