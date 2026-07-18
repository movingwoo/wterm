/* W-Term 프론트엔드: 프로젝트 목록 + xterm.js 터미널 + WebSocket 연동 */
(() => {
  "use strict";

  const projectListEl = document.getElementById("project-list");
  const connStatusEl = document.getElementById("conn-status");
  const loginOverlayEl = document.getElementById("login-overlay");
  const loginFormEl = document.getElementById("login-form");
  const loginPasswordEl = document.getElementById("login-password");
  const loginErrorEl = document.getElementById("login-error");

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
  let current = null; // { name, mode }
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
    } else {
      loginErrorEl.textContent =
        res.status === 401 ? "패스워드가 올바르지 않습니다." : "로그인에 실패했습니다.";
      loginPasswordEl.select();
    }
  });

  function fitAndReport() {
    fitAddon.fit();
    sendJson({ type: "resize", cols: term.cols, rows: term.rows });
  }
  window.addEventListener("resize", fitAndReport);

  function connect(name, mode, shell = false) {
    intentionalClose = true;
    if (ws) ws.close();
    clearTimeout(reconnectTimer);

    current = { name, mode, shell };
    document.body.classList.add("session-open");
    term.reset();
    term.focus();
    setStatus("connecting", "연결 중…");

    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url =
      `${proto}://${location.host}/ws/${encodeURIComponent(name)}?mode=${mode}` +
      (shell ? "&shell=1" : "");
    const sock = new WebSocket(url);
    sock.binaryType = "arraybuffer";
    ws = sock;
    intentionalClose = false;

    sock.onopen = () => {
      reconnectAttempts = 0;
      setStatus("connected", `연결됨: ${name}${shell ? " (셸)" : ""}`);
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
      if (intentionalClose || ev.code === 4000 || ev.code === 4401 || ev.code === 4404) {
        setStatus("disconnected", "연결 종료");
        if (ev.code === 4000)
          term.write("\r\n\x1b[38;5;210m[W-Term] 다른 클라이언트가 연결하여 종료되었습니다.\x1b[0m\r\n");
        if (ev.code === 4401) showLogin("인증이 만료되었습니다. 다시 로그인하세요.");
        return;
      }
      // 비정상 단절: 자동 재연결 (라이브 세션이면 재접속, 아니면 claude -c로 최근 세션 이어하기)
      if (reconnectAttempts < 20) {
        reconnectAttempts += 1;
        const delay = Math.min(1000 * reconnectAttempts, 5000);
        setStatus("connecting", `재연결 시도 중… (${reconnectAttempts})`);
        reconnectTimer = setTimeout(
          () => connect(current.name, "continue", current.shell),
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
        badge.textContent = "LIVE";
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

      const resumeBtn = document.createElement("button");
      resumeBtn.className = "primary";
      resumeBtn.textContent = p.live ? "재접속" : "이어하기";
      resumeBtn.title = p.live
        ? "실행 중인 세션에 재접속"
        : "이전 세션 목록에서 선택해 이어하기 (claude --resume)";
      resumeBtn.disabled = !p.live && !p.has_history;
      resumeBtn.onclick = () => connect(p.name, "resume");

      const newBtn = document.createElement("button");
      newBtn.textContent = "새 세션";
      newBtn.onclick = () => {
        if (p.live && !confirm(`'${p.name}'의 실행 중인 세션을 종료하고 새로 시작할까요?`)) return;
        connect(p.name, "new");
      };

      const shellBtn = document.createElement("button");
      shellBtn.textContent = p.shell_live ? "셸 재접속" : "셸";
      shellBtn.title = p.ssh
        ? `원격 셸 열기 (ssh ${p.ssh})`
        : "이 디렉터리에서 로컬 셸 열기";
      shellBtn.onclick = () => connect(p.name, "attach", true);

      actions.append(resumeBtn, newBtn, shellBtn);
      card.append(nameEl, pathEl, actions);
      projectListEl.appendChild(card);
    }
  }

  loadProjects();
  setInterval(() => { if (!ws) loadProjects(); }, 10000);
})();
