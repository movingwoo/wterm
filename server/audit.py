"""W-Term 감사 기록의 정규화, pre-auth throttling, 파일 핸들러."""
from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

AUDIT_BACKUP_DAYS = 90
AUDIT_VALUE_MAX = 128
AUDIT_THROTTLE_WINDOW = 60.0
AUDIT_THROTTLE_KEYS = 256

_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")
_log = logging.getLogger("wterm.audit")
_log.setLevel(logging.INFO)
_log.propagate = False

# 인증 없이 닿는 저장소이므로 상한이 필요하다.
_throttle: OrderedDict[tuple[str, str], list] = OrderedDict()


def peer(conn) -> str:
    """요청/소켓의 상대 주소. UDS나 프록시 뒤에서는 없거나 프록시 IP뿐이다."""
    return conn.client.host if conn.client else "-"


def _value(value) -> str:
    """개행·제어문자를 제거하고 한 필드가 로그 한 줄을 밀어내지 못하게 한다."""
    text = _UNSAFE.sub("?", str(value))
    return text if len(text) <= AUDIT_VALUE_MAX else text[:AUDIT_VALUE_MAX] + "…"


def audit(event: str, **fields) -> None:
    """`event key=value ...` 형식의 감사 기록. 터미널 내용은 받지 않는다."""
    detail = " ".join(
        f"{key}={_value(value)}"
        for key, value in fields.items()
        if value is not None
    )
    _log.info("%s%s", event, f" {detail}" if detail else "")


def audit_throttled(event: str, client: str, reason: str, **fields) -> None:
    """같은 pre-auth (client, reason)을 창당 한 줄로 접는다."""
    key = (client, reason)
    now = time.monotonic()
    entry = _throttle.get(key)
    if entry is not None and now - entry[0] < AUDIT_THROTTLE_WINDOW:
        entry[1] += 1
        return
    suppressed = entry[1] if entry is not None else 0
    _throttle[key] = [now, 0]
    _throttle.move_to_end(key)
    while len(_throttle) > AUDIT_THROTTLE_KEYS:
        _throttle.popitem(last=False)
    audit(
        event,
        reason=reason,
        client=client,
        **fields,
        suppressed=suppressed or None,
    )


def setup_audit_logging(log_dir: Path, formatter: logging.Formatter) -> None:
    """90일 일별 rotation 핸들러를 설치한다."""
    handler = TimedRotatingFileHandler(
        log_dir / "wterm-audit.log",
        when="midnight",
        backupCount=AUDIT_BACKUP_DAYS,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    _log.addHandler(handler)
