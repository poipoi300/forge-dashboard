import { describe, expect, test } from "bun:test";
import { basename, bytes, count, homeRelative, percent } from "@/lib/format";
import { installHarness } from "./harness";

installHarness();

describe("bytes", () => {
  test.each<[number, string]>([
    [0, "0 B"],
    [1023, "1023 B"],
    [1024, "1.0 KB"],
    [1536, "1.5 KB"],
    [102_300, "99.9 KB"],
    [102_400, "100 KB"],
    [1_048_576, "1.0 MB"],
    [5_368_709_120, "5.0 GB"],
    [1_099_511_627_776, "1.0 TB"],
  ])("formats %d bytes as %s", (value, expected) => {
    expect(bytes(value)).toBe(expected);
  });

  test("stops at terabytes rather than inventing a larger unit", () => {
    expect(bytes(1024 ** 5)).toBe("1024 TB");
  });
});

describe("percent", () => {
  test("rounds to whole percent by default", () => {
    expect(percent(42.567)).toBe("43%");
    expect(percent(0)).toBe("0%");
  });

  test("keeps the requested number of decimals", () => {
    expect(percent(42.567, 1)).toBe("42.6%");
    expect(percent(100, 2)).toBe("100.00%");
  });
});

describe("count", () => {
  test.each<[number, string]>([
    [0, "0"],
    [999, "999"],
    [1284, "1,284"],
    [9999, "9,999"],
    [10_000, "10K"],
    [12_900, "12.9K"],
    [4_200_000, "4.2M"],
    [-12_000, "-12K"],
  ])("shows %d as %s", (value, expected) => {
    expect(count(value)).toBe(expected);
  });
});

describe("basename", () => {
  test("returns the last segment of a Windows path, ignoring a trailing separator", () => {
    expect(basename("C:\\code\\alpha\\")).toBe("alpha");
  });

  test("returns the last segment of a POSIX path", () => {
    expect(basename("/srv/forge/web/")).toBe("web");
  });

  test("returns a bare name unchanged", () => {
    expect(basename("notes.md")).toBe("notes.md");
  });

  test("returns an empty path unchanged", () => {
    expect(basename("")).toBe("");
  });
});

describe("homeRelative", () => {
  test("shortens a Windows home directory to ~ with forward slashes", () => {
    expect(homeRelative("C:\\Users\\Administrator\\Documents\\forge")).toBe("~/Documents/forge");
  });

  test("shortens a macOS home directory", () => {
    expect(homeRelative("/Users/ada/projects/x")).toBe("~/projects/x");
  });

  test("shortens a Linux home directory", () => {
    expect(homeRelative("/home/ada/src")).toBe("~/src");
  });

  test("shows the home directory itself as ~", () => {
    expect(homeRelative("C:/Users/Administrator")).toBe("~");
  });

  test("leaves paths outside a home directory as they are", () => {
    expect(homeRelative("/opt/forge")).toBe("/opt/forge");
    expect(homeRelative("D:/work/forge")).toBe("D:/work/forge");
  });
});
