# W-Term Repository Guide

## Project overview

W-Term is an internal web terminal for controlling Claude Code, Codex, and login-shell sessions. The browser uses xterm.js, the FastAPI server relays terminal traffic over WebSocket, and `server/session.py` owns PTY process lifecycles.

This file contains the detailed architecture, protocol, operational assumptions, and security constraints. `CLAUDE.md` only adds rules specific to working in this repo via Claude Code — read it too if you are Claude Code.

## Running the project

- Start the service with `./start.sh`.
- Server address, authentication, grace period, UDS, and the project whitelist come from `projects.json`.
- Use the existing `.venv/bin/python`; the system Python does not provide pip or venv.
- If dependencies must be recreated, use `~/.local/bin/uv` as documented in `CLAUDE.md`.
- The frontend has no build step. xterm.js dependencies are vendored under `static/vendor/`.

## Repository map

- `server/config.py`: loads and validates `projects.json`.
- `server/session.py`: spawns PTYs and manages input, resize, replay, detach grace, and termination.
- `server/main.py`: FastAPI routes, authentication, project status, and the WebSocket protocol.
- `static/app.js`: project actions, xterm.js integration, authentication UI, and reconnect behavior.
- `static/style.css`: application layout and visual states.
- `static/vendor/`: vendored third-party files; do not edit.

## Session invariants

- Keep Claude, Codex, and shell sessions independent with keys `<project>#claude`, `<project>#codex`, and `<project>#shell`.
- Allow at most one live session per session key and one attached WebSocket per live session.
- Preserve the disconnect grace period, the 256 KB replay buffer, PTY resize handling, and process-group termination behavior.
- Preserve non-blocking PTY writes. Partial writes and `BlockingIOError` must retain unsent UTF-8 bytes in the write buffer.
- New clients replace an existing client with close code 4000. Authentication and whitelist failures use their existing close codes.
- Claude resume commands are `claude --resume` and `claude --continue`.
- Codex resume commands are `codex resume` and `codex resume --last`.
- If resume history does not exist, start a new session instead of launching a broken resume flow.
- Local and SSH-backed projects must retain equivalent session behavior.

## WebSocket contract

Client-to-server messages are JSON text:

- `{"type":"input","data":"..."}`
- `{"type":"resize","cols":80,"rows":24}`

Server-to-client terminal output is binary. Status and exit notifications are JSON text. When changing the protocol, update both `server/main.py` and `static/app.js` in the same change.

## Development rules

- Preserve compatibility for existing Claude URLs; the default `agent` remains `claude`, and legacy `shell=1` remains supported.
- Do not edit `static/vendor/` or introduce a frontend build pipeline without an explicit requirement.
- Do not expose the server on `0.0.0.0`. Prefer loopback or UDS behind an authenticated VPN/reverse proxy.
- Treat `projects.json` and authentication settings as deployment configuration. Do not print or duplicate secrets.
- Do not terminate processes found only through broad commands such as `pgrep claude` or `pgrep codex`; verify ownership and parentage first.
- The running server does not auto-reload. Code changes require a deliberate restart before they become active.

## Verification

For server changes, run at minimum:

```bash
.venv/bin/python -m py_compile server/config.py server/session.py server/main.py
git diff --check
```

For session-lifecycle changes, also verify PTY startup, ANSI output, input, resize, detach/reconnect replay, grace cancellation, and child-process cleanup. Exercise both the affected AI CLI and shell paths. For protocol or UI changes, verify project status badges, new/resume actions, abnormal reconnect, authentication expiry, and mobile-width layout in a browser.

Do not add a test dependency solely for a one-off check. Prefer the installed environment and focused smoke tests unless a maintained test suite is being introduced intentionally.

## Known issues

- Safari/WebKit has a Korean IME composition bug in the terminal input. It does not occur in other browsers; recommend Chrome/Firefox to affected users rather than trying to work around it in `static/app.js`.
