# 변경 이력

형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/)를 따르고,
버전은 [유의적 버전](https://semver.org/lang/ko/)을 따른다.

배포 대상 프로그램이라 첫 코드 커밋부터 이력을 남긴다.

## [Unreleased]

### Phase 1 — 수집 레이어

실제 메트릭을 수집해 SQLite 에 쌓기 시작한다.

#### 추가

- **`argus.collector.system`** — 1Hz 시스템 스냅샷. psutil(사용률·처리량) + PDH(응답시간·큐·실효 클럭).
- **`argus.collector.pdh`** — Windows 성능 카운터. 디스크 응답시간처럼 psutil 로 얻을 수 없는
  "증상" 지표를 담당한다.
- **`argus.collector.procsource`** — 프로세스 스냅샷 소스 2종(PDH / psutil 폴백).
- **`argus.collector.process`** — 전 프로세스 매 틱 수집, 저장은 선별(활성 집합 1초 / 전체 30초).
  프로세스 생성·종료 이벤트 기록.
- **`argus.collector.gpu`** — NVML. 사용률·VRAM·온도·전력·P-State·클럭·**스로틀 사유**.
- **`argus.collector.network`** — 연결 스냅샷(30초). DNS 역조회는 하지 않는다.
- **`argus.storage.queue`** — 상한 도달 시 오래된 것부터 버리는 비블로킹 큐.
- **`argus.storage.writer`** — 200ms/500행 배치 삽입, 종료 시 잔여 데이터 flush.
- **`argus.storage.retention`** — 기한 지난 원본 삭제. VACUUM 은 하지 않는다(우리가 관측 대상인
  디스크에 큰 IO 를 만들어 스스로 이상을 유발한다).
- 마이그레이션 `002_metrics.sql` — `metrics_raw`, `gpu_metrics`, `process_metrics`,
  `process_events`, `net_connections`.

#### 설계 변경 — 계층을 수집이 아니라 저장에 둔다

`PLAN.md` 의 원안은 "전체는 10초마다, 활성 집합만 1초마다 **수집**"이었다. psutil 로는
전체 스캔이 1.4초나 걸려 수집 자체를 아껴야 했기 때문이다.

측정해 보니 전제가 틀렸다. PDH `Process V2` 와일드카드 질의는 전 프로세스를 **13ms** 에
가져온다(psutil 1,380ms 대비 106배). 수집을 아낄 이유가 없어졌다.

비싼 것은 저장이다. 프로세스 330개를 1초마다 넣으면 하루 2,800만 행이다. 그래서 계층을
저장 단계로 옮겼다 — 매 틱 전부 수집하되, 활성 집합만 1초 해상도로 저장하고 전체는 30초마다
저장한다. 전체 그림과 문제 프로세스 추적을 둘 다 얻으면서 행 수는 원안 수준으로 유지된다.

#### 실측으로 잡은 함정 3가지

이 세 가지는 모두 **틀린 채로도 그럴듯한 숫자가 나와** 눈으로는 알아챌 수 없었다.

1. **PDH 카운터 인덱스 하드코딩은 위험하다.** 널리 알려진 값인 `Context Switches/sec = 146`,
   `Avg. Disk sec/Transfer = 208` 을 넣었으나 이 시스템의 실제 값은 각각 **14340, 206** 이었고
   208 은 `Avg. Disk sec/Read` 였다. 그런데도 그럴듯한 수치가 나왔다.
   → 레지스트리의 영문(009) 카운터 목록에서 **이름으로 인덱스를 역조회**한 뒤, 그 인덱스로
   지역화된 이름을 얻어 경로를 조립하도록 바꿨다. 인덱스를 추측하지 않으면서 로케일 독립성도 유지된다.
2. **PDH `Process` (V1) 객체는 프로세스를 대량 누락한다.** 인스턴스 이름이 프로세스명뿐이라
   같은 이름이 여러 개면 합쳐진다. 실측 결과 336개 중 **200개(총 11GB RSS)** 를 놓쳤고,
   하필 chrome·Discord·Steam·claude 처럼 가장 보고 싶은 것들이었다.
   → `Process V2`(인스턴스명이 `name:pid`)를 쓴다. 커버리지 336/336, 누락 0.
3. **PDH 는 백분율을 100 에서 자른다.** `NOCAP100` 없이 읽으면 1코어 넘게 쓰는 프로세스가
   전부 100 으로 보고된다(실측: Idle 이 capped 100 / nocap 976). 기여도 계산(Phase 8)의
   근간이 조용히 망가질 뻔했다.

#### 결정

- **쓰지 않는 인덱스는 만들지 않는다.** `process_metrics(name, ts)` 인덱스가 데이터 172KB 에
  대해 98KB(DB 의 21%)를 차지했는데, 이를 쓰는 질의는 Phase 6 이전에는 없다. 이 테이블은
  보존 기한이 24시간이라 나중에 추가해도 재구축이 빠르다.
- **DNS 역조회를 하지 않는다.** 연결마다 이름을 물으면 그 자체가 네트워크 트래픽을 만들어
  관측 대상을 오염시키고, 조회 기록이 외부에 남는다.
- **두 프로세스 소스의 메모리 지표는 완전히 같지 않다.** PDH 는 `Working Set - Private`,
  psutil 폴백은 `Private Bytes` 다(실측 152MB vs 236MB). Windows 가 대응하는 값을 psutil 로
  싸게 주지 않는다. 한 머신에서 소스는 바뀌지 않으므로 베이스라인 일관성은 유지되며,
  머신 간 절대값 비교는 어차피 하지 않는다(하드웨어 무가정 원칙).

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
