"""화자 분리 결과(segments) 포맷팅 헬퍼."""


def fmt_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_segments_text(segments: list[dict], speaker_names: dict | None = None) -> str:
    """'[00:00:03 ~ 00:00:07] SPEAKER_00: 내용' 형태의 줄 단위 텍스트로 변환."""
    names = speaker_names or {}
    lines = []
    for seg in segments:
        speaker = seg.get("speaker")
        label = f" {names.get(speaker) or speaker}:" if speaker else ""
        lines.append(
            f"[{fmt_timestamp(seg.get('start', 0))} ~ {fmt_timestamp(seg.get('end', 0))}]"
            f"{label} {seg.get('text', '')}"
        )
    return "\n".join(lines)
