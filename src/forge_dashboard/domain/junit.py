"""JUnit XML reports (pytest, bun, cargo-nextest, ...) parsed into counts and failing tests.

Reports come from project commands, so they are untrusted input: parsing goes
through `defusedxml`, which refuses entity-expansion attacks.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from xml.etree.ElementTree import Element

from defusedxml import ElementTree
from pydantic import BaseModel, Field

from forge_dashboard.core.ui import ui

MAX_DETAIL_CHARS = 4000


class FailedCase(BaseModel):
    suite: str
    name: str
    kind: str = Field(json_schema_extra=ui(format="badge", tones={"failure": "bad", "error": "bad"}))
    message: str
    detail: str = Field(json_schema_extra=ui(format="code"))


class JUnitReport(BaseModel):
    tests: int
    failures: int
    errors: int
    skipped: int
    duration_s: float | None = Field(None, json_schema_extra=ui(format="duration"))
    failed: list[FailedCase] = Field(default_factory=list, json_schema_extra=ui(columns=["suite", "name", "message"]))

    @property
    def passed(self) -> int:
        return self.tests - self.failures - self.errors - self.skipped


class JUnitError(ValueError):
    """The report is missing or not JUnit XML."""


def _cases(element: Element, suite: str) -> Iterator[tuple[str, Element]]:
    for child in element:
        if child.tag == "testcase":
            yield suite, child
        elif child.tag in ("testsuite", "testsuites"):
            yield from _cases(child, child.get("name") or suite)


def parse_junit(text: str) -> JUnitReport:
    """Count test cases directly (suite-level totals disagree between tools) and collect failures.

    Raises:
        JUnitError: when the text is not XML with a `testsuites` or `testsuite` root.
    """
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as error:
        raise JUnitError(f"not valid XML: {error}") from error
    if root.tag not in ("testsuites", "testsuite"):
        raise JUnitError(f"unexpected root element <{root.tag}>")
    tests = failures = errors = skipped = 0
    failed: list[FailedCase] = []
    durations: list[float] = []
    for suite, case in _cases(root, root.get("name") or ""):
        tests += 1
        with_time = case.get("time")
        if with_time:
            durations.append(float(with_time))
        outcome = next((child for child in case if child.tag in ("failure", "error", "skipped")), None)
        if outcome is None:
            continue
        if outcome.tag == "skipped":
            skipped += 1
            continue
        if outcome.tag == "failure":
            failures += 1
        else:
            errors += 1
        failed.append(
            FailedCase(
                suite=case.get("classname") or suite,
                name=case.get("name") or "?",
                kind=outcome.tag,
                message=(outcome.get("message") or "").strip()[:500],
                detail=(outcome.text or "").strip()[:MAX_DETAIL_CHARS],
            )
        )
    return JUnitReport(
        tests=tests,
        failures=failures,
        errors=errors,
        skipped=skipped,
        duration_s=_duration(root, durations),
        failed=failed,
    )


def _duration(root: Element, case_durations: list[float]) -> float | None:
    """The report's own total when it has one, else its top-level suites' totals, else the cases' sum."""
    root_time = root.get("time")
    if root_time:
        return float(root_time)
    suite_times = [child.get("time") for child in root if child.tag == "testsuite"]
    if suite_times and all(suite_times):
        return sum(float(time) for time in suite_times if time)
    return sum(case_durations) if case_durations else None


def read_junit(path: Path) -> JUnitReport:
    """Parse a report file.

    Raises:
        JUnitError: when the file is missing or not JUnit XML.
    """
    if not path.is_file():
        raise JUnitError(f"the check wrote no report at {path}")
    return parse_junit(path.read_text(encoding="utf-8", errors="replace"))
