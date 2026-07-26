# 변경 이력

형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/)를 따르고,
버전은 [유의적 버전](https://semver.org/lang/ko/)을 따른다.

배포 대상 프로그램이라 첫 코드 커밋부터 이력을 남긴다.

## [Unreleased]

### Phase 0 — 골격과 자기 계측

첫 실행 가능한 형태. 아직 시스템 메트릭은 수집하지 않고 **Argus 자신만 관측한다.**
모니터가 병목이 되는 것이 이런 도구의 1순위 실패 모드라, 수집기보다 자기 계측을 먼저 만들었다.

#### 추가

- **`argus.paths`** — `%APPDATA%\Argus` 데이터 경로, PyInstaller `_MEIPASS` 대응 리소스 경로.
  `ARGUS_DATA_DIR` 로 덮어쓸 수 있어 개발 중 실사용 데이터를 건드리지 않는다.
- **`argus.logging_setup`** — 콘솔(사람용) + JSON Lines 파일(기계용) 이중 출력, 회전 로그,
  크래시 별도 파일 기록. 한국어 Windows 콘솔(cp949)에서 한글이 깨지지 않도록 UTF-8 로 전환.
- **`argus.config`** — 기본값 → 사용자 `settings.yaml` → 환경변수(`ARGUS_*`) 순 병합,
  pydantic 검증. 첫 실행 시 주석 포함 설정 파일을 사용자 폴더에 생성한다.
- **`argus.machine.capabilities`** — 관리자 권한·psutil·PDH·NVML·ETW·서명검증 가용성 탐지.
  없으면 이유(`reason`)와 함께 기록해 조용히 실패하지 않는다.
- **`argus.machine.calibration`** — CPU/메모리/디스크 짧은 벤치(약 3초)로 `machine_profile.json`
  생성. 하드웨어를 가정하지 않기 위한 기준선으로, 하드웨어 시그니처가 바뀌면 자동 재측정.
- **`argus.storage.hot`** — SQLite WAL, 번호순 SQL 마이그레이션 프레임(`PRAGMA user_version`),
  `meta`·`self_telemetry` 테이블.
- **`argus.runtime.supervisor`** — 컴포넌트 스레드 생명주기, 에러 격리(하나가 죽어도 나머지 지속),
  지수 백오프, 시그널 기반 정상 종료.
- **`argus.runtime.budget`** — 자기 CPU/RSS 예산 감시, 히스테리시스가 있는 4단계 스로틀.
  `cpu_percent` 를 논리 코어 수로 정규화해 "머신 전체의 N%" 의미가 되게 했다.
- **`argus.runtime.selftel`** — 자기 계측을 `self_telemetry` 에 기록 (핸들 수 포함 — Windows 에서
  핸들 누수는 메모리보다 먼저 드러난다).
- **`argus.__main__`** — `python -m argus`, `--check`, `--duration`, `--recalibrate`.
- **`tests/test_shutdown.py`** — 실제 시그널을 보내 정상 종료와 DB 무결성을 검증.

#### 결정

- **Python 3.12 고정** (`>=3.12,<3.13`). 시스템 기본은 3.14 였으나 PyTorch·river 지원이
  불확실해 Phase 7 에서 막힐 위험이 있어 회피했다. `PLAN.md` 의 3.11 표기도 3.12 로 수정.
- **스키마 마이그레이션을 첫 테이블과 함께 도입.** 배포 후에는 스키마 변경이 "남의 PC 에 있는
  DB 를 고치는 일"이 되므로 나중에 붙일 수 없다.
- **전체 스키마 파일을 두지 않는다.** 정본은 `migrations/NNN_*.sql` 뿐. 두 벌이 되면 어긋난다.

#### 수정

- 시그널 핸들러가 `_stop` 을 세운 뒤 `Supervisor.stop()` 이 조기 반환해 **스레드 join 과
  `teardown()` 이 건너뛰어지던 문제.** 데몬 스레드라 프로세스는 종료됐지만 진행 중인 DB 쓰기를
  기다리지 않았다. "종료 요청"과 "정리 완료"를 별도 플래그로 분리. (`tests/test_shutdown.py` 가 발견)

#### 측정 (Phase 0 완료 기준)

60초 상주 실행 기준 — AMD 6C/12T · 64GB · SSD:

| 항목 | 실측 | 예산 |
|---|---|---|
| CPU | 평균 0.013% / 최대 0.050% | 2.0% |
| RSS | 평균 67.5MB | 300MB |
| 기록 간격 | 평균 5.009초 (설정 5초) | — |
| 핸들 | 281 → 286 (안정) | 누수 없음 |
| `drop_count` | 0 | 0 |

24시간 연속 가동 검증은 Phase 1 에서 실제 수집기를 붙인 뒤 함께 수행한다.
