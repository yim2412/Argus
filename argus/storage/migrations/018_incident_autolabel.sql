-- 자동 라벨 — 기계가 매긴 "이 알림이 쓸모 있었나".
--
-- **`user_label` 과 같은 칸을 쓰지 않는다.** 라벨의 유일한 쓰임이 "알림을 줄일지"를
-- 정하는 것인데, 기계가 매긴 값을 사람 답과 같은 칸에 섞으면 **기계가 매긴 것으로
-- 기계를 고치게 된다.** 그 순간 "이 판단이 내 규칙의 메아리였나"를 되짚을 방법이
-- 사라진다 — 칸이 둘이면 언제든 사람 답만 다시 세어 볼 수 있다.
--
-- 판정 기준은 실측 라벨 7건에서 나왔다 (2026-08-14):
--
--   발열 스로틀링   3건 → 전부 real     (하드웨어가 실제로 성능을 깎았다)
--   CPU 병목·경합   4건 → 전부 normal   (원인이 내가 띄운 앱이었다)
--
-- 근거를 함께 남기는 이유: 라벨만 있으면 나중에 "왜 이게 정상이지"에 답할 수 없고,
-- 답할 수 없는 판정은 문턱을 고칠 근거가 못 된다.

ALTER TABLE incidents ADD COLUMN auto_label        TEXT;  -- 'normal' | 'real' | NULL(판정 없음)
ALTER TABLE incidents ADD COLUMN auto_label_reason TEXT;  -- 사람이 읽는 판정 근거 한 줄
ALTER TABLE incidents ADD COLUMN auto_labeled_at   REAL;
