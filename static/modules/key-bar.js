/* 모바일 특수키와 전역 터미널 글자 크기 preference. */

const MIN_FONT_SIZE = 12;
const MAX_FONT_SIZE = 20;
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

export function createKeyBar({
  root,
  mobileLayout,
  getTabs,
  getActiveTab,
  sendJson,
  scheduleFit,
}) {
  const storageKey = "wterm.terminal.fontSize";
  let fontSizePinned = false;
  let fontSize = mobileLayout.matches ? 16 : 14;
  try {
    const saved = Number(localStorage.getItem(storageKey));
    if (Number.isInteger(saved) && saved >= MIN_FONT_SIZE && saved <= MAX_FONT_SIZE) {
      fontSize = saved;
      fontSizePinned = true;
    }
  } catch {
    // 저장소가 막혀 있으면 반응형 기본값과 현재 페이지의 변경만 유지한다.
  }

  let smallerFont = null;
  let largerFont = null;
  let fontSizeOutput = null;
  let ctrlArmed = false;
  let ctrlKey = null;

  function paintFontControls() {
    if (!fontSizeOutput) return;
    fontSizeOutput.value = String(fontSize);
    fontSizeOutput.textContent = `${fontSize}px`;
    smallerFont.disabled = fontSize <= MIN_FONT_SIZE;
    largerFont.disabled = fontSize >= MAX_FONT_SIZE;
    smallerFont.setAttribute("aria-label", `터미널 글자 작게 (현재 ${fontSize}px)`);
    largerFont.setAttribute("aria-label", `터미널 글자 크게 (현재 ${fontSize}px)`);
  }

  function setFontSize(value, persist = false) {
    const next = Math.max(MIN_FONT_SIZE, Math.min(MAX_FONT_SIZE, value));
    if (next === fontSize && !persist) return;
    fontSize = next;
    if (persist) {
      fontSizePinned = true;
      try {
        localStorage.setItem(storageKey, String(next));
      } catch {
        // 현재 페이지에서는 바뀐 크기를 유지한다.
      }
    }
    for (const tab of getTabs()) tab.term.options.fontSize = next;
    paintFontControls();
    scheduleFit();
  }

  function setCtrlArmed(on) {
    ctrlArmed = on;
    if (ctrlKey) {
      ctrlKey.classList.toggle("armed", on);
      ctrlKey.setAttribute("aria-pressed", String(on));
    }
  }

  function applyCtrl(data) {
    if (!ctrlArmed) return data;
    setCtrlArmed(false);
    if (data.length !== 1) return data;
    const code = data.toUpperCase().charCodeAt(0);
    return code >= 0x40 && code <= 0x5f ? String.fromCharCode(code & 0x1f) : data;
  }

  function makeButton(label, title) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "key";
    button.textContent = label;
    button.title = title;
    // 포커스 이동을 막아 소프트 키보드가 접히지 않게 한다. click은 그대로 온다.
    button.addEventListener("pointerdown", (event) => event.preventDefault());
    return button;
  }

  function focusActiveTab() {
    const tab = getActiveTab();
    if (tab) tab.term.focus();
    return tab;
  }

  const fontControls = document.createElement("div");
  fontControls.className = "font-controls";
  fontControls.setAttribute("role", "group");
  fontControls.setAttribute("aria-label", "터미널 글자 크기");

  smallerFont = makeButton("A−", "터미널 글자 작게");
  smallerFont.classList.add("font-key");
  smallerFont.addEventListener("click", () => {
    setFontSize(fontSize - 1, true);
    focusActiveTab();
  });
  fontSizeOutput = document.createElement("output");
  fontSizeOutput.setAttribute("aria-live", "polite");
  largerFont = makeButton("A+", "터미널 글자 크게");
  largerFont.classList.add("font-key");
  largerFont.addEventListener("click", () => {
    setFontSize(fontSize + 1, true);
    focusActiveTab();
  });
  fontControls.append(smallerFont, fontSizeOutput, largerFont);
  root.appendChild(fontControls);
  paintFontControls();

  for (const [label, sequence, title] of BAR_KEYS) {
    const button = makeButton(label, title);
    if (sequence === null) {
      ctrlKey = button;
      button.setAttribute("aria-pressed", "false");
      button.addEventListener("click", () => {
        setCtrlArmed(!ctrlArmed);
        focusActiveTab();
      });
    } else {
      button.addEventListener("click", () => {
        const tab = getActiveTab();
        if (!tab) return;
        setCtrlArmed(false);
        sendJson(tab, { type: "input", data: sequence });
        tab.term.focus();
      });
    }
    root.appendChild(button);
  }

  mobileLayout.addEventListener("change", (event) => {
    if (!fontSizePinned) setFontSize(event.matches ? 16 : 14);
  });

  return {
    applyCtrl,
    getFontSize: () => fontSize,
    setCtrlArmed,
  };
}
