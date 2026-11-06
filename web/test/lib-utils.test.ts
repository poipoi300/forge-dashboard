import { describe, expect, test } from "bun:test";
import { cn } from "@/lib/utils";
import { installHarness } from "./harness";

installHarness();

describe("cn", () => {
  test("joins class names and skips empty or false conditions", () => {
    expect(cn("text-sm", false, undefined, null, "font-bold")).toBe("text-sm font-bold");
  });

  test("accepts conditional object and array forms", () => {
    expect(cn(["rounded-md", { "border-rule": true, hidden: false }])).toBe(
      "rounded-md border-rule",
    );
  });

  test("keeps the later of two conflicting utilities", () => {
    expect(cn("px-2 py-1", "p-4")).toBe("p-4");
  });
});
