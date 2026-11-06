"""Kiwi 형태소 분석기를 이용한 문장 단위 줄바꿈 포맷팅."""

from functools import lru_cache


@lru_cache(maxsize=1)
def _get_kiwi():
    from kiwipiepy import Kiwi
    return Kiwi()


def split_into_lines(text: str) -> str:
    """텍스트를 문장 단위로 분리해 줄마다 하나의 문장이 오도록 반환합니다."""
    if not text or not text.strip():
        return text

    kiwi = _get_kiwi()
    sentences = [seg.text for seg in kiwi.split_into_sents(text)]
    return "\n".join(sentences)
