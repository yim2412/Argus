"""옛 전체 사본 settings.yaml 을 "다른 값만 + 주석 템플릿"으로 정리한다 (감사 F-026).

첫 실행이 defaults.yaml **전체**를 사용자 settings.yaml 로 복사하던 시절의 파일은 모든 키가
사용자 값이라, 업데이트로 바뀐 기본값이 영영 안 먹는다. 이 도구는:
  1) 사용자 파일을 **백업**하고(`settings.yaml.bak-<시각>`)
  2) 현재 기본값과 **같은 키는 걷어 내고** 다른 키만 맨 위 블록에 남기며
  3) 그 아래에 주석 템플릿(설명서)을 붙인다.

⚠ 기계는 "사용자가 고친 값"과 "옛 기본값"을 가를 수 없다 — 둘 다 "현재 기본값과 다름"으로
남는다. 미리보기에 그 목록이 나오니 눈으로 보고, 옛 기본값이면 그 줄을 지운다.

    .venv\\Scripts\\python.exe tools\\settings_prune.py            # 미리보기(아무것도 안 쓴다)
    .venv\\Scripts\\python.exe tools\\settings_prune.py --apply    # 백업 후 쓴다
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from argus.config.loader import CONFIG_VERSION, user_config_path, user_config_template  # noqa: E402
from argus.paths import resource_path  # noqa: E402


def diff_from_defaults(user: dict, defaults: dict, prefix: str = "") -> tuple[dict, list[str], list[str]]:
    """(남길 값의 중첩 dict, 기본값과 다른 키 경로, 기본값에 없는 키 경로)."""
    keep: dict[str, Any] = {}
    changed: list[str] = []
    unknown: list[str] = []
    for key, value in user.items():
        path = f"{prefix}{key}"
        if key == "config_version":
            continue
        if key not in defaults:
            keep[key] = value
            unknown.append(path)
        elif isinstance(value, dict) and isinstance(defaults[key], dict):
            sub, c, u = diff_from_defaults(value, defaults[key], f"{path}.")
            if sub:
                keep[key] = sub
            changed += c
            unknown += u
        elif value != defaults[key]:
            keep[key] = value
            changed.append(f"{path}: {defaults[key]!r} → {value!r}")
    return keep, changed, unknown


def pruned_text(user_text: str, defaults_text: str) -> tuple[str, list[str], list[str]]:
    user = yaml.safe_load(user_text) or {}
    defaults = yaml.safe_load(defaults_text) or {}
    keep, changed, unknown = diff_from_defaults(user, defaults)
    head = [
        "# Argus 사용자 설정 — tools\\settings_prune.py 가 옛 전체 사본에서 정리했다.",
        "# 맨 위 블록은 옛 사본에서 **현재 기본값과 달랐던 값**만 옮긴 것이다. 사용자가 고친 값과",
        "# 옛 기본값이 섞여 있을 수 있다 — 옛 기본값이면 그 줄을 지우면 업데이트된 기본값을 따른다.",
        "",
        f"config_version: {CONFIG_VERSION}",
        "",
    ]
    if keep:
        head.append(yaml.safe_dump(keep, allow_unicode=True, sort_keys=False).rstrip())
        head.append("")
    head.append("# ---- 아래는 설명서(주석 템플릿) — 바꾸고 싶은 줄만 주석을 푼다 ----")
    template = user_config_template(defaults_text)
    body = "\n".join(line for line in template.splitlines() if not line.startswith("config_version:"))
    return "\n".join(head) + "\n" + body + "\n", changed, unknown


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="옛 전체 사본 settings.yaml 정리 (기본은 미리보기)")
    ap.add_argument("--path", type=pathlib.Path, default=None, help="사용자 settings.yaml (기본: %%APPDATA%%\\Argus)")
    ap.add_argument("--apply", action="store_true", help="백업한 뒤 실제로 쓴다")
    args = ap.parse_args()

    path = args.path or user_config_path()
    if not path.exists():
        print(f"[OK] 사용자 설정이 없다 — 할 일 없음 ({path})")
        return 0
    user_text = path.read_text(encoding="utf-8")
    if "config_version" in (yaml.safe_load(user_text) or {}):
        print(f"[OK] 이미 새 형식이다(config_version 있음) — 할 일 없음 ({path})")
        return 0
    new_text, changed, unknown = pruned_text(user_text, resource_path("config/defaults.yaml").read_text(encoding="utf-8"))
    print(f"대상: {path}")
    print(f"기본값과 다른 값 {len(changed)}개 (남긴다 — 사용자 값인지 옛 기본값인지 눈으로 본다):")
    for line in changed:
        print(f"  {line}")
    print(f"기본값에 없는 키 {len(unknown)}개 (남긴다 — 오타이거나 은퇴한 키):")
    for line in unknown:
        print(f"  {line}")
    if not args.apply:
        print("[OK] 미리보기 — 쓰려면 --apply")
        return 0
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    backup.write_text(user_text, encoding="utf-8")
    path.write_text(new_text, encoding="utf-8")
    print(f"[OK] 정리함 — 백업 {backup.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
