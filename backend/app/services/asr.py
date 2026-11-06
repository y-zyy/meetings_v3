import json

from app.config import settings
from app.services.http_clients import get_http_client, get_openai_client


def transcribe(file_path: str, effective: dict | None = None) -> str:
    """Transcribe audio and return plain text only."""
    return transcribe_detailed(file_path, effective)["text"]


def transcribe_detailed(file_path: str, effective: dict | None = None) -> dict:
    """Transcribe audio.

    Returns {"text": str, "segments": list[{start, end, speaker, text}]}.
    segments is empty when the backend provides no diarization (e.g. OpenAI).

    effective: dict returned by get_effective_settings_sync(); when None the
    module-level settings object is used directly (backward-compat).
    """
    cfg = effective or {k: getattr(settings, k, "") for k in (
        "OPENAI_API_KEY", "ASR_API_URL", "ASR_API_KEY",
        "ASR_FILE_FIELD", "ASR_RESPONSE_FIELD", "ASR_TIMEOUT",
    )}
    if cfg.get("OPENAI_API_KEY"):
        return {"text": _transcribe_whisper(file_path, cfg), "segments": []}
    return _transcribe_http(file_path, cfg)


def _transcribe_whisper(file_path: str, cfg: dict) -> str:
    timeout = int(cfg.get("ASR_TIMEOUT") or settings.ASR_TIMEOUT)
    client = get_openai_client(api_key=cfg["OPENAI_API_KEY"], timeout=timeout)
    with open(file_path, "rb") as f:
        response = client.audio.transcriptions.create(model="whisper-1", file=f)
    return response.text


def _transcribe_http(file_path: str, cfg: dict) -> dict:
    headers = {}
    if cfg.get("ASR_API_KEY"):
        headers["Authorization"] = f"Bearer {cfg['ASR_API_KEY']}"

    url = cfg.get("ASR_API_URL") or settings.ASR_API_URL
    file_field = cfg.get("ASR_FILE_FIELD") or settings.ASR_FILE_FIELD
    response_field = cfg.get("ASR_RESPONSE_FIELD") or settings.ASR_RESPONSE_FIELD
    timeout = int(cfg.get("ASR_TIMEOUT") or settings.ASR_TIMEOUT)

    with open(file_path, "rb") as audio_file:
        client = get_http_client(timeout)
        response = client.post(url, files={file_field: audio_file}, headers=headers)
        response.raise_for_status()

    data = response.json()
    text = data.get(response_field, "")
    if not isinstance(text, str):
        raise ValueError(f"ASR API 응답에서 텍스트 필드({response_field})를 찾을 수 없습니다.")
    return {"text": text, "segments": _clean_segments(data.get("segments"))}


def _clean_segments(raw) -> list[dict]:
    """ASR 서버가 돌려준 segments를 {start, end, speaker, text} 형태로 정규화."""
    if not isinstance(raw, list):
        return []
    out = []
    for seg in raw:
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(seg.get("start", 0.0))
            end = float(seg.get("end", start))
        except (TypeError, ValueError):
            continue
        item = {"start": start, "end": end, "text": text}
        if seg.get("speaker"):
            item["speaker"] = str(seg["speaker"])
        out.append(item)
    return out


def align_diarize(
    file_path: str,
    segments: list[dict],
    language: str | None = None,
    effective: dict | None = None,
) -> dict:
    """후처리된 segments 텍스트를 오디오에 정렬하고 화자를 부여한다 (WhisperX 서버 /align_diarize).

    Returns {"language", "diarized", "segments": [{start, end, speaker, text}]}.
    """
    cfg = effective or {}
    url = settings.ASR_DIARIZE_API_URL or _derive_diarize_url(
        cfg.get("ASR_API_URL") or settings.ASR_API_URL
    )
    api_key = cfg.get("ASR_API_KEY") or settings.ASR_API_KEY
    file_field = cfg.get("ASR_FILE_FIELD") or settings.ASR_FILE_FIELD
    timeout = int(cfg.get("ASR_TIMEOUT") or settings.ASR_TIMEOUT)

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    form = {
        "segments": json.dumps(
            [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in segments],
            ensure_ascii=False,
        ),
    }
    if language:
        form["language"] = language
    if settings.ASR_MIN_SPEAKERS:
        form["min_speakers"] = str(settings.ASR_MIN_SPEAKERS)
    if settings.ASR_MAX_SPEAKERS:
        form["max_speakers"] = str(settings.ASR_MAX_SPEAKERS)

    with open(file_path, "rb") as audio_file:
        client = get_http_client(timeout)
        response = client.post(
            url, data=form, files={file_field: audio_file}, headers=headers
        )
        response.raise_for_status()

    data = response.json()
    diarized = bool(data.get("diarized"))
    cleaned = _clean_segments(data.get("segments"))
    if not diarized:
        # 화자 분리가 수행되지 않았으면(HF_TOKEN 없음 등) UNKNOWN 라벨을 남기지 않는다.
        for seg in cleaned:
            seg.pop("speaker", None)
    return {"language": data.get("language"), "diarized": diarized, "segments": cleaned}


def _derive_diarize_url(asr_url: str) -> str:
    base = asr_url.rstrip("/")
    if base.endswith("/transcribe"):
        base = base[: -len("/transcribe")]
    return base + "/align_diarize"
