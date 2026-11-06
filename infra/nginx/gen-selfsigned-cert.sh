#!/usr/bin/env bash
# 사내망(사설 IP)용 자체 서명 HTTPS 인증서 생성 스크립트
#
# 사용법:
#   ./gen-selfsigned-cert.sh <서버 IP 또는 도메인> [추가 IP/도메인 ...]
#
# 예시:
#   ./gen-selfsigned-cert.sh 192.168.0.10
#   ./gen-selfsigned-cert.sh meetings.local 192.168.0.10
#
# 최신 브라우저는 인증서의 CN만으로는 신뢰하지 않고 SAN(subjectAltName)을
# 요구하므로, 인자로 받은 IP/도메인을 모두 SAN에 넣어 생성합니다.
# 생성된 certs/*.key, *.crt 는 git에 커밋하지 마세요 (.gitignore 처리됨).

set -euo pipefail
cd "$(dirname "$0")"

if [ "$#" -lt 1 ]; then
  echo "사용법: $0 <서버 IP 또는 도메인> [추가 IP/도메인 ...]" >&2
  echo "예시:   $0 192.168.0.10" >&2
  exit 1
fi

mkdir -p certs

SAN_ENTRIES="DNS:localhost,IP:127.0.0.1"
for host in "$@"; do
  if [[ "$host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    SAN_ENTRIES="$SAN_ENTRIES,IP:$host"
  else
    SAN_ENTRIES="$SAN_ENTRIES,DNS:$host"
  fi
done

openssl req -x509 -nodes -days 3650 \
  -newkey rsa:2048 \
  -keyout certs/selfsigned.key \
  -out certs/selfsigned.crt \
  -subj "/CN=$1" \
  -addext "subjectAltName=$SAN_ENTRIES"

chmod 600 certs/selfsigned.key

echo ""
echo "생성 완료:"
echo "  infra/nginx/certs/selfsigned.crt"
echo "  infra/nginx/certs/selfsigned.key"
echo "  SAN: $SAN_ENTRIES"
echo ""
echo "적용하려면 nginx 컨테이너를 재시작하세요:"
echo "  docker compose restart nginx"
