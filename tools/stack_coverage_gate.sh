#!/usr/bin/env bash
# Run the Python and TypeScript test suites under coverage, then require
# >=90% coverage on the lines this branch changed relative to its merge base
# with the comparison branch, across both languages at once. Both suites
# report LCOV, because diff-cover cannot mix LCOV with Cobertura. Used by the
# pre-push hook and by CI so both enforce the same threshold. Extends
# tools/coverage_gate.sh (kept verbatim from forge) to a two-language stack.
set -euo pipefail

COMPARE_BRANCH="${DIFF_COVER_COMPARE_BRANCH:-origin/main}"
FAIL_UNDER="${DIFF_COVER_FAIL_UNDER:-90}"
PYTHON_REPORT="coverage.lcov"
WEB_REPORT="web/coverage/lcov.info"

# Live tests run only when explicitly enabled (they spend tokens or reach the
# network), so no marker filtering is needed here.
(
  # Git fixtures operate on their own repositories, outside the hook context.
  git_local_env=$(git rev-parse --local-env-vars)
  while IFS= read -r name; do
    unset "$name"
  done <<< "$git_local_env"
  uv run coverage run -m pytest
)
uv run coverage lcov -o "${PYTHON_REPORT}"

rm -rf web/coverage
(cd web && bun test --coverage --coverage-reporter=lcov --coverage-dir=coverage)

# Both tools write source paths with the platform separator, and Bun's are
# relative to web/; diff-cover matches repo-relative, forward-slash git paths.
sed -i -e '/^SF:/ s#\\#/#g' "${PYTHON_REPORT}"
sed -i -e '/^SF:/ s#\\#/#g' -e 's#^SF:#SF:web/#' "${WEB_REPORT}"

# diff-cover needs the comparison branch present locally. In a fresh clone/CI
# checkout it may be missing; fetch it best-effort before comparing.
if ! git rev-parse --verify -q "${COMPARE_BRANCH}" >/dev/null; then
  git fetch --quiet origin "${COMPARE_BRANCH#origin/}" || true
fi

uv run diff-cover "${PYTHON_REPORT}" "${WEB_REPORT}" \
  --compare-branch="${COMPARE_BRANCH}" \
  --fail-under="${FAIL_UNDER}"
