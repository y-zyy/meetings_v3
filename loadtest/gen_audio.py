"""부하 테스트용 오디오 생성.

  python gen_audio.py --durations 30,60,120 --out-dir ./audio                 # 합성 신호
  python gen_audio.py --durations 30,60,120 --source speech.wav --format mp3  # 실제 음성 반복

실제 WhisperX 로 테스트할 때는 반드시 --source 로 실제 회의 음성을 넣어야 한다
(합성 신호는 VAD 에서 대부분 걸러져 전사/LLM 부하가 비현실적으로 작아진다).
목 서버로 파이프라인만 검증할 때는 합성 신호(기본)로 충분하다.
"""

import argparse
import os
import subprocess


def generate(minutes: int, out_dir: str, fmt: str, source: str | None) -> str:
    os.makedirs(out_dir, exist_ok=True)
    tag = "speech" if source else "synthetic"
    path = os.path.join(out_dir, f"{tag}_{minutes}min.{fmt}")
    if os.path.exists(path):
        return path
    seconds = minutes * 60
    if source:
        inputs = ["-stream_loop", "-1", "-i", source]
    else:
        # 압축이 잘 되는 저엔트로피 신호(톤 + 약한 노이즈) — 100건 업로드 시 디스크 절약
        inputs = ["-f", "lavfi", "-i", f"sine=frequency=220:sample_rate=16000:duration={seconds}"]
    codec = {"mp3": ["-c:a", "libmp3lame", "-b:a", "64k"],
             "flac": ["-c:a", "flac"],
             "wav": ["-c:a", "pcm_s16le"],
             "m4a": ["-c:a", "aac", "-b:a", "64k"]}[fmt]
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", *inputs, "-t", str(seconds),
         "-ac", "1", "-ar", "16000", *codec, path],
        check=True,
    )
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--durations", default="30,60,120", help="분 단위, 쉼표 구분")
    ap.add_argument("--out-dir", default="./audio")
    ap.add_argument("--format", default="mp3", choices=["mp3", "flac", "wav", "m4a"])
    ap.add_argument("--source", default=None, help="반복 재생할 실제 음성 파일")
    args = ap.parse_args()
    for m in [int(x) for x in args.durations.split(",") if x.strip()]:
        p = generate(m, args.out_dir, args.format, args.source)
        print(f"{m:>4}분 → {p} ({os.path.getsize(p) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
