"""평가 인프라 — 탐지기를 숫자로 채점한다.

이 패키지가 Phase 3 이후의 모든 판단 근거다. 이상 탐지에는 정답이 없으므로
결함 주입(`tools/fault_injector.py`)으로 정답 구간을 **만들고**, 저장된 데이터를
리플레이해서(`replay`) 탐지기에 흘린 뒤, 그 결과를 채점한다(`scoring`).

    python -m argus.eval --detector all

CLAUDE.md 의 "수치 없이 모델을 추가하지 않는다"가 여기에 기댄다.

하위 모듈을 여기서 import 하지 않는다 — `python -m argus.eval.replay` 로 단독 스모크를
돌릴 때 runpy 가 이중 로드를 경고하고, 그 경고가 `[OK]/[FAIL]` 출력을 더럽힌다.
"""

