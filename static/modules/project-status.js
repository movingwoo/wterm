/* 프로젝트 상태 WebSocket의 수명과 재연결만 담당한다. */

export function createProjectStatusChannel({
  hasProjects,
  onProjects,
  onUnauthorized,
  onWaiting,
}) {
  let socket = null;
  let reconnectTimer = null;
  let reconnectAttempts = 0;
  let enabled = true;

  function stop() {
    enabled = false;
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
    const old = socket;
    socket = null;
    if (old) old.close();
  }

  function connect() {
    if (!enabled) return;
    if (
      socket &&
      (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)
    ) return;
    clearTimeout(reconnectTimer);

    const proto = location.protocol === "https:" ? "wss" : "ws";
    const current = new WebSocket(`${proto}://${location.host}/api/projects/ws`);
    socket = current;
    if (!hasProjects()) onWaiting("프로젝트 상태 연결 중…");

    current.onopen = () => {
      if (socket !== current) return;
      reconnectAttempts = 0;
    };
    current.onmessage = (event) => {
      if (socket !== current || typeof event.data !== "string") return;
      try {
        const message = JSON.parse(event.data);
        if (message.type === "projects") onProjects(message.projects);
      } catch {
        // 잘못된 프레임 하나가 상태 채널의 재연결까지 깨뜨리지는 않는다.
      }
    };
    current.onclose = (event) => {
      if (socket !== current) return;
      socket = null;
      if (!enabled) return;
      if (event.code === 4401) {
        enabled = false;
        onUnauthorized("인증이 만료되었습니다. 다시 로그인하세요.");
        return;
      }
      // 서버 재시작처럼 오래 끊겨도 새로고침 없이 돌아온다. 속도만 제한하고
      // 횟수에는 상한을 두지 않는다.
      reconnectAttempts += 1;
      const delay = Math.min(1000 * reconnectAttempts, 5000);
      if (!hasProjects()) onWaiting("프로젝트 상태에 다시 연결하는 중…");
      reconnectTimer = setTimeout(connect, delay);
    };
  }

  function start() {
    // 로그인 직후에는 직전 토큰으로 연 소켓이 아직 4401 close를 받는 중일 수 있다.
    // 그것을 재사용하면 새 로그인이 곧바로 다시 잠기므로 항상 교체한다.
    stop();
    enabled = true;
    reconnectAttempts = 0;
    connect();
  }

  return { start, stop };
}
