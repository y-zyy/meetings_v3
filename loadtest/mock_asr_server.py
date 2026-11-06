"""WhisperX 서버(/transcribe, /align_diarize) 를 흉내 내는 부하 테스트용 목 서버.

실제 GPU 없이 백엔드 파이프라인(API → Celery → Redis/Postgres → ASR/LLM) 의
동시성·대기열 동작을 검증하기 위한 용도다. 처리 시간은 "오디오 길이 x RTF x TIME_SCALE" 로
흉내 내고, GPU 슬롯은 실제 서버와 동일한 우선순위 게이트로 직렬화한다.

환경 변수
  MOCK_GPU_SLOTS          동시 추론 슬롯 수 (= WHISPERX_MAX_CONCURRENCY x GPU 수)       [1]
  MOCK_RTF_TRANSCRIBE     전사 실시간 배수 (처리시간/오디오길이). large-v3 batched ≈ 0.02~0.05 [0.03]
  MOCK_RTF_DIARIZE        정렬+화자분리 실시간 배수 (pyannote 3.x ≈ 0.02~0.05)          [0.03]
  MOCK_TIME_SCALE         모든 대기 시간에 곱하는 배율 (0.01 이면 100배 빠르게)          [1.0]
  MOCK_PRIORITY           true: 정렬/화자분리 우선(개선안), false: FIFO(기존 서버 동작) [true]
  MOCK_SEGMENT_SECONDS    세그먼트 1개가 차지하는 오디오 길이(초)                         [6]

실행: uvicorn mock_asr_server:app --port 9000
"""

import asyncio
import heapq
import itertools
import json
import os
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

GPU_SLOTS = max(1, int(os.getenv("MOCK_GPU_SLOTS", "1")))
RTF_TRANSCRIBE = float(os.getenv("MOCK_RTF_TRANSCRIBE", "0.03"))
RTF_DIARIZE = float(os.getenv("MOCK_RTF_DIARIZE", "0.03"))
TIME_SCALE = float(os.getenv("MOCK_TIME_SCALE", "1.0"))
PRIORITY = os.getenv("MOCK_PRIORITY", "true").lower() in ("1", "true", "yes")
SEGMENT_SECONDS = float(os.getenv("MOCK_SEGMENT_SECONDS", "6"))

SAMPLE_SENTENCES = [
    "이번 분기 매출 목표 대비 진행 상황을 먼저 공유드리겠습니다",
    "데이터 파이프라인 지연 문제는 다음 주까지 원인을 파악하겠습니다",
    "고객사 요구사항 중에서 보안 인증 관련 항목이 가장 시급합니다",
    "일정이 빠듯하니까 우선순위를 다시 정리할 필요가 있을 것 같아요",
    "그 부분은 담당자를 지정해서 금요일까지 검토 결과를 받아보죠",
    "서버 증설 비용은 예산 범위 안에서 처리 가능한지 확인해 주세요",
]


class PriorityGate:
    def __init__(self, slots: int):
        self.slots, self.active = slots, 0
        self._waiters: list = []
        self._seq = itertools.count()
        self.stats = {"waiting": {}, "done": {}, "busy_seconds": 0.0, "skipped_disconnected": 0}

    def _waiting(self) -> int:
        return sum(1 for *_, f in self._waiters if not f.done())

    @asynccontextmanager
    async def acquire(self, priority: int, kind: str):
        w = self.stats["waiting"]
        w[kind] = w.get(kind, 0) + 1
        try:
            if self.active < self.slots and not self._waiting():
                self.active += 1
            else:
                fut = asyncio.get_running_loop().create_future()
                heapq.heappush(self._waiters, (priority if PRIORITY else 0, next(self._seq), fut))
                try:
                    await fut
                except asyncio.CancelledError:
                    if fut.done() and not fut.cancelled():
                        self._release()
                    raise
        finally:
            w[kind] -= 1
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.stats["busy_seconds"] += time.monotonic() - t0
            self.stats["done"][kind] = self.stats["done"].get(kind, 0) + 1
            self._release()

    def _release(self):
        while self._waiters:
            *_, fut = heapq.heappop(self._waiters)
            if not fut.done():
                fut.set_result(None)
                return
        self.active -= 1


gate = PriorityGate(GPU_SLOTS)
app = FastAPI(title="Mock WhisperX")


def _probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


async def _save(file: UploadFile) -> str:
    ext = os.path.splitext(file.filename or "")[-1].lower() or ".bin"
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        while chunk := await file.read(4 * 1024 * 1024):
            tmp.write(chunk)
        return tmp.name


async def _gpu(request: Request, priority: int, kind: str, seconds: float) -> dict:
    queued = time.monotonic()
    async with gate.acquire(priority, kind):
        if await request.is_disconnected():
            gate.stats["skipped_disconnected"] += 1
            raise HTTPException(status_code=499, detail="client disconnected")
        started = time.monotonic()
        await asyncio.sleep(seconds * TIME_SCALE)
    return {
        "queue_wait_seconds": round(started - queued, 3),
        "inference_seconds": round(time.monotonic() - started, 3),
    }


@app.get("/health")
async def health():
    return {
        "status": "ok", "model_loaded": True, "diarization_available": True,
        "max_concurrency": GPU_SLOTS, "priority": PRIORITY, "time_scale": TIME_SCALE,
        "gpu_active": gate.active, "gpu_waiting": dict(gate.stats["waiting"]),
        "gpu_done": dict(gate.stats["done"]),
        "gpu_busy_seconds": round(gate.stats["busy_seconds"], 2),
        "skipped_disconnected": gate.stats["skipped_disconnected"],
    }


@app.post("/transcribe")
async def transcribe(request: Request, file: UploadFile = File(...), language: str | None = None):
    path = await _save(file)
    try:
        duration = await asyncio.to_thread(_probe_duration, path)
    finally:
        os.unlink(path)
    timings = await _gpu(request, 1, "transcribe", duration * RTF_TRANSCRIBE)
    n = max(1, int(duration // SEGMENT_SECONDS))
    segments = [
        {"start": round(i * SEGMENT_SECONDS, 3), "end": round((i + 1) * SEGMENT_SECONDS - 0.5, 3),
         "text": SAMPLE_SENTENCES[i % len(SAMPLE_SENTENCES)]}
        for i in range(n)
    ]
    return JSONResponse({
        "text": " ".join(s["text"] for s in segments), "language": language or "ko",
        "segments": segments, "timings": timings,
    })


@app.post("/align_diarize")
async def align_diarize(
    request: Request,
    file: UploadFile = File(...),
    segments: str = Form(...),
    language: str | None = Form(None),
    diarize: bool = Form(True),
    min_speakers: int | None = Form(None),
    max_speakers: int | None = Form(None),
):
    path = await _save(file)
    try:
        duration = await asyncio.to_thread(_probe_duration, path)
    finally:
        os.unlink(path)
    parsed = json.loads(segments)
    timings = await _gpu(request, 0, "align_diarize", duration * RTF_DIARIZE)
    out = [
        {"start": s["start"], "end": s["end"], "speaker": f"SPEAKER_0{i % 3}", "text": s["text"]}
        for i, s in enumerate(parsed)
    ]
    return JSONResponse({"language": language or "ko", "diarized": True, "segments": out, "timings": timings})
