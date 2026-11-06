import { describe, expect, test } from "bun:test";
import { screen, within } from "@testing-library/react";
import { RunsPage } from "@/features/fleet/RunsPage";
import { makeForgeRun, makeForgeRunsState, makeStep, makeStepOutput } from "./fleet-support";
import {
  installHarness,
  MockApiError,
  mockApi,
  renderWithProviders,
  seedStream,
  settle,
  timeText,
} from "./harness";

installHarness();

const REFRESH = "/api/services/forge_runs/actions/refresh" as const;
const STEP_OUTPUT = "/api/services/forge_runs/actions/step-output" as const;

function runCard(name: string): HTMLElement {
  const card = screen.getByText(name).closest("li");
  if (!card) throw new Error(`no run card for ${name}`);
  return card;
}

function stepButton(card: HTMLElement, stepName: string): HTMLElement {
  const button = within(card).getByText(stepName).closest("button");
  if (!button) throw new Error(`no step button for ${stepName}`);
  return button;
}

describe("RunsPage list", () => {
  test("explains where forge looks for runs and links to Settings when none exist", async () => {
    mockApi();
    const { user, location } = renderWithProviders(<RunsPage />);
    seedStream({
      forge_runs: makeForgeRunsState([], {
        roots: [
          { path: "C:/runs/a", origin: "forge-toml", exists: true, projects: 0 },
          { path: "C:/runs/b", origin: "configured", exists: false, projects: 0 },
        ],
      }),
    });
    expect(await screen.findByText("No forge runs found")).toBeTruthy();
    expect(screen.getByText(/Run roots: C:\/runs\/a, C:\/runs\/b\./)).toBeTruthy();
    await user.click(screen.getByRole("link", { name: "Add a root" }));
    expect(location()).toBe("/settings");
  });

  test("says no root is known before the runs state arrives", async () => {
    mockApi();
    renderWithProviders(<RunsPage />);
    expect(screen.getByText("No forge runs found")).toBeTruthy();
    expect(screen.getByText(/Run roots: none known\./)).toBeTruthy();
    expect(screen.getByText("Select a step for its output.")).toBeTruthy();
  });

  test("counts only the run roots that exist", async () => {
    mockApi();
    renderWithProviders(<RunsPage />);
    seedStream({
      forge_runs: makeForgeRunsState([], {
        roots: [
          { path: "C:/runs/a", origin: "forge-toml", exists: true, projects: 1 },
          { path: "C:/runs/b", origin: "configured", exists: false, projects: 0 },
          { path: "C:/runs/c", origin: "sibling", exists: true, projects: 2 },
        ],
      }),
    });
    expect(await screen.findByText("2 run roots")).toBeTruthy();
  });

  test("prefers the state's notice over the root count", async () => {
    mockApi();
    renderWithProviders(<RunsPage />);
    seedStream({
      forge_runs: makeForgeRunsState([], { notice: "forge.toml has no [runs] table" }),
    });
    expect(await screen.findByText("forge.toml has no [runs] table")).toBeTruthy();
    expect(screen.queryByText(/run roots/)).toBeNull();
  });

  test("shows a run's steps with outcome, harness and duration and its summary line", async () => {
    mockApi();
    renderWithProviders(<RunsPage />);
    seedStream({
      forge_runs: makeForgeRunsState([
        makeForgeRun("hair-brawler", [
          makeStep("01-spec", 1, { name: "Specification", harness: "claude", duration_s: 42 }),
          makeStep("02-build", 2, {
            name: "Build",
            harness: "codex",
            ok: false,
            exit_code: 2,
            duration_s: 252,
          }),
          makeStep("03-ship", 3, {
            name: "Ship",
            ok: false,
            timed_out: true,
            exit_code: null,
            duration_s: 3600,
          }),
          makeStep("04-polish", 4, {
            name: "Polish",
            ok: false,
            timed_out: false,
            exit_code: null,
            duration_s: 0.5,
          }),
        ]),
      ]),
    });
    await screen.findByText("hair-brawler");
    const card = runCard("hair-brawler");
    expect(within(card).getByText(/1\/4 steps ok · 1 h 05 min ·/)).toBeTruthy();
    expect(within(card).getByText(timeText("30 min ago"))).toBeTruthy();
    const spec = within(stepButton(card, "Specification"));
    expect(spec.getByText("ok")).toBeTruthy();
    expect(spec.getByText("claude")).toBeTruthy();
    expect(spec.getByText("42 s")).toBeTruthy();
    expect(spec.getByText("1")).toBeTruthy();
    const build = within(stepButton(card, "Build"));
    expect(build.getByText("exit 2")).toBeTruthy();
    expect(build.getByText("codex")).toBeTruthy();
    expect(build.getByText("4 min 12 s")).toBeTruthy();
    expect(within(stepButton(card, "Ship")).getByText("timed out")).toBeTruthy();
    expect(within(stepButton(card, "Polish")).getByText("exit ?")).toBeTruthy();
    expect(within(stepButton(card, "Polish")).getByText("500 ms")).toBeTruthy();
  });

  test("shows the root of each run and flags step records that could not be read", async () => {
    mockApi();
    renderWithProviders(<RunsPage />);
    seedStream({
      forge_runs: makeForgeRunsState([
        makeForgeRun("alpha", [makeStep("01", 1)], {
          root: "C:/Users/me/runs/eval-1",
          unreadable: [
            { stem: "02", problem: "invalid JSON" },
            { stem: "03", problem: "missing exit code" },
          ],
        }),
        makeForgeRun("beta", [makeStep("01", 1)], { root: "C:/runs/eval-2" }),
      ]),
    });
    await screen.findByText("alpha");
    const alpha = within(runCard("alpha"));
    expect(
      alpha.getByText("2 step records could not be read: invalid JSON; missing exit code"),
    ).toBeTruthy();
    expect(alpha.getByText("~/runs/eval-1")).toBeTruthy();
    expect(within(runCard("beta")).queryByText(/could not be read/)).toBeNull();
    expect(within(runCard("beta")).getByText("C:/runs/eval-2")).toBeTruthy();
  });

  test("keeps runs with the same name under different roots apart", async () => {
    mockApi();
    renderWithProviders(<RunsPage />);
    seedStream({
      forge_runs: makeForgeRunsState([
        makeForgeRun("shared", [makeStep("01", 1)], { root: "C:/runs/one" }),
        makeForgeRun("shared", [makeStep("01", 1)], { root: "C:/runs/two" }),
      ]),
    });
    expect(await screen.findAllByText("shared")).toHaveLength(2);
    expect(screen.getByText("C:/runs/one")).toBeTruthy();
    expect(screen.getByText("C:/runs/two")).toBeTruthy();
  });

  test("re-reads the run folders on request", async () => {
    const state = makeForgeRunsState([]);
    const api = mockApi().action(REFRESH, state);
    const { user } = renderWithProviders(<RunsPage />);
    seedStream({ forge_runs: state });
    await settle();
    await user.click(await screen.findByRole("button", { name: "Re-read" }));
    await settle();
    expect(api.sent(REFRESH)).toEqual([{}]);
  });
});

describe("RunsPage step output", () => {
  function seedRuns(): void {
    seedStream({
      forge_runs: makeForgeRunsState([
        makeForgeRun("alpha", [
          makeStep("01-spec", 1, { name: "Specification" }),
          makeStep("02-build", 2, { name: "Build" }),
        ]),
        makeForgeRun("beta", [makeStep("01-spec", 1, { name: "Specification" })], {
          root: "C:/runs/eval-2",
        }),
      ]),
    });
  }

  test("loads the captured stdout of the clicked step", async () => {
    const api = mockApi().action(STEP_OUTPUT, makeStepOutput(["building…", "done"]));
    const { user } = renderWithProviders(<RunsPage />);
    seedRuns();
    await screen.findByText("alpha");
    await user.click(stepButton(runCard("alpha"), "Build"));
    expect(await screen.findByText("alpha · Build")).toBeTruthy();
    const output = await screen.findByText(/building…/);
    expect(output.textContent).toBe("building…\ndone");
    expect(api.sent(STEP_OUTPUT)).toEqual([
      {
        root: "C:/runs/eval-1",
        project: "alpha",
        step: "02-build",
        which: "stdout",
        offset: 0,
        limit: 2000,
      },
    ]);
    expect(screen.queryByText("Select a step for its output.")).toBeNull();
  });

  test("switches to stderr and says so when a stream is empty", async () => {
    const api = mockApi().action(STEP_OUTPUT, ({ which }) =>
      which === "stdout" ? makeStepOutput(["out line"]) : makeStepOutput([], "stderr"),
    );
    const { user } = renderWithProviders(<RunsPage />);
    seedRuns();
    await screen.findByText("alpha");
    await user.click(stepButton(runCard("alpha"), "Specification"));
    await screen.findByText("out line");
    await user.click(screen.getByRole("radio", { name: "stderr" }));
    expect(await screen.findByText("No stderr.")).toBeTruthy();
    expect(api.sent(STEP_OUTPUT).map((body) => (body as { which: string }).which)).toEqual([
      "stdout",
      "stderr",
    ]);
    await user.click(screen.getByRole("radio", { name: "stdout" }));
    expect(await screen.findByText("out line")).toBeTruthy();
  });

  test("notes how many lines were left out of a long output", async () => {
    mockApi().action(STEP_OUTPUT, makeStepOutput(["l1", "l2"], "stdout", 2500));
    const { user } = renderWithProviders(<RunsPage />);
    seedRuns();
    await screen.findByText("alpha");
    await user.click(stepButton(runCard("alpha"), "Specification"));
    const output = await screen.findByText(/l1/);
    expect(output.textContent).toBe("l1\nl2\n… 2498 more lines");
  });

  test("reloads the output for another step and for the same step name in another run", async () => {
    const api = mockApi().action(STEP_OUTPUT, ({ project, step }) =>
      makeStepOutput([`${project}/${step}`]),
    );
    const { user } = renderWithProviders(<RunsPage />);
    seedRuns();
    await screen.findByText("alpha");
    await user.click(stepButton(runCard("alpha"), "Specification"));
    await screen.findByText("alpha/01-spec");
    await user.click(stepButton(runCard("beta"), "Specification"));
    expect(await screen.findByText("beta/01-spec")).toBeTruthy();
    expect(screen.getByText("beta · Specification")).toBeTruthy();
    expect(screen.queryByText("alpha/01-spec")).toBeNull();
    expect(api.sent(STEP_OUTPUT)).toHaveLength(2);
  });

  test("reports a step output that cannot be read", async () => {
    mockApi().action(STEP_OUTPUT, () => {
      throw new MockApiError(404, "step directory is gone");
    });
    const { user } = renderWithProviders(<RunsPage />);
    seedRuns();
    await screen.findByText("alpha");
    await user.click(stepButton(runCard("alpha"), "Build"));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("step directory is gone");
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
  });
});
