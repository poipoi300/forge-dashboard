import { describe, expect, test } from "bun:test";
import type { BoundFunctions, queries } from "@testing-library/dom";
import { act, screen, within } from "@testing-library/react";
import { useStream } from "@/api/stream";
import { lateMinutes } from "@/app/Departures";
import { usePreferences } from "@/app/preferences";
import { clockTime, useClock } from "@/lib/time";
import { makeEventsState } from "./activity-support";
import { makeScheduleEntry, makeSchedulerState } from "./app-support";
import { NOW_MS, NOW_SECONDS } from "./constants";
import { makeProject, makeProjectsState } from "./factories";
import {
  makeComplianceCheck,
  makeComplianceReport,
  makeFork,
  makeForkRun,
  makeProjectStandards,
  makeServiceStatus,
  makeStandardsState,
} from "./fleet-support";
import { installHarness, mockApi, renderApp, seedStream } from "./harness";
import { makeJob, makeJobsState } from "./jobs-support";

installHarness();

type Scope = BoundFunctions<typeof queries>;

function rail(): Scope {
  return within(screen.getByRole("navigation", { name: "Sections" }));
}

function railLink(label: string): HTMLElement {
  return rail().getByRole("link", { name: new RegExp(`^${label}`) });
}

describe("the navigation rail", () => {
  test("lists every section of the dashboard in order with its destination", () => {
    mockApi();
    renderApp("/nope");
    const links = rail().getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/",
      "/jobs",
      "/forks",
      "/standards",
      "/services",
      "/runs",
      "/activity",
      "/settings",
    ]);
    expect(links.map((link) => link.textContent)).toEqual([
      "Overview",
      "Jobs",
      "Forks",
      "Standards",
      "Services",
      "Forge runs",
      "Activity",
      "Settings",
    ]);
  });

  test("shows how many jobs are running or waiting", () => {
    mockApi();
    renderApp("/nope");
    seedStream({
      jobs: makeJobsState([
        makeJob("a", { status: "running" }),
        makeJob("b", { status: "queued" }),
      ]),
    });
    expect(within(railLink("Jobs")).getByText("2")).toBeTruthy();
  });

  test("counts forks parked in each state that needs a human and hides the count when none are", () => {
    mockApi();
    renderApp("/nope");
    seedStream({
      forks: {
        forks: [
          makeFork("a", { last_run: makeForkRun("r1", "a", { state: "conflict" }) }),
          makeFork("b", { last_run: makeForkRun("r2", "b", { state: "needs-attention" }) }),
          makeFork("c", { last_run: makeForkRun("r3", "c", { state: "ready-to-apply" }) }),
          makeFork("d", { last_run: makeForkRun("r4", "d", { state: "synced" }) }),
          makeFork("e", { last_run: null }),
        ],
      },
    });
    expect(within(railLink("Forks")).getByText("3")).toBeTruthy();
    seedStream({ forks: { forks: [makeFork("d", { last_run: null })] } });
    expect(railLink("Forks").textContent).toBe("Forks");
  });

  test("counts projects whose standards report is not fully passing", () => {
    mockApi();
    renderApp("/nope");
    const failing = makeComplianceReport([
      makeComplianceCheck("a", "pass"),
      makeComplianceCheck("b", "fail"),
    ]);
    const passing = makeComplianceReport([makeComplianceCheck("a", "pass")]);
    seedStream({
      standards: makeStandardsState([
        makeProjectStandards("drifting", { report: failing }),
        makeProjectStandards("clean", { report: passing }),
        makeProjectStandards("unevaluated", { report: null }),
      ]),
    });
    expect(within(railLink("Standards")).getByText("1")).toBeTruthy();
  });

  test("counts supervised services that are running or adopted from outside", () => {
    mockApi();
    renderApp("/nope");
    seedStream({
      services: {
        services: [
          makeServiceStatus("a", "web", { state: "running" }),
          makeServiceStatus("a", "db", { state: "external" }),
          makeServiceStatus("b", "web", { state: "stopped" }),
        ],
        deployments: [],
      },
    });
    expect(within(railLink("Services")).getByText("2")).toBeTruthy();
  });

  test("shows the unread notification count on its link to the activity feed", () => {
    mockApi();
    renderApp("/nope");
    seedStream({ events: makeEventsState({ unread: 4 }) });
    const notifications = screen.getByRole("link", { name: /Notifications/ });
    expect(notifications.getAttribute("href")).toBe("/activity?notifications=1");
    expect(within(notifications).getByText("4")).toBeTruthy();
  });

  test("marks the section being viewed as current", () => {
    mockApi();
    renderApp("/jobs/job-9");
    expect(railLink("Jobs").getAttribute("aria-current")).toBe("page");
    expect(railLink("Overview").getAttribute("aria-current")).toBeNull();
  });

  test("keeps Overview current while a project page is open", () => {
    mockApi();
    renderApp("/p/alpha");
    expect(railLink("Overview").getAttribute("aria-current")).toBe("page");
  });

  test("navigates when a section is chosen", async () => {
    mockApi();
    const { user, location } = renderApp("/nope");
    await user.click(railLink("Forks"));
    expect(location()).toBe("/forks");
  });
});

describe("the theme switch", () => {
  test("offers light, dark and follow-system and starts on following the system", () => {
    mockApi();
    renderApp("/nope");
    const group = screen.getByRole("group", { name: "Theme" });
    const choices = within(group).getAllByRole("radio");
    expect(choices.map((choice) => choice.getAttribute("value"))).toEqual([
      "light",
      "dark",
      "system",
    ]);
    expect(
      (within(group).getByRole("radio", { name: "Follow system" }) as HTMLInputElement).checked,
    ).toBe(true);
  });

  test("choosing a theme stores it as the viewer's preference", async () => {
    mockApi();
    const { user } = renderApp("/nope");
    await user.click(screen.getByRole("radio", { name: "Dark" }));
    expect(usePreferences.getState().theme).toBe("dark");
    expect((screen.getByRole("radio", { name: "Dark" }) as HTMLInputElement).checked).toBe(true);
    await user.click(screen.getByRole("radio", { name: "Light" }));
    expect(usePreferences.getState().theme).toBe("light");
  });
});

describe("the connection banner", () => {
  test("stays hidden while the stream is live", () => {
    mockApi();
    renderApp("/nope");
    expect(screen.queryByText(/dashboard API is unreachable/)).toBeNull();
  });

  test("tells the viewer the API is unreachable and that the numbers are stale when offline", () => {
    mockApi();
    renderApp("/nope");
    act(() => useStream.setState({ connection: "offline" }));
    const banner = screen.getByRole("status");
    expect(banner.textContent).toContain("The dashboard API is unreachable; retrying.");
    expect(banner.textContent).toContain("last state received");
  });
});

describe("the small-screen menu", () => {
  test("opens the sections in a sheet and closes it after navigating", async () => {
    mockApi();
    const { user, location } = renderApp("/nope");
    await user.click(screen.getByRole("button", { name: "Open sections" }));
    const sheet = await screen.findByRole("dialog");
    await user.click(within(sheet).getByRole("link", { name: /^Activity/ }));
    expect(location()).toBe("/activity");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("closes after following the notifications link too", async () => {
    mockApi();
    const { user, location } = renderApp("/nope");
    await user.click(screen.getByRole("button", { name: "Open sections" }));
    const sheet = await screen.findByRole("dialog");
    await user.click(within(sheet).getByRole("link", { name: /Notifications/ }));
    expect(location()).toBe("/activity?notifications=1");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("closes after the logo link is followed", async () => {
    mockApi();
    const { user, location } = renderApp("/nope");
    await user.click(screen.getByRole("button", { name: "Open sections" }));
    const sheet = await screen.findByRole("dialog");
    await user.click(within(sheet).getByRole("link", { name: /forge dashboard/ }));
    expect(location()).toBe("/");
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("a route that does not exist", () => {
  test("says there is no page and links back to the overview", async () => {
    mockApi();
    const { user, location } = renderApp("/nope");
    expect(screen.getByText("There is no page here")).toBeTruthy();
    await user.click(screen.getByRole("link", { name: "Go to the overview" }));
    expect(location()).toBe("/");
  });
});

describe("the departures board", () => {
  const totals = { projects: 5, failing: 2, attention: 3, running_services: 4 };

  function board(): Scope {
    return within(screen.getByRole("region", { name: "Departures" }));
  }

  test("shows dashes for every count until the first state arrives", () => {
    mockApi();
    renderApp("/");
    expect(board().getAllByText("–")).toHaveLength(4);
  });

  test("shows the fleet's headline counts, each linking to its filtered view", () => {
    mockApi();
    renderApp("/");
    seedStream({
      projects: makeProjectsState([makeProject("a")], { totals }),
    });
    const counter = (label: string) => board().getByRole("link", { name: new RegExp(label) });
    expect(counter("projects").getAttribute("href")).toBe("/");
    expect(counter("projects").textContent).toContain("5");
    expect(counter("failing").getAttribute("href")).toBe("/?health=failing");
    expect(counter("failing").textContent).toContain("2");
    expect(counter("services up").getAttribute("href")).toBe("/services");
    expect(counter("services up").textContent).toContain("4");
    expect(counter("need attention").getAttribute("href")).toBe("/?attention=1");
    expect(counter("need attention").textContent).toContain("3");
  });

  test("says in words that failing and attention counts need attention, so colour is not the only signal", () => {
    mockApi();
    renderApp("/");
    seedStream({ projects: makeProjectsState([], { totals }) });
    expect(board().getAllByText("(needs attention)")).toHaveLength(2);
    seedStream({
      projects: makeProjectsState([], {
        totals: { projects: 5, failing: 0, attention: 0, running_services: 4 },
      }),
    });
    expect(board().queryByText("(needs attention)")).toBeNull();
  });

  test("shows the wall-clock time and whether the stream is live, connecting or offline", () => {
    mockApi();
    renderApp("/");
    expect(board().getByText(clockTime(NOW_MS))).toBeTruthy();
    expect(board().getByText("live")).toBeTruthy();
    expect(board().getByRole("img", { name: "Live" })).toBeTruthy();
    act(() => useStream.setState({ connection: "connecting" }));
    expect(board().getByText("connecting…")).toBeTruthy();
    act(() => useStream.setState({ connection: "offline" }));
    expect(board().getByText("offline, retrying")).toBeTruthy();
    expect(board().getByRole("img", { name: "Disconnected" })).toBeTruthy();
  });

  test("moves the clock on when the shared tick advances", () => {
    mockApi();
    renderApp("/");
    act(() => useClock.setState({ now: NOW_MS + 5 * 60_000 }));
    expect(board().getByText(clockTime(NOW_MS + 5 * 60_000))).toBeTruthy();
  });

  test("lists up to three scheduled departures with their time, title and project", () => {
    mockApi();
    renderApp("/");
    seedStream({
      scheduler: makeSchedulerState([
        makeScheduleEntry("one", {
          title: "Sync alpha",
          project_id: "alpha",
          next_run_at: NOW_SECONDS + 600,
        }),
        makeScheduleEntry("two", { title: "Nightly checks", next_run_at: NOW_SECONDS + 7200 }),
        makeScheduleEntry("three", { title: "Manual only", next_run_at: null }),
        makeScheduleEntry("four", { title: "Beyond the board", next_run_at: NOW_SECONDS + 9000 }),
      ]),
    });
    const sync = board().getByRole("link", { name: /Sync alpha/ });
    expect(sync.textContent).toContain("in 10 min");
    expect(sync.textContent).toContain("alpha");
    expect(sync.getAttribute("href")).toBe("/p/alpha");
    const nightly = board().getByRole("link", { name: /Nightly checks/ });
    expect(nightly.textContent).toContain("in 2 h");
    expect(nightly.getAttribute("href")).toBe("/jobs");
    expect(board().getByRole("link", { name: /Manual only/ }).textContent).toContain("manual");
    expect(board().queryByText(/Beyond the board/)).toBeNull();
  });

  test("says nothing is scheduled when the queue is empty", () => {
    mockApi();
    renderApp("/");
    seedStream({ scheduler: makeSchedulerState([]) });
    expect(board().getByText(/Nothing scheduled/)).toBeTruthy();
  });

  test("lists running jobs with their progress, elapsed time or an ellipsis and the waiting count", () => {
    mockApi();
    renderApp("/");
    seedStream({
      jobs: makeJobsState([
        makeJob("p", {
          status: "running",
          title: "Syncing",
          progress: 0.42,
          progress_label: "files",
        }),
        makeJob("e", {
          status: "running",
          title: "Checking",
          started_at: NOW_SECONDS - 120,
          project_id: "alpha",
        }),
        makeJob("n", { status: "running", title: "Starting", started_at: null }),
        makeJob("w", { status: "queued", title: "Waiting job" }),
        makeJob("d", { status: "succeeded", title: "Done job" }),
      ]),
    });
    const syncing = board().getByRole("link", { name: /Syncing/ });
    expect(syncing.textContent).toContain("42%");
    expect(syncing.textContent).toContain("files");
    expect(syncing.getAttribute("href")).toBe("/jobs/p");
    const checking = board().getByRole("link", { name: /Checking/ });
    expect(checking.textContent).toContain("2 min");
    expect(checking.textContent).not.toContain("ago");
    expect(checking.textContent).toContain("alpha");
    expect(board().getByRole("link", { name: /Starting/ }).textContent).toContain("…");
    expect(board().getByText(/1 waiting/)).toBeTruthy();
    expect(board().queryByText(/Waiting job/)).toBeNull();
    expect(board().queryByText(/Done job/)).toBeNull();
  });

  test("says no jobs have run yet when the list is empty", () => {
    mockApi();
    renderApp("/");
    seedStream({ jobs: makeJobsState([]) });
    expect(board().getByText("Recent arrivals")).toBeTruthy();
    expect(board().getByText("No jobs yet.")).toBeTruthy();
  });

  test("lists the three most recent finished jobs as arrivals while nothing is running", () => {
    mockApi();
    renderApp("/");
    seedStream({
      jobs: makeJobsState([
        makeJob("a", { status: "succeeded", title: "Oldest", ended_at: NOW_SECONDS - 4000 }),
        makeJob("b", { status: "failed", title: "Broke", ended_at: NOW_SECONDS - 300 }),
        makeJob("c", { status: "cancelled", title: "Stopped", ended_at: NOW_SECONDS - 200 }),
        makeJob("d", { status: "attention", title: "Parked", ended_at: NOW_SECONDS - 100 }),
        makeJob("e", { status: "succeeded", title: "Fine", ended_at: NOW_SECONDS - 3000 }),
      ]),
    });
    expect(board().getByText("Recent arrivals")).toBeTruthy();
    const arrivals = board()
      .getAllByRole("link")
      .filter((link) => link.getAttribute("href")?.startsWith("/jobs/"));
    expect(arrivals.map((link) => link.getAttribute("href"))).toEqual([
      "/jobs/d",
      "/jobs/c",
      "/jobs/b",
    ]);
    expect(arrivals[0]?.textContent).toContain("needs you");
    expect(arrivals[1]?.textContent).toContain("cancelled");
    expect(arrivals[2]?.textContent).toContain("failed");
    expect(arrivals[0]?.textContent).toContain(clockTime((NOW_SECONDS - 100) * 1000));
    expect(board().queryByText(/Oldest/)).toBeNull();
  });

  test("marks a departure that is overdue with the minutes it is late", () => {
    mockApi();
    renderApp("/");
    seedStream({
      scheduler: makeSchedulerState([
        makeScheduleEntry("late", {
          title: "Overdue sync",
          next_run_at: NOW_SECONDS - 5 * 60 - 20,
        }),
        makeScheduleEntry("soon", { title: "On time", next_run_at: NOW_SECONDS - 30 }),
      ]),
    });
    expect(
      within(board().getByRole("link", { name: /Overdue sync/ })).getByTitle("5 min late")
        .textContent,
    ).toBe("+5");
    expect(board().getByRole("link", { name: /On time/ }).textContent).not.toContain("+");
  });

  test("tells a failure on the attention list apart from a warning", () => {
    mockApi();
    renderApp("/");
    seedStream({
      projects: makeProjectsState(
        [
          makeProject("a", { attention: [{ source: "ci", level: "bad", message: "red" }] }),
          makeProject("b", { attention: [{ source: "ci", level: "warn", message: "slow" }] }),
        ],
        { totals },
      ),
    });
    expect(board().getByRole("link", { name: /need attention/ }).textContent).toContain("3");
  });
});

describe("how late a departure is", () => {
  const now = NOW_MS;

  test("is unknown without a due time", () => {
    expect(lateMinutes(null, now)).toBeNull();
    expect(lateMinutes(undefined, now)).toBeNull();
  });

  test("is nothing until a full minute has passed", () => {
    expect(lateMinutes(NOW_SECONDS + 600, now)).toBeNull();
    expect(lateMinutes(NOW_SECONDS - 59, now)).toBeNull();
  });

  test("counts whole minutes past the due time", () => {
    expect(lateMinutes(NOW_SECONDS - 60, now)).toBe(1);
    expect(lateMinutes(NOW_SECONDS - 7 * 60 - 59, now)).toBe(7);
  });
});

describe("the compact departures strip on other pages", () => {
  test("shows the clock, the counts and the next scheduled run in one line", () => {
    mockApi();
    renderApp("/nope");
    seedStream({
      projects: makeProjectsState([], {
        totals: { projects: 7, failing: 2, attention: 0, running_services: 0 },
      }),
      jobs: makeJobsState([makeJob("a", { status: "running" })]),
      scheduler: makeSchedulerState([
        makeScheduleEntry("one", { title: "Nightly checks", next_run_at: NOW_SECONDS + 1800 }),
        makeScheduleEntry("two", { title: "Later", next_run_at: NOW_SECONDS + 3600 }),
      ]),
    });
    expect(screen.queryByRole("region", { name: "Departures" })).toBeNull();
    const strip = screen.getByText(clockTime(NOW_MS)).parentElement as HTMLElement;
    expect(strip.textContent).toBe(
      `${clockTime(NOW_MS)}7 projects2 failing1 runningnext in 30 min Nightly checks`,
    );
  });

  test("shows dashes before the first state arrives and omits the next run when nothing is queued", () => {
    mockApi();
    renderApp("/nope");
    const strip = screen.getByText(clockTime(NOW_MS)).parentElement as HTMLElement;
    expect(strip.textContent).toBe(`${clockTime(NOW_MS)}– projects– failing0 running`);
    seedStream({ scheduler: makeSchedulerState([]) });
    expect(strip.textContent).not.toContain("next");
  });

  test("leaves the time blank for a next run with no scheduled instant", () => {
    mockApi();
    renderApp("/nope");
    seedStream({
      scheduler: makeSchedulerState([
        makeScheduleEntry("one", { title: "On demand", next_run_at: null }),
      ]),
    });
    expect(screen.getByText(/On demand/)).toBeTruthy();
  });
});
