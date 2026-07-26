"""수집 레이어.

계층을 나눈다.
  T1  시스템 카운터 1Hz     — psutil + PDH
  T2  프로세스 적응형       — 활성 집합만 1Hz, 전체는 느리게
  T3  GPU 1Hz              — pynvml

각 하위 모듈은 `python -m argus.collector.<name>` 으로 단독 실행해 확인할 수 있다.
"""
