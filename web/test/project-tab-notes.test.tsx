import { describe, expect, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import type { Schemas } from "@/api/client";
import { NOW_SECONDS } from "./constants";
import { installHarness, MockApiError, mockApi, settle } from "./harness";
import { deferred, openProjectTab } from "./project-core-support";

installHarness();

const NOTES = "/api/services/projects/actions/notes" as const;
const SAVE_NOTES = "/api/services/projects/actions/save-notes" as const;

type Notes = Schemas["Notes"];

async function openNotes(notes: Notes) {
  const api = mockApi()
    .action(NOTES, notes)
    .action(SAVE_NOTES, (input) => ({ markdown: input.markdown, updated_at: NOW_SECONDS }));
  const opened = await openProjectTab("notes", {}, api);
  const editor = await screen.findByRole("textbox", { name: "Markdown" });
  return { ...opened, editor: editor as HTMLTextAreaElement };
}

function saveButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: "Save notes" }) as HTMLButtonElement;
}

describe("NotesTab loading", () => {
  test("asks for this project's notes", async () => {
    const { api } = await openNotes({ markdown: "", updated_at: null });
    expect(api.sent(NOTES)).toEqual([{ project_id: "alpha" }]);
  });

  test("shows loading rows until the notes arrive", async () => {
    const gate = deferred<Notes>();
    const api = mockApi().action(NOTES, () => gate.promise);
    await openProjectTab("notes", {}, api);
    expect(screen.getByRole("status", { name: "Loading" })).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: "Markdown" })).toBeNull();
    gate.resolve({ markdown: "hello", updated_at: null });
    expect(await screen.findByDisplayValue("hello")).toBeTruthy();
  });

  test("shows why the notes failed to load and retries on request", async () => {
    let failing = true;
    const api = mockApi().action(NOTES, () => {
      if (failing) throw new MockApiError(500, "state file unreadable");
      return { markdown: "recovered", updated_at: null };
    });
    const { rendered } = await openProjectTab("notes", {}, api);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("state file unreadable");
    failing = false;
    await rendered.user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByDisplayValue("recovered")).toBeTruthy();
  });
});

describe("NotesTab editor", () => {
  test("offers an empty editor for a project that never had notes", async () => {
    const { editor } = await openNotes({ markdown: "", updated_at: null });
    expect(editor.value).toBe("");
    expect(screen.getByText("never saved")).toBeTruthy();
    expect(screen.getByText("Nothing written yet.")).toBeTruthy();
    expect(saveButton().disabled).toBe(true);
  });

  test("shows saved notes beside a rendered preview and when they were saved", async () => {
    const { editor } = await openNotes({
      markdown: "# Decisions\n\nSee [the plan](https://example.com/plan).",
      updated_at: NOW_SECONDS - 3600,
    });
    expect(editor.value).toBe("# Decisions\n\nSee [the plan](https://example.com/plan).");
    expect(screen.getByRole("heading", { name: "Decisions" })).toBeTruthy();
    const link = screen.getByRole("link", { name: "the plan" });
    expect(link.getAttribute("href")).toBe("https://example.com/plan");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(screen.getByText("saved", { exact: false }).textContent).toBe("saved 1 h ago");
    expect(saveButton().disabled).toBe(true);
  });

  test("marks edits as unsaved, enables saving and updates the preview as you type", async () => {
    const { editor, rendered } = await openNotes({ markdown: "", updated_at: null });
    await rendered.user.type(editor, "## Next steps");
    expect(screen.getByText("unsaved changes")).toBeTruthy();
    expect(saveButton().disabled).toBe(false);
    expect(screen.getByRole("heading", { name: "Next steps" })).toBeTruthy();
    expect(screen.queryByText("Nothing written yet.")).toBeNull();
  });

  test("treats whitespace-only text as nothing written", async () => {
    const { editor, rendered } = await openNotes({ markdown: "", updated_at: null });
    await rendered.user.type(editor, "   ");
    expect(screen.getByText("Nothing written yet.")).toBeTruthy();
    expect(saveButton().disabled).toBe(false);
  });

  test("returns to saved once the edit is reverted by hand", async () => {
    const { editor, rendered } = await openNotes({ markdown: "abc", updated_at: null });
    await rendered.user.type(editor, "d");
    expect(saveButton().disabled).toBe(false);
    await rendered.user.type(editor, "{Backspace}");
    expect(saveButton().disabled).toBe(true);
    expect(screen.getByText("never saved")).toBeTruthy();
  });

  test("replaces the draft when the saved notes change on the server", async () => {
    const { editor, rendered, api } = await openNotes({ markdown: "first", updated_at: null });
    await rendered.user.type(editor, " draft");
    api.action(NOTES, { markdown: "rewritten elsewhere", updated_at: NOW_SECONDS - 60 });
    await rendered.queryClient.invalidateQueries();
    await waitFor(() => expect(editor.value).toBe("rewritten elsewhere"));
    expect(screen.getByText("saved", { exact: false }).textContent).toBe("saved 1 min ago");
  });
});

describe("NotesTab saving", () => {
  test("sends the draft with the project id and confirms with a toast", async () => {
    const { editor, rendered, api } = await openNotes({ markdown: "", updated_at: null });
    await rendered.user.type(editor, "Ship on Friday");
    await rendered.user.click(saveButton());
    expect(await screen.findByText("Notes saved")).toBeTruthy();
    expect(api.sent(SAVE_NOTES)).toEqual([{ project_id: "alpha", markdown: "Ship on Friday" }]);
  });

  test("leaves the unsaved state once the save lands and shows when it was saved", async () => {
    let stored: Notes = { markdown: "", updated_at: null };
    const api = mockApi()
      .action(NOTES, () => stored)
      .action(SAVE_NOTES, (input) => {
        stored = { markdown: input.markdown, updated_at: NOW_SECONDS };
        return stored;
      });
    const { rendered } = await openProjectTab("notes", {}, api);
    const editor = (await screen.findByRole("textbox", {
      name: "Markdown",
    })) as HTMLTextAreaElement;
    await rendered.user.type(editor, "Ship on Friday");
    expect(screen.getByText("unsaved changes")).toBeTruthy();
    await rendered.user.click(saveButton());
    expect(await screen.findByText("Notes saved")).toBeTruthy();
    await waitFor(() => expect(screen.queryByText("unsaved changes")).toBeNull());
    expect(saveButton().disabled).toBe(true);
    expect(screen.getByText(/^saved/).textContent).toBe("saved just now");
  });

  test("saves with Ctrl+S while editing", async () => {
    const { editor, rendered, api } = await openNotes({ markdown: "", updated_at: null });
    await rendered.user.type(editor, "quick note");
    await rendered.user.keyboard("{Control>}s{/Control}");
    expect(await screen.findByText("Notes saved")).toBeTruthy();
    expect(api.sent(SAVE_NOTES)).toEqual([{ project_id: "alpha", markdown: "quick note" }]);
    expect(editor.value).toBe("quick note");
  });

  test("saves with Cmd+S too", async () => {
    const { editor, rendered, api } = await openNotes({ markdown: "", updated_at: null });
    await rendered.user.type(editor, "mac note");
    await rendered.user.keyboard("{Meta>}s{/Meta}");
    expect(await screen.findByText("Notes saved")).toBeTruthy();
    expect(api.sent(SAVE_NOTES)).toEqual([{ project_id: "alpha", markdown: "mac note" }]);
  });

  test("ignores Ctrl+S when nothing changed", async () => {
    const { editor, rendered, api } = await openNotes({ markdown: "stable", updated_at: null });
    await rendered.user.click(editor);
    await rendered.user.keyboard("{Control>}s{/Control}");
    await settle();
    expect(api.callsTo("POST", SAVE_NOTES)).toEqual([]);
  });

  test("ignores other shortcuts and plain typing of s", async () => {
    const { editor, rendered, api } = await openNotes({ markdown: "", updated_at: null });
    await rendered.user.type(editor, "s");
    await rendered.user.keyboard("{Control>}a{/Control}");
    await settle();
    expect(api.callsTo("POST", SAVE_NOTES)).toEqual([]);
    expect(editor.value).toBe("s");
  });

  test("disables saving while the request is in flight", async () => {
    const gate = deferred<Notes>();
    const { editor, rendered, api } = await openNotes({ markdown: "", updated_at: null });
    api.action(SAVE_NOTES, () => gate.promise);
    await rendered.user.type(editor, "slow note");
    await rendered.user.click(saveButton());
    await waitFor(() => expect(api.callsTo("POST", SAVE_NOTES)).toHaveLength(1));
    expect(saveButton().disabled).toBe(true);
    gate.resolve({ markdown: "slow note", updated_at: NOW_SECONDS });
    expect(await screen.findByText("Notes saved")).toBeTruthy();
    await waitFor(() => expect(saveButton().disabled).toBe(false));
  });

  test("keeps the draft and reports the server's reason when saving fails", async () => {
    const { editor, rendered, api } = await openNotes({ markdown: "", updated_at: null });
    api.action(SAVE_NOTES, () => {
      throw new MockApiError(507, "disk is full");
    });
    await rendered.user.type(editor, "precious words");
    await rendered.user.click(saveButton());
    expect(await screen.findByText("Notes were not saved")).toBeTruthy();
    expect(screen.getByText("disk is full")).toBeTruthy();
    expect(editor.value).toBe("precious words");
    expect(screen.getByText("unsaved changes")).toBeTruthy();
    await waitFor(() => expect(saveButton().disabled).toBe(false));
  });

  test("reports an unexpected failure by its description", async () => {
    const { editor, rendered, api } = await openNotes({ markdown: "", updated_at: null });
    api.action(SAVE_NOTES, () => {
      throw new Error("connection reset");
    });
    await rendered.user.type(editor, "words");
    await rendered.user.click(saveButton());
    expect(await screen.findByText("Notes were not saved")).toBeTruthy();
    expect(screen.getByText("Error: connection reset")).toBeTruthy();
  });
});
