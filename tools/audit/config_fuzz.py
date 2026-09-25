"""사용자 설정·룰 파일을 한 칸씩 망가뜨려 로더가 어떻게 반응하는지 분류한다 (2026-09-25 감사).

사용자는 `%APPDATA%\\Argus\\settings.yaml`·`rules.yaml` 을 손으로 고친다. 그때 나올 수 있는
결과는 넷이다:

    ok         그대로 읽힌다 (빠진 키는 코드 기본값)
    human      ConfigError/RuleError — 사람이 읽는 메시지로 거부 (바라는 모양)
    crash      그 밖의 예외 — 트레이스백이 사용자에게 간다
    silent     알 수 없는 키가 **조용히 무시된다** — 오타를 고쳐도 아무 일도 안 일어난다

`crash` 와 `silent` 는 대장 후보다. 이 도구는 판정하지 않고 센다.

사용:
    .venv\\Scripts\\python.exe tools\\audit\\config_fuzz.py            # 요약
    .venv\\Scripts\\python.exe tools\\audit\\config_fuzz.py --verbose  # 전부
    .venv\\Scripts\\python.exe tools\\audit\\config_fuzz.py --selftest
"""

from __future__ import annotations

import argparse
import copy
import logging
import pathlib
import sys
import tempfile
from collections import Counter
from typing import Any, Iterator

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from argus.config.loader import ConfigError, Settings  # noqa: E402
from argus.detection.rules import RuleError, load_rules  # noqa: E402

BAD_VALUES: dict[str, Any] = {"null": None, "문자열": "abc", "음수": -1, "리스트": [1]}


def _leaves(d: Any, path: tuple = ()) -> Iterator[tuple]:
    if isinstance(d, dict):
        for k, v in d.items():
            yield from _leaves(v, path + (k,))
    else:
        yield path


def _set(d: dict, path: tuple, value: Any, delete: bool = False) -> dict:
    out = copy.deepcopy(d)
    cur = out
    for k in path[:-1]:
        cur = cur[k]
    if delete:
        del cur[path[-1]]
    else:
        cur[path[-1]] = value
    return out


def _validate(data: dict) -> str:
    from pydantic import ValidationError

    try:
        Settings.model_validate(data)
        return "ok"
    except ValidationError:
        return "human"  # load_settings 가 ConfigError 로 감싼다
    except ConfigError:
        return "human"
    except Exception as e:  # noqa: BLE001
        return f"crash:{type(e).__name__}"


def fuzz_settings(base: dict) -> list[tuple[str, str, str]]:
    out = []
    for path in _leaves(base):
        key = ".".join(map(str, path))
        out.append((key, "삭제", _validate(_set(base, path, None, delete=True))))
        for label, v in BAD_VALUES.items():
            out.append((key, label, _validate(_set(base, path, v))))
    # 알 수 없는 키 — 오타. 막지 않는 것이 설계다(은퇴한 키가 기동을 막으면 안 된다) — 대신
    # `unknown_keys` 가 그 키를 짚어 경고·창 표시로 드러내면 `warned`, 못 짚으면 `silent`(감사 F-007).
    from argus.config.loader import unknown_keys

    def _typo(data: dict, key: str) -> str:
        res = _validate(data)
        if res != "ok":
            return res
        return "warned" if key in unknown_keys(data) else "silent"

    for section in [k for k, v in base.items() if isinstance(v, dict)]:
        key = f"{section}.__오타_키__"
        out.append((key, "알수없는키", _typo(_set(base, (section, "__오타_키__"), 1), key)))
    out.append(("__오타_섹션__", "알수없는키", _typo(_set(base, ("__오타_섹션__",), {"a": 1}), "__오타_섹션__")))
    return out


def _load_rules_text(text: str) -> str:
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "rules.yaml"
        p.write_text(text, encoding="utf-8")
        try:
            load_rules(p)
            return "ok"
        except RuleError:
            return "human"
        except Exception as e:  # noqa: BLE001
            return f"crash:{type(e).__name__}"


def fuzz_rules(base: dict) -> list[tuple[str, str, str]]:
    out = []
    rule0 = base["rules"][0]
    for field in list(rule0):
        for label, v in {"삭제": ..., **BAD_VALUES}.items():
            doc = copy.deepcopy(base)
            if v is ...:
                del doc["rules"][0][field]
            else:
                doc["rules"][0][field] = v
            out.append((f"rules[0].{field}", label, _load_rules_text(yaml.safe_dump(doc, allow_unicode=True))))
    cond0 = rule0["when"]["all"][0]
    for field in list(cond0):
        for label, v in {"삭제": ..., **BAD_VALUES}.items():
            doc = copy.deepcopy(base)
            c = doc["rules"][0]["when"]["all"][0]
            if v is ...:
                del c[field]
            else:
                c[field] = v
            out.append((f"rules[0].when.all[0].{field}", label,
                        _load_rules_text(yaml.safe_dump(doc, allow_unicode=True))))
    # 구조 자체
    for label, text in {
        "룰이 문자열": "rules:\n  - 그냥문자열\n",
        "rules 가 매핑": "rules: {a: 1}\n",
        "최상위 리스트": "- a\n- b\n",
        "YAML 문법 오류": "rules: [\n",
        "식 문법 오류": "rules:\n  - name: x\n    when: {all: [{metric: cpu_total, op: '>', value: 'median +* 2'}]}\n",
        "알수없는 연산자": "rules:\n  - name: x\n    when: {all: [{metric: cpu_total, op: '=~', value: 1}]}\n",
        "알수없는 심각도": "rules:\n  - name: x\n    severity: 매우나쁨\n    when: {all: [{metric: cpu_total, op: '>', value: 1}]}\n",
        "알수없는 메트릭": "rules:\n  - name: x\n    when: {all: [{metric: 없는지표, op: '>', value: 1}]}\n",
    }.items():
        res = _load_rules_text(text)
        silent_labels = ("알수없는 심각도", "알수없는 메트릭")
        out.append(("rules(구조)", label, "silent" if res == "ok" and label in silent_labels else res))
    return out


def report(rows: list[tuple[str, str, str]], title: str, verbose: bool) -> Counter:
    c = Counter(r.split(":")[0] for _, _, r in rows)
    print(f"\n== {title}: {len(rows)}건 — " + " · ".join(f"{k} {v}" for k, v in sorted(c.items())))
    for key, label, res in rows:
        if verbose or res.startswith(("crash", "silent")):
            print(f"  {res:28s} {key}  [{label}]")
    return c


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.disable(logging.CRITICAL)
    ap = argparse.ArgumentParser(description="설정·룰 퍼징")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    settings = yaml.safe_load((ROOT / "argus/config/defaults.yaml").read_text(encoding="utf-8"))
    rules = yaml.safe_load((ROOT / "argus/config/rules.yaml").read_text(encoding="utf-8"))

    if args.selftest:
        # 분류기가 네 갈래를 실제로 가르는지: 원본은 ok, 명백히 틀린 값은 human,
        # 구조가 깨진 룰은 crash 나 human 중 하나여야 하고 ok 면 안 된다.
        checks = [
            ("원본 설정 = ok", _validate(settings) == "ok"),
            ("원본 룰 = ok", _load_rules_text(yaml.safe_dump(rules, allow_unicode=True)) == "ok"),
            ("음수 예산 = human", _validate(_set(settings, ("budget", "cpu_percent"), "abc")) == "human"),
            ("문법 오류 룰 ≠ ok", _load_rules_text("rules: [\n") != "ok"),
            ("스캔 대상 하한", sum(1 for _ in _leaves(settings)) >= 100),
        ]
        for k, ok in checks:
            print(f"  {'[OK]' if ok else '[FAIL]'} {k}")
        return 0 if all(ok for _, ok in checks) else 1

    total = report(fuzz_settings(settings), "defaults.yaml", args.verbose)
    total += report(fuzz_rules(rules), "rules.yaml", args.verbose)
    bad = sum(v for k, v in total.items() if k in ("crash", "silent"))
    print(f"\ncrash+silent {bad}건 (판정은 대장에서)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
