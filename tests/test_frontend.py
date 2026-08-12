"""빌드 단계 없는 프론트엔드의 벤더 연결을 고정한다.

브라우저 렌더링은 release-checklist에서 보지만, 전역 스크립트의 버전이나 순서가
틀리면 화면을 보기 전에도 확정적으로 실패하므로 소스 수준에서 먼저 잡는다.
"""

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_search_addon_matches_xterm_and_loads_before_app():
    xterm = (ROOT / "static" / "vendor" / "xterm.js").read_text()
    search = (ROOT / "static" / "vendor" / "addon-search.js").read_text()
    index = (ROOT / "static" / "index.html").read_text()
    app = (ROOT / "static" / "app.js").read_text()

    assert "@xterm/xterm@5.5.0" in xterm
    assert "@xterm/addon-search 0.15.0" in search
    assert index.index("/static/vendor/addon-search.js") < index.index(
        "/static/app.js"
    )
    assert "new SearchAddon.SearchAddon()" in app


def test_project_status_uses_push_without_browser_polling():
    app = (ROOT / "static" / "app.js").read_text()
    status = (ROOT / "static" / "modules" / "project-status.js").read_text()
    index = (ROOT / "static" / "index.html").read_text()

    assert 'new WebSocket(`${proto}://${location.host}/api/projects/ws`)' in status
    assert 'type="module" src="/static/app.js"' in index
    assert 'from "./modules/project-status.js"' in app
    for source in (app, status):
        assert 'fetch("/api/projects")' not in source
        assert "setInterval(" not in source


def test_project_sidebar_is_an_es_module_boundary():
    app = (ROOT / "static" / "app.js").read_text()
    sidebar = (ROOT / "static" / "modules" / "project-sidebar.js").read_text()

    assert 'from "./modules/project-sidebar.js"' in app
    assert "export function createProjectSidebar" in sidebar
    assert "endingKeys" in sidebar
    assert 'className = "agent-row"' in sidebar


def test_theme_and_key_bar_are_es_module_boundaries():
    app = (ROOT / "static" / "app.js").read_text()
    theme = (ROOT / "static" / "modules" / "theme.js").read_text()
    key_bar = (ROOT / "static" / "modules" / "key-bar.js").read_text()

    assert 'from "./modules/theme.js"' in app
    assert "export function createThemeController" in theme
    assert 'const storageKey = "wterm.theme"' in theme
    assert 'from "./modules/key-bar.js"' in app
    assert "export function createKeyBar" in key_bar
    assert 'const storageKey = "wterm.terminal.fontSize"' in key_bar
