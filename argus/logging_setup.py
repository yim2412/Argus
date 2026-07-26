"""로깅 구성.

두 갈래로 내보낸다.
  - **콘솔**: 사람이 읽는 한 줄 형식. 개발 중 눈으로 보는 용도.
  - **파일**: JSON Lines. 남의 PC 에서 난 문제를 나중에 기계적으로 훑어야 하므로
    구조화해서 남긴다. 회전(rotating)시켜 무한정 커지지 않게 한다.

크래시는 `crash_<timestamp>.json` 으로 따로 떨군다. 로그 파일은 회전하면서
증거가 사라질 수 있는데, 크래시는 반드시 보존되어야 하기 때문이다.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from . import __version__
from .paths import logs_dir

_CONFIGURED = False

# LogRecord 의 기본 속성. extra 로 들어온 사용자 필드만 골라내기 위해 제외 목록으로 쓴다.
_STD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """LogRecord → JSON 한 줄."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "thread": record.threadName,
        }
        if record.exc_info:
            payload["exc"] = "".join(traceback.format_exception(*record.exc_info)).strip()

        # logger.info("...", extra={"pid": 123}) 로 넘어온 구조화 필드
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = value

        return json.dumps(payload, ensure_ascii=False, default=str)


def setup(level: str = "INFO", console: bool = True) -> None:
    """루트 로거를 구성한다. 여러 번 불러도 한 번만 적용된다."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # 핸들러별로 레벨을 따로 건다

    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir() / "argus.jsonl",
        maxBytes=8 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    if console:
        # 한국어 Windows 콘솔은 기본이 cp949 라 한글 로그가 깨진다. 가능하면 UTF-8 로
        # 돌리고, 안 되면(파이프로 리다이렉트된 경우 등) 깨진 글자 대신 대체문자를 쓴다.
        for stream_obj in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream_obj, "reconfigure", None)
            if reconfigure is not None:
                try:
                    reconfigure(encoding="utf-8", errors="replace")
                except (OSError, ValueError):
                    pass
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(getattr(logging, level.upper(), logging.INFO))
        stream.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)-24s %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        root.addHandler(stream)

    _CONFIGURED = True
    logging.getLogger(__name__).debug("로깅 시작", extra={"version": __version__})


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def write_crash(exc: BaseException, context: str = "") -> Path:
    """크래시를 구조화해 별도 파일로 남기고 경로를 돌려준다.

    남의 PC 에서 난 예외는 재현이 불가능하다. 이게 없으면 디버깅이 성립하지 않는다.
    """
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = logs_dir() / f"crash_{stamp}.json"
    payload = {
        "ts": time.time(),
        "version": __version__,
        "context": context,
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ).strip(),
        "python": sys.version,
        "platform": sys.platform,
    }
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        # 크래시 기록에 실패했다고 크래시 처리 자체가 멈추면 안 된다.
        logging.getLogger(__name__).exception("크래시 파일 기록 실패")
    return path


if __name__ == "__main__":  # 스모크: python -m argus.logging_setup
    setup(level="DEBUG")
    log = get_logger("smoke")
    log.info("정보 로그", extra={"sample": 1})
    log.warning("경고 로그")
    try:
        raise ValueError("의도된 예외")
    except ValueError as e:
        log.exception("예외 로그")
        p = write_crash(e, context="smoke")
        print(f"  크래시 파일: {p}")
    print(f"  로그 파일: {logs_dir() / 'argus.jsonl'}")
    print("[OK] logging_setup")
