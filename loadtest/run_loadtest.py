"""회의록 서비스 End-to-End 부하 테스트.

동시 건수(기본 5,10,30,50,100) x 음성 길이(기본 30,60,120분) 시나리오를 차례로 실행한다.
각 시나리오는 N건을 동시에 업로드한 뒤 /status 를 폴링해 단계 전이 시각을 기록하고,
모든 건이 done/failed 가 되거나 제한 시간이 지나면 다음 시나리오로 넘어간다.

  python run_loadtest.py --base-url https://10.0.0.5:8443 --insecure \\
      --username loadtest --password '***' \\
      --concurrency 5,10,30 --durations 30,60 --audio-dir ./audio --out-dir ./results

결과
  results/<run>/scenario_c{N}_d{M}.json   건별 단계 타임라인 + 요약
  results/<run>/summary.csv               시나리오별 요약 1행씩
  results/<run>/report.md                 사람이 읽는 표
"""

import argparse
import asyncio
import csv
import json
import os
import statistics
import time
from datetime import datetime

import httpx

from gen_audio import generate

STAGES = ["queued", "audio_processing", "asr_processing", "stt_postprocessing",
          "diarizing", "llm_processing"]
TERMINAL = {"done", "failed"}


class Client:
    def __init__(self, base_url: str, username: str, password: str, verify: bool):
        self.base_url, self.username, self.password = base_url.rstrip("/"), username, password
        limits = httpx.Limits(max_connections=200, max_keepalive_connections=50)
        self.http = httpx.AsyncClient(
            base_url=self.base_url, verify=verify, limits=limits,
            timeout=httpx.Timeout(60, read=1800, write=1800),
        )
        self.token = None
        self._login_lock = asyncio.Lock()

    async def login(self):
        async with self._login_lock:
            r = await self.http.post("/api/auth/login",
                                     data={"username": self.username, "password": self.password})
            r.raise_for_status()
            self.token = r.json()["access_token"]

    async def request(self, method: str, url: str, **kw) -> httpx.Response:
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self.token}"}
            r = await self.http.request(method, url, headers=headers, **kw)
            if r.status_code == 401 and attempt == 0:  # 장시간 테스트 중 토큰 만료
                await self.login()
                continue
            return r
        return r

    async def close(self):
        await self.http.aclose()


async def sample_health(http: httpx.AsyncClient, url: str | None) -> dict | None:
    if not url:
        return None
    try:
        r = await http.get(url, timeout=5)
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


async def run_scenario(cli: Client, n: int, minutes: int, audio_path: str, args) -> dict:
    label = f"c{n}_d{minutes}"
    print(f"\n=== 시나리오 {label}: 동시 {n}건 x {minutes}분 ({os.path.basename(audio_path)}) ===")
    jobs = [{"idx": i, "timeline": {}, "status": None} for i in range(n)]
    t0 = time.time()

    async def upload(job):
        start = time.time()
        with open(audio_path, "rb") as f:
            files = {"file": (os.path.basename(audio_path), f, "application/octet-stream")}
            data = {"title": f"loadtest {label} #{job['idx']:03d}", "language": "ko",
                    "attendees": "홍길동, 김철수, 이영희", "agenda": "부하 테스트"}
            try:
                r = await cli.request("POST", "/api/meetings", files=files, data=data)
            except Exception as exc:
                job.update(status="upload_failed", error=str(exc))
                return
        job["upload_seconds"] = round(time.time() - start, 3)
        if r.status_code != 201:
            job.update(status="upload_failed", error=f"HTTP {r.status_code}: {r.text[:200]}")
            return
        body = r.json()
        job["meeting_id"] = body["id"]
        job["status"] = body["status"]
        job["timeline"][body["status"]] = time.time()

    await asyncio.gather(*(upload(j) for j in jobs))
    uploaded = [j for j in jobs if j.get("meeting_id")]
    print(f"  업로드 완료 {len(uploaded)}/{n} ({time.time() - t0:.1f}s)")

    health_samples = []
    deadline = t0 + args.timeout_minutes * 60
    last_print = 0.0
    while time.time() < deadline:
        pending = [j for j in uploaded if j["status"] not in TERMINAL]
        if not pending:
            break

        async def poll(job):
            try:
                r = await cli.request("GET", f"/api/meetings/{job['meeting_id']}/status")
                if r.status_code == 200:
                    st = r.json()
                    if st["status"] != job["status"]:
                        job["status"] = st["status"]
                        job["timeline"].setdefault(st["status"], time.time())
                    if st.get("error_message"):
                        job["error"] = st["error_message"]
            except Exception as exc:
                job.setdefault("poll_errors", 0)
                job["poll_errors"] += 1
                job["last_poll_error"] = str(exc)

        sem = asyncio.Semaphore(32)

        async def guarded(job):
            async with sem:
                await poll(job)

        await asyncio.gather(*(guarded(j) for j in pending))
        now = time.time()
        if args.asr_health_url or args.llm_health_url:
            health_samples.append({
                "t": round(now - t0, 1),
                "asr": await sample_health(cli.http, args.asr_health_url),
                "llm": await sample_health(cli.http, args.llm_health_url),
            })
        if now - last_print >= args.print_interval:
            counts: dict[str, int] = {}
            for j in uploaded:
                counts[j["status"]] = counts.get(j["status"], 0) + 1
            print(f"  [{now - t0:7.0f}s] " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
            last_print = now
        await asyncio.sleep(args.poll_interval)

    summary = summarize(jobs, n, minutes, t0)
    result = {"scenario": label, "concurrency": n, "audio_minutes": minutes,
              "audio_file": audio_path, "started_at": datetime.fromtimestamp(t0).isoformat(),
              "summary": summary, "jobs": jobs, "health_samples": health_samples}

    if args.cleanup:
        await asyncio.gather(*(cli.request("DELETE", f"/api/meetings/{j['meeting_id']}")
                               for j in uploaded if j["status"] in TERMINAL))
    return result


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return round(values[lo] + (values[hi] - values[lo]) * (k - lo), 1)


def summarize(jobs: list[dict], n: int, minutes: int, t0: float) -> dict:
    done = [j for j in jobs if j["status"] == "done"]
    failed = [j for j in jobs if j["status"] in ("failed", "upload_failed")]
    unfinished = [j for j in jobs if j["status"] not in TERMINAL | {"upload_failed"}]

    e2e = [j["timeline"]["done"] - t0 for j in done]
    stage_durations: dict[str, list[float]] = {}
    for j in done:
        tl = j["timeline"]
        seen = [(s, tl[s]) for s in STAGES + ["done"] if s in tl]
        for (s, ts), (_, nxt) in zip(seen, seen[1:]):
            stage_durations.setdefault(s, []).append(nxt - ts)
    first_done = min(e2e) if e2e else None
    makespan = max(e2e) if e2e else None
    audio_hours = len(done) * minutes / 60
    return {
        "submitted": n,
        "done": len(done),
        "failed": len(failed),
        "unfinished": len(unfinished),
        "errors": sorted({str(j.get("error"))[:120] for j in failed})[:5],
        "upload_p50_s": _pct([j["upload_seconds"] for j in jobs if "upload_seconds" in j], 0.5),
        "upload_max_s": _pct([j["upload_seconds"] for j in jobs if "upload_seconds" in j], 1.0),
        "first_done_s": round(first_done, 1) if first_done else None,
        "e2e_p50_s": _pct(e2e, 0.5),
        "e2e_p95_s": _pct(e2e, 0.95),
        "makespan_s": round(makespan, 1) if makespan else None,
        "throughput_audio_h_per_h": round(audio_hours / (makespan / 3600), 2) if makespan else None,
        "stage_avg_s": {s: round(statistics.mean(v), 1) for s, v in stage_durations.items()},
        "stage_p95_s": {s: _pct(v, 0.95) for s, v in stage_durations.items()},
    }


def write_reports(out_dir: str, results: list[dict], time_scale: float = 1.0):
    rows = []
    for r in results:
        s = r["summary"]
        row = {"scenario": r["scenario"], "concurrency": r["concurrency"],
               "audio_minutes": r["audio_minutes"]}
        row.update({k: v for k, v in s.items() if not isinstance(v, (dict, list))})
        for stage in STAGES:
            row[f"avg_{stage}_s"] = s["stage_avg_s"].get(stage)
        rows.append(row)
    if rows:
        with open(os.path.join(out_dir, "summary.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    def fmt(sec):
        if sec is None:
            return "-"
        sec = sec / time_scale
        return f"{sec / 60:.1f}m" if sec >= 60 else f"{sec:.0f}s"

    lines = ["# 부하 테스트 결과", "",
             "| 시나리오 | 동시 | 음성 | 완료/실패/미완 | 첫 완료 | E2E p50 | E2E p95 | 전체 소요 | 처리량(음성h/h) |"
             " 대기(queued) | 변환 | ASR | 후처리 | 화자분리 | 회의록 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        s, a = r["summary"], r["summary"]["stage_avg_s"]
        lines.append(
            f"| {r['scenario']} | {r['concurrency']} | {r['audio_minutes']}분 | "
            f"{s['done']}/{s['failed']}/{s['unfinished']} | {fmt(s['first_done_s'])} | "
            f"{fmt(s['e2e_p50_s'])} | {fmt(s['e2e_p95_s'])} | {fmt(s['makespan_s'])} | "
            f"{round(s['throughput_audio_h_per_h'] * time_scale, 2) if s['throughput_audio_h_per_h'] else '-'} | "
            + " | ".join(fmt(a.get(st)) for st in STAGES) + " |"
        )
    if time_scale != 1.0:
        lines += ["", f"> 목 서버 MOCK_TIME_SCALE={time_scale} 로 압축 실행한 결과를 실제 시간으로 환산(÷{time_scale})한 값. "
                  "summary.csv / scenario JSON 은 환산 전 실측값."]
    lines += ["", "단계 시간은 /status 폴링으로 관측한 상태 전이 간격의 평균이다. "
              "asr_processing/diarizing 에는 GPU 대기 시간이 포함되며, "
              "queued 는 업로드 완료 후 변환 워커가 잡기까지의 대기다."]
    errs = [(r["scenario"], e) for r in results for e in r["summary"]["errors"]]
    if errs:
        lines += ["", "## 실패 사유(시나리오별 최대 5개)"] + [f"- {s}: {e}" for s, e in errs]
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


async def main_async(args):
    concurrency = [int(x) for x in args.concurrency.split(",") if x.strip()]
    durations = [int(x) for x in args.durations.split(",") if x.strip()]
    run_dir = os.path.join(args.out_dir, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "config.json"), "w") as f:
        json.dump(vars(args), f, ensure_ascii=False, indent=2)

    audio = {m: generate(m, args.audio_dir, args.format, args.source) for m in durations}
    cli = Client(args.base_url, args.username, args.password, verify=not args.insecure)
    await cli.login()
    results = []
    try:
        for m in durations:
            for n in concurrency:
                res = await run_scenario(cli, n, m, audio[m], args)
                results.append(res)
                with open(os.path.join(run_dir, f"scenario_{res['scenario']}.json"), "w",
                          encoding="utf-8") as f:
                    json.dump(res, f, ensure_ascii=False, indent=2)
                write_reports(run_dir, results, args.time_scale)
                print(json.dumps(res["summary"], ensure_ascii=False))
                if args.cooldown:
                    await asyncio.sleep(args.cooldown)
    finally:
        await cli.close()
    print(f"\n결과: {run_dir}/report.md")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--username", default=os.getenv("LOADTEST_USERNAME", "admin"))
    ap.add_argument("--password", default=os.getenv("LOADTEST_PASSWORD", "change-me"))
    ap.add_argument("--insecure", action="store_true", help="자체 서명 인증서 검증 생략")
    ap.add_argument("--concurrency", default="5,10,30,50,100")
    ap.add_argument("--durations", default="30,60,120", help="음성 길이(분)")
    ap.add_argument("--audio-dir", default="./audio")
    ap.add_argument("--format", default="mp3", choices=["mp3", "flac", "wav", "m4a"])
    ap.add_argument("--source", default=None, help="실제 회의 음성(반복해서 원하는 길이로 생성)")
    ap.add_argument("--out-dir", default="./results")
    ap.add_argument("--poll-interval", type=float, default=5.0)
    ap.add_argument("--print-interval", type=float, default=30.0)
    ap.add_argument("--timeout-minutes", type=float, default=24 * 60, help="시나리오당 최대 대기")
    ap.add_argument("--cooldown", type=float, default=10.0, help="시나리오 사이 대기(초)")
    ap.add_argument("--asr-health-url", default=None, help="예: http://gpu-host:9000/health (GPU 대기열 샘플링)")
    ap.add_argument("--llm-health-url", default=None)
    ap.add_argument("--time-scale", type=float, default=1.0,
                    help="목 서버 MOCK_TIME_SCALE 과 같은 값을 주면 report.md 를 실제 시간으로 환산")
    ap.add_argument("--cleanup", action="store_true", help="시나리오 종료 후 테스트 회의 삭제")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
