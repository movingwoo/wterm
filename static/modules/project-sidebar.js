/* 프로젝트 카드 DOM과 세션 시작/재접속/종료 액션을 담당한다. */

export function createProjectSidebar({
  root,
  getProjects,
  getActiveTab,
  findTab,
  activateTab,
  openTab,
  showLogin,
}) {
  // 응답 전에 상태 푸시가 와서 목록을 다시 그릴 수 있으므로 DOM 버튼이 아니라
  // 세션 키에 진행 상태를 둔다.
  const endingKeys = new Set();

  function makeEndButton(project, agent, label, live) {
    if (!live) return null;
    const key = `${project.name}#${agent}`;
    const button = document.createElement("button");
    button.className = "end";
    button.title = endingKeys.has(key)
      ? `${label} 세션을 종료하는 중입니다`
      : `실행 중인 ${label} 세션을 종료합니다`;
    // 진행 중 표시는 "종료"와 같은 폭이어야 한다. 더 긴 글자는 그 순간 카드의
    // grid track을 다시 배분해 이웃 버튼을 두 줄로 접는다.
    const ending = endingKeys.has(key);
    button.textContent = ending ? "…" : "종료";
    button.disabled = ending;
    button.setAttribute("aria-busy", String(ending));
    button.onclick = async () => {
      if (!confirm(`'${project.name}'의 실행 중인 ${label} 세션을 종료할까요?`)) return;
      endingKeys.add(key);
      button.disabled = true;
      button.textContent = "…";
      button.setAttribute("aria-busy", "true");
      try {
        const response = await fetch(
          `/api/session/end?project=${encodeURIComponent(project.name)}` +
          `&agent=${encodeURIComponent(agent)}`,
          { method: "POST" }
        );
        if (response.status === 401) showLogin();
      } catch {
        // 아래 렌더에서 버튼을 복구한다. 실제 상태는 서버 푸시가 갱신한다.
      } finally {
        endingKeys.delete(key);
      }
      render();
    };
    return button;
  }

  function makeAgentRow(project, label, agent, live, hasHistory) {
    const row = document.createElement("div");
    row.className = "agent-row";
    const labelEl = document.createElement("span");
    labelEl.className = `agent-label ${agent}`;
    labelEl.textContent = label;

    const open = findTab(project.name, agent);
    const resumeButton = document.createElement("button");
    resumeButton.className = "primary";
    if (open && open.ws) {
      // 같은 세션을 다시 연결하면 서버가 기존 controller를 4000으로 끊는다.
      resumeButton.textContent = "보기";
      resumeButton.title = "열려 있는 탭으로 이동";
      resumeButton.onclick = () => activateTab(open);
    } else {
      resumeButton.textContent = live ? "재접속" : "이어하기";
      resumeButton.title = live
        ? `실행 중인 ${label} 세션에 재접속`
        : `${label}의 이전 세션 목록에서 선택해 이어하기`;
      resumeButton.disabled = !live && !hasHistory;
      resumeButton.onclick = () => openTab(project.name, agent, "resume");
    }

    const newButton = document.createElement("button");
    newButton.textContent = "새 세션";
    newButton.onclick = () => {
      if (
        live &&
        !confirm(`'${project.name}'의 실행 중인 ${label} 세션을 종료하고 새로 시작할까요?`)
      ) return;
      openTab(project.name, agent, "new");
    };
    row.append(labelEl, resumeButton, newButton);
    const endButton = makeEndButton(project, agent, label, live);
    // has-end가 있는 행만 네 번째 track을 연다. 빈 track은 오른쪽 정렬을 깨뜨린다.
    if (endButton) {
      row.appendChild(endButton);
      row.classList.add("has-end");
    }
    return row;
  }

  function render() {
    root.replaceChildren();
    for (const project of getProjects()) {
      const card = document.createElement("div");
      const focused = getActiveTab();
      const active = focused !== null && focused.name === project.name;
      card.className = "project" + (active ? " active" : "");

      const nameEl = document.createElement("div");
      nameEl.className = "name";
      nameEl.textContent = project.name;
      for (const [live, cls, text] of [
        [project.live, "", "CLAUDE"],
        [project.codex_live, "codex", "CODEX"],
        [project.shell_live, "shell", "SHELL"],
      ]) {
        if (!live) continue;
        const badge = document.createElement("span");
        badge.className = `badge${cls ? ` ${cls}` : ""}`;
        badge.textContent = text;
        nameEl.appendChild(badge);
      }

      const pathEl = document.createElement("div");
      pathEl.className = "path";
      pathEl.textContent = project.ssh ? `${project.ssh}:${project.path}` : project.path;

      const actions = document.createElement("div");
      actions.className = "actions";
      actions.append(
        makeAgentRow(project, "Claude", "claude", project.live, project.has_history),
        makeAgentRow(
          project, "Codex", "codex", project.codex_live, project.codex_has_history
        )
      );

      const openShell = findTab(project.name, "shell");
      const shellButton = document.createElement("button");
      if (openShell && openShell.ws) {
        shellButton.textContent = "셸 보기";
        shellButton.title = "열려 있는 셸 탭으로 이동";
        shellButton.onclick = () => activateTab(openShell);
      } else {
        shellButton.textContent = project.shell_live ? "셸 재접속" : "셸";
        shellButton.title = project.ssh
          ? `원격 셸 열기 (ssh ${project.ssh})`
          : "이 디렉터리에서 로컬 셸 열기";
        shellButton.onclick = () => openTab(project.name, "shell", "attach");
      }

      const shellRow = document.createElement("div");
      shellRow.className = "agent-row shell-row";
      const shellLabel = document.createElement("span");
      shellLabel.className = "agent-label shell";
      shellLabel.textContent = "Shell";
      shellRow.append(shellLabel, shellButton);
      const endShellButton = makeEndButton(
        project, "shell", "셸", project.shell_live
      );
      if (endShellButton) {
        shellRow.appendChild(endShellButton);
        shellRow.classList.add("has-end");
      }
      actions.append(shellRow);
      card.append(nameEl, pathEl, actions);
      root.appendChild(card);
    }
  }

  return { render };
}
