#!/usr/bin/env bash
# Run the Python and TypeScript test suites under coverage, then require
# >=90% coverage on the lines this branch changed relative to its merge base
# with the comparison branch, across both languages at once: Cobertura from
# coverage.py and LCOV from `bun test`. Used by the pre-push hook and by CI so
# both enforce the same threshold. Extends tools/coverage_gate.sh (kept
# verbatim from forge) to a two-language stack.
set -euo pipefail

COMPARE_BRANCH="${DIFF_COVER_COMPARE_BRANCH:-origin/main}"
FAIL_UNDER="${DIFF_COVER_FAIL_UNDER:-90}"
LCOV_REPORT="web/coverage/lcov.info"

# Tests marked "live" self-skip when their dependency is unreachable, so no
# marker filtering is needed here.
uv run coverage run -m pytest
uv run coverage xml -o coverage.xml

rm -rf web/coverage
(cd web && bun test --coverage --coverage-reporter=lcov --coverage-dir=coverage)
# Bun writes source paths relative to web/ with the platform separator;
# diff-cover matches them against repo-relative, forward-slash git paths.
sed -i -e '/^SF:/ s#\\#/#g' -e 's#^SF:#SF:web/#' "${LCOV_REPORT}"

# diff-cover needs the comparison branch present locally. In a fresh clone/CI
# checkout it may be missing; fetch it best-effort before comparing.
if ! git rev-parse --verify -q "${COMPARE_BRANCH}" >/dev/null; then
  git fetch --quiet origin "${COMPARE_BRANCH#origin/}" || true
fi

uv run diff-cover coverage.xml "${LCOV_REPORT}" \
  --compare-branch="${COMPARE_BRANCH}" \
  --fail-under="${FAIL_UNDER}"
