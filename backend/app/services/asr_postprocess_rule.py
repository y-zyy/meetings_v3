"""Rule-based STT post-processing using Aho-Corasick algorithm."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import ahocorasick


@dataclass
class CorrectionResult:
    original: str
    corrected: str
    changed: bool
    matches: list[dict] = field(default_factory=list)

    def __repr__(self):
        tag = "✓" if self.changed else "-"
        return f"[{tag}] {self.original!r} -> {self.corrected!r}"


@dataclass
class SentenceResult:
    original: str
    corrected: str
    units: list[CorrectionResult] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.original != self.corrected


@dataclass
class ParagraphResult:
    original: str
    corrected: str
    sentences: list[SentenceResult] = field(default_factory=list)

    @property
    def changed_count(self) -> int:
        return sum(1 for s in self.sentences if s.changed)


class GlossaryIndex:
    """Aho-Corasick 기반 용어 교정 인덱스."""

    def __init__(self):
        self._automaton: ahocorasick.Automaton = ahocorasick.Automaton()
        self._built = False
        self._patterns: dict[str, str] = {}
        self._pending: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def build(self, pairs: list[tuple[str, str]]) -> "GlossaryIndex":
        with self._lock:
            self._automaton = ahocorasick.Automaton()
            for wrong, correct in pairs:
                self._add_to_automaton(wrong, correct)
                self._patterns[wrong] = correct
            if self._patterns:
                self._automaton.make_automaton()
            self._built = True
        return self

    def _add_to_automaton(self, wrong: str, correct: str):
        self._automaton.add_word(wrong, (wrong, correct))

    def add(self, wrong: str, correct: str):
        """신규 패턴 추가 — pending에만 쌓음 (재빌드 없음)."""
        with self._lock:
            if wrong not in self._patterns:
                self._pending.append((wrong, correct))

    def flush(self):
        """pending 패턴을 automaton에 반영하고 재빌드. 주기적으로 호출."""
        with self._lock:
            if not self._pending:
                return
            for wrong, correct in self._pending:
                self._add_to_automaton(wrong, correct)
                self._patterns[wrong] = correct
            self._automaton.make_automaton()
            self._pending.clear()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def pattern_count(self) -> int:
        return len(self._patterns)

    def search(self, text: str) -> list[dict]:
        if not self._built or not self._patterns:
            return []
        hits = []
        for end_idx, (wrong, correct) in self._automaton.iter(text):
            start_idx = end_idx - len(wrong) + 1
            hits.append({
                "start": start_idx,
                "end": end_idx + 1,
                "wrong": wrong,
                "correct": correct,
            })
        return hits

    def replace(self, text: str) -> CorrectionResult:
        """탐지된 패턴을 일괄 치환. 겹치는 패턴은 가장 긴 것 우선 (greedy)."""
        hits = self.search(text)
        if not hits:
            return CorrectionResult(text, text, False)

        hits = self._resolve_overlaps(hits)
        hits_sorted = sorted(hits, key=lambda x: x["start"], reverse=True)
        chars = list(text)

        for h in hits_sorted:
            chars[h["start"]:h["end"]] = list(h["correct"])

        corrected = "".join(chars)
        return CorrectionResult(
            original=text,
            corrected=corrected,
            changed=(corrected != text),
            matches=hits,
        )

    @staticmethod
    def _resolve_overlaps(hits: list[dict]) -> list[dict]:
        hits = sorted(hits, key=lambda x: (x["start"], -(x["end"] - x["start"])))
        resolved = []
        last_end = -1
        for h in hits:
            if h["start"] >= last_end:
                resolved.append(h)
                last_end = h["end"]
        return resolved

    def save(self, path: str):
        data = [{"wrong": w, "correct": c} for w, c in self._patterns.items()]
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str) -> "GlossaryIndex":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        idx = cls()
        idx.build([(d["wrong"], d["correct"]) for d in data])
        return idx


def load_seed_pairs(path: str) -> list[tuple[str, str]]:
    """시드 JSON 파일에서 (wrong, correct) 쌍을 로드. 파일마다 매번 새로 읽으므로
    파일 수정 시 재배포/재시작 없이 다음 호출부터 바로 반영된다."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [(d["wrong"], d["correct"]) for d in data if d.get("wrong") and d.get("correct")]


# ── 문장/문단 처리 ──────────────────────────────────────────────────────────────

import re  # noqa: E402


class Segmenter:
    SENTENCE_PATTERN = re.compile(r'(?<=[.?!])\s+|(?<=[요다죠])\s+|(?<=\n)')

    def split_sentences(self, paragraph: str) -> list[str]:
        sentences = self.SENTENCE_PATTERN.split(paragraph.strip())
        return [s.strip() for s in sentences if s.strip()]

    def split_units(self, sentence: str) -> list[str]:
        return sentence.split()

    def reconstruct(self, units: list[str]) -> str:
        return " ".join(units)


class ASRPostProcessor:
    """단일 텍스트 / 문단 Rule-based 후처리."""

    def __init__(self, glossary: GlossaryIndex):
        self.glossary = glossary
        self.segmenter = Segmenter()

    def correct_sentence(self, sentence: str) -> SentenceResult:
        units = self.segmenter.split_units(sentence)
        results = [self.glossary.replace(u) for u in units]
        corrected_sentence = self.segmenter.reconstruct([r.corrected for r in results])
        return SentenceResult(original=sentence, corrected=corrected_sentence, units=results)

    def correct_paragraph(self, paragraph: str) -> ParagraphResult:
        sentences = self.segmenter.split_sentences(paragraph)
        sentence_results = [self.correct_sentence(s) for s in sentences]
        corrected_paragraph = " ".join(sr.corrected for sr in sentence_results)
        return ParagraphResult(
            original=paragraph,
            corrected=corrected_paragraph,
            sentences=sentence_results,
        )

    def correct_text(self, text: str) -> str:
        """전체 텍스트를 후처리하여 교정된 텍스트 반환 (파이프라인 진입점)."""
        if not text or not text.strip():
            return text
        result = self.correct_paragraph(text)
        return result.corrected


# ── 모듈 레벨 편의 함수 ────────────────────────────────────────────────────────

def build_glossary_index(pairs: list[tuple[str, str]]) -> GlossaryIndex:
    return GlossaryIndex().build(pairs)


def apply_rule_based_correction(text: str, pairs: list[tuple[str, str]]) -> str:
    """patterns dict 또는 pairs 목록으로 텍스트 교정 (Celery 태스크용)."""
    if not text or not pairs:
        return text
    idx = build_glossary_index(pairs)
    processor = ASRPostProcessor(idx)
    return processor.correct_text(text)
