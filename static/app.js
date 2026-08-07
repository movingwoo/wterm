/* W-Term 프론트엔드: 프로젝트 목록 + xterm.js 터미널 + WebSocket 연동 */
(() => {
  "use strict";

  const sidebarEl = document.getElementById("sidebar");
  const sidebarToggleEl = document.getElementById("sidebar-toggle");
  const terminalPaneEl = document.getElementById("terminal-pane");
  const projectListEl = document.getElementById("project-list");
  const connStatusEl = document.getElementById("conn-status");
  const loginOverlayEl = document.getElementById("login-overlay");
  const loginFormEl = document.getElementById("login-form");
  const loginPasswordEl = document.getElementById("login-password");
  const loginErrorEl = document.getElementById("login-error");
  const logoutBtnEl = document.getElementById("logout-btn");

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
  term.open(document.getElementById("terminal"));

  let ws = null;
  let current = null; // { name, mode, agent }
  let reconnectTimer = null;
  let reconnectAttempts = 0;
  let intentionalClose = false;

  function setStatus(cls, text) {
    connStatusEl.className = cls;
    connStatusEl.textContent = text;
  }

  function sendJson(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }

  term.onData((data) => sendJson({ type: "input", data }));

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
    intentionalClose = true;
    if (ws) ws.close();
    if (reconnectTimer) clearTimeout(reconnectTimer);
    document.body.classList.remove("session-open");
    term.reset();
    setStatus("disconnected", "연결 안 됨");
    projectListEl.replaceChildren();
    showLogin("로그아웃되었습니다.");
  });

  function fitAndReport() {
    fitAddon.fit();
    sendJson({ type: "resize", cols: term.cols, rows: term.rows });
  }

  let fitFrame = null;
  function scheduleFitAndReport() {
    if (fitFrame !== null) cancelAnimationFrame(fitFrame);
    fitFrame = requestAnimationFrame(() => {
      fitFrame = null;
      fitAndReport();
    });
  }

  window.addEventListener("resize", scheduleFitAndReport);
  if ("ResizeObserver" in window) {
    const terminalResizeObserver = new ResizeObserver(scheduleFitAndReport);
    terminalResizeObserver.observe(terminalPaneEl);
  }

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
    scheduleFitAndReport();
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

  function connect(name, mode, agent = "claude") {
    intentionalClose = true;
    if (ws) ws.close();
    clearTimeout(reconnectTimer);

    current = { name, mode, agent };
    document.body.classList.add("session-open");
    term.reset();
    term.focus();
    setStatus("connecting", "연결 중…");

    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url =
      `${proto}://${location.host}/ws/${encodeURIComponent(name)}` +
      `?mode=${mode}&agent=${encodeURIComponent(agent)}`;
    const sock = new WebSocket(url);
    sock.binaryType = "arraybuffer";
    ws = sock;
    intentionalClose = false;

    sock.onopen = () => {
      reconnectAttempts = 0;
      const label = agent === "shell" ? "셸" : agent === "codex" ? "Codex" : "Claude";
      setStatus("connected", `연결됨: ${name} (${label})`);
      fitAndReport();
      loadProjects();
    };

    sock.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        const msg = JSON.parse(ev.data);
        if (msg.type === "status") {
          term.write(`\r\n\x1b[38;5;183m[W-Term] ${msg.message}\x1b[0m\r\n`);
        } else if (msg.type === "exit") {
          intentionalClose = true;
          term.write(
            `\r\n\x1b[38;5;210m[W-Term] 세션이 종료되었습니다 (exit=${msg.code}).\x1b[0m\r\n`
          );
        }
      } else {
        term.write(new Uint8Array(ev.data));
      }
    };

    sock.onclose = (ev) => {
      if (ws !== sock) return; // 이미 다른 연결로 교체됨
      ws = null;
      loadProjects();
      // 오리진 거절(4403)은 여기로 오지 않는다. 서버가 accept 전에 닫아 핸드셰이크가
      // HTTP 403으로 끝나고, 브라우저는 그것을 1006으로만 알려준다. 아래 재연결
      // 경로를 타고 "재연결 실패"로 끝나며, 원인은 서버 로그에 남는다.
      if (
        intentionalClose || ev.code === 4000 || ev.code === 4401 ||
        ev.code === 4404 || ev.code === 4400
      ) {
        setStatus("disconnected", "연결 종료");
        if (ev.code === 4000)
          term.write("\r\n\x1b[38;5;210m[W-Term] 다른 클라이언트가 연결하여 종료되었습니다.\x1b[0m\r\n");
        if (ev.code === 4401) showLogin("인증이 만료되었습니다. 다시 로그인하세요.");
        // 재연결해봐야 같은 이유로 거절당하는 설정 문제다. 서버가 보낸 사유를 그대로 띄운다.
        if (ev.code === 4404 || ev.code === 4400)
          term.write(`\r\n\x1b[38;5;210m[W-Term] 연결이 거절되었습니다: ${ev.reason || ev.code}\x1b[0m\r\n`);
        return;
      }
      // 비정상 단절: 자동 재연결 (라이브 세션이면 재접속, 아니면 claude -c로 최근 세션 이어하기)
      if (reconnectAttempts < 20) {
        reconnectAttempts += 1;
        const delay = Math.min(1000 * reconnectAttempts, 5000);
        setStatus("connecting", `재연결 시도 중… (${reconnectAttempts})`);
        reconnectTimer = setTimeout(
          () => connect(current.name, "continue", current.agent),
          delay
        );
      } else {
        setStatus("disconnected", "재연결 실패");
      }
    };
  }

  async function loadProjects() {
    let projects;
    try {
      const res = await fetch("/api/projects");
      if (res.status === 401) {
        showLogin();
        return;
      }
      projects = await res.json();
    } catch {
      projectListEl.textContent = "프로젝트 목록을 불러오지 못했습니다.";
      return;
    }
    projectListEl.replaceChildren();
    for (const p of projects) {
      const card = document.createElement("div");
      card.className = "project" + (current && current.name === p.name ? " active" : "");

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

        const resumeBtn = document.createElement("button");
        resumeBtn.className = "primary";
        resumeBtn.textContent = live ? "재접속" : "이어하기";
        resumeBtn.title = live
          ? `실행 중인 ${label} 세션에 재접속`
          : `${label}의 이전 세션 목록에서 선택해 이어하기`;
        resumeBtn.disabled = !live && !hasHistory;
        resumeBtn.onclick = () => connect(p.name, "resume", agent);

        const newBtn = document.createElement("button");
        newBtn.textContent = "새 세션";
        newBtn.onclick = () => {
          if (live && !confirm(`'${p.name}'의 실행 중인 ${label} 세션을 종료하고 새로 시작할까요?`)) return;
          connect(p.name, "new", agent);
        };
        row.append(labelEl, resumeBtn, newBtn);
        return row;
      }
      actions.append(
        makeAgentRow("Claude", "claude", p.live, p.has_history),
        makeAgentRow("Codex", "codex", p.codex_live, p.codex_has_history)
      );


      const shellBtn = document.createElement("button");
      shellBtn.textContent = p.shell_live ? "셸 재접속" : "셸";
      shellBtn.title = p.ssh
        ? `원격 셸 열기 (ssh ${p.ssh})`
        : "이 디렉터리에서 로컬 셸 열기";
      shellBtn.onclick = () => connect(p.name, "attach", "shell");

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
  setInterval(() => { if (!ws) loadProjects(); }, 10000);
})();
