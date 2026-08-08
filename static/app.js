/* W-Term 프론트엔드: 프로젝트 목록 + 탭별 xterm.js 터미널 + WebSocket 연동 */
(() => {
  "use strict";

  const sidebarEl = document.getElementById("sidebar");
  const sidebarToggleEl = document.getElementById("sidebar-toggle");
  const panesEl = document.getElementById("panes");
  const keyBarEl = document.getElementById("key-bar");
  const projectListEl = document.getElementById("project-list");
  const connStatusEl = document.getElementById("conn-status");
  const loginOverlayEl = document.getElementById("login-overlay");
  const loginFormEl = document.getElementById("login-form");
  const loginPasswordEl = document.getElementById("login-password");
  const loginErrorEl = document.getElementById("login-error");
  const logoutBtnEl = document.getElementById("logout-btn");
  const notifyBtnEl = document.getElementById("notify-btn");

  const AGENT_LABEL = { claude: "Claude", codex: "Codex", shell: "셸" };
  const BASE_TITLE = document.title;

  // 탭 하나 = 세션 하나(`<project>#<agent>`) = WebSocket 하나.
  //
  // 서버는 세션 키가 다르면 서로 완전히 독립이므로 탭 여러 개가 동시에 붙어 있어도
  // "라이브 세션당 WS 하나"라는 불변조건은 그대로다. 반대로 같은 키를 두 번 열면
  // 서버가 먼저 붙어 있던 소켓을 4000으로 끊는다 — 그래서 openTab은 키로 기존 탭을
  // 찾아 재사용하고, 새 탭을 만드는 것은 그 키가 아직 없을 때뿐이다.
  //
  // 탭은 창(pane)에 담긴다. 창은 자기 탭 목록과 활성 탭을 가지고, 그중 하나가
  // 포커스를 받아 키 바·상태줄·주소 해시의 대상이 된다. 창이 하나뿐이면 지금까지와
  // 똑같이 동작한다.
  //
  // 상한이 둘인 것은 취향이 아니라 열 수 때문이다. 14px 고정폭에서 한 열이 약
  // 8.4px이라, 14인치 화면(1512px)에서 사이드바를 편 채로 2분할하면 창당 75열이고
  // 3분할이면 50열이다. Claude Code TUI는 박스 드로잉이라 그쯤에서 접히기 시작한다 —
  // 에디터와 달리 터미널은 좁아지면 읽기 불편한 정도가 아니라 깨진다.
  const MAX_PANES = 2;
  const panes = [];
  let focusedPane = null;
  let lastProjects = [];
  // 종료 요청이 나가 있는 세션 키(`<project>#<agent>`). 셸은 SIGTERM을 무시해서
  // 응답까지 10초 가까이 걸리는데, 그 사이 폴링이 목록을 다시 그린다.
  const endingKeys = new Set();

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
    for (const tab of allTabs()) closeTab(tab, { refresh: false });
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
      for (const tab of allTabs()) fitTab(tab);
    });
  }

  // 관찰 대상은 창 전체가 아니라 창마다의 터미널 더미다. 창은 탭 줄과 터미널을
  // 세로로 쌓는 flex 컨테이너라, 그 안에서 탭 줄에 가로 스크롤바가 생기면 줄어드는
  // 것은 더미뿐이고 창의 크기는 그대로다 — 창을 보고 있으면 그 경우에 리핏이 걸리지
  // 않는다. 더미는 터미널이 실제로 차지하는 상자다. (분할 스플리터를 끄는 동안에도
  // 이 관찰자가 양쪽 창의 리핏을 몰아준다.)
  const stackResizeObserver =
    "ResizeObserver" in window ? new ResizeObserver(scheduleFit) : null;
  window.addEventListener("resize", scheduleFit);

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

  // ── 모바일 키 바 ───────────────────────────────────────────────────
  //
  // 폰 소프트 키보드에는 Esc·Tab·Ctrl·방향키가 없다. Claude Code TUI는 Esc(중단)와
  // 방향키(메뉴 선택)가 있어야 쓸 수 있으므로, 그것들 없이는 폰에서 사실상 텍스트
  // 입력만 가능하다 — "폰에서 다시 열면 이어집니다"라는 이 도구의 용도가 반쪽이 된다.
  //
  // 보내는 것은 term.paste가 아니라 입력 메시지 그대로다. paste는 괄호 붙여넣기
  // 모드(bracketed paste)에서 \x1b[200~ ... \x1b[201~로 감싸여 나가고, 그 안의
  // 바이트는 제어문자가 아니라 텍스트로 취급된다 — Esc가 Esc로 도착하지 않는다.

  // 시퀀스가 null인 것은 보낼 것이 없는 고정 키(Ctrl)다.
  const BAR_KEYS = [
    ["Esc", "\x1b", "Esc (중단)"],
    ["Tab", "\t", "Tab"],
    ["Ctrl", null, "Ctrl (다음 한 글자에만 적용)"],
    ["←", "\x1b[D", "왼쪽"],
    ["↑", "\x1b[A", "위"],
    ["↓", "\x1b[B", "아래"],
    ["→", "\x1b[C", "오른쪽"],
    ["^C", "\x03", "Ctrl-C (인터럽트)"],
  ];

  let ctrlArmed = false;
  let ctrlKeyEl = null;

  function setCtrlArmed(on) {
    ctrlArmed = on;
    if (ctrlKeyEl) {
      ctrlKeyEl.classList.toggle("armed", on);
      ctrlKeyEl.setAttribute("aria-pressed", String(on));
    }
  }

  /** Ctrl이 걸려 있으면 다음 한 글자를 제어문자로 접는다. */
  function applyCtrl(data) {
    if (!ctrlArmed) return data;
    // 소프트 키보드에는 동시 누르기가 없다. Ctrl은 누르면 다음 한 글자에만
    // 걸리는 고정 키(sticky)여야 Ctrl-R 같은 조합을 낼 수 있다.
    setCtrlArmed(false);
    if (data.length !== 1) return data; // IME 조합 결과나 붙여넣기는 건드리지 않는다
    const code = data.toUpperCase().charCodeAt(0);
    // 0x40~0x5f(@A-Z[\]^_)만 접는다. 이 범위 밖에는 대응하는 제어문자가 없다.
    return code >= 0x40 && code <= 0x5f ? String.fromCharCode(code & 0x1f) : data;
  }

  function sendKey(seq) {
    const tab = activeTab();
    if (!tab) return;
    setCtrlArmed(false); // 바의 키들은 이미 완성된 시퀀스다
    sendJson(tab, { type: "input", data: seq });
    tab.term.focus();
  }

  function makeKeyButton(label, title) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "key";
    btn.textContent = label;
    btn.title = title;
    // 버튼이 포커스를 가져가면 폰에서 소프트 키보드가 접힌다. pointerdown을
    // 취소하면 포커스 이동만 막히고 click은 그대로 온다.
    btn.addEventListener("pointerdown", (e) => e.preventDefault());
    return btn;
  }

  for (const [label, seq, title] of BAR_KEYS) {
    const btn = makeKeyButton(label, title);
    if (seq === null) {
      ctrlKeyEl = btn;
      btn.setAttribute("aria-pressed", "false");
      btn.addEventListener("click", () => {
        setCtrlArmed(!ctrlArmed);
        const tab = activeTab();
        if (tab) tab.term.focus();
      });
    } else {
      btn.addEventListener("click", () => sendKey(seq));
    }
    keyBarEl.appendChild(btn);
  }

  // ── 벨 알림 ────────────────────────────────────────────────────────
  //
  // "노트북 덮고 나갔다 돌아온다"가 이 도구의 용도인데, Claude가 질문을 띄우고
  // 멈춘 것을 알 방법이 없어 결국 주기적으로 들여다보게 된다. TUI가 사람을 부를 때
  // 내는 BEL을 잡아 탭과 문서 제목을 바꾸고, 권한이 있으면 알림까지 띄운다.
  //
  // 감지는 클라이언트에서 한다 — 서버는 터미널 내용을 들여다보지 않는다(AGENTS.md).
  // 판정도 우리가 바이트를 뒤지는 대신 xterm 파서의 onBell에 맡긴다: 셸 프롬프트가
  // 매번 내보내는 창 제목 시퀀스(OSC ... BEL)의 끝에도 BEL이 있어서, 직접 스캔하면
  // 프롬프트가 뜰 때마다 오탐이 된다.
  //
  // 한계: 탭이 열려 있어야만 동작한다. 진짜 푸시는 서비스 워커와 푸시 서버가
  // 필요하고, 그건 이 서버의 무상태 설계와 맞지 않는다.

  function updateDocTitle() {
    const waiting = allTabs().filter((t) => t.bell).length;
    document.title = waiting ? `(${waiting}) ${BASE_TITLE}` : BASE_TITLE;
  }

  function clearBell(tab) {
    if (!tab.bell) return;
    tab.bell = false;
    tab.el.classList.remove("bell");
    updateDocTitle();
  }

  function onBell(tab) {
    // 지금 눈에 보이는 화면이면 알릴 것이 없다 — 부른 이유가 이미 앞에 있다.
    // 판정 기준은 "포커스된 탭"이 아니라 "어느 창에든 보이는 탭"이다. 분할해서
    // 옆 창으로 지켜보는 중인 세션이 계속 부름 표시를 쌓으면 그 표시가 곧
    // 무의미해지고, 분할이 값어치 있는 이유(전환 없이 본다)와도 어긋난다.
    if (isVisible(tab) && !document.hidden) return;
    if (!tab.bell) {
      tab.bell = true;
      tab.el.classList.add("bell");
      updateDocTitle();
    }
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    try {
      const note = new Notification(`${tab.name} (${AGENT_LABEL[tab.agent]})`, {
        body: "세션이 입력을 기다리고 있습니다.",
        tag: `${tab.name}#${tab.agent}`, // 같은 세션의 알림은 쌓이지 않고 덮인다
      });
      note.onclick = () => {
        window.focus();
        if (tab.pane) activateTab(tab); // 그 사이 닫혔으면 pane이 비어 있다
        note.close();
      };
    } catch {
      // 모바일 크롬/사파리는 이 생성자를 막고 서비스 워커를 요구한다. 탭과 문서
      // 제목 표시는 그대로 남으므로 여기서 더 할 일은 없다.
    }
  }

  document.addEventListener("visibilitychange", () => {
    // 보이는 탭 전부다. 분할해 두었으면 돌아온 순간 두 창을 다 본 것이다.
    if (!document.hidden) for (const t of allTabs()) if (isVisible(t)) clearBell(t);
  });

  if ("Notification" in window && Notification.permission === "default") {
    notifyBtnEl.hidden = false;
  }
  notifyBtnEl.addEventListener("click", () => {
    notifyBtnEl.hidden = true; // 허용이든 거부든 다시 물을 수 있는 상태가 아니다
    try {
      Notification.requestPermission();
    } catch {
      /* 지원하지 않는 브라우저 */
    }
  });

  // ── 창(pane) ───────────────────────────────────────────────────────
  //
  // 큰 화면에서 세션 하나를 보려고 다른 하나를 덮는 것이 탭의 한계다. 창은 그
  // 한계만 푼다: 탭 줄과 터미널 더미를 한 벌 더 두고 좌우로 나눈다. 세로 분할이나
  // 중첩은 없다 — 터미널에서 행을 반으로 자르는 것은 열을 자르는 것보다 손해가 크고,
  // 레이아웃 트리는 브라우저 테스트가 없는 이 저장소에서 조용히 깨지기 딱 좋다.

  function allTabs() {
    return panes.flatMap((pane) => pane.tabs);
  }

  function activeTab() {
    return focusedPane ? focusedPane.activeTab : null;
  }

  /** 이 탭이 지금 화면에 보이는가 (자기 창의 활성 탭인가). */
  function isVisible(tab) {
    return tab.pane !== null && tab.pane.activeTab === tab;
  }

  function createPane() {
    const el = document.createElement("section");
    el.className = "pane";

    const tabBarEl = document.createElement("div");
    tabBarEl.className = "tab-bar";
    tabBarEl.setAttribute("role", "tablist");
    tabBarEl.setAttribute("aria-label", "열린 세션");

    // 탭 줄은 넘치면 가로로 스크롤된다. 창 버튼이 그 안에 있으면 같이 밀려나가므로
    // 한 줄(head) 안에서 형제로 두고 버튼 쪽만 고정한다.
    const headEl = document.createElement("div");
    headEl.className = "pane-head";

    const actionsEl = document.createElement("div");
    actionsEl.className = "pane-actions";

    const splitEl = document.createElement("button");
    splitEl.type = "button";
    splitEl.className = "pane-btn";
    splitEl.addEventListener("click", () => {
      if (panes.length < MAX_PANES) addPane();
      else removePane(pane);
    });

    actionsEl.appendChild(splitEl);

    const stackEl = document.createElement("div");
    stackEl.className = "term-stack";

    const placeholderEl = document.createElement("div");
    placeholderEl.className = "pane-placeholder";
    placeholderEl.textContent = "좌측에서 프로젝트를 선택하세요.";
    stackEl.appendChild(placeholderEl);

    headEl.append(tabBarEl, actionsEl);
    el.append(headEl, stackEl);

    const pane = { el, tabBarEl, actionsEl, splitEl, stackEl, tabs: [], activeTab: null };
    // 창 안 어디를 눌러도 그 창이 포커스를 가져간다. 키 바와 상태줄, 주소 해시가
    // 따라오므로 "지금 어느 창을 쓰고 있나"가 클릭 한 번으로 정해져야 한다.
    el.addEventListener("pointerdown", () => focusPane(pane), true);

    panes.push(pane);
    panesEl.appendChild(el);
    if (stackResizeObserver) stackResizeObserver.observe(stackEl);
    return pane;
  }

  function addPane() {
    if (panes.length >= MAX_PANES) return null;
    const pane = createPane();
    layoutPanes();
    focusPane(pane);
    scheduleFit();
    return pane;
  }

  /** 창을 없애고 그 안의 탭을 남는 창으로 옮긴다. 마지막 창은 없애지 않는다. */
  function removePane(pane) {
    if (panes.length < 2) return;
    const index = panes.indexOf(pane);
    const survivor = panes[index === 0 ? 1 : 0];
    for (const tab of pane.tabs.slice()) moveTabToPane(tab, survivor, { refit: false });
    panes.splice(index, 1);
    if (stackResizeObserver) stackResizeObserver.unobserve(pane.stackEl);
    pane.el.remove();
    layoutPanes();
    if (survivor.activeTab) activateTab(survivor.activeTab);
    else focusPane(survivor);
    scheduleFit();
  }

  function moveTabToPane(tab, pane, { refit = true } = {}) {
    const from = tab.pane;
    if (from === pane) return;
    from.tabs.splice(from.tabs.indexOf(tab), 1);
    pane.tabs.push(tab);
    tab.pane = pane;
    // xterm은 컨테이너째 옮겨도 살아남는다 — 우리가 옮기는 것은 host 하나이고
    // 터미널 DOM은 그 안에 통째로 들어 있다. 다만 새 창의 폭에 맞춰 다시 재야 한다.
    pane.tabBarEl.appendChild(tab.el);
    pane.stackEl.appendChild(tab.hostEl);
    if (from.activeTab === tab) from.activeTab = from.tabs[from.tabs.length - 1] || null;
    pane.activeTab = tab;
    if (refit) {
      activateTab(tab); // 포커스·해시·크기·상태줄을 한 곳에서 맞춘다
      // 옮긴 직후 한 번은 다시 그린다. 폭이 달라진 상자로 옮겨왔는데 fit이
      // 같은 열 수를 내놓으면 xterm은 리사이즈가 없었다고 보고 다시 그리지 않는다.
      tab.term.refresh(0, tab.term.rows - 1);
    }
  }

  const splitStorageKey = "wterm.split.ratio";
  let splitRatio = 0.5;
  try {
    const saved = parseFloat(localStorage.getItem(splitStorageKey));
    if (saved >= 0.2 && saved <= 0.8) splitRatio = saved;
  } catch {
    // 저장소가 막혀 있어도 이번 세션에서 끌어 쓰는 것은 그대로 동작한다.
  }

  let splitterEl = null;

  function layoutPanes() {
    const split = panes.length > 1;
    document.body.classList.toggle("split", split);
    if (split) {
      if (!splitterEl) {
        splitterEl = document.createElement("div");
        splitterEl.className = "splitter";
        splitterEl.setAttribute("role", "separator");
        splitterEl.setAttribute("aria-orientation", "vertical");
        splitterEl.setAttribute("aria-label", "창 너비 조절");
        splitterEl.addEventListener("pointerdown", startSplitDrag);
      }
      panes[0].el.after(splitterEl);
    } else if (splitterEl) {
      splitterEl.remove();
    }
    panesEl.style.setProperty("--split-ratio", String(splitRatio));
    for (const pane of panes) {
      const canSplit = panes.length < MAX_PANES;
      // 기호가 아니라 글자다. ◧/✕는 뜻을 짐작해야 하고, 이 자리에는 글자가 들어갈
      // 폭이 있다 — 좁아지면 창 버튼은 어차피 사라진다.
      pane.splitEl.textContent = canSplit ? "분할" : "합치기";
      pane.splitEl.title = canSplit
        ? "창을 좌우로 나눕니다"
        : "이 창을 닫습니다 (탭은 옆 창으로 옮겨집니다)";
      pane.splitEl.setAttribute("aria-label", pane.splitEl.title);
      // 나눌 수 없는 폭에서는 버튼 자체를 감춘다. 눌러도 되지 않는 버튼을
      // 남겨두면 그게 고장으로 보인다.
      pane.splitEl.hidden = !wideEnough.matches;
    }
    paintPanes();
  }

  function startSplitDrag(e) {
    e.preventDefault();
    splitterEl.setPointerCapture(e.pointerId);
    const move = (ev) => {
      const box = panesEl.getBoundingClientRect();
      if (!box.width) return;
      // 20~80%로 자른다. 그 밖으로 나가면 한쪽이 TUI가 깨지는 폭이 되고,
      // 0에 가까워지면 fit이 최소 2열로 잘려 PTY가 그 값으로 줄어든다.
      splitRatio = Math.min(0.8, Math.max(0.2, (ev.clientX - box.left) / box.width));
      panesEl.style.setProperty("--split-ratio", String(splitRatio));
      scheduleFit(); // rAF로 묶여 있어 드래그 한 프레임에 한 번만 돈다
    };
    const up = () => {
      splitterEl.releasePointerCapture(e.pointerId);
      splitterEl.removeEventListener("pointermove", move);
      splitterEl.removeEventListener("pointerup", up);
      splitterEl.removeEventListener("pointercancel", up);
      try {
        localStorage.setItem(splitStorageKey, String(splitRatio));
      } catch {
        /* 저장소가 막혀 있어도 이번 세션 동안은 유지된다 */
      }
    };
    splitterEl.addEventListener("pointermove", move);
    splitterEl.addEventListener("pointerup", up);
    splitterEl.addEventListener("pointercancel", up);
  }

  // 폰에서는 분할하지 않는다. 좁은 화면을 반으로 자르면 두 창 다 못 쓴다.
  // 창을 줄여 분할 폭 아래로 내려가면 조용히 합친다 — 사용자가 고칠 수 있는
  // 상태가 아니므로 오류로 남겨두면 안 된다.
  const wideEnough = window.matchMedia("(min-width: 900px)");

  function applyWidthLimit() {
    while (!wideEnough.matches && panes.length > 1) removePane(panes[panes.length - 1]);
    layoutPanes();
  }

  wideEnough.addEventListener("change", applyWidthLimit);

  function focusPane(pane) {
    if (focusedPane === pane) return;
    focusedPane = pane;
    // 걸려 있던 Ctrl은 창을 옮기면 푼다 — 다음 한 글자가 걸릴 대상이 바뀐다.
    setCtrlArmed(false);
    paintPanes();
    syncHash();
    const tab = pane.activeTab;
    if (tab) setStatus(tab.statusCls, tab.statusText);
    else setStatus("disconnected", "연결 안 됨");
    renderProjects();
  }

  // ── 탭 ─────────────────────────────────────────────────────────────

  function findTab(name, agent) {
    return allTabs().find((t) => t.name === name && t.agent === agent) || null;
  }

  function setTabStatus(tab, cls, text) {
    tab.statusCls = cls;
    tab.statusText = text;
    tab.dotEl.className = `tab-dot ${cls}`;
    tab.el.title = `${tab.name} (${AGENT_LABEL[tab.agent]}) — ${text}`;
    if (tab === activeTab()) setStatus(cls, text);
  }

  function paintPanes() {
    panes.forEach((pane, paneIndex) => {
      pane.el.classList.toggle("focused", pane === focusedPane && panes.length > 1);
      pane.el.classList.toggle("empty", pane.tabs.length === 0);
      for (const t of pane.tabs) {
        const on = t === pane.activeTab;
        t.el.classList.toggle("active", on);
        t.el.setAttribute("aria-selected", String(on));
        t.el.tabIndex = on ? 0 : -1;
        t.hostEl.classList.toggle("active", on);
        t.moveEl.hidden = panes.length < 2;
        // 화살표는 갈 방향을 가리켜야 한다. 오른쪽 창의 탭에 →가 붙어 있으면
        // 화면 밖으로 보낸다는 뜻으로 읽힌다.
        t.moveEl.textContent = paneIndex === 0 ? "→" : "←";
        t.moveEl.title =
          paneIndex === 0 ? "이 탭을 오른쪽 창으로" : "이 탭을 왼쪽 창으로";
        t.moveEl.setAttribute(
          "aria-label", `${t.name} ${AGENT_LABEL[t.agent]} 탭을 ${t.moveEl.title.slice(3)}`
        );
      }
    });
    document.body.classList.toggle("session-open", allTabs().length > 0);
  }

  function activateTab(tab) {
    const pane = tab.pane;
    pane.activeTab = tab;
    tab.unread = false;
    tab.el.classList.remove("unread");
    clearBell(tab);
    // 걸려 있던 Ctrl은 탭을 옮기면 푼다. 안 그러면 옆 세션의 첫 글자가 제어문자로
    // 나가는데, 그건 여기서 누른 적이 없는 키다.
    setCtrlArmed(false);
    focusedPane = pane;
    paintPanes();
    syncHash();
    tab.el.scrollIntoView({ block: "nearest", inline: "nearest" });
    setStatus(tab.statusCls, tab.statusText);
    fitTab(tab);
    tab.term.focus();
    renderProjects();
  }

  function createTab(name, agent, pane) {
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

    // 탭을 옆 창으로 보내는 버튼. 드래그 앤 드롭은 두지 않는다 — 터치에서
    // 드래그와 스크롤을 가려내는 비용이 얻는 것보다 크다.
    const moveEl = document.createElement("button");
    moveEl.className = "tab-move";
    moveEl.type = "button";
    moveEl.textContent = "→";
    moveEl.title = "이 탭을 옆 창으로 옮깁니다";
    moveEl.setAttribute("aria-label", `${name} ${AGENT_LABEL[agent]} 탭을 옆 창으로`);
    moveEl.hidden = true;

    const closeEl = document.createElement("button");
    closeEl.className = "tab-close";
    closeEl.type = "button";
    closeEl.textContent = "×";
    closeEl.setAttribute("aria-label", `${name} ${AGENT_LABEL[agent]} 탭 닫기`);
    // 탭을 닫아도 세션은 죽지 않는다. 소켓만 떨어지므로 서버는 grace 동안
    // 프로세스를 붙들고, 그 안에 다시 열면 화면째 복원된다.
    closeEl.title = "탭 닫기 (세션은 유예 시간 동안 서버에 남아 있습니다)";

    el.append(dotEl, nameEl, agentEl, moveEl, closeEl);

    const tab = {
      name, agent, term, fitAddon, hostEl, el, dotEl, moveEl,
      pane: null,
      ws: null,
      reconnectTimer: null,
      reconnectAttempts: 0,
      intentionalClose: false,
      statusCls: "disconnected",
      statusText: "연결 안 됨",
      unread: false,
      bell: false,
    };

    el.addEventListener("click", (e) => {
      if (e.target === closeEl || e.target === moveEl) return;
      activateTab(tab);
    });
    el.addEventListener("auxclick", (e) => {
      if (e.button === 1) {
        e.preventDefault();
        closeTab(tab);
      }
    });
    moveEl.addEventListener("click", () => {
      const other = panes.find((p) => p !== tab.pane);
      if (other) moveTabToPane(tab, other);
    });
    closeEl.addEventListener("click", () => closeTab(tab));
    term.onData((data) => sendJson(tab, { type: "input", data: applyCtrl(data) }));
    // BEL 판정은 xterm 파서가 한다 — 위 "벨 알림" 주석 참고.
    term.onBell(() => onBell(tab));

    tab.pane = pane;
    pane.tabs.push(tab);
    pane.tabBarEl.appendChild(el);
    pane.stackEl.appendChild(hostEl);
    // term.open 전에 활성 표시를 걸어 둔다. 문자 크기를 재는 것은 open 시점이라,
    // 숨은 상태에서 열면 첫 fit이 엉뚱한 크기로 잡힌다.
    pane.activeTab = tab;
    focusedPane = pane;
    paintPanes();
    term.open(hostEl);
    setTabStatus(tab, "disconnected", "연결 안 됨");
    return tab;
  }

  function closeTab(tab, { refresh = true } = {}) {
    const pane = tab.pane;
    if (pane === null) return;
    const index = pane.tabs.indexOf(tab);
    tab.intentionalClose = true;
    clearTimeout(tab.reconnectTimer);
    if (tab.ws) tab.ws.close();
    tab.ws = null;
    tab.term.dispose();
    tab.el.remove();
    tab.hostEl.remove();
    pane.tabs.splice(index, 1);
    tab.pane = null;
    updateDocTitle(); // 닫힌 탭이 부르고 있었다면 제목의 대기 수도 줄어든다

    if (pane.activeTab === tab) {
      pane.activeTab = pane.tabs[index] || pane.tabs[index - 1] || null;
      if (pane.activeTab && pane === focusedPane) {
        activateTab(pane.activeTab);
      } else {
        paintPanes();
        syncHash();
        if (pane === focusedPane && !pane.activeTab) setStatus("disconnected", "연결 안 됨");
      }
    } else {
      paintPanes();
    }
    if (refresh) renderProjects();
  }

  // ── 주소창 해시 ────────────────────────────────────────────────────
  //
  // 새로고침하면 빈 화면이 되어 사이드바에서 다시 눌러야 했다. 활성 탭을
  // `#<프로젝트>/<에이전트>`로 적어 두고 로드할 때 복원한다 — 링크 공유는 덤이다.
  //
  // 적는 것은 활성 탭 하나뿐이다. 열린 탭 전부를 담으면 공유할 수 없는 주소가 되고,
  // 나머지 세션은 어차피 서버에 살아 있어 사이드바 배지로 보이고 한 번에 다시 연다.
  //
  // pushState가 아니라 replaceState인 이유: 탭을 옮길 때마다 히스토리가 쌓이면
  // 뒤로 가기가 페이지를 못 벗어난다. 탭 이동은 탭 줄이 다룰 일이지 브라우저
  // 히스토리가 다룰 일이 아니다. (replaceState는 hashchange도 쏘지 않는다.)

  function syncHash() {
    const tab = activeTab();
    const hash = tab ? `#${encodeURIComponent(tab.name)}/${tab.agent}` : "";
    if (location.hash === hash) return;
    history.replaceState(null, "", hash || location.pathname + location.search);
  }

  let hashRestored = false;

  function restoreFromHash() {
    const raw = location.hash.slice(1);
    const cut = raw.lastIndexOf("/");
    if (cut < 0) return;
    // 프로젝트 이름에는 /가 들어갈 수 있지만 에이전트 이름에는 없다.
    let name;
    try {
      name = decodeURIComponent(raw.slice(0, cut));
    } catch {
      return; // 손으로 고친 주소
    }
    const agent = raw.slice(cut + 1);
    const project = lastProjects.find((p) => p.name === name);
    if (!project || !(agent in AGENT_LABEL)) return;
    // 살아 있는 세션에만 다시 붙는다. 죽은 세션에 attach하면 새 세션이 뜨는데,
    // 주소를 여는 것만으로 claude 프로세스가 새로 뜨는 것은 복원이 아니다.
    const live = { claude: project.live, codex: project.codex_live, shell: project.shell_live };
    if (!live[agent]) return;
    openTab(name, agent, "attach");
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
    // 순환은 포커스된 창 안에서만 한다. 창을 넘나들면 "지금 어디를 보고 있나"가
    // 키 하나로 바뀌어버려서, 옆 창은 클릭으로 고르는 편이 예측 가능하다.
    const list = focusedPane ? focusedPane.tabs : [];
    if (list.length < 2) return;
    e.preventDefault();
    e.stopPropagation();
    const index = list.indexOf(focusedPane.activeTab);
    activateTab(list[(index + delta + list.length) % list.length]);
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
        if (!isVisible(tab) && !tab.unread) {
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
        ev.code === 4404 || ev.code === 4400 || ev.code === 4408 || ev.code === 4409
      ) {
        setTabStatus(tab, "disconnected", "연결 종료");
        if (ev.code === 4000)
          tab.term.write("\r\n\x1b[38;5;210m[W-Term] 다른 클라이언트가 연결하여 종료되었습니다.\x1b[0m\r\n");
        if (ev.code === 4401) showLogin("인증이 만료되었습니다. 다시 로그인하세요.");
        // 4408(유휴 종료)·4409(사용자 종료)에서 재연결하면 방금 정리한 세션이
        // 곧바로 다시 뜬다. 사유는 서버가 닫기 직전 보낸 status 메시지에 이미
        // 찍혀 있다. 나머지는 재연결해봐야 같은 이유로 거절당하는 설정 문제다.
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
    // 이미 열려 있으면 그 탭이 있는 창으로 간다 — 창을 옮겨 오지는 않는다.
    // 새로 여는 것은 포커스된 창이다.
    const tab = findTab(name, agent) || createTab(name, agent, focusedPane);
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
    // 복원은 목록을 받은 뒤에야 할 수 있다 — 그 세션이 아직 살아 있는지를
    // 여기서만 알 수 있기 때문이다. 첫 목록에서 한 번만 시도한다(로그인이
    // 필요한 상태였다면 위에서 빠져나가므로, 로그인 직후의 호출이 그 한 번이 된다).
    if (!hashRestored) {
      hashRestored = true;
      restoreFromHash();
    }
  }

  function renderProjects() {
    projectListEl.replaceChildren();
    for (const p of lastProjects) {
      const card = document.createElement("div");
      const focused = activeTab();
      const cardActive = focused !== null && focused.name === p.name;
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

      // 탭을 닫는 것은 종료가 아니라 분리다 — 세션은 유예 시간 동안 살아 있고,
      // 그래서 다시 열면 화면이 복원된다. 실제로 끝내려면 이 경로가 필요하다.
      // 라이브일 때만 나온다 (끝낼 것이 없으면 버튼도 없다).
      function makeEndButton(agent, label, live) {
        if (!live) return null;
        const key = `${p.name}#${agent}`;
        const btn = document.createElement("button");
        btn.className = "end";
        btn.title = `실행 중인 ${label} 세션을 종료합니다`;
        // 진행 중 상태는 버튼이 아니라 endingKeys가 들고 있다 — 목록은 10초마다
        // 통째로 다시 그려지므로 버튼에만 담아두면 그때 지워진다.
        const ending = endingKeys.has(key);
        btn.textContent = ending ? "종료 중…" : "종료";
        btn.disabled = ending;
        btn.onclick = async () => {
          if (!confirm(`'${p.name}'의 실행 중인 ${label} 세션을 종료할까요?`)) return;
          // 대화형 셸은 SIGTERM을 무시해서 SIGKILL까지 시간이 걸린다. 그동안
          // 아무 표시가 없으면 버튼이 먹지 않은 것으로 보인다.
          endingKeys.add(key);
          btn.disabled = true;
          btn.textContent = "종료 중…";
          try {
            const res = await fetch(
              `/api/session/end?project=${encodeURIComponent(p.name)}` +
              `&agent=${encodeURIComponent(agent)}`,
              { method: "POST" }
            );
            if (res.status === 401) showLogin();
          } catch {
            /* 아래 loadProjects가 실제 상태를 다시 가져온다 */
          } finally {
            endingKeys.delete(key);
          }
          loadProjects();
        };
        return btn;
      }

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
        const endBtn = makeEndButton(agent, label, live);
        if (endBtn) row.appendChild(endBtn);
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
      const endShellBtn = makeEndButton("shell", "셸", p.shell_live);
      if (endShellBtn) shellRow.appendChild(endShellBtn);
      actions.append(shellRow);
      card.append(nameEl, pathEl, actions);
      projectListEl.appendChild(card);
    }
  }

  // 창 하나로 시작한다. 두 번째는 분할 버튼을 눌러야 생긴다.
  focusPane(createPane());
  layoutPanes();
  applyWidthLimit();

  loadProjects();
  // 붙어 있는 소켓이 없는 탭이 하나라도 있으면(또는 탭이 없으면) 배지가 바뀌어도
  // 알 방법이 없다. 소켓이 전부 살아 있을 때는 그쪽 이벤트로 갱신되므로 쉰다.
  setInterval(() => {
    const open = allTabs();
    if (open.length === 0 || open.some((t) => !t.ws)) loadProjects();
  }, 10000);
})();
