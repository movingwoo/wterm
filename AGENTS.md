# W-Term Repository Guide

## Project overview

W-Term is an internal web terminal for controlling Claude Code, Codex, and login-shell sessions. The browser uses xterm.js, the FastAPI server relays terminal traffic over WebSocket, and `server/session.py` owns PTY process lifecycles.

This file contains the detailed architecture, protocol, operational assumptions, and security constraints. `CLAUDE.md` only adds rules specific to working in this repo via Claude Code — read it too if you are Claude Code.

## Running the project

- Start the service with `./start.sh`; stop it with `./stop.sh` on either platform; restart with `./stop.sh && ./start.sh`. The long-running deployment is supervised — a LaunchDaemon on macOS (`scripts/install-launchd.sh`) or a systemd unit on Linux (`scripts/install-systemd.sh`) — and `start.sh` goes through the supervisor when one is installed, which needs root (`sudo ./start.sh`). See "Process lifecycle".
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
- `scripts/cert-status.sh`: certificate health check (expiry, served-vs-file match, renewal job) plus secret-file permissions. The permission section runs before the "TLS not configured" early exit, so it covers deployments without TLS.
- `scripts/install-launchd.sh`: installs the boot-time LaunchDaemon and moves renewal off cron (macOS).
- `tests/`: pytest smoke suite. `conftest.py` owns the repo-copy fixture that every test's server runs from.
- `scripts/install-systemd.sh`: the Linux counterpart — a `wterm.service` system unit plus a `wterm-certrenew.timer`. Keep the two installers behaviourally equivalent; a change to one usually belongs in the other.

## Session invariants

- Keep Claude, Codex, and shell sessions independent with keys `<project>#claude`, `<project>#codex`, and `<project>#shell`.
- Allow at most one live session per session key and one attached WebSocket per live session.
- Preserve the disconnect grace period, the 256 KB replay buffer, PTY resize handling, and process-group termination behavior.
- Preserve non-blocking PTY writes. Partial writes and `BlockingIOError` must retain unsent UTF-8 bytes in the write buffer.
- New clients replace an existing client with close code 4000. Authentication and whitelist failures use their existing close codes.
- Note that 4403 never reaches the client. The origin check runs *before* `ws.accept()`, so Starlette rejects the handshake with HTTP 403 and the close code is never delivered — the browser sees a generic 1006. That is the desired trade (a hostile page never holds an open socket, and the reason is logged server-side instead); do not "fix" it by moving the origin check after `accept()`. Every other rejection closes after `accept()` so the code does arrive and the client can stop retrying.
- Check authentication (4401) *before* the whitelist (4404) and agent (4400) checks. Both of those run after `accept()`, so reversing the order would let an unauthenticated caller probe which projects exist by reading the close code.
- Claude resume commands are `claude --resume` and `claude --continue`.
- Codex resume commands are `codex resume` and `codex resume --last`.
- If resume history does not exist, start a new session instead of launching a broken resume flow.
- Local and SSH-backed projects must retain equivalent session behavior.

## WebSocket contract

Client-to-server messages are JSON text:

- `{"type":"input","data":"..."}`
- `{"type":"resize","cols":80,"rows":24}`

Server-to-client terminal output is binary. Status and exit notifications are JSON text. When changing the protocol, update both `server/main.py` and `static/app.js` in the same change.

## Authentication

Authentication is a second lock behind the network boundary, not a substitute for it. The binding rule below still governs. Keep these properties:

- **Origin is checked on every WebSocket handshake and every POST.** WS handshakes are not subject to CORS, so a cookie check alone lets any page the user visits open a socket here — which on this server is arbitrary command execution. The default rule needs no configuration: the origin's host must equal the `Host` header, which holds because an attacker cannot change the `Host` the victim's browser sends to us. `allowed_origins` is the escape hatch for proxies that rewrite `Host`. A missing `Origin` is rejected; `static/app.js` is the only client and browsers always send it.
- **Never call `_password_hasher.verify` on the event loop.** It is a 64 MiB, ~35 ms synchronous CPU operation; running it inline stalls every live PTY session for its duration, and unauthenticated requests can drive it. It goes through `anyio.to_thread.run_sync` under `_login_slots`, which caps concurrent verifications so the total cost stays bounded.
- **Rate limiting must reject before the hash runs.** The point is not to slow guessing (argon2 already does that) but to avoid paying the cost at all. A blocked bucket returns 429 without touching argon2 — including for the correct password.
- **Token expiry is enforced server-side.** Cookie `max-age` is only a request to the browser; `_valid_tokens` stores issue times and `is_authed` checks them, so `AUTH_TOKEN_TTL` and the cookie's `max-age` must stay the same value. `/api/logout` is the revocation path — a restart is not one, since it kills every PTY session.
- Both `_valid_tokens` and `_login_fails` are bounded `OrderedDict`s. They are reachable by unauthenticated requests, so any change must keep them from growing without limit.

## Audit log

`wterm.audit` writes to its own file, `logs/wterm-audit.log`, with 90 days of rotation. It exists for post-incident investigation, not detection.

- **Never log terminal content — input or output.** Only session metadata (project, agent, mode, time, peer address). Logging input would turn this file into a channel for every password and API key typed into a session. `tests/test_hardening.py` asserts this.
- It does not propagate to the root logger. `wterm.log` is ERROR-only and keeps `backupCount=1`, so anything that leaks there effectively has two days of retention and drowns real errors in routine traffic.
- A blocked login is recorded once, when the block is set — not per rejected attempt. Per-attempt logging would let an unauthenticated caller inflate the file without limit.
- The handler is installed in `setup_logging()`. Entry points that skip it (importing the app under a bare `uvicorn` invocation) silently drop audit records.
- `server-start` / `server-stop` mark the boundary where every token and PTY session dies. Without them a gap in the log is unreadable.

## Response headers

`SECURITY_HEADERS` is applied by an HTTP middleware, not a route dependency, because `/static` is a `StaticFiles` mount and never runs route hooks — an unprotected `app.js` makes the CSP pointless.

- `style-src` carries `'unsafe-inline'` and must keep it. The xterm.js DOM renderer creates `<style>` elements at runtime and assigns `textContent` (`_dimensionsStyleElement`, `_themeStyleElement`); blocking that leaves cell metrics and theme colours wrong. A nonce cannot fix it — the element is created by vendored xterm.js, not by our code.
- `script-src` must stay strict. Script injection on this server is arbitrary command execution, so `'unsafe-inline'`/`'unsafe-eval'` never belong there. Both directions are asserted in `tests/test_hardening.py`.
- No HSTS. When `tls_enabled` there is no plaintext listener to be downgraded from, and the remaining threat requires the attacker to already be inside the tailnet — not worth a directive that persists in browsers.

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
- `_release_pid_file` only deletes the file when it still holds this process's pid, so a slow-dying predecessor cannot delete a newer server's pid file. It checks through the fd it already holds: re-opening the pid file anywhere in this process would drop the lock below (POSIX advisory locks are released on *any* close of the file by that process).
- That pid file is also the single-instance lock. `_claim_pid_file` takes an exclusive `fcntl.lockf` on it and **refuses to start** (exit code 3) when another server holds it, instead of overwriting the pid. Overwriting was the old behaviour and it stranded the running server: `stop.sh` and the certificate `--reloadcmd` both go through that file, so the first process became unreachable except by hand — and it kept serving old code on its old port. Port collision does not catch this; two servers on different ports both bind fine.
- The lock is on the inode, so `_claim_pid_file` re-opens if the path was replaced between open and lock. A kernel-held lock also disappears with the process however it dies, so there is no stale-pid heuristic to get wrong. Locks are not inherited across `fork`, and `os.open` is close-on-exec, so PTY children never hold it.
- Supervisors retry a refused start (exit 3 is a failure exit), which is intended: a restart racing a slow-dying predecessor should reattach on the next interval (~10s for both launchd and systemd).
- `start.sh` waits for the server to publish its pid and reports startup failures instead of recording a dead pid. Its own "already running" check is a fast path only — the server-side lock is what actually enforces it, since `.venv/bin/python -m server` bypasses the launcher.
- When a supervisor unit is installed, `start.sh` starts the server *through it* (`launchctl kickstart` / `systemctl start`) rather than with `nohup`. Starting it directly produces a process outside the supervisor: nothing resurrects it on a crash, and it inherits the calling shell's environment instead of the unit's `PATH`/`HOME`/`LANG` — which is how `claude` resolution ends up working by hand and failing as a daemon. That path needs root, so `start.sh` tries `sudo -n` and otherwise prints the command and stops. Do not add a silent fallback to `nohup`: it recreates exactly the situation this prevents.
- Restart with `./stop.sh && ./start.sh`. `launchctl kickstart -k` is not equivalent — `-k` kills the running instance instead of going through the SIGTERM path, so `SessionManager.shutdown` never runs and the PTY children, which have their own process groups from `setsid`, survive the server that owned them.
- The supervisor restarts the server only on an unsuccessful exit (launchd `KeepAlive: SuccessfulExit: false`, systemd `Restart=on-failure`), so a deliberate `stop.sh` is not immediately undone. `stop.sh` needs no root for that reason; `bootout` / `systemctl disable` is a different action (unregister, do not start at boot) and stays a hint rather than something the script does.
- That rule only holds because the server converts a SIGTERM shutdown into exit code 0 (`_exit_success` in `server/main.py`). uvicorn re-raises the signal it captured once graceful shutdown finishes, so without a handler installed *before* `uvicorn.run()` the process dies by signal, the supervisor reads that as a crash, and `stop.sh` gets silently undone. Do not remove that handler, and re-verify it when upgrading uvicorn.
- Anything that kills the server with SIGKILL — including `stop.sh`'s own 20-second fallback — does read as a crash and will be resurrected. `SessionManager.shutdown` therefore terminates sessions concurrently so total shutdown stays bounded by `SIGTERM_WAIT` rather than scaling with session count.
- A daemon gets a minimal `PATH`, no login shell, and no `$SHELL`. `claude` and `codex` are spawned by the server directly, so their directories must stay in the unit's `PATH`; shell sessions resolve the login shell from the passwd entry (`login_shell()`) rather than `$SHELL` for the same reason.

## Development rules

- macOS and Linux are both supported targets. The server code is plain POSIX (`pty`, `fcntl`, `termios`, `os.killpg`) and must stay that way — no `/proc`, no `launchctl`, no Darwin-only syscalls in `server/`. Platform differences belong in `scripts/`, branched on `uname -s`. Note that only macOS has been exercised in production so far; Linux paths are written but unverified.
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
.venv/bin/python -m pytest
.venv/bin/python -m py_compile server/config.py server/session.py server/main.py
git diff --check
```

`tests/` is a pytest smoke suite; `requirements-dev.txt` holds its two extra dependencies. GitHub Actions runs it on pushes to `main` and on pull requests, on macOS and ubuntu with Python 3.10 (the production interpreter) — see `.github/workflows/ci.yml`. A 3.13 job existed and was removed: `test_https_and_wss_work` times out on its wss handshake there, on 3.13 only. Restore that job before moving production off 3.10.

- Tests start the server **as a real process from a copy of the repository**, never in-process. `server/main.py` reads `projects.json` and `logs/wterm.pid` from fixed paths at import time, so a copy is what keeps a test run from touching the deployment or colliding with a running server. It is also the only way to cover what has actually broken here: the pid-file lock, exit code 0 on SIGTERM, and `SIGHUP` certificate reload are all process-level behaviour.
- The suite needs no `claude`/`codex` CLI. Session behaviour is exercised through a login shell, and the agent path is covered only up to "reports exit 127 when the binary is not on `PATH`".
- Interactive `bash` ignores SIGTERM, so a live shell session makes shutdown wait the full `SIGTERM_WAIT` before `SIGKILL`. Tests that keep a session open end it with `exit` (`end_shell`) rather than leaving it to teardown; that is also the only coverage of a normal exit notification.
- `end_shell` sends `true` before `exit` deliberately. A bare `exit` returns `$?`, which at that point is whatever the shell's startup files left behind — macOS `/etc/bashrc` ends on `[ -r "/etc/bashrc_$TERM_PROGRAM" ] && …`, false whenever `TERM_PROGRAM` is unset, so a login shell in a daemon or CI environment starts with `$?` at 1. The assertion is about the server propagating the child's exit code, not about someone else's rc file; do not "fix" a failure here by relaxing `assert payload["code"] == 0`.
- `tests/test_hardening.py` reads `logs/wterm-audit.log` scoped to `server-start pid=<this process>`. `repo_copy` is session-scoped, so the whole file is shared across every server in a run and unscoped assertions would pass on another test's records.
- A separate `systemd` CI job installs `scripts/install-systemd.sh` on the runner for real and walks install → start → SIGKILL/resurrect → `stop.sh`/stay-down → `cert-status.sh` → uninstall. It is the only place the supervisor contract (`Restart=on-failure` paired with exit code 0) is actually exercised, so changes to either installer or to shutdown behaviour must keep it passing. Reboot auto-start and the pre-240 `append:` fallback stay unverifiable there.
- Do not weaken an assertion to make a test pass. These encode the invariants in this file, and each one exists because that behaviour broke before.

For TLS changes, serve on a spare port with a throwaway self-signed certificate and verify: HTTPS responds, a `wss://` upgrade succeeds, overwriting the files changes nothing until `SIGHUP`, `SIGHUP` swaps the served certificate without dropping the process, and a corrupt certificate file leaves the previous one in service.

For session-lifecycle changes, also verify PTY startup, ANSI output, input, resize, detach/reconnect replay, grace cancellation, and child-process cleanup. Exercise both the affected AI CLI and shell paths. For protocol or UI changes, verify project status badges, new/resume actions, abnormal reconnect, authentication expiry, and mobile-width layout in a browser.

Keep `requirements-dev.txt` minimal. The suite deliberately drives the server over the wire with an HTTP client and the WebSocket client uvicorn already pulls in, rather than adding a test-client or async-test framework. Do not add a dependency for a one-off check.

## Known issues

- Safari/WebKit has a Korean IME composition bug in the terminal input. It does not occur in other browsers; recommend Chrome/Firefox to affected users rather than trying to work around it in `static/app.js`.
