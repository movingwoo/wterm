/* CSS 토큰을 xterm 팔레트와 [W-Term] SGR에 연결하는 테마 controller. */

const THEMES = [["system", "시스템"], ["light", "라이트"], ["dark", "다크"]];
const TERM_COLORS = [
  ["background", "bg"], ["foreground", "fg"], ["cursor", "cursor"],
  ["black", "black"], ["red", "red"], ["green", "green"],
  ["yellow", "yellow"], ["blue", "blue"], ["magenta", "magenta"],
  ["cyan", "cyan"], ["white", "white"],
  ["brightBlack", "bright-black"], ["brightRed", "bright-red"],
  ["brightGreen", "bright-green"], ["brightYellow", "bright-yellow"],
  ["brightBlue", "bright-blue"], ["brightMagenta", "bright-magenta"],
  ["brightCyan", "bright-cyan"], ["brightWhite", "bright-white"],
];

export function createThemeController({ picker, getTabs, refreshSearches }) {
  const storageKey = "wterm.theme";
  const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
  let theme = "system";
  try {
    const saved = localStorage.getItem(storageKey);
    if (THEMES.some(([value]) => value === saved)) theme = saved;
  } catch {
    // 저장소가 막혀 있으면 이번 페이지에서만 유지한다.
  }

  function xtermTheme() {
    const css = getComputedStyle(document.documentElement);
    const result = {};
    for (const [key, token] of TERM_COLORS) {
      // 다크의 ANSI 16색처럼 빈 토큰은 키째 빼 xterm 기본 팔레트를 유지한다.
      const value = css.getPropertyValue(`--term-${token}`).trim();
      if (value) result[key] = value;
    }
    return result;
  }

  function resolvedTheme() {
    if (theme !== "system") return theme;
    return darkQuery.matches ? "dark" : "light";
  }

  function noticeSgr(kind) {
    const light = resolvedTheme() === "light";
    if (kind === "alert") return light ? "\x1b[38;5;125m" : "\x1b[38;5;210m";
    return light ? "\x1b[38;5;92m" : "\x1b[38;5;183m";
  }

  function apply() {
    if (theme === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", theme);
    for (const button of picker.children) {
      button.setAttribute("aria-pressed", String(button.dataset.theme === theme));
    }
    const colors = xtermTheme();
    for (const tab of getTabs()) tab.term.options.theme = colors;
    // 이미 그린 검색 장식은 생성 당시의 색을 들고 있어 다시 만들어야 한다.
    refreshSearches();
  }

  for (const [value, label] of THEMES) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.theme = value;
    button.textContent = label;
    button.addEventListener("click", () => {
      theme = value;
      try {
        localStorage.setItem(storageKey, value);
      } catch {
        // 현재 페이지의 테마 전환은 그대로 유지된다.
      }
      apply();
    });
    picker.appendChild(button);
  }

  darkQuery.addEventListener("change", () => {
    if (theme === "system") apply();
  });
  apply();

  return { xtermTheme, noticeSgr };
}
