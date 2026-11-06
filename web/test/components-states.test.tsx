import { describe, expect, mock, test } from "bun:test";
import { screen } from "@testing-library/react";
import { ApiError } from "@/api/client";
import {
  EmptyState,
  ErrorState,
  errorText,
  LoadingRows,
  QueryState,
  Section,
} from "@/components/states";
import { isPresent } from "./components-support";
import { installHarness, renderWithProviders } from "./harness";

installHarness();

interface Project {
  name: string;
}

function queryWith(overrides: {
  data?: Project;
  error?: unknown;
  isPending?: boolean;
  refetch?: () => unknown;
}) {
  return {
    data: overrides.data,
    error: overrides.error ?? null,
    isPending: overrides.isPending ?? false,
    refetch: overrides.refetch ?? (() => undefined),
  };
}

describe("errorText", () => {
  test("shows the server's reason for an API error", () => {
    expect(errorText(new ApiError(404, "no such job"))).toBe("no such job");
  });

  test("shows the message of an ordinary error", () => {
    expect(errorText(new Error("socket closed"))).toBe("socket closed");
  });

  test("shows any other thrown value as text", () => {
    expect(errorText("gateway timeout")).toBe("gateway timeout");
    expect(errorText(42)).toBe("42");
  });
});

describe("EmptyState", () => {
  test("states its title, explains it and offers the action", () => {
    renderWithProviders(
      <EmptyState title="No projects yet" action={<button type="button">Add a project</button>}>
        Point discovery at a folder that holds your repos.
      </EmptyState>,
    );
    expect(screen.getByText("No projects yet")).toBeTruthy();
    expect(screen.getByText("Point discovery at a folder that holds your repos.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Add a project" })).toBeTruthy();
  });

  test("shows only the title when there is nothing to explain", () => {
    renderWithProviders(<EmptyState title="Nothing staged" />);
    expect(screen.getByText("Nothing staged")).toBeTruthy();
    expect(isPresent(screen.queryByRole("button"))).toBe(false);
  });
});

describe("LoadingRows", () => {
  test("announces itself as busy loading content with three rows by default", () => {
    renderWithProviders(<LoadingRows />);
    const loading = screen.getByRole("status", { name: "Loading" });
    expect(loading.getAttribute("aria-busy")).toBe("true");
    expect(loading.children).toHaveLength(3);
  });

  test("draws as many placeholder rows as asked", () => {
    renderWithProviders(<LoadingRows rows={5} />);
    expect(screen.getByRole("status", { name: "Loading" }).children).toHaveLength(5);
  });
});

describe("ErrorState", () => {
  test("says the content could not be loaded and shows why", () => {
    renderWithProviders(<ErrorState error={new ApiError(500, "disk is full")} />);
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.getByText("This could not be loaded")).toBeTruthy();
    expect(screen.getByText("disk is full")).toBeTruthy();
  });

  test("uses the caller's title when given", () => {
    renderWithProviders(
      <ErrorState error={new Error("no branch")} title="Branches are unavailable" />,
    );
    expect(screen.getByText("Branches are unavailable")).toBeTruthy();
  });

  test("offers no retry when there is nothing to retry", () => {
    renderWithProviders(<ErrorState error={new Error("gone")} />);
    expect(isPresent(screen.queryByRole("button", { name: "Try again" }))).toBe(false);
  });

  test("retries when the user asks to try again", async () => {
    const retry = mock(() => undefined);
    const { user } = renderWithProviders(<ErrorState error={new Error("gone")} onRetry={retry} />);
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(retry).toHaveBeenCalledTimes(1);
  });
});

describe("QueryState", () => {
  test("renders the data through the children once it has arrived", () => {
    const children = mock((project: Project) => <p>{project.name}</p>);
    renderWithProviders(
      <QueryState query={queryWith({ data: { name: "alpha" } })}>{children}</QueryState>,
    );
    expect(screen.getByText("alpha")).toBeTruthy();
    expect(children).toHaveBeenCalledWith({ name: "alpha" });
  });

  test("shows placeholder rows while the first answer is pending", () => {
    const children = mock((project: Project) => <p>{project.name}</p>);
    renderWithProviders(
      <QueryState query={queryWith({ isPending: true })} rows={2}>
        {children}
      </QueryState>,
    );
    expect(screen.getByRole("status", { name: "Loading" }).children).toHaveLength(2);
    expect(children).not.toHaveBeenCalled();
  });

  test("shows placeholder rows when a settled query still has no data", () => {
    renderWithProviders(
      <QueryState query={queryWith({})}>{(project: Project) => <p>{project.name}</p>}</QueryState>,
    );
    expect(screen.getByRole("status", { name: "Loading" })).toBeTruthy();
  });

  test("shows the error with the default title and retries the query on request", async () => {
    const refetch = mock(() => undefined);
    const { user } = renderWithProviders(
      <QueryState query={queryWith({ error: new Error("offline"), refetch })}>
        {(project: Project) => <p>{project.name}</p>}
      </QueryState>,
    );
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.getByText("This could not be loaded")).toBeTruthy();
    expect(screen.getByText("offline")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  test("uses the caller's error title", () => {
    renderWithProviders(
      <QueryState
        query={queryWith({ error: new Error("offline") })}
        errorTitle="Projects are unavailable"
      >
        {(project: Project) => <p>{project.name}</p>}
      </QueryState>,
    );
    expect(screen.getByText("Projects are unavailable")).toBeTruthy();
  });

  test("prefers the error over stale data", () => {
    renderWithProviders(
      <QueryState query={queryWith({ data: { name: "stale" }, error: new Error("offline") })}>
        {(project: Project) => <p>{project.name}</p>}
      </QueryState>,
    );
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(isPresent(screen.queryByText("stale"))).toBe(false);
  });
});

describe("Section", () => {
  test("titles its content and anchors it by id for other views to link to", () => {
    renderWithProviders(
      <Section title="Branches" id="branches" description="Local and remote">
        <p>main</p>
      </Section>,
    );
    const region = screen.getByRole("region", { name: "Branches" });
    expect(region.id).toBe("branches");
    expect(screen.getByText("Local and remote")).toBeTruthy();
    expect(screen.getByText("main")).toBeTruthy();
  });

  test("places the actions beside the title", () => {
    renderWithProviders(
      <Section title="Commits" actions={<button type="button">Refresh</button>}>
        <p>body</p>
      </Section>,
    );
    expect(screen.getByRole("button", { name: "Refresh" })).toBeTruthy();
  });

  test("titles a section without an id with its heading alone", () => {
    renderWithProviders(
      <Section title="Tags">
        <p>v1.0</p>
      </Section>,
    );
    const heading = screen.getByRole("heading", { name: "Tags" });
    expect(heading.id).toBe("");
    expect(screen.getByText("v1.0")).toBeTruthy();
  });
});
