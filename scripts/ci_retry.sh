#!/usr/bin/env bash
# ACL-1710: ограниченный по времени запуск с повторами для шагов CI, которые ходят в сеть
# (apt-get update у Playwright иногда висит ~35 минут, пока GitHub не отменит job).
#
# Использование: ci_retry.sh ПОПЫТОК СЕКУНД_НА_ПОПЫТКУ ПАУЗА_СЕК -- команда аргументы...
# Перед каждым повтором выполняется CI_RETRY_CLEANUP (если задана): снять замки apt,
# убрать недокачанные списки. Если все попытки провалились, выходим с 1 и внятным текстом.
set -u
# нужен именно coreutils timeout (на Windows в PATH бывает системный timeout.exe)
if ! command -v timeout >/dev/null 2>&1 || ! timeout --version 2>&1 | grep -qi coreutils; then
  echo "::error::ci_retry: coreutils 'timeout' not found; refusing to run unbounded"
  exit 2
fi
attempts=$1; per_try=$2; pause=$3; shift 3
[ "${1:-}" = "--" ] && shift

for n in $(seq 1 "$attempts"); do
  echo "::group::attempt $n of $attempts (limit ${per_try}s): $*"
  start=$(date +%s)
  # -k: если команда не реагирует на TERM, через 10 с добиваем KILL
  timeout -k 10 "$per_try" "$@"
  code=$?
  echo "::endgroup::"
  took=$(( $(date +%s) - start ))
  if [ "$code" -eq 0 ]; then
    echo "ci_retry: ok on attempt $n after ${took}s"
    exit 0
  fi
  if [ "$code" -eq 124 ] || [ "$code" -eq 137 ]; then
    echo "::warning::ci_retry: attempt $n hit the ${per_try}s limit (hung), exit $code"
  else
    echo "::warning::ci_retry: attempt $n failed with exit $code after ${took}s"
  fi
  if [ "$n" -lt "$attempts" ]; then
    if [ -n "${CI_RETRY_CLEANUP:-}" ]; then
      echo "ci_retry: cleanup before retry"
      timeout 60 bash -c "$CI_RETRY_CLEANUP" || true
    fi
    sleep "$pause"
  fi
done
echo "::error::ci_retry: all $attempts attempts failed for: $*"
exit 1
