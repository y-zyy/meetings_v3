"""OpenAI 호환 /v1/chat/completions 목 서버 (vLLM 등 로컬 LLM 서버 흉내).

- 동시 시퀀스 수 제한(MOCK_LLM_MAX_SEQS) 을 넘는 요청은 대기한다(vLLM 스케줄러와 유사).
- 처리 시간 = 입력 토큰 / PREFILL_TPS + 출력 토큰 / DECODE_TPS (x TIME_SCALE)
  DECODE_TPS 는 동시 실행 시퀀스가 늘수록 MOCK_LLM_DECODE_DEGRADE 만큼 느려진다.
- STT 후처리 요청은 원문 줄을 그대로 돌려주되, max_tokens 를 넘으면 실제 모델처럼 잘라낸다.
  (기존 코드의 "전체 스크립트 1회 호출 → 출력 잘림 → 줄 수 불일치로 결과 폐기" 를 재현)

환경 변수
  MOCK_LLM_MAX_SEQS         동시 처리 시퀀스 수                       [32]
  MOCK_LLM_PREFILL_TPS      입력 토큰 처리 속도(토큰/초)              [4000]
  MOCK_LLM_DECODE_TPS       시퀀스 1개 기준 출력 속도(토큰/초)        [40]
  MOCK_LLM_DECODE_DEGRADE   동시 시퀀스 1개 추가 시 속도 감소율       [0.02]
  MOCK_LLM_CONTEXT          최대 컨텍스트 토큰 (초과 시 400)          [131072]
  MOCK_LLM_CHARS_PER_TOKEN  한국어 글자/토큰 환산                     [1.2]
  MOCK_LLM_MINUTES_TOKENS   회의록 생성 응답 토큰 수                  [2000]
  MOCK_TIME_SCALE           대기 시간 배율                            [1.0]

실행: uvicorn mock_llm_server:app --port 8001   (LLM_API_BASE_URL=http://host:8001/v1)
"""

import asyncio
import os
import re
import time

from fastapi import FastAPI, HTTPException, Request

MAX_SEQS = int(os.getenv("MOCK_LLM_MAX_SEQS", "32"))
PREFILL_TPS = float(os.getenv("MOCK_LLM_PREFILL_TPS", "4000"))
DECODE_TPS = float(os.getenv("MOCK_LLM_DECODE_TPS", "40"))
DEGRADE = float(os.getenv("MOCK_LLM_DECODE_DEGRADE", "0.02"))
CONTEXT = int(os.getenv("MOCK_LLM_CONTEXT", "131072"))
CHARS_PER_TOKEN = float(os.getenv("MOCK_LLM_CHARS_PER_TOKEN", "1.2"))
MINUTES_TOKENS = int(os.getenv("MOCK_LLM_MINUTES_TOKENS", "2000"))
TIME_SCALE = float(os.getenv("MOCK_TIME_SCALE", "1.0"))

app = FastAPI(title="Mock LLM")
_sem = asyncio.Semaphore(MAX_SEQS)
_state = {"active": 0, "waiting": 0, "requests": 0, "truncated": 0, "context_errors": 0,
          "prompt_tokens": 0, "completion_tokens": 0, "max_active": 0}

_TRANSCRIPT_RE = re.compile(r"=== 음성인식 원문[^\n]*===\n(.*?)\n\n위 원문에서", re.S)


def _tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


@app.get("/health")
async def health():
    return dict(_state, max_seqs=MAX_SEQS, time_scale=TIME_SCALE)


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "mock", "object": "model"}]}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    prompt = "\n".join(m.get("content", "") for m in messages)
    max_tokens = int(body.get("max_tokens") or 4096)
    prompt_tokens = _tokens(prompt)
    _state["requests"] += 1
    if prompt_tokens + max_tokens > CONTEXT:
        _state["context_errors"] += 1
        raise HTTPException(400, f"context length exceeded: {prompt_tokens}+{max_tokens} > {CONTEXT}")

    match = _TRANSCRIPT_RE.search(prompt)
    finish = "stop"
    if match:  # STT 후처리: 원문 그대로(교정 없음) 반환, max_tokens 초과분은 잘림
        content = match.group(1)
        max_chars = int(max_tokens * CHARS_PER_TOKEN)
        if len(content) > max_chars:
            content, finish = content[:max_chars], "length"
            _state["truncated"] += 1
    else:  # 회의록 생성
        content = "## 주요 내용\n" + ("- 논의 내용 요약 문장입니다.\n" * (MINUTES_TOKENS // 12))
    completion_tokens = min(max_tokens, _tokens(content))

    _state["waiting"] += 1
    async with _sem:
        _state["waiting"] -= 1
        _state["active"] += 1
        _state["max_active"] = max(_state["max_active"], _state["active"])
        try:
            started = time.monotonic()
            await asyncio.sleep(prompt_tokens / PREFILL_TPS * TIME_SCALE)
            # 디코딩: 동시 시퀀스 수에 따라 속도가 변하므로 조각 단위로 진행
            remaining = completion_tokens
            while remaining > 0:
                tps = DECODE_TPS / (1 + DEGRADE * max(0, _state["active"] - 1))
                step = min(remaining, max(1, int(tps)))  # 최대 1초 분량씩
                await asyncio.sleep(step / tps * TIME_SCALE)
                remaining -= step
        finally:
            _state["active"] -= 1
    _state["prompt_tokens"] += prompt_tokens
    _state["completion_tokens"] += completion_tokens
    return {
        "id": "mock-1", "object": "chat.completion", "created": int(time.time()),
        "model": body.get("model", "mock"),
        "choices": [{"index": 0, "finish_reason": finish,
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
        "x_elapsed_seconds": round(time.monotonic() - started, 3),
    }
