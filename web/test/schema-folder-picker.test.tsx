import { describe, expect, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { FolderPicker } from "@/components/FolderPicker";
import { installHarness, MockApiError, mockApi, renderWithProviders } from "./harness";
import { BROWSE, browseTree, folderEntry, folderListing } from "./schema-support";

installHarness();

const MACHINE = folderListing(null, null, [folderEntry("C:", "C:/"), folderEntry("D:", "D:/")]);
const TREE = {
  "C:/": folderListing("C:/", null, [folderEntry("Users", "C:/Users")]),
  "C:/Users": folderListing("C:/Users", "C:/", [folderEntry("me", "C:/Users/me")]),
  "C:/Users/me": folderListing("C:/Users/me", "C:/Users", [
    folderEntry("forge", "C:/Users/me/forge", true),
    folderEntry("notes", "C:/Users/me/notes"),
  ]),
  "C:/Users/me/notes": folderListing("C:/Users/me/notes", "C:/Users/me", []),
};

function Host({
  start,
  picked,
  closed,
  initiallyOpen = true,
}: {
  start: string | null;
  picked: string[];
  closed: boolean[];
  initiallyOpen?: boolean;
}) {
  const [open, setOpen] = useState(initiallyOpen);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Reopen
      </button>
      <FolderPicker
        open={open}
        start={start}
        onOpenChange={(next) => {
          closed.push(next);
          setOpen(next);
        }}
        onPick={(path) => picked.push(path)}
      />
    </>
  );
}

function renderPicker(start: string | null = null, initiallyOpen = true) {
  const picked: string[] = [];
  const closed: boolean[] = [];
  const rendered = renderWithProviders(
    <Host start={start} picked={picked} closed={closed} initiallyOpen={initiallyOpen} />,
    { dialogHost: false },
  );
  return { ...rendered, picked, closed };
}

async function dialog() {
  return within(await screen.findByRole("dialog"));
}

describe("FolderPicker", () => {
  test("asks the server for nothing while closed", async () => {
    const api = mockApi();
    renderPicker(null, false);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(api.callsTo("POST", BROWSE)).toEqual([]);
  });

  test("starts at the machine's roots when no folder is given", async () => {
    const api = mockApi().action(BROWSE, browseTree(TREE, MACHINE));
    renderPicker();
    const view = await dialog();
    expect(await view.findByRole("button", { name: "C:" })).toBeTruthy();
    expect(view.getByRole("button", { name: "D:" })).toBeTruthy();
    expect(view.getByText("This machine")).toBeTruthy();
    expect(view.queryByRole("button", { name: "Up" })).toBeNull();
    expect(
      (view.getByRole("button", { name: "Use this folder" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(api.sent(BROWSE)).toEqual([{ path: null }]);
    expect(api.unrouted).toEqual([]);
  });

  test("starts in the given folder and shows its path relative to home", async () => {
    const api = mockApi().action(BROWSE, browseTree(TREE, MACHINE));
    renderPicker("C:/Users/me");
    const view = await dialog();
    expect(await view.findByRole("button", { name: "forge" })).toBeTruthy();
    expect(view.getByText("~")).toBeTruthy();
    expect(api.sent(BROWSE)).toEqual([{ path: "C:/Users/me" }]);
  });

  test("opens a folder when clicked and goes back up with the Up row", async () => {
    const api = mockApi().action(BROWSE, browseTree(TREE, MACHINE));
    const { user } = renderPicker();
    const view = await dialog();
    await user.click(await view.findByRole("button", { name: "C:" }));
    await user.click(await view.findByRole("button", { name: "Users" }));
    await user.click(await view.findByRole("button", { name: "me" }));
    expect(await view.findByRole("button", { name: "notes" })).toBeTruthy();
    expect(view.getByText("~")).toBeTruthy();
    await user.click(view.getByRole("button", { name: "Up" }));
    expect(await view.findByRole("button", { name: "me" })).toBeTruthy();
    expect(api.sent(BROWSE)).toEqual([
      { path: null },
      { path: "C:/" },
      { path: "C:/Users" },
      { path: "C:/Users/me" },
      { path: "C:/Users" },
    ]);
  });

  test("goes up from a drive back to the machine's roots", async () => {
    mockApi().action(BROWSE, browseTree(TREE, MACHINE));
    const { user } = renderPicker("C:/");
    const view = await dialog();
    await user.click(await view.findByRole("button", { name: "Up" }));
    expect(await view.findByRole("button", { name: "D:" })).toBeTruthy();
    expect(view.getByText("This machine")).toBeTruthy();
  });

  test("hands back the folder being browsed and closes nothing by itself", async () => {
    mockApi().action(BROWSE, browseTree(TREE, MACHINE));
    const { user, picked, closed } = renderPicker("C:/Users/me");
    const view = await dialog();
    await user.click(await view.findByRole("button", { name: "notes" }));
    await view.findByText("No subfolders.");
    await user.click(view.getByRole("button", { name: "Use this folder" }));
    expect(picked).toEqual(["C:/Users/me/notes"]);
    expect(closed).toEqual([]);
  });

  test("says when a folder has no subfolders but still offers the way up", async () => {
    mockApi().action(BROWSE, browseTree(TREE, MACHINE));
    renderPicker("C:/Users/me/notes");
    const view = await dialog();
    expect(await view.findByText("No subfolders.")).toBeTruthy();
    expect(view.getByRole("button", { name: "Up" })).toBeTruthy();
  });

  test("shows the server's reason when a folder cannot be listed", async () => {
    mockApi().action(BROWSE, () => {
      throw new MockApiError(403, "permission denied");
    });
    renderPicker("C:/locked");
    const view = await dialog();
    expect((await view.findByText(/permission denied/)).textContent).toContain("permission denied");
    expect(view.queryByText("No subfolders.")).toBeNull();
  });

  test("closes with Cancel, picking nothing", async () => {
    mockApi().action(BROWSE, browseTree(TREE, MACHINE));
    const { user, picked, closed } = renderPicker("C:/Users");
    const view = await dialog();
    await view.findByRole("button", { name: "me" });
    await user.click(view.getByRole("button", { name: "Cancel" }));
    expect(closed).toEqual([false]);
    expect(picked).toEqual([]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  test("closes on Escape", async () => {
    mockApi().action(BROWSE, browseTree(TREE, MACHINE));
    const { user, closed } = renderPicker("C:/Users");
    await (await dialog()).findByRole("button", { name: "me" });
    await user.keyboard("{Escape}");
    expect(closed).toEqual([false]);
  });

  test("returns to the starting folder each time it is reopened", async () => {
    const api = mockApi().action(BROWSE, browseTree(TREE, MACHINE));
    const { user } = renderPicker("C:/Users");
    let view = await dialog();
    await user.click(await view.findByRole("button", { name: "me" }));
    await view.findByRole("button", { name: "notes" });
    await user.click(view.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await user.click(screen.getByRole("button", { name: "Reopen" }));
    view = await dialog();
    expect(await view.findByRole("button", { name: "me" })).toBeTruthy();
    expect(view.queryByRole("button", { name: "notes" })).toBeNull();
    expect(api.sent(BROWSE).at(-1)).toEqual({ path: "C:/Users" });
  });
});
