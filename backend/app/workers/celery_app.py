from celery import Celery
from celery.signals import worker_process_init

from app.config import settings


_soft_limit = settings.CELERY_TASK_SOFT_TIME_LIMIT
_hard_limit = max(settings.CELERY_TASK_TIME_LIMIT, _soft_limit + 60)
_visibility_timeout = max(settings.CELERY_VISIBILITY_TIMEOUT, _hard_limit + 60)

celery_app = Celery(
    "meetings",
    broker=settings.REDIS_URL,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    timezone="Asia/Seoul",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_ignore_result=True,
    task_store_errors_even_if_ignored=False,
    task_soft_time_limit=_soft_limit,
    task_time_limit=_hard_limit,
    broker_transport_options={"visibility_timeout": _visibility_timeout},
    visibility_timeout=_visibility_timeout,
    task_routes={
        "process_meeting": {"queue": "ingest"},
        "process_meeting.ingest": {"queue": "ingest"},
        "process_meeting.asr": {"queue": "asr"},
        "process_meeting.postprocess": {"queue": "postprocess"},
        # 화자 분리는 전사(asr)와 큐를 분리한다. 같은 FIFO 큐를 쓰면 1번 회의의 화자 분리가
        # 나머지 N-1 건의 전사가 모두 끝날 때까지 밀려, 첫 결과 지연과 LLM 단계 몰림이 발생한다.
        "process_meeting.diarize": {"queue": "diarize"},
        "process_meeting.minutes": {"queue": "minutes"},
    },
)



@worker_process_init.connect
def _reset_db_pool_after_fork(**_kwargs):
    """prefork 자식 프로세스가 부모의 DB 커넥션을 공유하지 않도록 풀을 버린다."""
    from app.workers.tasks import _engine

    _engine.dispose(close=False)
