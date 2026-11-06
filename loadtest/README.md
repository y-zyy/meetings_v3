# 부하 테스트 (loadtest/)

동시 건수 **5 / 10 / 30 / 50 / 100건** x 음성 길이 **30분 / 1시간 / 2시간** 시나리오를
End-to-End(업로드 → 변환 → ASR → STT 후처리 → 화자 분리 → 회의록)로 측정한다.

| 파일 | 역할 |
|---|---|
| `run_loadtest.py` | 시나리오 실행기. N건 동시 업로드 → `/status` 폴링으로 단계 전이 시각 기록 → 요약/리포트 |
| `gen_audio.py` | 30/60/120분 테스트 음성 생성 (실제 음성 파일 반복 또는 합성 신호) |
| `mock_asr_server.py` | WhisperX 목 서버 (GPU 슬롯·우선순위 게이트·처리 시간 RTF 흉내) |
| `mock_llm_server.py` | OpenAI 호환 LLM 목 서버 (동시 시퀀스 제한, 토큰 속도, max_tokens 잘림 재현) |
| `local_stack.sh` | Docker 없이 Postgres/Redis/API/Celery/목 서버 전체 기동 |
| `docker-compose.loadtest.yml` | 배포 스택에 목 ASR/LLM 을 붙이는 compose 오버레이 |

## 1. 실제 서버 대상 테스트 (권장: 최종 용량 산정용)

```bash
pip install -r loadtest/requirements.txt
```

```bash
# 실제 회의 음성(5~10분 이상)을 반복해 30/60/120분 파일 생성
python loadtest/gen_audio.py --source sample_meeting.m4a --format mp3 --out-dir loadtest/audio
```

```bash
cd loadtest
python run_loadtest.py --base-url https://<서버IP>:8443 --insecure \
  --username <테스트계정> --password '<비밀번호>' \
  --concurrency 5,10,30,50,100 --durations 30,60,120 \
  --source sample_meeting.m4a --format mp3 \
  --asr-health-url http://<GPU서버>:9000/health \
  --cleanup
```

- 시나리오는 `음성 길이 → 동시 건수` 순서로 실행되며, 각 시나리오 결과는 끝날 때마다 저장된다
  (`results/<실행시각>/scenario_c{N}_d{M}.json`, `summary.csv`, `report.md`).
- 일부만 돌리려면 `--concurrency 30 --durations 60` 처럼 지정한다.
- 실제 GPU 1대 기준 100건 x 2시간은 GPU 작업만 수 시간~10시간 이상 걸린다. `--timeout-minutes` 로 상한을 둔다.
- `--asr-health-url` 을 주면 WhisperX `/health` 의 GPU 대기열(`gpu_waiting`)·누적 GPU 사용 시간을
  폴링마다 기록해 GPU 사용률을 계산할 수 있다.
- 합성 신호(기본값)는 VAD 에서 걸러져 전사량이 0에 가깝다. **실제 서버 테스트에는 반드시 `--source` 로 실제 음성을 사용**한다.

## 2. GPU/LLM 없이 파이프라인만 테스트 (목 서버)

백엔드 오케스트레이션(큐, 워커 동시성, DB 커넥션, 단계 순서)을 검증하거나 설정 변경 효과를 빠르게 비교할 때 사용한다.

### Docker compose

```bash
cd infra
MOCK_TIME_SCALE=0.05 docker compose -f docker-compose.yml -f ../loadtest/docker-compose.loadtest.yml up -d --build
```

### Docker 없이 (로컬)

```bash
cd loadtest
PYTHON=/path/to/venv/bin/python MOCK_TIME_SCALE=0.05 ./local_stack.sh start
```

```bash
python run_loadtest.py --base-url http://localhost:8000 --format flac \
  --concurrency 5,10,30,50,100 --durations 30,60,120 \
  --time-scale 0.05 --poll-interval 1 --cleanup \
  --asr-health-url http://127.0.0.1:9000/health --llm-health-url http://127.0.0.1:8001/health
```

```bash
./local_stack.sh stop
```

- `MOCK_TIME_SCALE=0.05` 는 목 서버의 처리 시간을 1/20 로 압축한다. `run_loadtest.py --time-scale 0.05` 를 같이 주면
  `report.md` 가 실제 시간으로 환산되어 출력된다. (폴링 간격·업로드 등 압축되지 않는 고정 지연도 함께 20배로
  환산되므로 수십 초 단위 오차가 있다.)
- GPU 대수/슬롯은 `MOCK_GPU_SLOTS`, 처리 속도는 `MOCK_RTF_TRANSCRIBE` / `MOCK_RTF_DIARIZE`,
  LLM 은 `MOCK_LLM_MAX_SEQS` / `MOCK_LLM_DECODE_TPS` 로 조정한다. 실제 서버에서 1건씩 측정한 값으로 맞추면 된다.
- 기존(FIFO) 서버 동작과 비교하려면 `MOCK_PRIORITY=false`.
- 워커 구성: `WORKERS="ingest:4 asr:2 diarize:2 postprocess:6 minutes:10"`.

## 지표 읽는 법

| 지표 | 의미 |
|---|---|
| 첫 완료 | 업로드 시작부터 첫 회의록 완료까지 |
| E2E p50/p95 | 건별 완료 시간 분포 |
| 전체 소요(makespan) | 마지막 건 완료까지 — GPU 총 작업량 / GPU 슬롯 수 에 수렴 |
| 처리량 | 처리된 음성 시간 / 실제 경과 시간 |
| 단계 열 | `/status` 상태가 그 값으로 머문 평균 시간. **다음 단계 워커를 기다린 시간이 포함**된다 (예: `queued` = ASR 슬롯 대기) |
