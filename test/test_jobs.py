"""Jobs: resource limits, logs, outcomes, cancellation, history, restart recovery."""

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from forge_dashboard.core.events import EventEnvelope
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.core.service import ActionError
from forge_dashboard.services.jobs import (
    JobContext,
    JobIdInput,
    JobListInput,
    JobLogInput,
    JobOutcome,
    JobsService,
    JobStatus,
)


def _jobs(runtime: Runtime) -> JobsService:
    service = runtime.services["jobs"]
    assert isinstance(service, JobsService)
    return service


async def test_jobs_sharing_a_resource_run_one_at_a_time(started_runtime: Runtime) -> None:
    jobs = _jobs(started_runtime)
    active = 0
    peak = 0

    async def work(context: JobContext) -> JobOutcome:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return JobOutcome(summary="done")

    refs = [await jobs.submit(kind="t", title=f"job {i}", work=work, resources=["agent"]) for i in range(3)]
    finals = [await jobs.wait(ref.job_id) for ref in refs]
    assert peak == 1
    assert {final.status for final in finals} == {JobStatus.SUCCEEDED}


async def test_queued_jobs_report_the_resource_they_wait_for(started_runtime: Runtime) -> None:
    jobs = _jobs(started_runtime)
    release = asyncio.Event()

    async def blocker(_context: JobContext) -> JobOutcome:
        await release.wait()
        return JobOutcome()

    first = await jobs.submit(kind="t", title="first", work=blocker, resources=["repo:demo"])
    second = await jobs.submit(kind="t", title="second", work=blocker, resources=["repo:demo"])
    await asyncio.sleep(0.05)
    waiting = {summary.id: summary for summary in jobs.state.jobs}
    assert waiting[second.job_id].status is JobStatus.QUEUED
    assert waiting[second.job_id].waiting_for == "repo:demo"
    assert jobs.state.running == 1 and jobs.state.queued == 1
    release.set()
    await jobs.wait(first.job_id)
    await jobs.wait(second.job_id)


async def test_job_logs_stream_as_events_and_persist(started_runtime: Runtime, tmp_path: Path) -> None:
    jobs = _jobs(started_runtime)

    async def work(context: JobContext) -> JobOutcome:
        context.log("starting")
        result = await context.run([sys.executable, "-c", "print('from child')"], tmp_path)
        return JobOutcome(summary=f"exit {result.exit_code}")

    async with started_runtime.bus.subscribe() as events:
        ref = await jobs.submit(kind="t", title="logged", work=work, project_id="demo")
        final = await jobs.wait(ref.job_id)
        received: list[EventEnvelope] = []
        while not events.empty():
            received.append(events.get_nowait())
    assert final.summary == "exit 0"
    log = await jobs.log(JobLogInput(job_id=ref.job_id))
    assert log.lines[0] == "starting" and "from child" in log.lines and log.done
    streamed = [line for envelope in received if envelope.type == "job.log" for line in envelope.data["lines"]]  # type: ignore[union-attr]
    assert streamed == log.lines
    finished = [envelope for envelope in received if envelope.type == "job.finished"]
    assert finished[0].persist and finished[0].project_id == "demo"


async def test_failures_and_attention_are_recorded(started_runtime: Runtime) -> None:
    jobs = _jobs(started_runtime)

    async def fails(_context: JobContext) -> JobOutcome:
        raise ActionError("could not do it")

    async def crashes(_context: JobContext) -> JobOutcome:
        raise ZeroDivisionError("bug")

    async def parks(_context: JobContext) -> JobOutcome:
        return JobOutcome(status=JobStatus.ATTENTION, summary="parked")

    failed = await jobs.wait((await jobs.submit(kind="t", title="f", work=fails)).job_id)
    crashed = await jobs.wait((await jobs.submit(kind="t", title="c", work=crashes)).job_id)
    parked = await jobs.wait((await jobs.submit(kind="t", title="p", work=parks)).job_id)
    assert (failed.status, failed.error) == (JobStatus.FAILED, "could not do it")
    assert crashed.status is JobStatus.FAILED and "ZeroDivisionError" in (crashed.error or "")
    assert (parked.status, parked.summary) == (JobStatus.ATTENTION, "parked")


async def test_cancel_stops_a_running_job_and_its_process(started_runtime: Runtime, tmp_path: Path) -> None:
    jobs = _jobs(started_runtime)
    started = asyncio.Event()

    async def work(context: JobContext) -> JobOutcome:
        started.set()
        await context.run([sys.executable, "-c", "import time; time.sleep(60)"], tmp_path)
        return JobOutcome()

    ref = await jobs.submit(kind="t", title="long", work=work)
    await started.wait()
    await asyncio.sleep(0.3)
    await jobs.cancel(JobIdInput(job_id=ref.job_id))
    final = await jobs.wait(ref.job_id)
    assert final.status is JobStatus.CANCELLED
    with pytest.raises(ActionError, match="not running"):
        await jobs.cancel(JobIdInput(job_id=ref.job_id))


async def test_history_filters_by_project_and_survives_restart(make_runtime: Callable[[], Runtime]) -> None:
    runtime = make_runtime()
    await runtime.start()
    jobs = _jobs(runtime)

    async def work(_context: JobContext) -> JobOutcome:
        return JobOutcome(summary="ok")

    await jobs.wait((await jobs.submit(kind="a", title="a", work=work, project_id="one")).job_id)
    await jobs.wait((await jobs.submit(kind="b", title="b", work=work, project_id="two")).job_id)
    await runtime.stop()

    restarted = make_runtime()
    await restarted.start()
    try:
        history = await _jobs(restarted).history(JobListInput(project_id="one"))
        assert [job.title for job in history.jobs] == ["a"]
        assert {job.title for job in _jobs(restarted).state.jobs} == {"a", "b"}
        detail = await _jobs(restarted).get(JobIdInput(job_id=history.jobs[0].id))
        assert detail.job.summary == "ok"
    finally:
        await restarted.stop()


async def test_jobs_left_running_by_a_crash_are_marked_interrupted(make_runtime: Callable[[], Runtime]) -> None:
    runtime = make_runtime()
    await runtime.open()
    await runtime.db.execute(
        "INSERT INTO jobs (id, kind, title, status, created_at, log_path) VALUES ('j1', 'k', 't', 'running', 0, 'x')"
    )
    await runtime.stop()
    restarted = make_runtime()
    await restarted.start()
    try:
        detail = await _jobs(restarted).get(JobIdInput(job_id="j1"))
        assert detail.job.status is JobStatus.FAILED
        assert detail.job.error == "interrupted by a dashboard restart"
    finally:
        await restarted.stop()
