"""독립 리뷰 #4 프로브 — 절전 복귀가 베이스라인을 통째로 버려, 복귀 직후의 이상을 놓치는가.

`DetectionComponent.on_time_gap` → 탐지기마다 `reset()` → `RuleEngine.reset()` 이 **베이스라인 전체**
(전역·프로그램별·부하 축 6시간)를 비운다. docstring 의 의도는 "지속 조건 시계를 버린다"인데 평소값까지
버린다. 기동 때는 DB 에서 다시 채우지만(`warm`) 복귀 때는 채우지 않는다.

**베이스라인은 이상값도 배운다.** 평소 창(1800개)이면 이상이 중앙값을 끌어올리는 데 ~15분이 걸려
`for` 를 채우고도 남는다. 복귀 직후엔 창이 60개뿐이라 이상이 ~60초 만에 "평소"가 되고, `for: 90s`
를 채우기 전에 조건이 꺼진다 → 복귀 직후 시작한 이상은 영영 안 잡힌다.

처음엔 오탐(한가한 1분이 평소가 되어 평범한 사용이 튄다)을 재려 했다 — 같은 흡수 때문에 그쪽은
성립하지 않았다(대조 2 가 90% 에도 발화 0 으로 드러냈다).

탐지기만 쓴다(DB 불필요): 제품 룰(`registry.build("rules")`)에 평소 30분(메모리 45~46%) →
[복귀: reset] → 한가한 1분 40% → 메모리 90% 로 3분(누가 봐도 이상).

PASS = 「메모리 이상 증가」가 발화한다.  FAIL = 발화하지 않는다(복귀 직후 이상을 놓친다).
대조: reset 하지 않으면 발화해야 한다. 대조 2: reset 뒤라도 한가한 구간이 10분이면 발화해야 한다
(창이 짧아서라는 설명이 맞는지).
"""
import logging
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
logging.disable(logging.CRITICAL)

from argus.detection import registry  # noqa: E402
from argus.detection.base import Observation  # noqa: E402

RULE = "메모리 이상 증가"


def fired(reset: bool, idle_s: int) -> bool:
    eng = registry.build("rules")
    t = 1_790_000_000.0
    for i in range(1800):
        eng.observe(Observation(ts=t + i, metrics={"mem_percent": 45 + (i % 7) / 7, "cpu_total": 10.0}))
    t += 1800
    if reset:
        eng.reset()                                        # on_time_gap 이 하는 일
    for i in range(idle_s):
        eng.observe(Observation(ts=t + i, metrics={"mem_percent": 40.0, "cpu_total": 10.0}))
    t += idle_s
    for i in range(180):
        d = eng.observe(Observation(ts=t + i, metrics={"mem_percent": 90.0, "cpu_total": 10.0}))
        if d is not None and RULE in (d.features.get("rules") or [d.features.get("rule")]):
            return True
    return False


ctrl = fired(reset=False, idle_s=60)
ctrl2 = fired(reset=True, idle_s=600)
print(f"대조(복귀 없음): 발화 {ctrl} · 대조 2(복귀 뒤 한가한 10분): 발화 {ctrl2}")
if not (ctrl and ctrl2):
    print("[FAIL] 대조가 성립하지 않는다 — 프로브를 먼저 고친다")
    sys.exit(2)
got = fired(reset=True, idle_s=60)
print(f"복귀 뒤 1분 만에 메모리 90%: 발화 {got}")
if got:
    print("[PASS] 복귀 직후의 이상도 잡는다")
    sys.exit(0)
print("[FAIL] 복귀 직후 시작한 이상(메모리 40→90%)이 베이스라인에 흡수돼 발화하지 않는다")
sys.exit(1)
