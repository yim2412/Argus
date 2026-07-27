-- Phase 1 마무리: 자기 계측의 메모리 지표를 RSS 하나에서 셋으로 늘린다.
--
-- 6시간 소크 중간 집계에서 RSS 가 63MB → 18MB 로 **내려갔다.** 메모리를 반납한 게
-- 아니라, 백그라운드(pythonw)로 도는 프로세스의 워킹셋을 Windows 가 트림해 페이지를
-- 디스크로 내보낸 것이다. 즉 **RSS 만으로는 장기 메모리 추세를 판정할 수 없다.**
-- 누수가 실제로 있어도 트림이 그걸 가려 "괜찮아 보이는" 그래프가 나온다.
--
-- private_bytes 는 커밋된 사설 메모리라 트림의 영향을 받지 않는다. 누수는 여기서 는다.
-- peak_wset 은 되돌아가지 않는 상한이라, 트림된 구간에서도 실제로 얼마나 썼는지 남는다.
-- page_faults 는 트림이 실제로 일어났는지 확인하는 근거다(트림 후 재접근 = 폴트 급증).
--
-- USS 는 넣지 않았다. 워킹셋 기반이라 트림되면 RSS 와 함께 줄어 같은 문제를 겪고,
-- Windows 에서 계산 비용도 가장 크다(전 페이지 스캔). 관측자는 가벼워야 한다.

ALTER TABLE self_telemetry ADD COLUMN private_mb   REAL;    -- 커밋된 사설 메모리 (누수 판정의 정본)
ALTER TABLE self_telemetry ADD COLUMN peak_wset_mb REAL;    -- 워킹셋 최대치 (단조 증가)
ALTER TABLE self_telemetry ADD COLUMN page_faults  INTEGER; -- 누적 페이지 폴트 (트림 발생 근거)
