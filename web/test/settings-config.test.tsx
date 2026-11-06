import { describe, expect, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import { SettingsPage } from "@/features/settings/SettingsPage";
import {
  formField,
  installHarness,
  MockApiError,
  mockApi,
  renderWithProviders,
  settle,
} from "./harness";
import { DISCOVERY_CONFIG, routeConfig } from "./settings-support";

installHarness();

const DISCOVERY_PATH = "/api/services/discovery/config";

function saveButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
}

async function openSection(
  user: ReturnType<typeof renderWithProviders>["user"],
  name: string,
): Promise<void> {
  const nav = screen.getByRole("navigation", { name: "Settings sections" });
  await user.click(within(nav).getByRole("button", { name }));
}

async function renderDiscovery(
  saved?: (body: unknown) => Record<string, unknown> | Promise<Record<string, unknown>>,
): Promise<{ api: ReturnType<typeof mockApi> } & ReturnType<typeof renderWithProviders>> {
  const api = routeConfig(mockApi(), "discovery", { ...DISCOVERY_CONFIG }, saved);
  const rendered = renderWithProviders(<SettingsPage />, { path: "/settings" });
  await screen.findByRole("heading", { name: "Discovery" });
  await screen.findByRole("button", { name: "Save" });
  return { api, ...rendered };
}

describe("SettingsPage config form", () => {
  test("shows a loading state until the service descriptors arrive", async () => {
    routeConfig(mockApi(), "discovery", { ...DISCOVERY_CONFIG });
    renderWithProviders(<SettingsPage />, { path: "/settings" });
    expect(screen.getByRole("status", { name: "Loading" })).toBeTruthy();
    expect(await screen.findByRole("heading", { name: "Discovery" })).toBeTruthy();
  });

  test("opens the Discovery section by default with its description and grouped fields", async () => {
    const { api } = await renderDiscovery();
    expect(
      screen.getByText(
        "Finds forge projects under the configured roots and watches them for changes.",
      ),
    ).toBeTruthy();
    const groups = screen
      .getAllByRole("group")
      .map((group) => group.querySelector("legend")?.textContent);
    expect(groups).toEqual(["Scan", "Projects", "Standards source"]);
    expect(api.callsTo("GET", DISCOVERY_PATH)).toHaveLength(1);
    const rescan = formField("rescan_hours").getByRole("spinbutton") as HTMLInputElement;
    expect(rescan.value).toBe("24");
    expect((formField("roots").getAllByRole("textbox")[0] as HTMLInputElement).value).toBe(
      "C:/code",
    );
    expect(
      formField("forge_repo").getByText("Overrides the auto-detected forge repo location."),
    ).toBeTruthy();
  });

  test("keeps Save disabled and Revert hidden until something changes", async () => {
    await renderDiscovery();
    expect(saveButton().disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "Revert" })).toBeNull();
  });

  test("enables Save and Revert on an edit and Revert restores the saved value", async () => {
    const { user } = await renderDiscovery();
    const rescan = formField("rescan_hours").getByRole("spinbutton") as HTMLInputElement;
    await user.clear(rescan);
    await user.type(rescan, "48");
    expect(rescan.value).toBe("48");
    expect(saveButton().disabled).toBe(false);
    await user.click(screen.getByRole("button", { name: "Revert" }));
    expect(rescan.value).toBe("24");
    expect(saveButton().disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "Revert" })).toBeNull();
  });

  test("treats an edit that returns to the saved value as unchanged", async () => {
    const { user } = await renderDiscovery();
    const rescan = formField("rescan_hours").getByRole("spinbutton") as HTMLInputElement;
    await user.clear(rescan);
    await user.type(rescan, "25");
    expect(saveButton().disabled).toBe(false);
    await user.clear(rescan);
    await user.type(rescan, "24");
    expect(saveButton().disabled).toBe(true);
  });

  test("saves the whole edited config, confirms it and settles back to clean", async () => {
    const { api, user } = await renderDiscovery();
    const rescan = formField("rescan_hours").getByRole("spinbutton") as HTMLInputElement;
    await user.clear(rescan);
    await user.type(rescan, "48");
    await user.click(formField("roots").getByRole("button", { name: "Add folder" }));
    const rootInputs = formField("roots").getAllByRole("textbox");
    await user.type(rootInputs[1] as HTMLElement, "D:/work");
    await user.type(formField("forge_repo").getByRole("textbox"), "C:/code/forge");
    await user.click(saveButton());
    expect(await screen.findByText("Discovery settings saved")).toBeTruthy();
    expect(api.callsTo("PUT", DISCOVERY_PATH).map((call) => call.body)).toEqual([
      {
        roots: ["C:/code", "D:/work"],
        max_depth: 6,
        rescan_hours: 48,
        extra_projects: [],
        ignored: [],
        forge_repo: "C:/code/forge",
      },
    ]);
    await waitFor(() => expect(saveButton().disabled).toBe(true));
    expect(screen.queryByRole("button", { name: "Revert" })).toBeNull();
  });

  test("shows the values the server stored when it normalizes the config", async () => {
    const { user } = await renderDiscovery((body) => ({
      ...(body as Record<string, unknown>),
      forge_repo: "C:/code/forge",
    }));
    const forgeRepo = formField("forge_repo").getByRole("textbox") as HTMLInputElement;
    await user.type(forgeRepo, "c:/code/forge/");
    await user.click(saveButton());
    await screen.findByText("Discovery settings saved");
    await waitFor(() => expect(forgeRepo.value).toBe("C:/code/forge"));
    expect(saveButton().disabled).toBe(true);
  });

  test("disables Save while the request is in flight", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const { user } = await renderDiscovery(async (body) => {
      await gate;
      return body as Record<string, unknown>;
    });
    const rescan = formField("rescan_hours").getByRole("spinbutton") as HTMLInputElement;
    await user.clear(rescan);
    await user.type(rescan, "36");
    await user.click(saveButton());
    expect(saveButton().disabled).toBe(true);
    release();
    expect(await screen.findByText("Discovery settings saved")).toBeTruthy();
  });

  test("shows the server's validation problems and keeps the edit for another try", async () => {
    let attempts = 0;
    const { api, user } = await renderDiscovery((body) => {
      attempts += 1;
      if (attempts === 1)
        throw new MockApiError(422, [
          { msg: "rescan_hours must be at most 720" },
          { msg: "roots must exist" },
        ]);
      return body as Record<string, unknown>;
    });
    const rescan = formField("rescan_hours").getByRole("spinbutton") as HTMLInputElement;
    await user.clear(rescan);
    await user.type(rescan, "300");
    await user.click(saveButton());
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("rescan_hours must be at most 720; roots must exist");
    expect(rescan.value).toBe("300");
    expect(saveButton().disabled).toBe(false);
    await user.clear(rescan);
    await user.type(rescan, "100");
    await user.click(saveButton());
    expect(await screen.findByText("Discovery settings saved")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(api.callsTo("PUT", DISCOVERY_PATH)).toHaveLength(2);
  });

  test("shows a plain-text server error", async () => {
    const { user } = await renderDiscovery(() => {
      throw new MockApiError(500, "database is locked");
    });
    const rescan = formField("rescan_hours").getByRole("spinbutton") as HTMLInputElement;
    await user.clear(rescan);
    await user.type(rescan, "30");
    await user.click(saveButton());
    expect((await screen.findByRole("alert")).textContent).toBe("database is locked");
  });

  test("reports a config that cannot be loaded and retries it", async () => {
    let attempts = 0;
    mockApi()
      .config("discovery", {})
      .route("GET", DISCOVERY_PATH, () => {
        attempts += 1;
        if (attempts === 1) throw new MockApiError(500, "config file is corrupt");
        return { ...DISCOVERY_CONFIG };
      });
    const { user } = renderWithProviders(<SettingsPage />, { path: "/settings" });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("config file is corrupt");
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("button", { name: "Save" })).toBeTruthy();
    expect(formField("rescan_hours").getByRole("spinbutton")).toBeTruthy();
  });
});

describe("SettingsPage service sections", () => {
  test("lists the services that have configuration, then the fixed sections, with the open one current", async () => {
    await renderDiscovery();
    const nav = screen.getByRole("navigation", { name: "Settings sections" });
    const labels = within(nav)
      .getAllByRole("button")
      .map((button) => button.textContent);
    expect(labels.slice(-4)).toEqual([
      "GitHub sign-in",
      "Backups and reports",
      "Hidden projects",
      "Widget gallery",
    ]);
    expect(labels).toContain("Jobs");
    expect(labels).toContain("Discovery");
    expect(labels).not.toContain("Manifests");
    expect(labels).not.toContain("Schema gallery");
    expect(
      within(nav).getByRole("button", { name: "Discovery" }).getAttribute("aria-current"),
    ).toBe("page");
    expect(
      within(nav).getByRole("button", { name: "Jobs" }).getAttribute("aria-current"),
    ).toBeNull();
  });

  test("switches to another service's config and loads it from its own route", async () => {
    const api = routeConfig(mockApi(), "discovery", { ...DISCOVERY_CONFIG });
    routeConfig(api, "jobs", { agent_concurrency: 1, check_concurrency: 2 });
    const { user } = renderWithProviders(<SettingsPage />, { path: "/settings" });
    await screen.findByRole("heading", { name: "Discovery" });
    await openSection(user, "Jobs");
    expect(await screen.findByRole("heading", { name: "Jobs" })).toBeTruthy();
    expect(
      screen.getByText(
        "Long-running work with live logs, resource limits, cancellation, and history.",
      ),
    ).toBeTruthy();
    expect(await screen.findByText("Agent runs at once.")).toBeTruthy();
    expect(formField("check_concurrency").getByText("2")).toBeTruthy();
    expect(api.callsTo("GET", "/api/services/jobs/config")).toHaveLength(1);
    expect(
      screen
        .getByRole("navigation", { name: "Settings sections" })
        .querySelector("[aria-current=page]")?.textContent,
    ).toBe("Jobs");
  });

  test("saves a slider edit with the changed number", async () => {
    const api = routeConfig(mockApi(), "discovery", { ...DISCOVERY_CONFIG });
    routeConfig(api, "jobs", { agent_concurrency: 1, check_concurrency: 2 });
    const { user } = renderWithProviders(<SettingsPage />, { path: "/settings" });
    await screen.findByRole("heading", { name: "Discovery" });
    await openSection(user, "Jobs");
    await screen.findByText("Agent runs at once.");
    const thumb = formField("agent_concurrency").getByRole("slider");
    thumb.focus();
    await user.keyboard("{ArrowRight}{ArrowRight}");
    expect(formField("agent_concurrency").getByText("3")).toBeTruthy();
    await user.click(saveButton());
    expect(await screen.findByText("Jobs settings saved")).toBeTruthy();
    expect(api.callsTo("PUT", "/api/services/jobs/config").map((call) => call.body)).toEqual([
      { agent_concurrency: 3, check_concurrency: 2 },
    ]);
  });

  test("drops unsaved edits when another section is opened and comes back", async () => {
    const api = routeConfig(mockApi(), "discovery", { ...DISCOVERY_CONFIG });
    routeConfig(api, "jobs", { agent_concurrency: 1, check_concurrency: 2 });
    const { user } = renderWithProviders(<SettingsPage />, { path: "/settings" });
    await screen.findByRole("button", { name: "Save" });
    const rescan = formField("rescan_hours").getByRole("spinbutton") as HTMLInputElement;
    await user.clear(rescan);
    await user.type(rescan, "99");
    await openSection(user, "Jobs");
    await screen.findByText("Agent runs at once.");
    await openSection(user, "Discovery");
    await screen.findByRole("button", { name: "Save" });
    const again = formField("rescan_hours").getByRole("spinbutton") as HTMLInputElement;
    expect(again.value).toBe("24");
    expect(saveButton().disabled).toBe(true);
  });

  test("adds a launcher command as a tag and saves it as a list entry", async () => {
    const api = routeConfig(mockApi(), "discovery", { ...DISCOVERY_CONFIG });
    routeConfig(api, "settings", { editor: [], terminal: ["wt"], file_manager: [] });
    const { user } = renderWithProviders(<SettingsPage />, { path: "/settings" });
    await screen.findByRole("heading", { name: "Discovery" });
    await openSection(user, "Settings");
    await screen.findByText(
      "Command that opens a terminal; it also starts in the folder, so {path} may be omitted.",
    );
    await user.type(formField("editor").getByRole("textbox"), "zed{Enter}");
    expect(formField("editor").getByText("zed")).toBeTruthy();
    await user.click(formField("terminal").getByRole("button", { name: "Remove wt" }));
    await user.click(saveButton());
    expect(await screen.findByText("Settings settings saved")).toBeTruthy();
    expect(api.callsTo("PUT", "/api/services/settings/config").map((call) => call.body)).toEqual([
      { editor: ["zed"], terminal: [], file_manager: [] },
    ]);
  });

  test("hides dependent fields while their controlling switch is off", async () => {
    const api = routeConfig(mockApi(), "discovery", { ...DISCOVERY_CONFIG });
    routeConfig(api, "events", {
      desktop_notifications: true,
      ci_failure: true,
      fork_conflict: true,
      fork_failure: true,
      fork_success: false,
      scheduled_failure: true,
      check_regression: true,
      service_crash: true,
      retention_days: 90,
    });
    const { user } = renderWithProviders(<SettingsPage />, { path: "/settings" });
    await screen.findByRole("heading", { name: "Discovery" });
    await openSection(user, "Events");
    await screen.findByText("CI failed on a project's default branch.");
    await user.click(formField("desktop_notifications").getByRole("switch"));
    await settle();
    expect(document.querySelector('[data-field="ci_failure"]')).toBeNull();
    expect(saveButton().disabled).toBe(false);
    await user.click(saveButton());
    await screen.findByText("Events settings saved");
    expect(api.callsTo("PUT", "/api/services/events/config")[0]?.body).toMatchObject({
      desktop_notifications: false,
    });
  });
});
