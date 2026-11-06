# meetings_v2

## HTTPS 설정 (사내망, 자체 서명 인증서)

마이크 녹음(`getUserMedia`)은 브라우저 보안 컨텍스트(HTTPS 또는 `localhost`)에서만
동작합니다. 사설 IP로 접속하는 사내망 환경에서는 아래 절차로 자체 서명 인증서를
발급해 nginx에 HTTPS를 붙이세요.

### 1. 인증서 발급

```bash
cd infra/nginx
./gen-selfsigned-cert.sh <서버 IP 또는 도메인>
# 예: ./gen-selfsigned-cert.sh 192.168.0.10
```

여러 IP/도메인으로 접속한다면 인자를 이어서 추가하면 됩니다.

```bash
./gen-selfsigned-cert.sh meetings.local 192.168.0.10
```

`infra/nginx/certs/selfsigned.{crt,key}` 파일이 생성됩니다. 이 파일들은
`.gitignore`에 등록되어 있어 커밋되지 않습니다.

### 2. nginx 적용

```bash
cd infra
docker compose up -d --build
# 이미 떠 있다면
docker compose restart nginx
```

### 3. 접속

- `https://<서버 IP>:8443` 으로 접속하세요.
- `http://<서버 IP>:8080` 으로 접속하면 자동으로 `https://<서버 IP>:8443` 으로 리다이렉트됩니다.
- 자체 서명 인증서이므로 최초 접속 시 브라우저가 "안전하지 않음" 경고를 띄웁니다.
  Chrome 기준 "고급" → "**(주소)**(안전하지 않음)로 이동" 을 클릭하면 정상적으로
  접속되고, 이후 마이크 녹음 기능도 정상 동작합니다.
- 경고 자체를 없애려면 `infra/nginx/certs/selfsigned.crt` 를 사내 PC들의 신뢰된
  루트 인증기관 저장소에 배포하면 됩니다.

### 실제 도메인이 있는 경우

외부에서 접근 가능한 실제 도메인이 있다면 자체 서명 인증서 대신 Let's Encrypt
(certbot) 등 정식 인증서 발급으로 교체하는 것을 권장합니다.

## 화자 분리 (WhisperX + pyannote)

`whisperx_fastapi/` 는 별도 GPU 도커로 구동하는 ASR 서버입니다.

```bash
cd whisperx_fastapi
docker build -t whisperx-asr .
docker run --gpus all -p 9000:9000 -e HF_TOKEN=<HuggingFace 토큰> whisperx-asr
```

- 처리 순서: **ASR → STT 후처리(규칙/LLM 교정) → 정렬·화자 분리 → 회의록 생성**
  - `POST /transcribe`: 전사만 수행. 응답 `{"text", "language", "segments": [{"start", "end", "text"}]}`
  - `POST /align_diarize`: 오디오 + 후처리된 `segments`(JSON, form 필드)를 받아 후처리 텍스트를 강제 정렬(align)하고
    화자를 부여. 응답 `{"language", "diarized", "segments": [{"start", "end", "speaker", "text"}]}`
    (form: `language`, `diarize`, `min_speakers`, `max_speakers`)
  - `HF_TOKEN` 이 없으면 화자 분리는 건너뛰고 `diarized=false`, `speaker` 없이 시간 구간만 반환합니다.
  - 백엔드는 `ASR_API_URL` 의 `/transcribe` 를 `/align_diarize` 로 치환해 호출합니다
    (`ASR_DIARIZE_API_URL` 로 지정 가능, `ASR_DIARIZE_ENABLED=false` 로 끌 수 있음).
  - 화자 분리 단계가 실패하면 화자 정보 없이 후처리된 텍스트로 회의록 생성을 계속합니다.
- 메인 백엔드는 `segments` 를 `meetings.segments` (JSON)에 저장하고, 화면의 "발화 기록"에
  시간 구간 / 화자 / 발화 내용을 표시합니다. 화자 이름 클릭 시 표시 이름을 변경할 수 있습니다(`speaker_names`).
- 기존 DB는 백엔드 기동 시 `segments`, `speaker_names` 컬럼이 자동 추가됩니다
  (alembic `005_diarization` 과 동일).
- OpenAI Whisper 사용 시에는 화자 분리 없이 기존처럼 텍스트만 표시됩니다.
