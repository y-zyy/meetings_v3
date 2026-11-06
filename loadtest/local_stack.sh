#!/usr/bin/env bash
# Docker 없이 로컬에서 전체 백엔드 파이프라인 + 목 ASR/LLM 서버를 띄운다 (부하 테스트용).
#
#   ./local_stack.sh start      # 기동
#   ./local_stack.sh stop       # 종료 (DB 데이터 포함 STATE_DIR 삭제는 하지 않음)
#
# 주요 환경 변수
#   PYTHON          백엔드 의존성이 설치된 python               [python3]
#   BACKEND_DIR     백엔드 소스 경로(기존 버전 비교 시 다른 체크아웃 지정) [../backend]
#   STATE_DIR       pg 데이터/로그/업로드 저장 위치             [./.stack]
#   WORKERS         "큐:동시성" 목록                            ["ingest:4 asr:2 diarize:2 postprocess:6 minutes:10"]
#   API_WORKERS     uvicorn 프로세스 수                         [4]
#   MOCK_*          mock_asr_server.py / mock_llm_server.py 참고
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-python3}"
BACKEND_DIR="$(cd "${BACKEND_DIR:-$HERE/../backend}" && pwd)"
STATE_DIR="${STATE_DIR:-$HERE/.stack}"
WORKERS="${WORKERS:-ingest:4 asr:2 diarize:2 postprocess:6 minutes:10}"
API_WORKERS="${API_WORKERS:-4}"
PG_PORT="${PG_PORT:-55432}"
REDIS_PORT="${REDIS_PORT:-56379}"
API_PORT="${API_PORT:-8000}"
ASR_PORT="${ASR_PORT:-9000}"
LLM_PORT="${LLM_PORT:-8001}"
# root 실행 시 PG_DATA 상위 경로는 postgres 계정이 접근 가능해야 한다
PG_DATA="${PG_DATA:-$STATE_DIR/pg}"
PG_LOG="${PG_LOG:-$PG_DATA.log}"
PG_BIN="${PG_BIN:-$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | tail -1)}"

mkdir -p "$STATE_DIR/logs" "$STATE_DIR/uploads" "$STATE_DIR/pids"

# postgres 는 root 로 실행할 수 없으므로 root 일 때는 postgres 계정으로 실행
PG_AS=()
if [ "$(id -u)" = "0" ]; then
  PG_AS=(runuser -u postgres --)
  mkdir -p "$PG_DATA"
  chown postgres "$PG_DATA"
  touch "$PG_LOG"; chown postgres "$PG_LOG"
fi

export DATABASE_URL="postgresql+asyncpg://meetings:meetings@127.0.0.1:${PG_PORT}/meetings"
export REDIS_URL="redis://127.0.0.1:${REDIS_PORT}/0"
export UPLOAD_DIR="$STATE_DIR/uploads"
export ASR_API_URL="http://127.0.0.1:${ASR_PORT}/transcribe"
export LLM_API_BASE_URL="http://127.0.0.1:${LLM_PORT}/v1"
export LLM_MODEL="${LLM_MODEL:-mock}"
export CORRECTION_RULES_SEED_PATH="$BACKEND_DIR/data/correction_rules_seed.json"
export ACCESS_TOKEN_EXPIRE_MINUTES=1440
export SECRET_KEY="loadtest-secret"

bg() {  # name, cmd...
  local name="$1"; shift
  nohup "$@" >"$STATE_DIR/logs/$name.log" 2>&1 &
  echo $! >"$STATE_DIR/pids/$name.pid"
}

start() {
  if [ ! -f "$PG_DATA/PG_VERSION" ]; then
    "${PG_AS[@]}" "$PG_BIN/initdb" -D "$PG_DATA" -U meetings --auth=trust -E UTF8 --locale=C.UTF-8 >/dev/null
  fi
  "${PG_AS[@]}" "$PG_BIN/pg_ctl" -D "$PG_DATA" -l "$PG_LOG" \
    -o "-p $PG_PORT -c max_connections=300 -k /tmp" start >/dev/null
  until "$PG_BIN/pg_isready" -h 127.0.0.1 -p "$PG_PORT" >/dev/null 2>&1; do sleep 0.5; done
  psql -h 127.0.0.1 -p "$PG_PORT" -U meetings -d postgres -tc \
    "SELECT 1 FROM pg_database WHERE datname='meetings'" | grep -q 1 \
    || psql -h 127.0.0.1 -p "$PG_PORT" -U meetings -d postgres -c "CREATE DATABASE meetings" >/dev/null

  bg redis redis-server --port "$REDIS_PORT" --save "" --appendonly no

  (cd "$HERE" && bg mock-asr "$PYTHON" -m uvicorn mock_asr_server:app --port "$ASR_PORT" --log-level warning)
  (cd "$HERE" && bg mock-llm "$PYTHON" -m uvicorn mock_llm_server:app --port "$LLM_PORT" --log-level warning)

  # uvicorn 여러 프로세스가 동시에 create_all 을 실행하면 빈 DB 에서 충돌하므로 스키마를 먼저 1회 생성
  (cd "$BACKEND_DIR" && "$PYTHON" -c "import asyncio; from app.main import on_startup; asyncio.run(on_startup())")
  (cd "$BACKEND_DIR" && bg api "$PYTHON" -m uvicorn app.main:app --port "$API_PORT" \
      --workers "$API_WORKERS" --log-level warning)
  for spec in $WORKERS; do
    q="${spec%%:*}"; c="${spec##*:}"
    (cd "$BACKEND_DIR" && bg "worker-$q" "$PYTHON" -m celery -A app.workers.celery_app worker \
        --loglevel=info --concurrency="$c" --queues="$q" -n "$q@%h")
  done

  for i in $(seq 1 60); do
    curl -sf "http://127.0.0.1:${API_PORT}/api/health" >/dev/null && break; sleep 1
  done
  echo "stack up: api=:${API_PORT} asr=:${ASR_PORT} llm=:${LLM_PORT} (logs: $STATE_DIR/logs)"
}

stop() {
  for f in "$STATE_DIR"/pids/*.pid; do
    [ -f "$f" ] || continue
    kill "$(cat "$f")" 2>/dev/null || true
    rm -f "$f"
  done
  sleep 2
  pkill -f "celery -A app.workers.celery_app" 2>/dev/null || true
  "${PG_AS[@]}" "$PG_BIN/pg_ctl" -D "$PG_DATA" stop -m fast >/dev/null 2>&1 || true
  echo "stack stopped"
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  *) echo "usage: $0 start|stop"; exit 1 ;;
esac
