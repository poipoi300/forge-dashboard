import { describe, expect, test } from "bun:test";
import { screen, within } from "@testing-library/react";
import { DiffView } from "@/components/DiffView";
import { installHarness, renderWithProviders } from "./harness";

installHarness();

const TWO_HUNKS_TWO_FILES = `diff --git a/src/app.ts b/src/app.ts
index 1111111..2222222 100644
--- a/src/app.ts
+++ b/src/app.ts
@@ -1,4 +1,4 @@
 import { start } from "./start";
-const port = 8000;
+const port = 8001;

 start(port);
@@ -20,3 +20,4 @@ export function stop() {
   return true;
 }
+// shutdown hook
diff --git a/src/new.txt b/src/new.txt
new file mode 100644
index 0000000..8888888
--- /dev/null
+++ b/src/new.txt
@@ -0,0 +1 @@
+hello
`;

const DELETED_FILE = `diff --git a/gone.txt b/gone.txt
deleted file mode 100644
index 7777777..0000000
--- a/gone.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-first
-second
`;

const PURE_RENAME = `diff --git a/old/name.ts b/new/name.ts
similarity index 100%
rename from old/name.ts
rename to new/name.ts
`;

const BINARY_CHANGE = `diff --git a/logo.png b/logo.png
index 5555555..6666666 100644
Binary files a/logo.png and b/logo.png differ
`;

function section(path: string): HTMLElement {
  const header = screen.getByText(path, { selector: "code" });
  const element = header.closest("section");
  if (!element) throw new Error(`no diff block for ${path}`);
  return element;
}

describe("DiffView", () => {
  test("gives each changed file its own block with its path and kind", () => {
    renderWithProviders(<DiffView diff={TWO_HUNKS_TWO_FILES} />);
    expect(within(section("src/app.ts")).getByText("modify")).toBeTruthy();
    expect(within(section("src/new.txt")).getByText("add")).toBeTruthy();
  });

  test("renders the added, removed and context lines of every hunk", () => {
    renderWithProviders(<DiffView diff={TWO_HUNKS_TWO_FILES} />);
    const block = section("src/app.ts");
    expect(within(block).getByText("const port = 8000;")).toBeTruthy();
    expect(within(block).getByText("const port = 8001;")).toBeTruthy();
    expect(within(block).getByText('import { start } from "./start";')).toBeTruthy();
    expect(within(block).getByText("// shutdown hook")).toBeTruthy();
    expect(within(section("src/new.txt")).getByText("hello")).toBeTruthy();
  });

  test("names a deleted file by the path it had before the deletion", () => {
    renderWithProviders(<DiffView diff={DELETED_FILE} />);
    const block = section("gone.txt");
    expect(within(block).getByText("delete")).toBeTruthy();
    expect(within(block).getByText("first")).toBeTruthy();
    expect(within(block).getByText("second")).toBeTruthy();
  });

  test("shows a rename without content changes as old path to new path", () => {
    renderWithProviders(<DiffView diff={PURE_RENAME} />);
    const block = section("old/name.ts → new/name.ts");
    expect(within(block).getByText("rename")).toBeTruthy();
  });

  test("marks a binary change as having no text to show", () => {
    renderWithProviders(<DiffView diff={BINARY_CHANGE} />);
    expect(within(section("logo.png")).getByText("Binary or mode-only change.")).toBeTruthy();
  });

  test("says there are no changes when the diff is empty", () => {
    renderWithProviders(<DiffView diff="" />);
    expect(screen.getByText("No changes.")).toBeTruthy();
  });

  test("says there are no changes when the diff is only whitespace", () => {
    renderWithProviders(<DiffView diff={"  \n\t\n"} />);
    expect(screen.getByText("No changes.")).toBeTruthy();
  });

  test("uses the caller's words for the empty message", () => {
    renderWithProviders(<DiffView diff="" empty="Nothing staged." />);
    expect(screen.getByText("Nothing staged.")).toBeTruthy();
  });
});
