import { expect, test } from "bun:test";
import { screen } from "@testing-library/react";
import { useProjectLabel } from "@/features/project/names";
import { makeProject, makeProjectsState } from "./factories";
import { installHarness, mockApi, renderWithProviders, seedStream } from "./harness";

installHarness();

function Labels({ ids }: { ids: (string | null | undefined)[] }) {
  const labelOf = useProjectLabel();
  return (
    <ul>
      {ids.map((id) => (
        <li key={String(id)}>{`[${labelOf(id)}]`}</li>
      ))}
    </ul>
  );
}

function renderedLabels(): string[] {
  return screen.getAllByRole("listitem").map((item) => item.textContent ?? "");
}

test("labels a project by its name when no other project shares it", () => {
  mockApi();
  renderWithProviders(<Labels ids={["alpha", "beta"]} />);
  seedStream({
    projects: makeProjectsState([
      makeProject("alpha", { name: "Alpha" }),
      makeProject("beta", { name: "Beta" }),
    ]),
  });
  expect(renderedLabels()).toEqual(["[Alpha]", "[Beta]"]);
});

test("adds the id to projects that share a name so they stay distinguishable", () => {
  mockApi();
  renderWithProviders(<Labels ids={["clone-a", "clone-b", "other"]} />);
  seedStream({
    projects: makeProjectsState([
      makeProject("clone-a", { name: "Tool" }),
      makeProject("clone-b", { name: "Tool" }),
      makeProject("other", { name: "Other" }),
    ]),
  });
  expect(renderedLabels()).toEqual(["[Tool (clone-a)]", "[Tool (clone-b)]", "[Other]"]);
});

test("falls back to the id for a project the dashboard does not track", () => {
  mockApi();
  renderWithProviders(<Labels ids={["ghost"]} />);
  seedStream({ projects: makeProjectsState([makeProject("alpha")]) });
  expect(renderedLabels()).toEqual(["[ghost]"]);
});

test("gives no label when there is no project id", () => {
  mockApi();
  renderWithProviders(<Labels ids={[null, undefined, ""]} />);
  seedStream({ projects: makeProjectsState([makeProject("alpha")]) });
  expect(renderedLabels()).toEqual(["[]", "[]", "[]"]);
});

test("uses ids until the projects service has published and then switches to names", () => {
  mockApi();
  renderWithProviders(<Labels ids={["alpha"]} />);
  expect(renderedLabels()).toEqual(["[alpha]"]);
  seedStream({ projects: makeProjectsState([makeProject("alpha", { name: "Alpha" })]) });
  expect(renderedLabels()).toEqual(["[Alpha]"]);
});

test("relabels when a second project with the same name appears", () => {
  mockApi();
  renderWithProviders(<Labels ids={["alpha"]} />);
  seedStream({ projects: makeProjectsState([makeProject("alpha", { name: "Tool" })]) });
  expect(renderedLabels()).toEqual(["[Tool]"]);
  seedStream(
    {
      projects: makeProjectsState([
        makeProject("alpha", { name: "Tool" }),
        makeProject("alpha-copy", { name: "Tool" }),
      ]),
    },
    2,
  );
  expect(renderedLabels()).toEqual(["[Tool (alpha)]"]);
});
