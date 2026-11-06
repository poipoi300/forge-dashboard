import { describe, expect, test } from "bun:test";
import { screen, within } from "@testing-library/react";
import { ServicesPage } from "@/features/fleet/ServicesPage";
import { makeProject, makeProjectsState } from "./factories";
import { makeMachine, makeServiceStatus } from "./fleet-support";
import {
  installHarness,
  MockApiError,
  mockApi,
  NOW_SECONDS,
  renderWithProviders,
  seedStream,
  settle,
  timeText,
} from "./harness";
import { waitUntilGone } from "./jobs-support";

installHarness();

const STOP_ALL = "/api/services/services/actions/stop-all" as const;
const SAMPLE = "/api/services/machine/actions/refresh" as const;

const GIB = 1024 ** 3;

function serviceRow(label: string): HTMLElement {
  const table = screen.getAllByRole("table")[0];
  if (!table) throw new Error("no services table");
  const row = within(table).getByText(label).closest("tr");
  if (!row) throw new Error(`no service row for ${label}`);
  return row;
}

function seedServices(...services: ReturnType<typeof makeServiceStatus>[]): void {
  seedStream({
    projects: makeProjectsState([makeProject("alpha"), makeProject("beta")]),
    services: { services, deployments: [] },
  });
}

describe("ServicesPage services table", () => {
  test("explains that no service is declared and offers no stop-all", async () => {
    mockApi();
    renderWithProviders(<ServicesPage />);
    seedServices();
    expect(await screen.findByText("No services declared")).toBeTruthy();
    expect(screen.getByText("0 of 0 declared services are up or starting")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Stop all" })).toBeNull();
  });

  test("shows each service with its project link, state, address, pid and restarts", async () => {
    mockApi();
    const { user, location } = renderWithProviders(<ServicesPage />);
    seedServices(
      makeServiceStatus("alpha", "api", {
        label: "API server",
        state: "running",
        host: "127.0.0.1",
        port: 8000,
        url: "http://127.0.0.1:8000",
        pid: 4242,
        restarts: 3,
        detail: "ready on :8000",
      }),
    );
    await screen.findByText("API server");
    const row = within(serviceRow("API server"));
    expect(row.getByText("running").closest("[title]")?.getAttribute("title")).toBe(
      "ready on :8000",
    );
    const address = row.getByRole("link", { name: "127.0.0.1:8000" });
    expect(address.getAttribute("href")).toBe("http://127.0.0.1:8000");
    expect(address.getAttribute("target")).toBe("_blank");
    expect(row.getByText("4242")).toBeTruthy();
    expect(row.getByText("3")).toBeTruthy();
    await user.click(row.getByRole("link", { name: "alpha" }));
    expect(location()).toBe("/p/alpha/launch");
  });

  test("counts the services that are not stopped as up or starting", async () => {
    mockApi();
    renderWithProviders(<ServicesPage />);
    seedServices(
      makeServiceStatus("alpha", "api", { label: "api", state: "running" }),
      makeServiceStatus("alpha", "worker", { label: "worker", state: "starting" }),
      makeServiceStatus("beta", "web", { label: "web", state: "stopped" }),
    );
    expect(await screen.findByText("2 of 3 declared services are up or starting")).toBeTruthy();
  });

  test("renders a port without a url as plain text and a service without a port as a dash", async () => {
    mockApi();
    renderWithProviders(<ServicesPage />);
    seedServices(
      makeServiceStatus("alpha", "db", { label: "database", state: "running", port: 5432 }),
      makeServiceStatus("alpha", "job", { label: "batch", state: "stopped", port: null }),
    );
    await screen.findByText("database");
    const database = within(serviceRow("database"));
    expect(database.getByText("127.0.0.1:5432")).toBeTruthy();
    expect(database.queryByRole("link", { name: "127.0.0.1:5432" })).toBeNull();
    expect(within(serviceRow("batch")).getAllByText("—")).toHaveLength(2);
  });

  test("falls back to the external process id for a service started outside the dashboard", async () => {
    mockApi();
    renderWithProviders(<ServicesPage />);
    seedServices(
      makeServiceStatus("alpha", "api", {
        label: "external api",
        state: "external",
        owner: "external",
        pid: null,
        external: { pid: 777, process: "uvicorn", port: 8000 },
      }),
    );
    await screen.findByText("external api");
    expect(within(serviceRow("external api")).getByText("777")).toBeTruthy();
    expect(within(serviceRow("external api")).getByText("external")).toBeTruthy();
  });

  test.each([
    ["running", "good"],
    ["external", "good"],
    ["starting", "info"],
    ["building", "info"],
    ["backoff", "warn"],
    ["failed", "bad"],
    ["build failed", "bad"],
    ["blocked", "bad"],
    ["stopped", "neutral"],
    ["stopping", "neutral"],
  ] as const)("marks a %s service with the %s tone", async (state, tone) => {
    mockApi();
    renderWithProviders(<ServicesPage />);
    seedServices(makeServiceStatus("alpha", "svc", { label: "svc", state }));
    await screen.findByText("svc");
    const mark = within(serviceRow("svc")).getByText(state).closest("[data-tone]");
    expect(mark?.getAttribute("data-tone")).toBe(tone);
  });

  test("distinguishes projects that share a name by id", async () => {
    mockApi();
    renderWithProviders(<ServicesPage />);
    seedStream({
      projects: makeProjectsState([
        makeProject("tool-a", { name: "tool" }),
        makeProject("tool-b", { name: "tool" }),
      ]),
      services: {
        services: [makeServiceStatus("tool-b", "api", { label: "api" })],
        deployments: [],
      },
    });
    expect(await screen.findByRole("link", { name: "tool (tool-b)" })).toBeTruthy();
  });
});

describe("ServicesPage stop all", () => {
  test("stops every service only after the confirmation dialog", async () => {
    const api = mockApi().action(STOP_ALL, { ok: true, message: "Stopped 2 services" });
    const { user } = renderWithProviders(<ServicesPage />);
    seedServices(
      makeServiceStatus("alpha", "api", { label: "api", state: "running" }),
      makeServiceStatus("beta", "web", { label: "web", state: "building" }),
    );
    await settle();
    await user.click(await screen.findByRole("button", { name: "Stop all" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Stop every service the dashboard started/)).toBeTruthy();
    expect(api.sent(STOP_ALL)).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Stop all services" }));
    expect(await screen.findByText("Stopped 2 services")).toBeTruthy();
    expect(api.sent(STOP_ALL)).toEqual([{}]);
    await waitUntilGone(() => screen.queryByRole("dialog"));
  });

  test("sends nothing when the confirmation is cancelled", async () => {
    const api = mockApi();
    const { user } = renderWithProviders(<ServicesPage />);
    seedServices(makeServiceStatus("alpha", "api", { label: "api", state: "running" }));
    await settle();
    await user.click(await screen.findByRole("button", { name: "Stop all" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitUntilGone(() => screen.queryByRole("dialog"));
    expect(api.callsTo("POST", STOP_ALL)).toEqual([]);
  });

  test("shows the server reason in the dialog when stopping fails", async () => {
    mockApi().action(STOP_ALL, () => {
      throw new MockApiError(500, "supervisor is busy");
    });
    const { user } = renderWithProviders(<ServicesPage />);
    seedServices(makeServiceStatus("alpha", "api", { label: "api", state: "running" }));
    await settle();
    await user.click(await screen.findByRole("button", { name: "Stop all" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Stop all services" }));
    expect((await within(dialog).findByRole("alert")).textContent).toBe("supervisor is busy");
  });
});

describe("ServicesPage machine", () => {
  test("omits the machine panel until the machine state arrives", async () => {
    mockApi();
    renderWithProviders(<ServicesPage />);
    seedServices();
    await screen.findByText("No services declared");
    expect(screen.queryByRole("button", { name: "Sample now" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Ports" })).toBeNull();
  });

  test("shows the hostname, operating system, core count and when it was sampled", async () => {
    mockApi();
    renderWithProviders(<ServicesPage />);
    seedStream({
      machine: makeMachine({
        hostname: "build-box",
        operating_system: "Linux 6.1",
        cpu_count: 8,
        hardware_sampled_at: NOW_SECONDS - 120,
      }),
    });
    expect(await screen.findByRole("heading", { name: "build-box" })).toBeTruthy();
    expect(screen.getByText("Linux 6.1 · 8 cores")).toBeTruthy();
    expect(screen.getByText(timeText("2 min ago"))).toBeTruthy();
  });

  test("leaves out the core count when the machine does not report one", async () => {
    mockApi();
    renderWithProviders(<ServicesPage />);
    seedStream({ machine: makeMachine({ cpu_count: null }) });
    expect(await screen.findByText("Windows 11")).toBeTruthy();
  });

  test("gauges CPU, memory, GPU load and memory, and each disk", async () => {
    mockApi();
    renderWithProviders(<ServicesPage />);
    seedStream({
      machine: makeMachine({
        cpu_percent: 45,
        ram_used: 8 * GIB,
        ram_total: 16 * GIB,
        gpus: [
          {
            name: "RTX 4090",
            load_percent: 80,
            memory_used: 20 * GIB,
            memory_total: 24 * GIB,
          },
        ],
        disks: [{ mount: "C:", free: 100 * GIB, total: 500 * GIB, project_ids: ["alpha", "beta"] }],
      }),
    });
    expect(await screen.findByRole("meter", { name: "CPU 45%" })).toBeTruthy();
    expect(screen.getByText("45%")).toBeTruthy();
    expect(screen.getByRole("meter", { name: "Memory 50%" })).toBeTruthy();
    expect(screen.getByText("8.0 GB of 16.0 GB")).toBeTruthy();
    expect(screen.getByRole("meter", { name: "RTX 4090 80%" })).toBeTruthy();
    expect(screen.getByRole("meter", { name: "RTX 4090 memory 83%" })).toBeTruthy();
    expect(screen.getByText("20.0 GB of 24.0 GB")).toBeTruthy();
    expect(screen.getByRole("meter", { name: "Disk C: 80%" })).toBeTruthy();
    expect(screen.getByText("100 GB free")).toBeTruthy();
    expect(screen.getByText("2 projects here")).toBeTruthy();
  });

  test("skips the gauges for readings the machine did not provide", async () => {
    mockApi();
    renderWithProviders(<ServicesPage />);
    seedStream({
      machine: makeMachine({
        cpu_percent: null,
        ram_used: null,
        ram_total: 0,
        gpus: [{ name: "iGPU", load_percent: null, memory_used: null, memory_total: null }],
        gpu_error: "nvidia-smi not found",
        disks: [{ mount: "E:", free: null, total: null, project_ids: [] }],
      }),
    });
    expect(await screen.findByText("GPU: nvidia-smi not found")).toBeTruthy();
    expect(screen.queryAllByRole("meter")).toEqual([]);
  });

  test("samples the machine on demand", async () => {
    const machine = makeMachine();
    const api = mockApi().action(SAMPLE, machine);
    const { user } = renderWithProviders(<ServicesPage />);
    seedStream({ machine });
    await settle();
    await user.click(await screen.findByRole("button", { name: "Sample now" }));
    await settle();
    expect(api.sent(SAMPLE)).toEqual([{}]);
  });
});

describe("ServicesPage ports", () => {
  test("says the ports have not been sampled yet", async () => {
    mockApi();
    renderWithProviders(<ServicesPage />);
    seedStream({ machine: makeMachine({ ports_sampled_at: null }) });
    expect(await screen.findByText("Not sampled yet.")).toBeTruthy();
  });

  test("lists every listening port with its process, pid and owning project", async () => {
    mockApi();
    const { user, location } = renderWithProviders(<ServicesPage />);
    seedStream({
      projects: makeProjectsState([makeProject("alpha"), makeProject("beta")]),
      machine: makeMachine({
        ports: [
          {
            port: 8000,
            address: "127.0.0.1",
            pid: 4242,
            process: "python.exe",
            project_id: "alpha",
          },
          { port: 5432, address: "0.0.0.0", pid: null, process: null, project_id: null },
        ],
      }),
    });
    await screen.findByText("python.exe");
    const portsTable = screen.getByRole("table");
    const alphaRow = within(portsTable).getByText("8000").closest("tr");
    if (!alphaRow) throw new Error("no 8000 row");
    expect(within(alphaRow).getByText("127.0.0.1")).toBeTruthy();
    expect(within(alphaRow).getByText("(4242)")).toBeTruthy();
    await user.click(within(alphaRow).getByRole("link", { name: "alpha" }));
    expect(location()).toBe("/p/alpha");
    const unknownRow = within(portsTable).getByText("5432").closest("tr");
    if (!unknownRow) throw new Error("no 5432 row");
    expect(within(unknownRow).getByText("?")).toBeTruthy();
    expect(within(unknownRow).queryByRole("link")).toBeNull();
    expect(within(unknownRow).queryByText(/\(/)).toBeNull();
  });

  test("calls out a port with several listeners and a port claimed by two projects", async () => {
    mockApi();
    renderWithProviders(<ServicesPage />);
    seedStream({
      projects: makeProjectsState([makeProject("alpha"), makeProject("beta")]),
      machine: makeMachine({
        conflicts: [
          { port: 3000, kind: "multiple-listeners", pids: [11, 22], project_ids: [] },
          { port: 8000, kind: "claimed-by-projects", pids: [], project_ids: ["alpha", "beta"] },
        ],
      }),
    });
    expect(await screen.findByText("port 3000: 2 processes listen (11, 22)")).toBeTruthy();
    expect(screen.getByText("port 8000: claimed by alpha and beta")).toBeTruthy();
  });
});
