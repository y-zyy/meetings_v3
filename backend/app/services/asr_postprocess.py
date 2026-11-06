"""STT 후처리: LLM을 사용해 음성인식 오류를 보정합니다.

긴 회의(30분 이상)를 한 번의 호출로 보내면 출력이 LLM_MAX_TOKENS 에서 잘려
줄 수가 맞지 않게 되고, 그 결과는 통째로 버려진다(LLM 시간만 소모).
그래서 발화(줄) 단위로 청크를 나눠 병렬 호출하고, 청크별로 줄 수를 검증해
불일치/실패한 청크만 원문을 유지한다.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

from app.config import settings
from app.services.http_clients import get_openai_client

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "당신은 음성인식(STT) 결과를 후처리하는 전문가입니다. "
    "음성인식 출력에서 발생한 오인식 용어, 인명, 장소, 전문용어 등의 오류를 수정합니다. "
    "다음 원칙을 반드시 준수하세요:\n"
    "1. 원문의 표현, 말투, 문체, 구조를 그대로 유지합니다.\n"
    "2. 확실한 오인식(잘못 인식된 고유명사, 전문용어, 인명, 장소 등)만 수정합니다.\n"
    "3. 내용 요약, 재구성, 문장 다듬기는 하지 않습니다.\n"
    "4. 제공된 용어사전의 단어들이 정확하게 표기되도록 수정합니다.\n"
    "5. 수정된 텍스트만 출력하고, 설명이나 다른 텍스트는 포함하지 마세요.\n"
    "6. 줄바꿈 위치와 줄 수를 원문과 정확히 동일하게 유지합니다."
)

POSTPROCESS_PROMPT_TEMPLATE = """\
아래 회의 정보와 용어사전을 참고하여 음성인식 결과를 후처리하세요.

=== 회의 정보 ===
회의 제목: {title}
회의 일시: {meeting_date}
회의 장소: {location}
참석자: {attendees}
회의 안건:
{agenda}
참고사항: {notes}

=== 용어사전 ===
{glossary_section}

=== 음성인식 원문 ({line_count}줄) ===
{transcript}

위 원문에서 회의 정보 및 용어사전을 참고하여 확실한 오인식만 수정한 후처리 결과를 출력하세요.
원문의 표현과 구조를 그대로 유지하고, 정확히 {line_count}줄로 수정된 텍스트만 출력하세요."""


def chunk_lines(lines: list[str], max_chars: int) -> list[tuple[int, int]]:
    """줄 목록을 글자 수 기준으로 [start, end) 구간들로 나눈다. 한 줄은 쪼개지 않는다."""
    max_chars = max(1, max_chars)
    ranges: list[tuple[int, int]] = []
    start, size = 0, 0
    for i, line in enumerate(lines):
        length = len(line) + 1
        if i > start and size + length > max_chars:
            ranges.append((start, i))
            start, size = i, 0
        size += length
    if start < len(lines):
        ranges.append((start, len(lines)))
    return ranges


def _glossary_section(admin_terms: list[str], user_terms: list[str]) -> str:
    parts = []
    if admin_terms:
        parts.append("【전역 용어사전】\n" + "\n".join(f"- {t}" for t in admin_terms))
    if user_terms:
        parts.append("【사용자 용어사전】\n" + "\n".join(f"- {t}" for t in user_terms))
    return "\n\n".join(parts) if parts else "(용어사전 없음)"


def postprocess_lines(
    lines: list[str],
    title: str,
    meeting_date: str,
    location: str,
    attendees: str,
    agenda: str,
    notes: str,
    admin_terms: list[str],
    user_terms: list[str],
    effective: dict | None = None,
    *,
    chunk_chars: int | None = None,
    parallelism: int | None = None,
) -> tuple[list[str], dict]:
    """줄 목록을 청크 단위로 LLM 후처리한다. 반환 줄 수는 항상 입력과 같다.

    Returns (corrected_lines, stats) — stats: {"chunks", "applied", "mismatched", "failed"}.
    """
    stats = {"chunks": 0, "applied": 0, "mismatched": 0, "failed": 0}
    if not lines or not any(line.strip() for line in lines):
        return list(lines), stats

    chunk_chars = chunk_chars or settings.LLM_POSTPROCESS_CHUNK_CHARS
    parallelism = max(1, parallelism or settings.LLM_POSTPROCESS_PARALLELISM)
    context = {
        "title": title or "",
        "meeting_date": meeting_date or "",
        "location": location or "",
        "attendees": attendees or "",
        "agenda": agenda or "",
        "notes": notes or "",
        "glossary_section": _glossary_section(admin_terms, user_terms),
    }
    ranges = chunk_lines(lines, chunk_chars)
    stats["chunks"] = len(ranges)

    def _process(rng: tuple[int, int]) -> tuple[list[str], str]:
        original = lines[rng[0]:rng[1]]
        prompt = POSTPROCESS_PROMPT_TEMPLATE.format(
            line_count=len(original), transcript="\n".join(original), **context
        )
        try:
            result = _call_llm(prompt, effective).strip("\n")
        except Exception as exc:  # 청크 하나의 실패가 전체를 막지 않도록
            logger.warning("STT 후처리 청크 %s 실패: %s", rng, exc)
            return original, "failed"
        corrected = result.split("\n") if result.strip() else []
        if len(corrected) != len(original):
            logger.warning(
                "STT 후처리 청크 %s 줄 수 불일치(%d != %d) — 원문 유지",
                rng, len(corrected), len(original),
            )
            return original, "mismatched"
        return [c.strip() or o for c, o in zip(corrected, original)], "applied"

    if len(ranges) == 1 or parallelism == 1:
        results = [_process(r) for r in ranges]
    else:
        with ThreadPoolExecutor(max_workers=min(parallelism, len(ranges))) as pool:
            results = list(pool.map(_process, ranges))

    out: list[str] = []
    for chunk, outcome in results:
        out.extend(chunk)
        stats[outcome] += 1
    return out, stats


def postprocess_transcript(
    transcript: str,
    title: str,
    meeting_date: str,
    location: str,
    attendees: str,
    agenda: str,
    notes: str,
    admin_terms: list[str],
    user_terms: list[str],
    effective: dict | None = None,
) -> str:
    """STT 결과(여러 줄 텍스트)를 LLM으로 후처리하여 반환합니다. (하위 호환용 래퍼)"""
    if not transcript or not transcript.strip():
        return transcript
    lines, _ = postprocess_lines(
        transcript.split("\n"), title, meeting_date, location, attendees,
        agenda, notes, admin_terms, user_terms, effective,
    )
    return "\n".join(lines)


def _call_llm(prompt: str, effective: dict | None) -> str:
    cfg = effective or {}
    base_url = cfg.get("LLM_API_BASE_URL") or settings.LLM_API_BASE_URL
    api_key = cfg.get("LLM_API_KEY") or "none"
    model = cfg.get("LLM_MODEL") or settings.LLM_MODEL
    timeout = int(cfg.get("LLM_TIMEOUT") or settings.LLM_TIMEOUT)
    max_tokens = int(cfg.get("LLM_MAX_TOKENS") or settings.LLM_MAX_TOKENS)

    client = get_openai_client(base_url=base_url, api_key=api_key, timeout=timeout)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content or ""
