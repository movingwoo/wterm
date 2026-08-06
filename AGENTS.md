# W-Term Repository Guide

## Project overview

W-Term is an internal web terminal for controlling Claude Code, Codex, and login-shell sessions. The browser uses xterm.js, the FastAPI server relays terminal traffic over WebSocket, and `server/session.py` owns PTY process lifecycles.

This file contains the detailed architecture, protocol, operational assumptions, and security constraints. `CLAUDE.md` only adds rules specific to working in this repo via Claude Code — read it too if you are Claude Code.

## Running the project

- Start the service with `./start.sh`. On macOS the long-running deployment is a LaunchDaemon (`scripts/install-launchd.sh`); restart that one with `launchctl kickstart -k system/com.wterm.server`.
- Server address, authentication, grace period, UDS, TLS certificate paths, and the project whitelist come from `projects.json`.
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
- `scripts/cert-setup.sh`: one-shot TLS certificate issuance via acme.sh; not invoked by the server.
- `scripts/cert-status.sh`: certificate health check (expiry, served-vs-file match, renewal job).
- `scripts/install-launchd.sh`: installs the boot-time LaunchDaemon and moves renewal off cron.

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

## TLS

The server terminates TLS itself when `tls_certfile` and `tls_keyfile` are both set. Keep this boundary intact:

- The server reads certificate files. It must not implement, embed, or shell out to an ACME client. Issuance and renewal belong to an external tool (`scripts/cert-setup.sh` wires up acme.sh) whose only contact point is `SIGHUP`.
- `SIGHUP` reloads the certificate into the existing `SSLContext` via `load_cert_chain`. Never satisfy a certificate change by restarting the process — PTY sessions live in this process and a restart kills all of them.
- A failed reload must keep the previous certificate and leave the server running. Only a failure at startup is fatal.
- `wterm.tls` carries its own `INFO` level so successful reloads are recorded even though the root logger is pinned to `ERROR`. Do not log successes at `ERROR` to make them visible — a routine reload must not read as a failure in the log.
- Uvicorn intercepts `SIGINT`/`SIGTERM` only, so the `SIGHUP` handler survives. If uvicorn's signal handling changes, re-verify this.
- TLS settings are ignored under `uds`, where a front proxy owns TLS.
- Enabling TLS does not relax the binding rule below. TLS is transport encryption, not access control.

## Process lifecycle

- The server writes `logs/wterm.pid` itself and removes it on clean exit. Do not move this back into `start.sh`: launchd runs the server in the foreground, so a launcher-written pid file would not exist under launchd and both `stop.sh` and the certificate reload hook depend on it.
- `_release_pid_file` only deletes the file when it still holds this process's pid, so a slow-dying predecessor cannot delete a newer server's pid file.
- `start.sh` waits for the server to publish its pid and reports startup failures instead of recording a dead pid.
- Under launchd, `KeepAlive` is `SuccessfulExit: false` so a deliberate `stop.sh` is not immediately undone. Restart with `launchctl kickstart -k`, not by editing the plist.
- A LaunchDaemon gets a minimal `PATH` and no login shell. `claude` and `codex` are spawned by the server directly, so their directories must stay in the plist's `PATH`.

## Development rules

- Preserve compatibility for existing Claude URLs; the default `agent` remains `claude`, and legacy `shell=1` remains supported.
- Do not edit `static/vendor/` or introduce a frontend build pipeline without an explicit requirement.
- Do not expose the server on `0.0.0.0`. Prefer loopback or UDS behind an authenticated VPN/reverse proxy. When the server terminates TLS itself and other devices must reach it directly, binding to a private VPN-interface address (a tailnet IP, for example) is acceptable; a routable public address is not.
- Invoke `acme.sh` with `LC_ALL=C`. It parses certificate dates with `date -j -f "%b %d ..."`, which fails under non-English locales and silently disables its "renewal scheduled after expiry" guard.
- Treat `projects.json`, authentication settings, and certificate paths as deployment configuration. Do not print or duplicate secrets, and never commit certificate or key files.
- Do not terminate processes found only through broad commands such as `pgrep claude` or `pgrep codex`; verify ownership and parentage first.
- The running server does not auto-reload. Code changes require a deliberate restart before they become active.

## Verification

For server changes, run at minimum:

```bash
.venv/bin/python -m py_compile server/config.py server/session.py server/main.py
git diff --check
```

For TLS changes, serve on a spare port with a throwaway self-signed certificate and verify: HTTPS responds, a `wss://` upgrade succeeds, overwriting the files changes nothing until `SIGHUP`, `SIGHUP` swaps the served certificate without dropping the process, and a corrupt certificate file leaves the previous one in service.

For session-lifecycle changes, also verify PTY startup, ANSI output, input, resize, detach/reconnect replay, grace cancellation, and child-process cleanup. Exercise both the affected AI CLI and shell paths. For protocol or UI changes, verify project status badges, new/resume actions, abnormal reconnect, authentication expiry, and mobile-width layout in a browser.

Do not add a test dependency solely for a one-off check. Prefer the installed environment and focused smoke tests unless a maintained test suite is being introduced intentionally.

## Known issues

- Safari/WebKit has a Korean IME composition bug in the terminal input. It does not occur in other browsers; recommend Chrome/Firefox to affected users rather than trying to work around it in `static/app.js`.
