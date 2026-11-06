import re

from app.config import settings

USER_PROMPT_TEMPLATE = """\
회의 제목: {title}
참석자: {attendees}
회의 안건: {agenda}

회의록 스크립트: {transcript}

1. 주요 내용 예약
회의록 스크립트에 대해서 상세하게 요약해줘.
회의 정보(회의 제목, 참석자, 회의 안건)을 적극적으로 활용해서 상세 요약에 활용해줘.

2. Action Item 추출
Action Item도 함께 상세하게 요약해줘.
주어진 참석자 정보를 활용하여 다음과 같은 markdown 표 형태로 추출해줘.
|Action Item|담당자|일정|비고|
|------|-----|-----|-----|

3. 작성 형식
일반 텍스트(plain markdown)로만 작성하고, LaTeX/수식 표기(예: $...$, \\rightarrow)는 사용하지 마.
화살표는 반드시 유니코드 문자 → 를 그대로 사용해줘.
"""

SECOND_PASS_PROMPT_TEMPLATE = """\
아래는 회의록 초본과 원본 스크립트입니다.
초본을 기준으로 더욱 상세하고 완성도 높은 최종 회의록을 작성해줘.

[원본 스크립트]
{transcript}

[회의록 초본]
{first_draft}

다음 지침에 따라 초본을 개선해줘.

1. 주요 내용 상세화
- 초본의 요약 내용을 바탕으로, 스크립트에서 누락된 세부 논의 사항, 발언 맥락, 결정 근거 등을 보완해줘.
- 단순 나열이 아닌, 논의 흐름이 자연스럽게 이어지도록 작성해줘.

2. Action Item 보완
- 초본의 Action Item 표를 검토하여, 스크립트에서 추가로 확인되는 항목을 보완해줘.
- 담당자, 일정, 비고가 누락된 경우 스크립트에서 근거를 찾아 채워줘.
- 기존 항목도 더 구체적인 내용으로 보완해줘.

|Action Item|담당자|일정|비고|
|------|-----|-----|-----|

3. 작성 형식
일반 텍스트(plain markdown)로만 작성하고, LaTeX/수식 표기(예: $...$, \\rightarrow)는 사용하지 마.
화살표는 반드시 유니코드 문자 → 를 그대로 사용해줘.
"""


def generate_minutes(
    title: str,
    meeting_date: str,
    location: str,
    attendees: str,
    agenda: str,
    transcript: str,
    effective: dict | None = None,
) -> dict:
    """Call the LLM API and return meeting minutes as free-form text."""
    cfg = effective or {k: getattr(settings, k, "") for k in (
        "LLM_API_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_TIMEOUT", "LLM_MAX_TOKENS",
    )}

    first_prompt = USER_PROMPT_TEMPLATE.format(
        title=title,
        attendees=attendees,
        agenda=agenda,
        transcript=transcript,
    )
    first_draft = _call_openai(first_prompt, cfg)

    second_prompt = SECOND_PASS_PROMPT_TEMPLATE.format(
        transcript=transcript,
        first_draft=first_draft,
    )
    final = _call_openai(second_prompt, cfg)

    return {"summary": _sanitize_latex_arrows(final), "decisions": [], "action_items": []}


_LATEX_ARROW_MAP = {
    r"\rightarrow": "→",
    r"\Rightarrow": "⇒",
    r"\leftarrow": "←",
    r"\Leftarrow": "⇐",
    r"\leftrightarrow": "↔",
    r"\Leftrightarrow": "⇔",
}

# Matches a LaTeX math span like "$...\rightarrow...$" and unwraps it,
# since the frontend's markdown renderer doesn't evaluate LaTeX.
_LATEX_MATH_SPAN_RE = re.compile(r"\$([^$\n]*(?:\\\w+)[^$\n]*)\$")


def _sanitize_latex_arrows(text: str) -> str:
    def _unwrap(match: re.Match) -> str:
        inner = match.group(1)
        for latex, unicode_char in _LATEX_ARROW_MAP.items():
            inner = inner.replace(latex, unicode_char)
        return inner

    text = _LATEX_MATH_SPAN_RE.sub(_unwrap, text)
    for latex, unicode_char in _LATEX_ARROW_MAP.items():
        text = text.replace(latex, unicode_char)
    return text


def _call_openai(prompt: str, cfg: dict) -> str:
    from app.services.http_clients import get_openai_client

    base_url = cfg.get("LLM_API_BASE_URL") or settings.LLM_API_BASE_URL
    api_key = cfg.get("LLM_API_KEY") or "none"
    model = cfg.get("LLM_MODEL") or settings.LLM_MODEL
    timeout = int(cfg.get("LLM_TIMEOUT") or settings.LLM_TIMEOUT)
    max_tokens = int(cfg.get("LLM_MAX_TOKENS") or settings.LLM_MAX_TOKENS)

    # 호출마다 새 클라이언트(=새 커넥션 풀/TLS 핸드셰이크)를 만들지 않고 프로세스 단위로 재사용
    client = get_openai_client(base_url=base_url, api_key=api_key, timeout=timeout)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "user", "content": prompt},
        ],
        temperature=0.9, # follows the default settings
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content or ""
