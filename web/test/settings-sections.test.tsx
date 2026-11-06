import { describe, expect, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import { SettingsPage } from "@/features/settings/SettingsPage";
import { makeProject, makeProjectsState } from "./factories";
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
import {
  DISCOVERY_CONFIG,
  makeBackup,
  makeGalleryState,
  makeGitHubState,
} from "./settings-support";

installHarness();

const SAVE_TOKEN = "/api/services/github/actions/save-token" as const;
const CLEAR_TOKEN = "/api/services/github/actions/clear-token" as const;
const BACKUP = "/api/services/settings/actions/backup" as const;
const EXPORT_REPORT = "/api/services/settings/actions/export-report" as const;
const RESTORE = "/api/services/settings/actions/restore" as const;
const LIST_ALL = "/api/services/projects/actions/list-all" as const;
const SAVE_SETTINGS = "/api/services/projects/actions/save-settings" as const;

async function openSection(
  user: ReturnType<typeof renderWithProviders>["user"],
  name: string,
): Promise<void> {
  const nav = await screen.findByRole("navigation", { name: "Settings sections" });
  await user.click(within(nav).getByRole("button", { name }));
}

function renderSettings() {
  const api = mockApi().config("discovery", { ...DISCOVERY_CONFIG });
  return { api, ...renderWithProviders(<SettingsPage />, { path: "/settings" }) };
}

describe("SettingsPage GitHub sign-in", () => {
  test("shows a loading state until the GitHub state arrives", async () => {
    const { user } = renderSettings();
    await openSection(user, "GitHub sign-in");
    const heading = await screen.findByRole("heading", { name: "GitHub sign-in" });
    const section = heading.closest("section");
    if (!section) throw new Error("no GitHub sign-in section");
    expect(within(section).getByRole("status", { name: "Loading" })).toBeTruthy();
    seedStream({ github: makeGitHubState() });
    expect(await screen.findByText("not signed in")).toBeTruthy();
  });

  test("says where the token came from and how much of the rate limit is left", async () => {
    const { user } = renderSettings();
    seedStream({
      github: makeGitHubState({
        authenticated: true,
        auth_source: "git-credential",
        rate_limit: 5000,
        rate_remaining: 4200,
        rate_reset_at: NOW_SECONDS + 1800,
      }),
    });
    await openSection(user, "GitHub sign-in");
    expect(await screen.findByText("signed in")).toBeTruthy();
    expect(screen.getByText("token from git credential")).toBeTruthy();
    expect(screen.getByText(/4200 of 5000 requests left/)).toBeTruthy();
    expect(screen.getByText(timeText("in 30 min"))).toBeTruthy();
  });

  test("omits the reset time when GitHub did not give one and the limit when it is unknown", async () => {
    const { user } = renderSettings();
    seedStream({
      github: makeGitHubState({ rate_limit: 60, rate_remaining: 12, rate_reset_at: null }),
    });
    await openSection(user, "GitHub sign-in");
    const note = await screen.findByText(/12 of 60 requests left/);
    expect(note.textContent).not.toContain("resets");
    expect(screen.getByText("token from none")).toBeTruthy();
    seedStream({ github: makeGitHubState({ rate_limit: null, rate_remaining: null }) }, 2);
    await waitUntilGone(() => screen.queryByText(/requests left/));
  });

  test("saves a token typed into the dialog", async () => {
    const { api, user } = renderSettings();
    api.action(SAVE_TOKEN, { ok: true, message: "Token saved to the keyring" });
    seedStream({ github: makeGitHubState() });
    await openSection(user, "GitHub sign-in");
    await user.click(await screen.findByRole("button", { name: "Save a token" }));
    const dialog = await screen.findByRole("dialog");
    const token = dialog.querySelector<HTMLInputElement>("[data-field=token] input");
    if (!token) throw new Error("no token input");
    expect(token.type).toBe("password");
    await user.type(token, "ghp_example");
    await user.click(within(dialog).getByRole("button", { name: "Save GitHub token" }));
    expect(await screen.findByText("Token saved to the keyring")).toBeTruthy();
    expect(api.sent(SAVE_TOKEN)).toEqual([{ token: "ghp_example" }]);
    await waitUntilGone(() => screen.queryByRole("dialog"));
  });

  test("forgets the stored token only after confirmation", async () => {
    const { api, user } = renderSettings();
    api.action(CLEAR_TOKEN, { ok: true, message: "Stored token removed" });
    seedStream({ github: makeGitHubState({ authenticated: true, auth_source: "keyring" }) });
    await openSection(user, "GitHub sign-in");
    await user.click(await screen.findByRole("button", { name: "Forget the stored token" }));
    const dialog = await screen.findByRole("dialog");
    expect(api.sent(CLEAR_TOKEN)).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Forget saved GitHub token" }));
    expect(await screen.findByText("Stored token removed")).toBeTruthy();
    expect(api.sent(CLEAR_TOKEN)).toEqual([{}]);
  });
});

describe("SettingsPage backups and reports", () => {
  test("says there are no backups yet", async () => {
    const { user } = renderSettings();
    await openSection(user, "Backups and reports");
    expect(await screen.findByText("No backups yet.")).toBeTruthy();
    seedStream({ settings: { backups: [] } });
    expect(screen.getByText("No backups yet.")).toBeTruthy();
  });

  test("lists each backup with its size and age", async () => {
    const { user } = renderSettings();
    seedStream({
      settings: {
        backups: [
          makeBackup("20260115-110000.sqlite3", {
            size_bytes: 2048,
            created_at: NOW_SECONDS - 3600,
          }),
          makeBackup("20260114-090000.sqlite3", {
            size_bytes: 5 * 1024 * 1024,
            created_at: NOW_SECONDS - 3 * 86_400,
          }),
        ],
      },
    });
    await openSection(user, "Backups and reports");
    const first = (await screen.findByText("20260115-110000.sqlite3")).closest("li");
    const second = screen.getByText("20260114-090000.sqlite3").closest("li");
    if (!first || !second) throw new Error("missing backup rows");
    expect(within(first).getByText(/2\.0 KB/)).toBeTruthy();
    expect(within(first).getByText(timeText("1 h ago"))).toBeTruthy();
    expect(within(second).getByText(/5\.0 MB/)).toBeTruthy();
    expect(within(second).getByText(timeText("3 d ago"))).toBeTruthy();
    expect(screen.queryByText("No backups yet.")).toBeNull();
  });

  test("backs up the dashboard state in one click", async () => {
    const { api, user } = renderSettings();
    api.action(BACKUP, makeBackup("20260115-120000.sqlite3"));
    await openSection(user, "Backups and reports");
    await user.click(await screen.findByRole("button", { name: "Back up now" }));
    expect(await screen.findByText("Back up dashboard state: done")).toBeTruthy();
    expect(api.sent(BACKUP)).toEqual([{}]);
  });

  test("reports a failed backup", async () => {
    const { api, user } = renderSettings();
    api.action(BACKUP, () => {
      throw new MockApiError(500, "backups folder is read-only");
    });
    await openSection(user, "Backups and reports");
    await user.click(await screen.findByRole("button", { name: "Back up now" }));
    expect(await screen.findByText("Back up dashboard state failed")).toBeTruthy();
    expect(screen.getByText("backups folder is read-only")).toBeTruthy();
  });

  test("restores a backup only after its name is typed to confirm", async () => {
    const { api, user } = renderSettings();
    api.action(RESTORE, {
      name: "20260114-090000.sqlite3",
      safety_backup: "20260115-120001.sqlite3",
      services_reloaded: 12,
    });
    seedStream({
      settings: {
        backups: [makeBackup("20260115-110000.sqlite3"), makeBackup("20260114-090000.sqlite3")],
      },
    });
    await openSection(user, "Backups and reports");
    const row = (await screen.findByText("20260114-090000.sqlite3")).closest("li");
    if (!row) throw new Error("missing backup row");
    await user.click(within(row).getByRole("button", { name: "Restore" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("20260114-090000.sqlite3")).toBeTruthy();
    const submit = within(dialog).getByRole("button", {
      name: "Restore backup",
    }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    await user.type(within(dialog).getByRole("textbox"), "20260115-110000.sqlite3");
    expect(submit.disabled).toBe(true);
    await user.clear(within(dialog).getByRole("textbox"));
    await user.type(within(dialog).getByRole("textbox"), "20260114-090000.sqlite3");
    expect(submit.disabled).toBe(false);
    expect(api.sent(RESTORE)).toEqual([]);
    await user.click(submit);
    expect(await screen.findByText("Restore backup: done")).toBeTruthy();
    expect(api.sent(RESTORE)).toEqual([{ name: "20260114-090000.sqlite3" }]);
    await waitUntilGone(() => screen.queryByRole("dialog"));
  });

  test("keeps the restore dialog open with the server reason when the backup is rejected", async () => {
    const { api, user } = renderSettings();
    api.action(RESTORE, () => {
      throw new MockApiError(400, "backup is not a dashboard database");
    });
    seedStream({ settings: { backups: [makeBackup("broken.sqlite3")] } });
    await openSection(user, "Backups and reports");
    await user.click(await screen.findByRole("button", { name: "Restore" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox"), "broken.sqlite3");
    await user.click(within(dialog).getByRole("button", { name: "Restore backup" }));
    expect((await within(dialog).findByRole("alert")).textContent).toBe(
      "backup is not a dashboard database",
    );
  });

  test("exports a Markdown report by default and a JSON one when chosen", async () => {
    const { api, user } = renderSettings();
    api.action(EXPORT_REPORT, ({ format }) => ({
      path: `C:/data/reports/report.${format === "json" ? "json" : "md"}`,
      format,
      size_bytes: 1024,
      projects: 4,
    }));
    await openSection(user, "Backups and reports");
    await user.click(await screen.findByRole("button", { name: "Export report…" }));
    let dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Export report" }));
    expect(await screen.findByText("Export report: done")).toBeTruthy();
    await waitUntilGone(() => screen.queryByRole("dialog"));
    await user.click(screen.getByRole("button", { name: "Export report…" }));
    dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("combobox"));
    await user.click(await screen.findByRole("option", { name: "json" }));
    await user.click(within(dialog).getByRole("button", { name: "Export report" }));
    await waitFor(() => expect(api.sent(EXPORT_REPORT)).toHaveLength(2));
    expect(api.sent(EXPORT_REPORT)).toEqual([{ format: "markdown" }, { format: "json" }]);
  });

  test("links to the Markdown report download", async () => {
    const { user } = renderSettings();
    await openSection(user, "Backups and reports");
    const link = await screen.findByRole("link", { name: "Download report (Markdown)" });
    expect(link.getAttribute("href")).toBe("/api/report.md");
    expect(link.hasAttribute("download")).toBe(true);
  });
});

describe("SettingsPage hidden projects", () => {
  test("says no project is hidden without asking for the project list", async () => {
    const { api, user } = renderSettings();
    seedStream({ projects: makeProjectsState([makeProject("alpha")], { hidden: [] }) });
    await openSection(user, "Hidden projects");
    expect(await screen.findByText("No hidden projects.")).toBeTruthy();
    expect(api.callsTo("POST", LIST_ALL).length).toBe(1);
  });

  test("lists hidden projects by name, falling back to the id, and shows them again", async () => {
    const { api, user } = renderSettings();
    api
      .action(LIST_ALL, {
        projects: [makeProject("alpha", { name: "Alpha" }), makeProject("beta", { name: "Beta" })],
      })
      .action(SAVE_SETTINGS, { hidden: false, editor_command: [] });
    seedStream({ projects: makeProjectsState([], { hidden: ["alpha", "ghost"] }) });
    await openSection(user, "Hidden projects");
    expect(await screen.findByText("Alpha")).toBeTruthy();
    expect(screen.queryByText("Beta")).toBeNull();
    const ghost = screen.getByText("ghost").closest("li");
    if (!ghost) throw new Error("missing ghost row");
    await user.click(within(ghost).getByRole("button", { name: "Show again" }));
    await waitFor(() => expect(api.sent(SAVE_SETTINGS)).toHaveLength(1));
    expect(api.sent(SAVE_SETTINGS)).toEqual([
      { project_id: "ghost", settings: { hidden: false, editor_command: [] } },
    ]);
  });

  test("reports a project list that cannot be loaded", async () => {
    const { api, user } = renderSettings();
    api.action(LIST_ALL, () => {
      throw new MockApiError(500, "registry is rebuilding");
    });
    seedStream({ projects: makeProjectsState([], { hidden: ["alpha"] }) });
    await openSection(user, "Hidden projects");
    expect((await screen.findByRole("alert")).textContent).toContain("registry is rebuilding");
  });
});

describe("SettingsPage widget gallery", () => {
  test("shows the gallery state and a button for each of its actions", async () => {
    const { user } = renderSettings();
    seedStream({ gallery: makeGalleryState({ commit: "deadbee", pings: 7 }) });
    await openSection(user, "Widget gallery");
    expect(
      await screen.findByText(/A service that exists to exercise the schema renderer/),
    ).toBeTruthy();
    expect(await screen.findByText("deadbee")).toBeTruthy();
    const buttons = screen
      .getAllByRole("button")
      .map((button) => button.textContent)
      .filter((text) =>
        ["Ping", "Echo", "Fail", "Reset counter", "Count slowly"].includes(text ?? ""),
      );
    expect(buttons.sort()).toEqual(["Count slowly", "Echo", "Fail", "Ping", "Reset counter"]);
  });

  test("runs a gallery action and reports its message", async () => {
    const { api, user } = renderSettings();
    api.action("/api/services/gallery/actions/ping", { ok: true, message: "pong" });
    seedStream({ gallery: makeGalleryState() });
    await openSection(user, "Widget gallery");
    await user.click(await screen.findByRole("button", { name: "Ping" }));
    expect(await screen.findByText("pong")).toBeTruthy();
    expect(api.sent("/api/services/gallery/actions/ping")).toEqual([{}]);
  });

  test("shows the action buttons before the gallery state has arrived", async () => {
    const { user } = renderSettings();
    await openSection(user, "Widget gallery");
    expect(await screen.findByRole("button", { name: "Ping" })).toBeTruthy();
    expect(screen.queryByText("1a2b3c4")).toBeNull();
    await settle();
  });
});
