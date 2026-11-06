"""Celery tasks for the staged ASR + LLM processing pipeline.

처리 순서: ingest(ffmpeg) → asr(GPU) → postprocess(LLM) → diarize(GPU) → minutes(LLM)

각 단계는 "짧은 DB 세션으로 입력 읽기 → 세션 반납 → 외부 호출(ASR/LLM) → 짧은 세션으로 결과 저장"
패턴을 따른다. 외부 호출은 수 분~수십 분 걸리므로 그 동안 DB 커넥션/트랜잭션을 붙잡고 있으면
(idle in transaction) 동시 처리 건수만큼 커넥션이 묶이고 VACUUM 도 막힌다.
"""

import copy
import logging
import os
import subprocess
from datetime import datetime, timezone

from celery import chain
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Sync engine for Celery workers (Celery does not use asyncio)
_sync_url = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
_engine = create_engine(_sync_url, pool_size=1, max_overflow=2, pool_pre_ping=True)
SyncSession = sessionmaker(bind=_engine, expire_on_commit=False)


def _get_meeting(session: Session, meeting_id: int):
    from app.models.meeting import Meeting
    return session.get(Meeting, meeting_id)


def _update_meeting(meeting_id: int, **fields) -> None:
    """짧은 세션으로 meeting 컬럼을 갱신한다."""
    from app.models.user import User  # noqa: F401 — registers relationship mapper

    with SyncSession() as session:
        meeting = _get_meeting(session, meeting_id)
        if meeting is None:
            return
        for key, value in fields.items():
            setattr(meeting, key, value)
        session.commit()


def _load_meeting_snapshot(meeting_id: int, *, with_effective: bool = True) -> dict | None:
    """처리에 필요한 meeting 필드를 dict 로 복사하고 세션을 즉시 반납한다."""
    from app.models.user import User  # noqa: F401 — registers relationship mapper
    from app.services.runtime_settings import get_effective_settings_sync

    with SyncSession() as session:
        meeting = _get_meeting(session, meeting_id)
        if meeting is None:
            return None
        snap = {
            "id": meeting.id,
            "title": meeting.title,
            "meeting_date": str(meeting.meeting_date) if meeting.meeting_date else "",
            "location": meeting.location,
            "attendees": meeting.attendees,
            "agenda": meeting.agenda,
            "notes": meeting.notes,
            "language": meeting.language,
            "file_path": meeting.file_path,
            "transcript": meeting.transcript,
            "segments": copy.deepcopy(meeting.segments or []),
            "speaker_names": meeting.speaker_names,
            "created_by": meeting.created_by,
        }
        if with_effective:
            snap["effective"] = get_effective_settings_sync(session)
        session.rollback()  # 읽기 전용 트랜잭션 종료
        return snap


def _save_markdown_snapshot(meeting: dict, suffix: str, heading: str, body: str) -> None:
    """오디오 파일과 같은 디렉터리에 처리 단계별 결과를 markdown 파일로 저장.

    파일명: {uid}_{suffix}.md (uid는 오디오 파일명에서 확장자를 뗀 부분)
    저장 실패는 전체 파이프라인을 막지 않도록 로그만 남기고 무시한다.
    """
    file_path = meeting.get("file_path")
    if not file_path:
        return
    try:
        dest_dir = os.path.dirname(file_path)
        uid, _ = os.path.splitext(os.path.basename(file_path))
        md_path = os.path.join(dest_dir, f"{uid}_{suffix}.md")

        generated_at = datetime.now(timezone.utc).isoformat()
        header = (
            f"# {heading}\n\n"
            f"- 회의: {meeting.get('title')}\n"
            f"- 회의 ID: {meeting.get('id')}\n"
            f"- 생성 일시(UTC): {generated_at}\n\n"
            "---\n\n"
        )
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(header + (body or ""))
        logger.info("[%s] markdown 저장 완료: %s", meeting.get("id"), md_path)
    except Exception:
        logger.exception("[%s] markdown 저장 실패 (suffix=%s)", meeting.get("id"), suffix)


def _retry_stage(task, meeting_id: int, stage: str, exc: Exception):
    """Retry only the failed stage and expose failure after retries are exhausted."""
    logger.exception("[%s] %s 단계 실패: %s", meeting_id, stage, exc)

    if task.request.retries >= task.max_retries:
        try:
            _update_meeting(
                meeting_id, status="failed", error_message=f"{stage}: {exc}"[:500]
            )
        except Exception:
            logger.exception("[%s] 실패 상태 저장 실패", meeting_id)
        raise exc

    countdown = min(60, 10 * (2 ** task.request.retries))
    raise task.retry(exc=exc, countdown=countdown)


def _probe_duration_seconds(path: str) -> int | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            check=True, capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        return int(round(float(out)))
    except Exception:
        logger.warning("오디오 길이 측정 실패: %s", path)
        return None


@celery_app.task(bind=True, name="process_meeting")
def process_meeting(self, meeting_id: int):
    """Replace the compatibility entry point with a checkpointed stage chain."""
    workflow = chain(
        convert_audio.si(meeting_id),
        transcribe_meeting.si(meeting_id),
        postprocess_meeting.si(meeting_id),
        diarize_meeting.si(meeting_id),
        generate_minutes_task.si(meeting_id),
    )
    return self.replace(workflow)


@celery_app.task(bind=True, name="process_meeting.ingest", max_retries=2)
def convert_audio(self, meeting_id: int):
    """Convert the uploaded source to 16 kHz mono FLAC outside the API process."""
    snap = _load_meeting_snapshot(meeting_id, with_effective=False)
    if not snap:
        logger.error("Meeting %s not found", meeting_id)
        return meeting_id

    source_path = snap["file_path"]
    if not source_path or not os.path.exists(source_path):
        _retry_stage(
            self, meeting_id, "오디오 변환",
            FileNotFoundError(source_path or "업로드 파일 경로가 없습니다."),
        )

    if os.path.splitext(source_path)[1].lower() == ".flac":
        logger.info("[%s] FLAC 입력 — 변환 생략", meeting_id)
        _update_meeting(meeting_id, duration_seconds=_probe_duration_seconds(source_path))
        return meeting_id

    target_path = os.path.splitext(source_path)[0] + ".flac"
    temporary_target = os.path.splitext(source_path)[0] + ".converting.flac"
    try:
        _update_meeting(meeting_id, status="audio_processing", error_message=None)
        logger.info("[%s] 오디오 변환 시작: %s", meeting_id, source_path)

        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-y", "-i", source_path,
                "-vn", "-ar", "16000", "-ac", "1", temporary_target,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        os.replace(temporary_target, target_path)
        if source_path != target_path and os.path.exists(source_path):
            os.remove(source_path)

        _update_meeting(
            meeting_id,
            file_path=target_path,
            duration_seconds=_probe_duration_seconds(target_path),
        )
        logger.info("[%s] 오디오 변환 완료: %s", meeting_id, target_path)
        return meeting_id
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        detail = stderr[-1000:] if stderr else str(exc)
        _retry_stage(self, meeting_id, "오디오 변환", RuntimeError(detail))
    except Exception as exc:
        _retry_stage(self, meeting_id, "오디오 변환", exc)
    finally:
        if os.path.exists(temporary_target):
            try:
                os.remove(temporary_target)
            except OSError:
                logger.warning(
                    "[%s] 임시 변환 파일 삭제 실패: %s", meeting_id, temporary_target
                )


@celery_app.task(bind=True, name="process_meeting.asr", max_retries=2)
def transcribe_meeting(self, meeting_id: int):
    from app.services import asr

    snap = _load_meeting_snapshot(meeting_id)
    if not snap:
        logger.error("Meeting %s not found", meeting_id)
        return meeting_id

    try:
        _update_meeting(meeting_id, status="asr_processing", error_message=None)
        logger.info("[%s] ASR 시작: %s", meeting_id, snap["file_path"])

        asr_result = asr.transcribe_detailed(snap["file_path"], snap["effective"])
        segments = asr_result["segments"]
        # 정렬/화자 분리는 후처리 이후(diarize_meeting)에 수행한다.
        transcript = (
            "\n".join(seg["text"] for seg in segments) if segments else asr_result["text"]
        )
        _update_meeting(
            meeting_id,
            segments=segments or None,
            language=asr_result.get("language") or snap["language"],
            transcript=transcript,
        )

        logger.info(
            "[%s] ASR 완료 (%d chars, %d segments)", meeting_id, len(transcript), len(segments)
        )
        _save_markdown_snapshot(snap, "01_asr", "음성인식 결과 (ASR)", transcript)
        return meeting_id
    except Exception as exc:
        _retry_stage(self, meeting_id, "ASR", exc)


def _load_postprocess_inputs(meeting_id: int, created_by: int) -> tuple[list, list[str], list[str]]:
    from sqlalchemy import select as sa_select

    from app.models.glossary import AdminCorrectionRule, AdminGlossaryTerm, UserGlossaryTerm
    from app.services.asr_postprocess_rule import load_seed_pairs

    seed_pairs = load_seed_pairs(settings.CORRECTION_RULES_SEED_PATH)
    with SyncSession() as session:
        db_pairs = [
            (r.wrong, r.correct)
            for r in session.execute(
                sa_select(AdminCorrectionRule).order_by(AdminCorrectionRule.created_at)
            ).scalars().all()
        ]
        admin_terms = [
            r.term + (f" ({r.description})" if r.description else "")
            for r in session.execute(
                sa_select(AdminGlossaryTerm).order_by(AdminGlossaryTerm.created_at)
            ).scalars().all()
        ]
        user_terms = [
            r.term + (f" ({r.description})" if r.description else "")
            for r in session.execute(
                sa_select(UserGlossaryTerm)
                .where(UserGlossaryTerm.user_id == created_by)
                .order_by(UserGlossaryTerm.order, UserGlossaryTerm.created_at)
            ).scalars().all()
        ]
        session.rollback()
    rule_pairs = list({**dict(seed_pairs), **dict(db_pairs)}.items())
    logger.info(
        "[%s] 교정 규칙 시드 %d + DB %d = 총 %d", meeting_id, len(seed_pairs), len(db_pairs), len(rule_pairs)
    )
    return rule_pairs, admin_terms, user_terms


@celery_app.task(bind=True, name="process_meeting.postprocess", max_retries=2)
def postprocess_meeting(self, meeting_id: int):
    from app.services import asr_postprocess
    from app.services.asr_postprocess_rule import (
        ASRPostProcessor,
        apply_rule_based_correction,
        build_glossary_index,
    )

    snap = _load_meeting_snapshot(meeting_id)
    if not snap:
        logger.error("Meeting %s not found", meeting_id)
        return meeting_id

    try:
        _update_meeting(meeting_id, status="stt_postprocessing")
        logger.info("[%s] STT 후처리 시작", meeting_id)

        segments = snap["segments"]
        rule_pairs, admin_terms, user_terms = _load_postprocess_inputs(
            meeting_id, snap["created_by"]
        )

        # 1) Rule-based 교정 (Aho-Corasick)
        if segments:
            lines = [seg["text"] for seg in segments]
            if rule_pairs:
                processor = ASRPostProcessor(build_glossary_index(rule_pairs))
                lines = [processor.correct_text(t) if t else t for t in lines]
        else:
            # 세그먼트가 없는 경로(OpenAI Whisper 등): 문장 단위 줄로 먼저 나눠야 청크 처리가 가능하다.
            from app.services.sentence_split import split_into_lines

            text = snap["transcript"] or ""
            if rule_pairs:
                text = apply_rule_based_correction(text, rule_pairs)
            lines = split_into_lines(text).split("\n") if text.strip() else []

        # 2) LLM 교정 (청크 병렬, 청크별 줄 수 검증)
        if settings.LLM_POSTPROCESS_ENABLED and lines:
            lines, stats = asr_postprocess.postprocess_lines(
                lines,
                title=snap["title"],
                meeting_date=snap["meeting_date"],
                location=snap["location"],
                attendees=snap["attendees"],
                agenda=snap["agenda"],
                notes=snap["notes"],
                admin_terms=admin_terms,
                user_terms=user_terms,
                effective=snap["effective"],
            )
            logger.info("[%s] LLM 후처리 청크 결과: %s", meeting_id, stats)

        fields: dict = {"transcript": "\n".join(lines)}
        if segments:
            for seg, line in zip(segments, lines):
                seg["text"] = line
            fields["segments"] = segments
        _update_meeting(meeting_id, **fields)

        logger.info("[%s] STT 후처리 완료 (%d chars)", meeting_id, len(fields["transcript"]))
        _save_markdown_snapshot(
            snap, "02_asr_postprocessed", "음성인식 후처리 결과", fields["transcript"]
        )
        return meeting_id
    except Exception as exc:
        _retry_stage(self, meeting_id, "STT 후처리", exc)


@celery_app.task(bind=True, name="process_meeting.diarize", max_retries=2)
def diarize_meeting(self, meeting_id: int):
    """후처리된 텍스트를 오디오에 정렬(align)하고 화자(diarize)를 부여한다."""
    from app.services import asr
    from app.services.speaker import format_segments_text

    snap = _load_meeting_snapshot(meeting_id)
    if not snap:
        logger.error("Meeting %s not found", meeting_id)
        return meeting_id

    segments = snap["segments"]
    if not segments or not settings.ASR_DIARIZE_ENABLED:
        logger.info("[%s] 화자 분리 생략 (segments 없음 또는 비활성화)", meeting_id)
        return meeting_id

    try:
        _update_meeting(meeting_id, status="diarizing")
        logger.info("[%s] 정렬/화자 분리 시작 (%d segments)", meeting_id, len(segments))

        result = asr.align_diarize(
            snap["file_path"], segments, snap["language"], snap["effective"],
        )
        if not result["segments"]:
            raise RuntimeError("정렬/화자 분리 결과가 비어 있습니다.")

        transcript = format_segments_text(result["segments"], snap["speaker_names"])
        _update_meeting(meeting_id, segments=result["segments"], transcript=transcript)
        logger.info(
            "[%s] 정렬/화자 분리 완료 (%d segments, diarized=%s)",
            meeting_id, len(result["segments"]), result["diarized"],
        )
        _save_markdown_snapshot(snap, "02b_diarized", "화자 분리 결과", transcript)
        return meeting_id
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            # 화자 분리 실패가 회의록 생성 자체를 막지 않도록, 화자 없는 후처리 텍스트로 계속 진행
            logger.exception("[%s] 화자 분리 실패 — 화자 정보 없이 계속 진행: %s", meeting_id, exc)
            return meeting_id
        _retry_stage(self, meeting_id, "화자 분리", exc)


@celery_app.task(bind=True, name="process_meeting.minutes", max_retries=2)
def generate_minutes_task(self, meeting_id: int):
    from app.models.meeting import ActionItem, Decision
    from app.models.user import User  # noqa: F401 — registers relationship mapper
    from app.services import llm

    snap = _load_meeting_snapshot(meeting_id)
    if not snap:
        logger.error("Meeting %s not found", meeting_id)
        return meeting_id

    try:
        _update_meeting(meeting_id, status="llm_processing")
        logger.info("[%s] LLM 시작", meeting_id)

        result = llm.generate_minutes(
            title=snap["title"],
            meeting_date=snap["meeting_date"],
            location=snap["location"],
            attendees=snap["attendees"],
            agenda=snap["agenda"],
            transcript=snap["transcript"] or "",
            effective=snap["effective"],
        )

        with SyncSession() as session:
            meeting = _get_meeting(session, meeting_id)
            if meeting is None:
                return meeting_id
            meeting.summary = result.get("summary", "")

            session.query(Decision).filter_by(meeting_id=meeting_id).delete()
            for i, content in enumerate(result.get("decisions", [])):
                session.add(Decision(meeting_id=meeting_id, content=str(content), order=i))

            session.query(ActionItem).filter_by(meeting_id=meeting_id).delete()
            for i, item in enumerate(result.get("action_items", [])):
                session.add(ActionItem(
                    meeting_id=meeting_id,
                    content=str(item.get("content", "")),
                    assignee=item.get("assignee") or None,
                    due_date=_parse_date(item.get("due_date")),
                    status="pending",
                    order=i,
                ))

            meeting.status = "done"
            meeting.error_message = None
            session.commit()

        _save_markdown_snapshot(snap, "03_summary", "LLM 요약 결과", result.get("summary", ""))
        logger.info("[%s] 처리 완료", meeting_id)
        return meeting_id
    except Exception as exc:
        _retry_stage(self, meeting_id, "회의록 생성", exc)


def _parse_date(value):
    if not value:
        return None
    from datetime import date
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None

