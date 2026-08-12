import { createProjectSidebar } from "./modules/project-sidebar.js";
import { createProjectStatusChannel } from "./modules/project-status.js";
import { createThemeController } from "./modules/theme.js";
import { createKeyBar } from "./modules/key-bar.js";

/* W-Term 프론트엔드: 탭별 xterm.js 터미널 + pane/입력/알림 조정 */
(() => {
  "use strict";

  const sidebarEl = document.getElementById("sidebar");
  const sidebarToggleEl = document.getElementById("sidebar-toggle");
  const sidebarScrimEl = document.getElementById("sidebar-scrim");
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
  const themePickerEl = document.getElementById("theme-picker");

  const AGENT_LABEL = { claude: "Claude", codex: "Codex", shell: "셸" };
  const BASE_TITLE = document.title;
  // 터치 중심 장치에서는 폭이 넓은 태블릿도 사이드바를 서랍으로 쓴다. 데스크톱은
  // 창을 640px 아래로 줄였을 때 같은 상태가 되어 브라우저에서 검증할 수 있다.
  const mobileLayout = window.matchMedia("(pointer: coarse), (max-width: 640px)");

  // 맥에서는 복붙 수식키가 ⌘라 Ctrl이 통째로 터미널 몫이지만, 윈도우·리눅스에는 ⌘가
  // 없어 브라우저 복붙과 터미널 제어문자가 같은 Ctrl을 두고 부딪친다 — createTab의
  // 키 핸들러 주석 참고. userAgentData는 아직 없는 브라우저가 있어 platform으로 뒤를 받친다.
  const IS_MAC = /mac|iphone|ipad/i.test(
    navigator.userAgentData?.platform || navigator.platform || ""
  );

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
  let nextPaneId = 1;
  let focusedPane = null;
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

  const projectStatus = createProjectStatusChannel({
    hasProjects: () => lastProjects.length > 0,
    onProjects: applyProjects,
    onUnauthorized: showLogin,
    onWaiting: (message) => { projectListEl.textContent = message; },
  });

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
      projectStatus.start();
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
    // 서버가 토큰을 폐기하며 상태 소켓을 4401로 닫기 전에 의도적으로 떼어 둔다.
    // 그렇지 않으면 정상 로그아웃 중간에 "인증 만료" 메시지가 먼저 번쩍인다.
    projectStatus.stop();
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
    // 모바일에서 투명해진 스크림은 포인터뿐 아니라 접근성 트리에서도 빠져야 한다.
    // display:none인 데스크톱에는 영향이 없고, 열린 서랍에서는 닫기 동작으로 읽힌다.
    sidebarScrimEl.setAttribute("aria-hidden", String(collapsed));
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

  let savedSidebarCollapsed = false;
  try {
    savedSidebarCollapsed = localStorage.getItem(sidebarStorageKey) === "true";
  } catch {
    // localStorage 사용 불가 시 기본값(펼침)을 유지한다.
  }
  let sidebarCollapsed = savedSidebarCollapsed;
  setSidebarCollapsed(sidebarCollapsed);

  sidebarToggleEl.addEventListener("click", () => {
    sidebarCollapsed = !sidebarCollapsed;
    savedSidebarCollapsed = sidebarCollapsed;
    setSidebarCollapsed(sidebarCollapsed, true);
  });

  // 모바일 서랍 뒤쪽을 누르면 닫되, 데스크톱에서 저장한 접기 선호까지 바꾸지는
  // 않는다. 창을 다시 넓히면 원래 데스크톱 상태로 돌아간다.
  sidebarScrimEl.addEventListener("click", () => {
    sidebarCollapsed = true;
    setSidebarCollapsed(true);
  });

  function collapseSidebarForMobile() {
    if (!mobileLayout.matches || sidebarCollapsed) return;
    sidebarCollapsed = true;
    setSidebarCollapsed(true);
  }

  mobileLayout.addEventListener("change", (e) => {
    if (!e.matches && sidebarCollapsed !== savedSidebarCollapsed) {
      sidebarCollapsed = savedSidebarCollapsed;
      setSidebarCollapsed(sidebarCollapsed);
    }
  });

  // ── 테마 ───────────────────────────────────────────────────────────

  const { xtermTheme, noticeSgr } = createThemeController({
    picker: themePickerEl,
    getTabs: allTabs,
    refreshSearches: refreshOpenSearches,
  });

  // ── 모바일 키 바 ───────────────────────────────────────────────────

  const { applyCtrl, getFontSize, setCtrlArmed } = createKeyBar({
    root: keyBarEl,
    mobileLayout,
    getTabs: allTabs,
    getActiveTab: activeTab,
    sendJson,
    scheduleFit,
  });

  // ── 대기 알림 ──────────────────────────────────────────────────────
  //
  // "노트북 덮고 나갔다 돌아온다"가 이 도구의 용도인데, Claude가 질문을 띄우고
  // 멈춘 것을 알 방법이 없어 결국 주기적으로 들여다보게 된다. TUI가 사람을 부를 때
  // 내는 BEL을 잡아 탭과 문서 제목을 바꾸고, 권한이 있으면 알림까지 띄운다.
  //
  // 감지는 클라이언트에서 한다 — 서버는 터미널 내용을 들여다보지 않는다(AGENTS.md).
  // 판정도 우리가 바이트를 뒤지는 대신 xterm 파서에 맡긴다: 셸 프롬프트가 매번
  // 내보내는 창 제목 시퀀스(OSC ... BEL)의 끝에도 BEL이 있어서, 직접 스캔하면
  // 프롬프트가 뜰 때마다 오탐이 된다.
  //
  // 받는 경로는 둘이다. CLI마다 사람을 부르는 방식이 다르기 때문이다:
  //   - BEL(`\a`) → term.onBell. Claude Code의 terminal_bell 채널.
  //   - OSC 9 → 아래 registerOscHandler(9). Codex의 tui.notifications와
  //     Claude Code의 iterm2 채널이 쓰는 형식이다. 끝이 BEL이라 겉보기에는
  //     비슷하지만 파서는 이것을 OSC 문자열로 먹으므로 onBell이 뜨지 않는다 —
  //     바로 위에서 "직접 스캔하면 오탐"이라고 한 그 시퀀스다.
  // 채널을 켜는 것은 server/session.py의 _agent_cmd가 세션마다 해 준다.
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

  // ── 터미널 검색 ────────────────────────────────────────────────────
  //
  // 검색 대상은 서버 출력 스트림이 아니라 각 xterm의 현재 스크롤백 버퍼다. 서버가
  // 터미널 내용을 들여다보지 않는 경계는 그대로고, 탭마다 SearchAddon 하나를 둬서
  // 같은 프로젝트의 세션이나 분할된 두 창이 서로의 선택을 건드리지 않는다.
  //
  // 검색 상자는 term-stack 위에 겹쳐 띄운다. 탭 줄과 스택 사이에 끼우면 열고 닫을
  // 때마다 터미널 높이가 달라지고 PTY에도 resize가 전송된다. 출력 몇 줄을 가리는
  // 작은 오버레이가 검색 한 번에 원격 TUI의 행 수를 바꾸는 것보다 예측 가능하다.

  function searchDecorationOptions() {
    const css = getComputedStyle(document.documentElement);
    const token = (name) => css.getPropertyValue(`--term-search-${name}`).trim();
    return {
      matchBackground: token("match"),
      matchOverviewRuler: token("ruler"),
      activeMatchBackground: token("active"),
      activeMatchColorOverviewRuler: token("ruler"),
    };
  }

  function paintSearchResults(tab, { resultIndex, resultCount }) {
    const pane = tab.pane;
    if (!pane || pane.activeTab !== tab || pane.searchEl.hidden) return;
    if (!pane.searchInputEl.value) {
      pane.searchResultEl.textContent = "";
    } else if (resultCount === 0) {
      pane.searchResultEl.textContent = "없음";
    } else if (resultIndex < 0) {
      // 애드온은 장식 상한(기본 1000개)을 넘으면 현재 인덱스를 -1로 준다.
      pane.searchResultEl.textContent = `${resultCount}+`;
    } else {
      pane.searchResultEl.textContent = `${resultIndex + 1}/${resultCount}`;
    }
  }

  function runSearch(pane, direction = "next", incremental = false) {
    const tab = pane.activeTab;
    if (!tab) return;
    // 한 창에서 보이는 검색은 활성 탭 하나뿐이다. 이전에 검색했던 숨은 탭의
    // 장식을 남기면 다시 열었을 때 다른 검색어가 이유 없이 칠해져 있다.
    for (const other of pane.tabs) {
      if (other !== tab) other.searchAddon.clearDecorations();
    }
    const query = pane.searchInputEl.value;
    if (!query) {
      tab.searchAddon.clearDecorations();
      pane.searchResultEl.textContent = "";
      return;
    }
    const options = {
      incremental,
      decorations: searchDecorationOptions(),
    };
    if (direction === "previous") tab.searchAddon.findPrevious(query, options);
    else tab.searchAddon.findNext(query, options);
  }

  function refreshPaneSearch(pane) {
    // clearDecorations가 애드온의 캐시된 검색어도 지운다. 탭 또는 테마가 바뀐 뒤
    // 같은 검색어를 다시 넣어도 장식 전체가 확실히 새로 만들어지는 이유다.
    for (const tab of pane.tabs) tab.searchAddon.clearDecorations();
    pane.searchResultEl.textContent = "";
    if (!pane.searchEl.hidden && pane.activeTab && pane.searchInputEl.value) {
      runSearch(pane, "next", true);
    }
  }

  function refreshOpenSearches() {
    for (const pane of panes) {
      if (!pane.searchEl.hidden) refreshPaneSearch(pane);
    }
  }

  function openSearch(pane) {
    if (!pane.activeTab) return;
    pane.searchEl.hidden = false;
    pane.searchBtnEl.setAttribute("aria-expanded", "true");
    if (pane.searchInputEl.value) refreshPaneSearch(pane);
    pane.searchInputEl.focus();
    pane.searchInputEl.select();
  }

  function closeSearch(pane, { focus = true } = {}) {
    pane.searchEl.hidden = true;
    pane.searchBtnEl.setAttribute("aria-expanded", "false");
    pane.searchResultEl.textContent = "";
    for (const tab of pane.tabs) tab.searchAddon.clearDecorations();
    if (focus && pane.activeTab) pane.activeTab.term.focus();
  }

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
    const paneId = nextPaneId++;
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

    const searchBtnEl = document.createElement("button");
    searchBtnEl.type = "button";
    searchBtnEl.className = "pane-btn pane-search-btn";
    searchBtnEl.textContent = "검색";
    searchBtnEl.title = `터미널 버퍼 검색 (${IS_MAC ? "⌘F" : "Ctrl+F"})`;
    searchBtnEl.setAttribute("aria-label", searchBtnEl.title);
    searchBtnEl.setAttribute("aria-controls", `pane-search-${paneId}`);
    searchBtnEl.setAttribute("aria-expanded", "false");

    const splitEl = document.createElement("button");
    splitEl.type = "button";
    splitEl.className = "pane-btn pane-split-btn";
    splitEl.addEventListener("click", () => {
      if (panes.length < MAX_PANES) addPane();
      else removePane(pane);
    });

    actionsEl.append(searchBtnEl, splitEl);

    const stackEl = document.createElement("div");
    stackEl.className = "term-stack";

    const placeholderEl = document.createElement("div");
    placeholderEl.className = "pane-placeholder";
    placeholderEl.textContent = "좌측에서 프로젝트를 선택하세요.";
    stackEl.appendChild(placeholderEl);

    const searchEl = document.createElement("form");
    searchEl.id = `pane-search-${paneId}`;
    searchEl.className = "search-bar";
    searchEl.setAttribute("role", "search");
    searchEl.hidden = true;

    const searchInputEl = document.createElement("input");
    searchInputEl.type = "search";
    searchInputEl.placeholder = "터미널 검색";
    searchInputEl.autocomplete = "off";
    searchInputEl.spellcheck = false;
    searchInputEl.enterKeyHint = "search";
    searchInputEl.setAttribute("aria-label", "터미널 버퍼 검색어");

    const searchResultEl = document.createElement("output");
    searchResultEl.className = "search-result";
    searchResultEl.setAttribute("aria-live", "polite");
    searchResultEl.setAttribute("aria-atomic", "true");

    function searchBarButton(text, label) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = text;
      btn.title = label;
      btn.setAttribute("aria-label", label);
      return btn;
    }

    const searchPreviousEl = searchBarButton("↑", "이전 일치 항목 (Shift+Enter)");
    const searchNextEl = searchBarButton("↓", "다음 일치 항목 (Enter)");
    const searchCloseEl = searchBarButton("×", "검색 닫기 (Esc)");
    searchEl.append(
      searchInputEl, searchResultEl, searchPreviousEl, searchNextEl, searchCloseEl
    );
    stackEl.appendChild(searchEl);

    headEl.append(tabBarEl, actionsEl);
    el.append(headEl, stackEl);

    const pane = {
      el, tabBarEl, actionsEl, splitEl, stackEl, searchBtnEl, searchEl,
      searchInputEl, searchResultEl, tabs: [], activeTab: null,
    };
    searchBtnEl.addEventListener("click", () => openSearch(pane));
    searchEl.addEventListener("submit", (e) => {
      e.preventDefault();
      runSearch(pane);
    });
    searchInputEl.addEventListener("input", () => runSearch(pane, "next", true));
    searchInputEl.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      runSearch(pane, e.shiftKey ? "previous" : "next");
    });
    searchPreviousEl.addEventListener("click", () => runSearch(pane, "previous"));
    searchNextEl.addEventListener("click", () => runSearch(pane));
    searchCloseEl.addEventListener("click", () => closeSearch(pane));
    // 창 안 어디를 눌러도 그 창이 포커스를 가져간다. 키 바와 상태줄, 주소 해시가
    // 따라오므로 "지금 어느 창을 쓰고 있나"가 클릭 한 번으로 정해져야 한다.
    //
    // 창의 포커스와 별개로 키보드 포커스도 터미널에 넘긴다. xterm은 자기 화면을
    // 눌렀을 때만 textarea에 포커스를 주므로, term-host의 여백이나 마지막 행 아래
    // 빈자리를 누르면 포커스가 직전 버튼이나 body에 남는다. 그 상태에서도 드래그
    // 선택은 되지만(선택은 마우스 이벤트만 보고 포커스와 무관하다) 복사·붙여넣기는
    // 조용히 안 된다 — copy/paste 이벤트가 포커스된 요소에서 올라오는데 그게
    // 터미널 밖이면 xterm의 리스너에 닿지 않는다. 선택 하이라이트는 멀쩡히 보이니
    // 원인을 짐작할 단서도 없다. 탭 줄의 버튼까지 뺏지 않도록 대상이 터미널 더미
    // 안일 때만 옮긴다. 터치는 제외한다 — 스크롤하려고 짚은 손가락에 소프트
    // 키보드가 올라온다.
    el.addEventListener("pointerdown", (e) => {
      focusPane(pane);
      if (e.pointerType === "touch") return;
      if (pane.activeTab && stackEl.contains(e.target)) pane.activeTab.term.focus();
    }, true);

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
    tab.searchAddon.clearDecorations();
    from.tabs.splice(from.tabs.indexOf(tab), 1);
    pane.tabs.push(tab);
    tab.pane = pane;
    // xterm은 컨테이너째 옮겨도 살아남는다 — 우리가 옮기는 것은 host 하나이고
    // 터미널 DOM은 그 안에 통째로 들어 있다. 다만 새 창의 폭에 맞춰 다시 재야 한다.
    pane.tabBarEl.appendChild(tab.el);
    pane.stackEl.appendChild(tab.hostEl);
    if (from.activeTab === tab) {
      from.activeTab = from.tabs[from.tabs.length - 1] || null;
      if (from.activeTab) refreshPaneSearch(from);
      else closeSearch(from, { focus: false });
    }
    pane.activeTab = tab;
    if (refit) {
      activateTab(tab); // 포커스·해시·크기·상태줄을 한 곳에서 맞춘다
      // 옮긴 직후 한 번은 다시 그린다. 폭이 달라진 상자로 옮겨왔는데 fit이
      // 같은 열 수를 내놓으면 xterm은 리사이즈가 없었다고 보고 다시 그리지 않는다.
      tab.term.refresh(0, tab.term.rows - 1);
    } else refreshPaneSearch(pane);
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
      pane.searchBtnEl.disabled = pane.tabs.length === 0;
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
    const previous = pane.activeTab;
    if (previous && previous !== tab) previous.searchAddon.clearDecorations();
    pane.activeTab = tab;
    tab.unread = false;
    tab.el.classList.remove("unread");
    clearBell(tab);
    // 걸려 있던 Ctrl은 탭을 옮기면 푼다. 안 그러면 옆 세션의 첫 글자가 제어문자로
    // 나가는데, 그건 여기서 누른 적이 없는 키다.
    setCtrlArmed(false);
    focusedPane = pane;
    // 프로젝트를 골랐는데 서랍이 화면을 계속 덮고 있으면 터미널이 열린 사실조차
    // 보이지 않는다. 모바일에서만 자동으로 닫고 데스크톱 접기 선호는 건드리지 않는다.
    collapseSidebarForMobile();
    paintPanes();
    syncHash();
    tab.el.scrollIntoView({ block: "nearest", inline: "nearest" });
    setStatus(tab.statusCls, tab.statusText);
    fitTab(tab);
    refreshPaneSearch(pane);
    tab.term.focus();
    renderProjects();
  }

  function createTab(name, agent, pane) {
    const term = new Terminal({
      fontFamily: '"Cascadia Code", "D2Coding", Menlo, monospace',
      fontSize: getFontSize(),
      cursorBlink: true,
      scrollback: 5000,
      theme: xtermTheme(),
    });
    const fitAddon = new FitAddon.FitAddon();
    const searchAddon = new SearchAddon.SearchAddon();
    term.loadAddon(fitAddon);
    term.loadAddon(searchAddon);

    // 윈도우·리눅스의 복사·붙여넣기.
    //
    // 터미널에서 Ctrl+C/Ctrl+V는 제어문자(\x03, \x16)이고 xterm은 기본값 그대로
    // 그렇게 보낸다. 맥은 ⌘가 따로 있어 문제가 없지만, ⌘가 없는 쪽에서는 그 결과가
    // "복붙이 아예 안 되는 터미널"이다.
    //
    // false를 돌려주면 xterm은 그 키를 처리하지도, preventDefault를 걸지도 않는다.
    // 그래서 브라우저의 기본 복사/붙여넣기가 그대로 돌고, 붙여넣은 텍스트는 xterm이
    // textarea에 걸어둔 paste 리스너로 되돌아온다 — 즉 bracketed paste 처리도
    // 그대로다. 클립보드를 직접 읽지 않는 이유는 그래야 해서가 아니라 읽으면 안
    // 돼서다. navigator.clipboard.readText()는 클립보드 읽기 권한을 이 페이지에
    // 여는 것이고, 스크립트 주입이 곧 임의 명령 실행인 이 서버에서 주입된 코드에게
    // 사용자의 클립보드까지 얹어줄 이유가 없다. 여기서 클립보드에 닿는 것은 브라우저
    // 자신뿐이고, 이 코드가 하는 일은 키를 가로채지 않겠다고 말하는 것뿐이다.
    //
    // 잃는 것은 Ctrl+V의 원래 뜻인 readline quoted-insert(\x16)다. 이 도구를 쓰는
    // 자리에서 복붙보다 그쪽이 급한 경우는 없다. Ctrl+Shift+V는 원래도 브라우저가
    // 처리하던 경로라 그대로 남는다.
    //
    // `key`는 현재 키보드 배열과 IME가 해석한 문자라 한글 입력 상태에서는 "c"/"v"가
    // 아닐 수 있다. 데스크톱 단축키는 물리 키 위치인 `code`로 먼저 판별하고, code를
    // 주지 않는 오래된 브라우저만 key로 받친다. 이 분기가 빠지면 xterm이 Ctrl+C/V를
    // 다시 \x03/\x16으로 보내서 우클릭만 되고 키보드 복붙은 조용히 실패한다.
    term.attachCustomKeyEventHandler((e) => {
      if (IS_MAC || e.type !== "keydown") return true;
      if (!e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return true;
      const key = e.code === "KeyC" ? "c"
        : e.code === "KeyV" ? "v"
          : e.key.toLowerCase();
      if (key === "v") return false;
      // 선택이 없을 때의 Ctrl+C는 여전히 인터럽트다. 선택이 있을 때만 복사로
      // 넘기고, 복사한 뒤에는 선택을 지운다 — 남겨두면 다음 Ctrl+C도 복사로
      // 먹혀서 "인터럽트가 안 걸린다". 지우는 것을 미루는 것은 이 핸들러가
      // 끝난 뒤에야 브라우저가 copy 이벤트를 띄우고, xterm이 그때 선택 내용을
      // 읽어가기 때문이다.
      if (key === "c" && term.hasSelection()) {
        setTimeout(() => term.clearSelection(), 0);
        return false;
      }
      return true;
    });

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
      name, agent, term, fitAddon, searchAddon, hostEl, el, dotEl, moveEl,
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
    searchAddon.onDidChangeResults((results) => paintSearchResults(tab, results));

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
    // 부름 판정은 xterm 파서가 한다 — 위 "대기 알림" 주석 참고.
    term.onBell(() => onBell(tab));
    // OSC 9은 데스크톱 알림 문자열이다. false를 돌려주면 xterm이 원래 하던
    // 처리(모르는 시퀀스는 무시)를 마저 하므로 화면에는 아무 영향이 없다.
    term.parser.registerOscHandler(9, () => {
      onBell(tab);
      return false;
    });

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
        if (pane === focusedPane && !pane.activeTab) {
          closeSearch(pane, { focus: false });
          setStatus("disconnected", "연결 안 됨");
        }
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

  // 브라우저 찾기 대신 포커스된 창의 xterm 버퍼를 찾는다. xterm은 자기 textarea의
  // keydown에서 즉시 PTY 입력을 보내므로 캡처 단계에서 막아야 한다. 맥의 Ctrl+F는
  // 터미널의 forward-char로 남기고 브라우저 관례대로 ⌘F만 검색에 쓴다.
  document.addEventListener("keydown", (e) => {
    if (!focusedPane) return;
    if (e.key === "Escape" && !focusedPane.searchEl.hidden) {
      e.preventDefault();
      e.stopPropagation();
      closeSearch(focusedPane);
      return;
    }
    const findModifier = IS_MAC
      ? e.metaKey && !e.ctrlKey
      : e.ctrlKey && !e.metaKey;
    const findKey = e.code === "KeyF" || e.key.toLowerCase() === "f";
    if (!findModifier || e.altKey || e.shiftKey || !findKey) return;
    if (!focusedPane.activeTab) return;
    e.preventDefault();
    e.stopPropagation();
    openSearch(focusedPane);
  }, true);

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
      renderProjects();
    };

    sock.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        const msg = JSON.parse(ev.data);
        if (msg.type === "status") {
          tab.term.write(`\r\n${noticeSgr("info")}[W-Term] ${msg.message}\x1b[0m\r\n`);
        } else if (msg.type === "exit") {
          tab.intentionalClose = true;
          tab.term.write(
            `\r\n${noticeSgr("alert")}[W-Term] 세션이 종료되었습니다 (exit=${msg.code}).\x1b[0m\r\n`
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
      renderProjects();
      // 오리진 거절(4403)은 여기로 오지 않는다. 서버가 accept 전에 닫아 핸드셰이크가
      // HTTP 403으로 끝나고, 브라우저는 그것을 1006으로만 알려준다. 아래 재연결
      // 경로를 타고 "재연결 실패"로 끝나며, 원인은 서버 로그에 남는다.
      if (
        tab.intentionalClose || ev.code === 4000 || ev.code === 4401 ||
        ev.code === 4404 || ev.code === 4400 || ev.code === 4408 || ev.code === 4409
      ) {
        setTabStatus(tab, "disconnected", "연결 종료");
        if (ev.code === 4000)
          tab.term.write(`\r\n${noticeSgr("alert")}[W-Term] 다른 클라이언트가 연결하여 종료되었습니다.\x1b[0m\r\n`);
        if (ev.code === 4401) showLogin("인증이 만료되었습니다. 다시 로그인하세요.");
        // 4408(유휴 종료)·4409(사용자 종료)에서 재연결하면 방금 정리한 세션이
        // 곧바로 다시 뜬다. 사유는 서버가 닫기 직전 보낸 status 메시지에 이미
        // 찍혀 있다. 나머지는 재연결해봐야 같은 이유로 거절당하는 설정 문제다.
        if (ev.code === 4404 || ev.code === 4400)
          tab.term.write(`\r\n${noticeSgr("alert")}[W-Term] 연결이 거절되었습니다: ${ev.reason || ev.code}\x1b[0m\r\n`);
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

  function applyProjects(projects) {
    if (!Array.isArray(projects)) return;
    lastProjects = projects;
    renderProjects();
    // 복원은 목록을 받은 뒤에야 할 수 있다 — 그 세션이 아직 살아 있는지를
    // 여기서만 알 수 있기 때문이다. 첫 스냅샷에서 한 번만 시도한다(인증이
    // 필요했다면 로그인 뒤 새 상태 소켓이 받은 스냅샷이 그 한 번이 된다).
    if (!hashRestored) {
      hashRestored = true;
      restoreFromHash();
    }
  }

  const projectSidebar = createProjectSidebar({
    root: projectListEl,
    getProjects: () => lastProjects,
    getActiveTab: activeTab,
    findTab,
    activateTab,
    openTab,
    showLogin,
  });

  function renderProjects() {
    projectSidebar.render();
  }

  // 창 하나로 시작한다. 두 번째는 분할 버튼을 눌러야 생긴다.
  focusPane(createPane());
  layoutPanes();
  applyWidthLimit();

  projectStatus.start();
})();
