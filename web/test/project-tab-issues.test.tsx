import { describe, expect, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import { IssuesTab } from "@/features/project/tabs/IssuesTab";
import { NOW_SECONDS } from "./constants";
import { makeProject, type ProjectSummary } from "./factories";
import {
  formField,
  installHarness,
  type MockApi,
  MockApiError,
  mockApi,
  renderWithProviders,
  seedStream,
  settle,
  timeText,
} from "./harness";
import {
  makeDetail,
  makeGithubProject,
  makeGithubState,
  makeIssue,
  makeIssueComment,
  makeIssueDetail,
  makeIssueFacts,
  makePull,
} from "./project-tabs-support";

installHarness();

const ISSUES = "/api/services/github/actions/issues" as const;
const ISSUE = "/api/services/github/actions/issue" as const;
const PULLS = "/api/services/github/actions/pulls" as const;
const CREATE_ISSUE = "/api/services/github/actions/create-issue" as const;
const COMMENT = "/api/services/github/actions/comment" as const;
const SET_LABELS = "/api/services/github/actions/set-labels" as const;
const CLOSE_ISSUE = "/api/services/github/actions/close-issue" as const;
const REOPEN_ISSUE = "/api/services/github/actions/reopen-issue" as const;
const DELEGATE = "/api/services/agents/actions/delegate-issue" as const;

const DEFAULT_QUERY = {
  project_id: "alpha",
  state: "open",
  labels: [],
  assignee: null,
  milestone: null,
  limit: 50,
};

function mockIssues(
  issues: ReturnType<typeof makeIssue>[] = [],
  pulls: ReturnType<typeof makePull>[] = [],
  truncated = false,
): MockApi {
  return mockApi()
    .action(ISSUES, { repo: "acme/alpha", issues, truncated })
    .action(PULLS, { repo: "acme/alpha", pulls });
}

function renderIssues(subject: ProjectSummary = makeGithubProject("alpha")) {
  return renderWithProviders(<IssuesTab project={subject} detail={makeDetail(subject)} />);
}

function sectionOf(title: string): ReturnType<typeof within> {
  const section = screen.getByRole("heading", { name: title }).closest("section");
  if (!section) throw new Error(`no section titled ${title}`);
  return within(section);
}

async function openIssue(
  user: ReturnType<typeof renderIssues>["user"],
  title: string,
): Promise<ReturnType<typeof within>> {
  await user.click(await screen.findByRole("button", { name: new RegExp(title) }));
  const article = (await screen.findByRole("article")) as HTMLElement;
  return within(article);
}

describe("IssuesTab when GitHub is not reachable", () => {
  const cases: [string, ProjectSummary["issues"], RegExp][] = [
    [
      "unauthenticated",
      makeIssueFacts("unauthenticated"),
      /Sign in to GitHub in Settings; the token is read from git's credential helper/,
    ],
    ["rate-limited", makeIssueFacts("rate-limited"), /The GitHub rate limit is spent/],
    ["error", makeIssueFacts("error"), /The last GitHub request failed/],
    ["unknown", null, /The last GitHub request failed/],
  ];
  for (const [status, issues, message] of cases) {
    test(`explains a ${status} GitHub status instead of listing issues`, async () => {
      const api = mockIssues();
      renderIssues(makeProject("alpha", { issues }));
      expect(await screen.findByText("GitHub is not reachable for this project")).toBeTruthy();
      expect(screen.getByText(message)).toBeTruthy();
      await settle();
      expect(api.callsTo("POST", ISSUES)).toEqual([]);
      expect(screen.queryByRole("button", { name: "New issue" })).toBeNull();
    });
  }
});

describe("IssuesTab list", () => {
  test("asks for the open issues of this project first and shows a placeholder until one is chosen", async () => {
    const api = mockIssues();
    renderIssues();
    expect(
      await screen.findByText("Select an issue to read it, comment, or delegate it to an agent."),
    ).toBeTruthy();
    expect(api.sent(ISSUES)).toEqual([DEFAULT_QUERY]);
    expect(api.sent(PULLS)).toEqual([{ project_id: "alpha", state: "open", limit: 30 }]);
  });

  test("shows loading rows while the issue list is pending", async () => {
    mockIssues();
    renderIssues();
    expect(sectionOf("Issues").getByRole("status", { name: "Loading" })).toBeTruthy();
    expect(await sectionOf("Issues").findByText("No issues match.")).toBeTruthy();
  });

  test("lists each issue with its age, author, comment count, labels and closed marker", async () => {
    mockIssues(
      [
        makeIssue(1, {
          title: "Crash on start",
          author: "grace",
          comments: 4,
          labels: ["bug", "p1"],
          updated_at: NOW_SECONDS - 2 * 3600,
        }),
        makeIssue(2, { title: "Old thing", state: "closed", author: null }),
        makeIssue(3, { title: "Quiet", comments: 0, author: "linus" }),
      ],
      [],
      true,
    );
    renderIssues();
    const issues = sectionOf("Issues");
    const crash = (await issues.findByText("Crash on start")).closest("button") as HTMLElement;
    const inCrash = within(crash);
    expect(inCrash.getByText("#1")).toBeTruthy();
    expect(inCrash.getByText(timeText("2 h ago"))).toBeTruthy();
    expect(inCrash.getByText(/grace · 4 comments/)).toBeTruthy();
    expect(inCrash.getByText("bug")).toBeTruthy();
    expect(inCrash.getByText("p1")).toBeTruthy();
    expect(inCrash.queryByText("closed ·")).toBeNull();
    const old = within(issues.getByText("Old thing").closest("button") as HTMLElement);
    expect(old.getByText("closed ·")).toBeTruthy();
    const quiet = issues.getByText("Quiet").closest("button") as HTMLElement;
    expect(quiet.textContent).not.toContain("comments");
    expect(issues.getByText("More on GitHub.")).toBeTruthy();
  });

  test("does not offer more on GitHub when the list is complete", async () => {
    mockIssues([makeIssue(1)]);
    renderIssues();
    await sectionOf("Issues").findByText("Issue 1");
    expect(screen.queryByText("More on GitHub.")).toBeNull();
  });

  test("says no issues match for an empty list", async () => {
    mockIssues();
    renderIssues();
    expect(await sectionOf("Issues").findByText("No issues match.")).toBeTruthy();
  });

  test("reports an issue list that cannot be read", async () => {
    mockIssues().action(ISSUES, () => {
      throw new MockApiError(502, "GitHub is down");
    });
    renderIssues();
    expect(await sectionOf("Issues").findByText("GitHub is down")).toBeTruthy();
  });

  test("sends each filter the user sets with the next request", async () => {
    const api = mockIssues();
    const { user } = renderIssues();
    await screen.findByText("No issues match.");
    await user.click(screen.getByRole("radio", { name: "Closed" }));
    await waitFor(() => expect(api.sent(ISSUES)).toHaveLength(2));
    expect(api.sent(ISSUES)[1]).toEqual({ ...DEFAULT_QUERY, state: "closed" });
    await user.click(screen.getByRole("radio", { name: "All" }));
    await waitFor(() => expect(api.sent(ISSUES)).toHaveLength(3));
    expect(api.sent(ISSUES)[2]).toEqual({ ...DEFAULT_QUERY, state: "all" });
    await user.type(screen.getByRole("textbox", { name: "Labels" }), "bug, ui ,,");
    await user.type(screen.getByRole("textbox", { name: "Assignee" }), "grace");
    await user.type(screen.getByRole("textbox", { name: "Milestone" }), "v1");
    await waitFor(() =>
      expect(api.sent(ISSUES).at(-1)).toEqual({
        ...DEFAULT_QUERY,
        state: "all",
        labels: ["bug", "ui"],
        assignee: "grace",
        milestone: "v1",
      }),
    );
  });

  test("keeps the current state filter when its selected toggle is clicked again", async () => {
    const api = mockIssues();
    const { user } = renderIssues();
    await screen.findByText("No issues match.");
    const open = screen.getByRole("radio", { name: "Open" });
    expect(open.getAttribute("aria-checked")).toBe("true");
    await user.click(open);
    expect(open.getAttribute("aria-checked")).toBe("true");
    expect(api.sent(ISSUES)).toHaveLength(1);
  });

  test("creates an issue from a dialog with title, body and labels", async () => {
    const api = mockIssues().action(CREATE_ISSUE, makeIssue(9));
    const { user } = renderIssues();
    await user.click(await screen.findByRole("button", { name: "New issue" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(formField("title", dialog).getByRole("textbox"), "Broken build");
    await user.type(within(dialog).getByPlaceholderText("Markdown"), "It fails.");
    await user.type(formField("labels", dialog).getByRole("textbox"), "bug{Enter}");
    await user.click(within(dialog).getByRole("button", { name: "Create issue" }));
    await waitFor(() => expect(api.sent(CREATE_ISSUE)).toHaveLength(1));
    expect(api.sent(CREATE_ISSUE)[0]).toEqual({
      project_id: "alpha",
      title: "Broken build",
      body: "It fails.",
      labels: ["bug"],
    });
    expect(await screen.findByText("Create issue: done")).toBeTruthy();
  });
});

describe("IssuesTab pull requests", () => {
  test("says there are no open pull requests", async () => {
    mockIssues();
    renderIssues();
    expect(await sectionOf("Open pull requests").findByText("No open pull requests.")).toBeTruthy();
  });

  test("lists pull requests with branches, draft marker and check status in every state", async () => {
    mockIssues(
      [],
      [
        makePull(1, { title: "Add parser", checks: "passing" }),
        makePull(2, { title: "Fix bug", checks: "failing", draft: true }),
        makePull(3, { title: "Refactor", checks: "pending" }),
        makePull(4, { title: "Docs", checks: "none" }),
        makePull(5, { title: "No status", checks: null }),
      ],
    );
    renderIssues();
    const pulls = sectionOf("Open pull requests");
    const link = await pulls.findByRole("link", { name: /Add parser/ });
    expect(link.getAttribute("href")).toBe("https://github.com/acme/alpha/pull/1");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.textContent).toContain("feature-1 → main");
    expect(link.textContent).not.toContain("draft");
    expect(pulls.getByRole("link", { name: /Fix bug/ }).textContent).toContain("· draft");
    const tone = (title: string): string | null | undefined =>
      pulls.getByText(title).closest("[data-tone]")?.getAttribute("data-tone");
    expect(tone("checks passing")).toBe("good");
    expect(tone("checks failing")).toBe("bad");
    expect(tone("checks pending")).toBe("info");
    expect(tone("checks none")).toBe("neutral");
    expect(pulls.getAllByText(/^checks /)).toHaveLength(4);
  });

  test("reports pull requests that cannot be read", async () => {
    mockIssues().action(PULLS, () => {
      throw new MockApiError(502, "pulls unavailable");
    });
    renderIssues();
    expect(await sectionOf("Open pull requests").findByText("pulls unavailable")).toBeTruthy();
  });
});

describe("IssuesTab issue detail", () => {
  const issue = makeIssue(7, {
    title: "Crash on start",
    author: "grace",
    labels: ["bug"],
    milestone: "v1",
    created_at: NOW_SECONDS - 2 * 86400,
  });

  function mockDetail(detail = makeIssueDetail(issue, { body: "It **crashes**." })): MockApi {
    return mockIssues([issue]).action(ISSUE, detail);
  }

  test("fetches the chosen issue and renders its header, markdown body and comments", async () => {
    const api = mockDetail(
      makeIssueDetail(issue, {
        body: "It **crashes** on [start](https://example.com/trace).",
        comments: [
          makeIssueComment(1, { author: "linus", body: "Same here." }),
          makeIssueComment(2, { author: null, body: "Me too." }),
        ],
      }),
    );
    const { user } = renderIssues();
    const detail = await openIssue(user, "Crash on start");
    expect(detail.getByRole("heading", { name: /Crash on start/ }).textContent).toContain("#7");
    expect(detail.getByText("open").closest("[data-tone]")?.getAttribute("data-tone")).toBe("good");
    expect(detail.getByText(/grace opened/)).toBeTruthy();
    expect(detail.getByText(timeText("2 d ago"))).toBeTruthy();
    expect(detail.getByText("v1")).toBeTruthy();
    expect(detail.getByText("bug")).toBeTruthy();
    const github = detail.getByRole("link", { name: /on GitHub/ });
    expect(github.getAttribute("href")).toBe("https://github.com/acme/alpha/issues/7");
    expect(detail.getByText("crashes").tagName).toBe("STRONG");
    expect(detail.getByRole("link", { name: "start" }).getAttribute("target")).toBe("_blank");
    expect(detail.getByText("Same here.")).toBeTruthy();
    expect(detail.getByText("linus")).toBeTruthy();
    expect(detail.getByText("Me too.")).toBeTruthy();
    expect(detail.getByText("someone")).toBeTruthy();
    expect(detail.queryByText("Older comments are on GitHub.")).toBeNull();
    expect(api.sent(ISSUE)).toEqual([{ project_id: "alpha", number: 7 }]);
  });

  test("says when the issue has no description and no comments, and falls back to someone for the author", async () => {
    mockDetail(
      makeIssueDetail(makeIssue(7, { title: "Crash on start", author: null }), { body: "" }),
    );
    const { user } = renderIssues();
    const detail = await openIssue(user, "Crash on start");
    expect(detail.getByText("No description.")).toBeTruthy();
    expect(detail.getByText(/someone opened/)).toBeTruthy();
    expect(detail.queryByRole("list")).toBeNull();
  });

  test("points to GitHub for older comments when the thread is truncated", async () => {
    mockDetail(
      makeIssueDetail(issue, {
        body: "x",
        comments: [makeIssueComment(1)],
        comments_truncated: true,
      }),
    );
    const { user } = renderIssues();
    const detail = await openIssue(user, "Crash on start");
    expect(detail.getByText("Older comments are on GitHub.")).toBeTruthy();
  });

  test("shows loading rows then an error with a retry when the issue cannot be read", async () => {
    let attempts = 0;
    mockIssues([issue]).action(ISSUE, () => {
      attempts += 1;
      if (attempts === 1) throw new MockApiError(500, "issue unavailable");
      return makeIssueDetail(issue, { body: "recovered" });
    });
    const { user } = renderIssues();
    await user.click(await screen.findByRole("button", { name: /Crash on start/ }));
    expect(await screen.findByText("issue unavailable")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("recovered")).toBeTruthy();
  });

  test("comments on the issue with the typed body", async () => {
    const api = mockDetail().action(COMMENT, makeIssueComment(5));
    const { user } = renderIssues();
    const detail = await openIssue(user, "Crash on start");
    await user.click(detail.getByRole("button", { name: "Comment" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByPlaceholderText("Markdown"), "On it.");
    await user.click(within(dialog).getByRole("button", { name: "Comment" }));
    await waitFor(() => expect(api.sent(COMMENT)).toHaveLength(1));
    expect(api.sent(COMMENT)[0]).toEqual({ project_id: "alpha", number: 7, body: "On it." });
  });

  test("replaces the labels with the ones entered in the dialog", async () => {
    const api = mockDetail().action(SET_LABELS, { number: 7, labels: ["bug", "p1"] });
    const { user } = renderIssues();
    const detail = await openIssue(user, "Crash on start");
    await user.click(detail.getByRole("button", { name: "Labels" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(formField("labels", dialog).getByRole("textbox"), "bug{Enter}p1{Enter}");
    await user.click(within(dialog).getByRole("button", { name: "Set labels" }));
    await waitFor(() => expect(api.sent(SET_LABELS)).toHaveLength(1));
    expect(api.sent(SET_LABELS)[0]).toEqual({
      project_id: "alpha",
      number: 7,
      labels: ["bug", "p1"],
    });
  });

  test("closes an open issue with the chosen reason after confirmation", async () => {
    const api = mockDetail().action(CLOSE_ISSUE, makeIssue(7, { state: "closed" }));
    const { user } = renderIssues();
    const detail = await openIssue(user, "Crash on start");
    expect(detail.queryByRole("button", { name: "Reopen" })).toBeNull();
    await user.click(detail.getByRole("button", { name: "Close" }));
    const dialog = await screen.findByRole("dialog");
    expect(api.sent(CLOSE_ISSUE)).toEqual([]);
    await user.click(formField("reason", dialog).getByRole("combobox"));
    await user.click(await screen.findByRole("option", { name: "not planned" }));
    await user.click(within(dialog).getByRole("button", { name: "Close issue" }));
    await waitFor(() => expect(api.sent(CLOSE_ISSUE)).toHaveLength(1));
    expect(api.sent(CLOSE_ISSUE)[0]).toEqual({
      project_id: "alpha",
      number: 7,
      reason: "not_planned",
    });
  });

  test("reopens a closed issue instead of offering to close it", async () => {
    const closed = makeIssue(7, { title: "Crash on start", state: "closed" });
    const api = mockIssues([closed])
      .action(ISSUE, makeIssueDetail(closed, { body: "x" }))
      .action(REOPEN_ISSUE, makeIssue(7));
    const { user } = renderIssues();
    const detail = await openIssue(user, "Crash on start");
    expect(detail.queryByRole("button", { name: "Close" })).toBeNull();
    expect(detail.getByText("closed").closest("[data-tone]")?.getAttribute("data-tone")).toBe(
      "neutral",
    );
    await user.click(detail.getByRole("button", { name: "Reopen" }));
    await waitFor(() => expect(api.sent(REOPEN_ISSUE)).toHaveLength(1));
    expect(api.sent(REOPEN_ISSUE)[0]).toEqual({ project_id: "alpha", number: 7 });
    expect(await screen.findByText("Reopen issue: done")).toBeTruthy();
  });

  test("delegates the issue to an agent with optional instructions after confirmation", async () => {
    const api = mockDetail().action(DELEGATE, {
      job_id: "job-5",
      kind: "agents",
      title: "Delegate #7",
    });
    const { user } = renderIssues();
    const detail = await openIssue(user, "Crash on start");
    await user.click(detail.getByRole("button", { name: "Delegate to agent" }));
    const dialog = await screen.findByRole("dialog");
    expect(api.sent(DELEGATE)).toEqual([]);
    await user.type(within(dialog).getByPlaceholderText("Extra guidance"), "Keep it small.");
    await user.click(within(dialog).getByRole("button", { name: "Delegate issue to agent" }));
    await waitFor(() => expect(api.sent(DELEGATE)).toHaveLength(1));
    expect(api.sent(DELEGATE)[0]).toEqual({
      project_id: "alpha",
      issue_number: 7,
      instructions: "Keep it small.",
    });
    expect(await screen.findByText("Started: Delegate #7")).toBeTruthy();
  });

  test("highlights the selected issue and switches the detail when another is chosen", async () => {
    const other = makeIssue(8, { title: "Second issue" });
    const api = mockIssues([issue, other]).action(ISSUE, (input) => {
      const number = (input as { number: number }).number;
      return makeIssueDetail(number === 7 ? issue : other, { body: `Body of ${number}` });
    });
    const { user } = renderIssues();
    await user.click(await screen.findByRole("button", { name: /Crash on start/ }));
    expect(await screen.findByText("Body of 7")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: /Second issue/ }));
    expect(await screen.findByText("Body of 8")).toBeTruthy();
    expect(screen.queryByText("Body of 7")).toBeNull();
    expect(api.sent(ISSUE)).toEqual([
      { project_id: "alpha", number: 7 },
      { project_id: "alpha", number: 8 },
    ]);
  });
});

describe("IssuesTab live refresh", () => {
  test("re-reads issues when the github service publishes a new revision", async () => {
    const api = mockIssues();
    renderIssues();
    await screen.findByText("No issues match.");
    expect(api.callsTo("POST", ISSUES)).toHaveLength(1);
    seedStream({ github: makeGithubState() }, 2);
    await waitFor(() => expect(api.callsTo("POST", ISSUES)).toHaveLength(2));
  });
});
