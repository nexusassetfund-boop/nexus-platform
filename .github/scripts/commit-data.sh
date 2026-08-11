#!/usr/bin/env bash
# docs/data 산출물을 커밋·푸시. 다른 워크플로(scan/value-screen/ipo/update-*)가
# 같은 docs/data에 동시 커밋하면 push가 non-fast-forward로 거부되는데(빨간 실패),
# 데이터 자체는 이미 생성됐으므로 rebase 후 재시도로 조용히 흡수한다.
#
# 사용법: bash .github/scripts/commit-data.sh "<commit message>" [path ...]
#   경로 미지정 시 docs/data 전체. 변경 없으면 커밋 생략(성공 처리).
#   충돌 시 -X theirs 로 "이번 실행이 방금 생성한 파일"을 채택(스캔·시세는 멱등).
set -euo pipefail

MSG="${1:?commit message required}"
shift || true
PATHS=("$@")
[ ${#PATHS[@]} -eq 0 ] && PATHS=("docs/data")

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add "${PATHS[@]}"

if git diff --cached --quiet; then
  echo "변경 없음 — 커밋 생략"
  exit 0
fi
git commit -m "$MSG"

# 8회 재시도 + 증가 백오프(3,6,9,… + 지터). 마감 close-run은 원장 이벤트가 하루 1회만
# 증분돼 push 유실 시 복구 경로가 없으므로, 지속 경합에도 살아남도록 재시도를 넉넉히 둔다.
ATTEMPTS=8
for i in $(seq 1 $ATTEMPTS); do
  # --autostash: 커밋 대상 밖의 추적 파일이 수정돼 있으면(예: 주석 캐시) rebase가
  # "cannot pull with rebase: You have unstaged changes"로 8회 전부 실패한다.
  # 경합이 아니라 더러운 작업트리가 원인이므로 재시도로는 절대 풀리지 않는다.
  if git pull --rebase --autostash -X theirs origin "${GITHUB_REF_NAME}" && git push; then
    echo "push 성공 (시도 $i)"
    exit 0
  fi
  git rebase --abort 2>/dev/null || true   # 재시도 전 rebase 상태 정리(커밋은 보존됨)
  echo "push 경합 — 재시도 $i/$ATTEMPTS"
  sleep $(( i * 3 + RANDOM % 3 ))
done

echo "push $ATTEMPTS회 재시도 실패" >&2
exit 1
