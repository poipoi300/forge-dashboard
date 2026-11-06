import { describe, expect, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import { LaunchTab } from "@/features/project/tabs/LaunchTab";
import { NOW_SECONDS } from "./constants";
import { makeProject, makeProjectsState } from "./factories";
import {
  installHarness,
  type MockApi,
  MockApiError,
  mockApi,
  renderWithProviders,
  seedStream,
  settle,
  timeText,
} from "./harness";
import {
  makeDeploymentStatus,
  makeDetail,
  makeLaunchGuide,
  makeManifest,
  makeServiceLogs,
  makeServiceStatus,
  makeServicesState,
  serviceLogReader,
} from "./project-tabs-support";

installHarness();

const GUIDE = "/api/services/projects/actions/launch-guide" as const;
const LOGS = "/api/services/services/actions/logs" as const;
const LAUNCH = "/api/services/services/actions/launch" as const;
const START = "/api/services/services/actions/start-service" as const;
const STOP = "/api/services/services/actions/stop-service" as const;
const RESTART = "/api/services/services/actions/restart-service" as const;
const RELEASE_PORT = "/api/services/services/actions/release-port" as const;
const SCAN = "/api/services/services/actions/scan-external" as const;
const CHECK_DEPLOYMENTS = "/api/services/services/actions/check-deployments" as const;

const project = makeProject("alpha");

function mockLaunch(guide = makeLaunchGuide()): MockApi {
  return mockApi().action(GUIDE, guide).action(LOGS, makeServiceLogs([]));
}

function renderLaunch(manifestDeployments = false) {
  const detail = makeDetail(
    project,
    manifestDeployments
      ? {
          manifest: makeManifest(project, {
            deployments: [
              {
                id: "prod",
                label: "Prod",
                url: "https://prod.example.com",
                machine: null,
                environment: "production",
                health: null,
              },
            ],
          }),
        }
      : {},
  );
  return renderWithProviders(<LaunchTab project={project} detail={detail} />);
}

function sectionOf(title: string): ReturnType<typeof within> {
  const section = screen.getByRole("heading", { name: title }).closest("section");
  if (!section) throw new Error(`no section titled ${title}`);
  return within(section);
}

function guideDetails(): HTMLDetailsElement {
  const details = screen.getByText("How to run it").closest("details");
  if (!details) throw new Error("no guide section");
  return details;
}

function guideSection(): ReturnType<typeof within> {
  return within(guideDetails());
}

function rowOf(label: string): ReturnType<typeof within> {
  const row = sectionOf("Services").getByRole("button", { name: label }).closest("tr");
  if (!row) throw new Error(`no row for ${label}`);
  return within(row);
}

function toneOf(element: HTMLElement): string | null | undefined {
  return element.closest("[data-tone]")?.getAttribute("data-tone");
}

describe("LaunchTab launch guide", () => {
  test("shows loading rows while the guide is pending", async () => {
    mockLaunch(makeLaunchGuide({ markdown: "# Hello" }));
    renderLaunch();
    expect(guideSection().getByRole("status", { name: "Loading" })).toBeTruthy();
    expect(await screen.findByRole("heading", { name: "Hello" })).toBeTruthy();
  });

  test("opens the guide when the project declares no services", async () => {
    mockLaunch(makeLaunchGuide({ markdown: "Start it by hand." }));
    renderLaunch();
    expect(await screen.findByText("Start it by hand.")).toBeTruthy();
    expect(guideDetails().open).toBe(true);
  });

  test("keeps the guide folded away once services exist", async () => {
    mockLaunch(makeLaunchGuide({ markdown: "Start it by hand." }));
    seedStream({ services: makeServicesState([makeServiceStatus("alpha", "web")]) });
    renderLaunch();
    await screen.findByText("Start it by hand.");
    expect(guideDetails().open).toBe(false);
  });

  test("renders the README guide as markdown and says where it came from", async () => {
    const api = mockLaunch(
      makeLaunchGuide({ source: "readme", markdown: "## Setup\n\nRun `uv sync` first." }),
    );
    renderLaunch();
    expect(await screen.findByText("From the README")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Setup" })).toBeTruthy();
    expect(screen.getByText("uv sync").tagName).toBe("CODE");
    expect(api.sent(GUIDE)).toEqual([{ project_id: "alpha" }]);
  });

  test("names the manifest guide file as the source", async () => {
    mockLaunch(makeLaunchGuide({ source: "manifest-file", markdown: "guide" }));
    renderLaunch();
    expect(await screen.findByText("From the manifest's guide file")).toBeTruthy();
  });

  test("names the manifest as the source of inline instructions", async () => {
    mockLaunch(makeLaunchGuide({ source: "manifest", markdown: "inline" }));
    renderLaunch();
    expect(await screen.findByText("From the manifest")).toBeTruthy();
  });

  test("explains how to add launch instructions and why none was found", async () => {
    mockLaunch(makeLaunchGuide({ source: "none", markdown: "", problem: "README not found" }));
    renderLaunch();
    expect(await screen.findByText("No launch instructions")).toBeTruthy();
    expect(screen.getByText(/README not found\. Add/)).toBeTruthy();
    expect(screen.getByText("[launch].instructions")).toBeTruthy();
  });

  test("explains how to add launch instructions without a problem prefix when none was reported", async () => {
    mockLaunch(makeLaunchGuide({ source: "none", markdown: "", problem: null }));
    renderLaunch();
    expect(await screen.findByText("No launch instructions")).toBeTruthy();
    expect(screen.queryByText(/README not found/)).toBeNull();
    expect(screen.getByText(/^Add/)).toBeTruthy();
  });

  test("reports a guide that cannot be read and retries it", async () => {
    let attempts = 0;
    mockApi()
      .action(LOGS, makeServiceLogs([]))
      .action(GUIDE, () => {
        attempts += 1;
        if (attempts === 1) throw new MockApiError(500, "guide unavailable");
        return makeLaunchGuide({ markdown: "recovered guide" });
      });
    const { user } = renderLaunch();
    expect(await screen.findByText("guide unavailable")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("recovered guide")).toBeTruthy();
  });

  test("re-reads the guide when the projects service publishes a new revision", async () => {
    const api = mockLaunch(makeLaunchGuide({ markdown: "guide" }));
    renderLaunch();
    await screen.findByText("guide");
    expect(api.callsTo("POST", GUIDE)).toHaveLength(1);
    seedStream({ projects: makeProjectsState([project]) }, 2);
    await waitFor(() => expect(api.callsTo("POST", GUIDE)).toHaveLength(2));
  });
});

describe("LaunchTab services", () => {
  test("invites declaring services and offers no actions when there are none", async () => {
    mockLaunch();
    renderLaunch();
    expect(await screen.findByText("No services declared")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Find running instances" })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Launch/ })).toBeNull();
    expect(screen.queryByRole("log")).toBeNull();
  });

  test("lists only this project's services with their detail, address, machine, restarts and uptime", async () => {
    mockLaunch();
    seedStream({
      services: makeServicesState([
        makeServiceStatus("alpha", "web", {
          label: "Web",
          state: "running",
          detail: "listening",
          host: "127.0.0.1",
          port: 8080,
          url: "http://127.0.0.1:8080",
          started_at: NOW_SECONDS - 4320,
          restarts: 3,
          machine: "workstation",
        }),
        makeServiceStatus("alpha", "worker", {
          label: "Worker",
          state: "stopped",
          host: "127.0.0.1",
          port: 9000,
          machine: "workstation",
        }),
        makeServiceStatus("alpha", "cron", { label: "Cron", state: "stopped", port: null }),
        makeServiceStatus("beta", "elsewhere", { label: "Elsewhere", state: "running" }),
      ]),
    });
    renderLaunch();
    const web = await screen.findByRole("button", { name: "Web" });
    expect(screen.queryByText("Elsewhere")).toBeNull();
    const webRow = within(web.closest("tr") as HTMLElement);
    expect(webRow.getByText("listening")).toBeTruthy();
    const address = webRow.getByRole("link", { name: "127.0.0.1:8080" });
    expect(address.getAttribute("href")).toBe("http://127.0.0.1:8080");
    expect(webRow.getByText("workstation")).toBeTruthy();
    expect(webRow.getByText("1 h 12 min")).toBeTruthy();
    expect(webRow.getByText("3")).toBeTruthy();
    const workerRow = rowOf("Worker");
    expect(workerRow.getByText("127.0.0.1:9000").tagName).toBe("CODE");
    expect(workerRow.getByText("—", { selector: "td span" })).toBeTruthy();
    const cronRow = rowOf("Cron");
    expect(cronRow.getAllByText("—")).toHaveLength(2);
    expect(sectionOf("Services").getByText("Ports are the ones actually bound.")).toBeTruthy();
  });

  test("marks every service state with its tone and the state's own word", async () => {
    mockLaunch();
    const states = [
      ["stopped", "neutral"],
      ["building", "info"],
      ["starting", "info"],
      ["running", "good"],
      ["backoff", "warn"],
      ["stopping", "neutral"],
      ["failed", "bad"],
      ["build failed", "bad"],
      ["blocked", "bad"],
    ] as const;
    seedStream({
      services: makeServicesState(
        states.map(([state]) =>
          makeServiceStatus("alpha", `svc-${state}`, { label: `S ${state}`, state }),
        ),
      ),
    });
    renderLaunch();
    for (const [state, tone] of states) {
      const row = rowOf(`S ${state}`);
      expect(toneOf(row.getByText(state))).toBe(tone);
    }
  });

  test("describes a service running outside the dashboard by its pid, with or without one on record", async () => {
    mockLaunch();
    seedStream({
      services: makeServicesState([
        makeServiceStatus("alpha", "api", {
          label: "Api",
          state: "external",
          external: { pid: 4242, process: "node", port: 3000 },
        }),
        makeServiceStatus("alpha", "ghost", { label: "Ghost", state: "external", external: null }),
      ]),
    });
    renderLaunch();
    await screen.findByRole("button", { name: "Api" });
    expect(rowOf("Api").getByText("running outside (pid 4242)")).toBeTruthy();
    expect(toneOf(rowOf("Api").getByText("running outside (pid 4242)"))).toBe("good");
    expect(rowOf("Ghost").getByText("running outside (pid ?)")).toBeTruthy();
  });

  test("shows a blocked service's port holder and offers to free the port", async () => {
    mockLaunch();
    seedStream({
      services: makeServicesState([
        makeServiceStatus("alpha", "web", {
          label: "Web",
          state: "blocked",
          port: 8080,
          blocked_by: "python.exe (pid 99)",
        }),
      ]),
    });
    renderLaunch();
    await screen.findByRole("button", { name: "Web" });
    expect(rowOf("Web").getByText("port held by python.exe (pid 99)")).toBeTruthy();
    expect(rowOf("Web").getByRole("button", { name: "Free the port" })).toBeTruthy();
  });

  test("frees the port only after the port number is typed as confirmation", async () => {
    const api = mockLaunch().action(RELEASE_PORT, { ok: true, message: "port freed" });
    seedStream({
      services: makeServicesState([
        makeServiceStatus("alpha", "web", { label: "Web", state: "blocked", port: 8080 }),
      ]),
    });
    const { user } = renderLaunch();
    await screen.findByRole("button", { name: "Web" });
    await user.click(rowOf("Web").getByRole("button", { name: "Free the port" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("8080")).toBeTruthy();
    const submit = within(dialog).getByRole("button", {
      name: "Free port and start",
    }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    await user.type(within(dialog).getByRole("textbox"), "8080");
    expect(api.sent(RELEASE_PORT)).toEqual([]);
    await user.click(submit);
    await waitFor(() =>
      expect(api.sent(RELEASE_PORT)).toEqual([{ project_id: "alpha", service_id: "web" }]),
    );
    expect(await screen.findByText("port freed")).toBeTruthy();
  });

  test("falls back to the word confirm when a blocked service has no port to type", async () => {
    mockLaunch();
    seedStream({
      services: makeServicesState([
        makeServiceStatus("alpha", "web", { label: "Web", state: "blocked", port: null }),
      ]),
    });
    const { user } = renderLaunch();
    await screen.findByRole("button", { name: "Web" });
    await user.click(rowOf("Web").getByRole("button", { name: "Free the port" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("confirm")).toBeTruthy();
  });

  test("offers start for an inactive service and sends its id", async () => {
    const api = mockLaunch().action(START, { ok: true, message: "started worker" });
    seedStream({
      services: makeServicesState([
        makeServiceStatus("alpha", "worker", { label: "Worker", state: "stopped" }),
      ]),
    });
    const { user } = renderLaunch();
    await screen.findByRole("button", { name: "Worker" });
    const row = rowOf("Worker");
    expect(row.queryByRole("button", { name: "Stop" })).toBeNull();
    expect(row.queryByRole("button", { name: "Restart" })).toBeNull();
    await user.click(row.getByRole("button", { name: "Start" }));
    await waitFor(() =>
      expect(api.sent(START)).toEqual([{ project_id: "alpha", service_id: "worker" }]),
    );
    expect(await screen.findByText("started worker")).toBeTruthy();
  });

  test("offers restart and stop for every active or external service and sends its id", async () => {
    const api = mockLaunch()
      .action(RESTART, { ok: true, message: "restarted" })
      .action(STOP, { ok: true, message: "stopped" });
    const active = ["building", "starting", "running", "backoff", "stopping"] as const;
    seedStream({
      services: makeServicesState([
        ...active.map((state) =>
          makeServiceStatus("alpha", `svc-${state}`, { label: `S ${state}`, state }),
        ),
        makeServiceStatus("alpha", "svc-external", { label: "S external", state: "external" }),
      ]),
    });
    const { user } = renderLaunch();
    await screen.findByRole("button", { name: "S running" });
    for (const state of [...active, "external"]) {
      const row = rowOf(`S ${state}`);
      expect(row.getByRole("button", { name: "Restart" })).toBeTruthy();
      expect(row.getByRole("button", { name: "Stop" })).toBeTruthy();
      expect(row.queryByRole("button", { name: "Start" })).toBeNull();
    }
    await user.click(rowOf("S running").getByRole("button", { name: "Restart" }));
    await waitFor(() =>
      expect(api.sent(RESTART)).toEqual([{ project_id: "alpha", service_id: "svc-running" }]),
    );
    await user.click(rowOf("S external").getByRole("button", { name: "Stop" }));
    await waitFor(() =>
      expect(api.sent(STOP)).toEqual([{ project_id: "alpha", service_id: "svc-external" }]),
    );
  });

  test("scans for instances running outside the dashboard", async () => {
    const api = mockLaunch().action(SCAN, makeServicesState([]));
    seedStream({
      services: makeServicesState([makeServiceStatus("alpha", "web", { label: "Web" })]),
    });
    const { user } = renderLaunch();
    await user.click(await screen.findByRole("button", { name: "Find running instances" }));
    await waitFor(() => expect(api.sent(SCAN)).toEqual([{}]));
    expect(await screen.findByText("Scan for external instances: done")).toBeTruthy();
  });

  test("shows uptime only for a running service", async () => {
    mockLaunch();
    seedStream({
      services: makeServicesState([
        makeServiceStatus("alpha", "web", {
          label: "Web",
          state: "starting",
          started_at: NOW_SECONDS - 125,
        }),
        makeServiceStatus("alpha", "api", {
          label: "Api",
          state: "running",
          started_at: NOW_SECONDS - 125,
        }),
      ]),
    });
    renderLaunch();
    await screen.findByRole("button", { name: "Web" });
    expect(rowOf("Web").queryByText("2 min 5 s")).toBeNull();
    expect(rowOf("Api").getByText("2 min 5 s")).toBeTruthy();
  });
});

describe("LaunchTab primary action", () => {
  test("offers nothing without services", async () => {
    mockLaunch();
    renderLaunch();
    await screen.findByText("No services declared");
    expect(screen.queryByRole("link", { name: /^Open/ })).toBeNull();
  });

  test("launches the first service when the guide names no primary one", async () => {
    const api = mockLaunch().action(LAUNCH, {
      job_id: "job-9",
      kind: "services",
      title: "Launch Web",
    });
    seedStream({
      services: makeServicesState([
        makeServiceStatus("alpha", "web", { label: "Web", state: "stopped" }),
        makeServiceStatus("alpha", "api", { label: "Api", state: "stopped" }),
      ]),
    });
    const { user } = renderLaunch();
    await user.click(await screen.findByRole("button", { name: "Launch Web" }));
    await waitFor(() => expect(api.sent(LAUNCH)).toEqual([{ project_id: "alpha" }]));
    expect(await screen.findByText("Started: Launch Web")).toBeTruthy();
  });

  test("launches the service the guide names as primary", async () => {
    mockLaunch(makeLaunchGuide({ markdown: "guide", primary_service: "api" }));
    seedStream({
      services: makeServicesState([
        makeServiceStatus("alpha", "web", { label: "Web", state: "stopped" }),
        makeServiceStatus("alpha", "api", { label: "Api", state: "stopped" }),
      ]),
    });
    renderLaunch();
    expect(await screen.findByRole("button", { name: "Launch Api" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Launch Web" })).toBeNull();
  });

  test("offers nothing when the guide's primary service is not among the project's services", async () => {
    mockLaunch(makeLaunchGuide({ markdown: "guide", primary_service: "ghost" }));
    seedStream({
      services: makeServicesState([makeServiceStatus("alpha", "web", { label: "Web" })]),
    });
    renderLaunch();
    await screen.findByText("guide");
    expect(screen.queryByRole("button", { name: /^Launch/ })).toBeNull();
    expect(screen.queryByRole("link", { name: /^Open/ })).toBeNull();
  });

  test("opens a running primary service's address instead of launching it", async () => {
    mockLaunch();
    seedStream({
      services: makeServicesState([
        makeServiceStatus("alpha", "web", {
          label: "Web",
          state: "running",
          url: "http://127.0.0.1:8080",
          port: 8080,
        }),
      ]),
    });
    renderLaunch();
    const open = await screen.findByRole("link", { name: "Open Web" });
    expect(open.getAttribute("href")).toBe("http://127.0.0.1:8080");
    expect(open.getAttribute("target")).toBe("_blank");
    expect(screen.queryByRole("button", { name: "Launch Web" })).toBeNull();
  });

  test("opens a primary service running outside the dashboard", async () => {
    mockLaunch();
    seedStream({
      services: makeServicesState([
        makeServiceStatus("alpha", "web", {
          label: "Web",
          state: "external",
          url: "http://127.0.0.1:9999",
        }),
      ]),
    });
    renderLaunch();
    expect((await screen.findByRole("link", { name: "Open Web" })).getAttribute("href")).toBe(
      "http://127.0.0.1:9999",
    );
  });

  test("launches rather than opens when the service is still starting or has no address", async () => {
    mockLaunch();
    seedStream({
      services: makeServicesState([
        makeServiceStatus("alpha", "web", {
          label: "Web",
          state: "starting",
          url: "http://127.0.0.1:8080",
        }),
      ]),
    });
    renderLaunch();
    expect(await screen.findByRole("button", { name: "Launch Web" })).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Open Web" })).toBeNull();
  });

  test("launches rather than opens when a running service has no address", async () => {
    mockLaunch();
    seedStream({
      services: makeServicesState([
        makeServiceStatus("alpha", "web", { label: "Web", state: "running", url: null }),
      ]),
    });
    renderLaunch();
    expect(await screen.findByRole("button", { name: "Launch Web" })).toBeTruthy();
  });
});

describe("LaunchTab service log", () => {
  test("follows the first active service by default and reads its log from the start", async () => {
    const api = mockLaunch().action(
      LOGS,
      serviceLogReader({ web: ["server booted", "listening on 8080"] }),
    );
    seedStream({
      services: makeServicesState([
        makeServiceStatus("alpha", "db", { label: "Db", state: "stopped" }),
        makeServiceStatus("alpha", "web", { label: "Web", state: "running" }),
      ]),
    });
    renderLaunch();
    const log = await screen.findByRole("log", { name: "Web log" });
    expect(await within(log).findByText("server booted")).toBeTruthy();
    expect(within(log).getByText("listening on 8080")).toBeTruthy();
    expect(api.sent(LOGS)[0]).toEqual({ project_id: "alpha", service_id: "web", offset: 0 });
  });

  test("shows an empty-log hint until the service writes something", async () => {
    mockLaunch();
    seedStream({
      services: makeServicesState([
        makeServiceStatus("alpha", "web", { label: "Web", state: "running" }),
      ]),
    });
    renderLaunch();
    expect(
      await within(await screen.findByRole("log", { name: "Web log" })).findByText(
        "The service has not written anything yet.",
      ),
    ).toBeTruthy();
  });

  test("switches to another service's log when its name is clicked", async () => {
    const api = mockLaunch().action(
      LOGS,
      serviceLogReader({ web: ["output of web"], db: ["output of db"] }),
    );
    seedStream({
      services: makeServicesState([
        makeServiceStatus("alpha", "db", { label: "Db", state: "stopped" }),
        makeServiceStatus("alpha", "web", { label: "Web", state: "running" }),
      ]),
    });
    const { user } = renderLaunch();
    await screen.findByText("output of web");
    await user.click(rowOf("Db").getByRole("button", { name: "Db" }));
    expect(await screen.findByRole("log", { name: "Db log" })).toBeTruthy();
    expect(await screen.findByText("output of db")).toBeTruthy();
    expect(screen.queryByText("output of web")).toBeNull();
    expect(api.sent(LOGS).map((body) => (body as { service_id: string }).service_id)).toContain(
      "db",
    );
  });

  test("opens no log while every service is stopped", async () => {
    mockLaunch();
    seedStream({
      services: makeServicesState([
        makeServiceStatus("alpha", "web", { label: "Web", state: "failed" }),
      ]),
    });
    renderLaunch();
    await screen.findByRole("button", { name: "Web" });
    await settle();
    expect(screen.queryByRole("log")).toBeNull();
  });
});

describe("LaunchTab deployments", () => {
  test("hides the section without deployments in the live state or the manifest", async () => {
    mockLaunch();
    renderLaunch();
    await screen.findByText("No services declared");
    expect(screen.queryByRole("heading", { name: "Deployments" })).toBeNull();
  });

  test("lists each deployment with its health, address, machine and check age", async () => {
    mockLaunch();
    seedStream({
      services: makeServicesState(
        [],
        [
          makeDeploymentStatus("alpha", "prod", {
            label: "Prod",
            environment: "production",
            url: "https://app.example.com/health",
            machine: "vps-1",
            health: "up",
            status_code: 200,
            checked_at: NOW_SECONDS - 300,
          }),
          makeDeploymentStatus("alpha", "stage", {
            label: "Stage",
            environment: "staging",
            url: "http://stage.example.com",
            health: "down",
            status_code: 502,
            error: "bad gateway",
            checked_at: NOW_SECONDS - 3600,
          }),
          makeDeploymentStatus("alpha", "preview", { label: "Preview", environment: "preview" }),
          makeDeploymentStatus("beta", "other", { label: "Other project's" }),
        ],
      ),
    });
    renderLaunch();
    const deployments = await screen.findByRole("heading", { name: "Deployments" });
    expect(deployments).toBeTruthy();
    const section = sectionOf("Deployments");
    expect(section.queryByText("Other project's")).toBeNull();
    const prod = within(section.getByText("Prod").closest("tr") as HTMLElement);
    expect(prod.getByText("production")).toBeTruthy();
    expect(toneOf(prod.getByText("up (200)"))).toBe("good");
    expect(prod.getByRole("link", { name: "app.example.com/health" }).getAttribute("href")).toBe(
      "https://app.example.com/health",
    );
    expect(prod.getByText("vps-1")).toBeTruthy();
    expect(prod.getByText(timeText("5 min ago"))).toBeTruthy();
    const stage = within(section.getByText("Stage").closest("tr") as HTMLElement);
    const down = stage.getByText("down (502)");
    expect(toneOf(down)).toBe("bad");
    expect(down.closest("[title]")?.getAttribute("title")).toBe("bad gateway");
    expect(stage.getByRole("link", { name: "stage.example.com" })).toBeTruthy();
    const preview = within(section.getByText("Preview").closest("tr") as HTMLElement);
    expect(toneOf(preview.getByText("unchecked"))).toBe("neutral");
    expect(preview.getByText(/never/)).toBeTruthy();
  });

  test("re-checks deployments on request", async () => {
    const api = mockLaunch().action(CHECK_DEPLOYMENTS, makeServicesState([]));
    seedStream({
      services: makeServicesState([], [makeDeploymentStatus("alpha", "prod", { label: "Prod" })]),
    });
    const { user } = renderLaunch();
    await user.click(await screen.findByRole("button", { name: "Check now" }));
    await waitFor(() => expect(api.sent(CHECK_DEPLOYMENTS)).toEqual([{}]));
    expect(await screen.findByText("Check deployments: done")).toBeTruthy();
  });

  test("shows the section for manifest-declared deployments before the first check", async () => {
    mockLaunch();
    renderLaunch(true);
    expect(await screen.findByRole("heading", { name: "Deployments" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Check now" })).toBeTruthy();
  });
});
