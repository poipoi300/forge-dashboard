import { describe, expect, test } from "bun:test";
import { screen, waitFor } from "@testing-library/react";
import { Route } from "wouter";
import type { Schemas } from "@/api/client";
import { type QueryOptions, useAction } from "@/api/query";
import { errorText } from "@/components/states";
import { deferred, makeGalleryState } from "./api-support";
import { makeProjectsState } from "./factories";
import { installHarness, mockApi, renderWithProviders, seedStream, settle } from "./harness";

installHarness();

const ECHO = "/api/services/gallery/actions/echo" as const;
const PING = "/api/services/gallery/actions/ping" as const;

type EchoOutput = Schemas["EchoOutput"];

function EchoAnswer({ message, options }: { message: string; options?: QueryOptions }) {
  const query = useAction(ECHO, { message, times: 1, shout: false }, options);
  if (query.error) return <p role="alert">{errorText(query.error)}</p>;
  return (
    <output aria-label="answer">{query.data ? query.data.echoed.join(",") : "pending"}</output>
  );
}

/** The echo probe reads its message from the router, so a navigation changes its input. */
function EchoRoute({ options }: { options?: QueryOptions }) {
  return (
    <Route path="/echo/:message">
      {(params) => <EchoAnswer message={params.message} options={options} />}
    </Route>
  );
}

/** Only a live query derives its service from the path, so only it can meet a non-action path. */
function LivePath({ path }: { path: typeof PING }) {
  useAction(path, {}, { live: true });
  return null;
}

function PingEvery({ refetchInterval }: { refetchInterval: number }) {
  useAction(PING, {}, { refetchInterval });
  return null;
}

function answer(): string {
  return screen.getByRole("status", { name: "answer" }).textContent ?? "";
}

describe("useAction", () => {
  test("posts its input to the action and shows the answer", async () => {
    const api = mockApi().action(ECHO, (input) => ({ echoed: [input.message] }));
    renderWithProviders(<EchoRoute />, { path: "/echo/hi" });
    await waitFor(() => expect(answer()).toBe("hi"));
    expect(api.sent(ECHO)).toEqual([{ message: "hi", times: 1, shout: false }]);
  });

  test("asks again when the owning service publishes a new revision", async () => {
    const api = mockApi().action(ECHO, { echoed: ["answer"] });
    renderWithProviders(<EchoRoute options={{ live: true }} />, { path: "/echo/hi" });
    await waitFor(() => expect(api.sent(ECHO)).toHaveLength(1));

    seedStream({ gallery: makeGalleryState() }, 1);
    await waitFor(() => expect(api.sent(ECHO)).toHaveLength(2));

    seedStream({ gallery: makeGalleryState({ pings: 1 }) }, 2);
    await waitFor(() => expect(api.sent(ECHO)).toHaveLength(3));
  });

  test("ignores revisions of services it does not follow unless asked to", async () => {
    const api = mockApi().action(ECHO, { echoed: ["answer"] });
    renderWithProviders(<EchoRoute />, { path: "/echo/hi" });
    await waitFor(() => expect(api.sent(ECHO)).toHaveLength(1));

    seedStream({ gallery: makeGalleryState() }, 1);
    await settle();
    expect(api.sent(ECHO)).toHaveLength(1);
  });

  test("also asks again when a followed service publishes a new revision", async () => {
    const api = mockApi().action(ECHO, { echoed: ["answer"] });
    renderWithProviders(<EchoRoute options={{ follow: ["projects"] }} />, { path: "/echo/hi" });
    await waitFor(() => expect(api.sent(ECHO)).toHaveLength(1));

    seedStream({ projects: makeProjectsState([]) }, 1);
    await waitFor(() => expect(api.sent(ECHO)).toHaveLength(2));
  });

  test("does not call the action while disabled", async () => {
    const api = mockApi().action(ECHO, { echoed: ["answer"] });
    renderWithProviders(<EchoRoute options={{ enabled: false }} />, { path: "/echo/hi" });
    await settle();
    expect(api.callsTo("POST", ECHO)).toEqual([]);
    expect(answer()).toBe("pending");
  });

  test("keeps showing the last answer while the same input reloads", async () => {
    const reload = deferred<EchoOutput>();
    let loads = 0;
    const api = mockApi().action(ECHO, (): EchoOutput | Promise<EchoOutput> => {
      loads += 1;
      return loads === 1 ? { echoed: ["first"] } : reload.promise;
    });
    renderWithProviders(<EchoRoute options={{ live: true }} />, { path: "/echo/hi" });
    await waitFor(() => expect(answer()).toBe("first"));

    seedStream({ gallery: makeGalleryState() }, 1);
    await waitFor(() => expect(api.sent(ECHO)).toHaveLength(2));
    expect(answer()).toBe("first");

    reload.resolve({ echoed: ["second"] });
    await waitFor(() => expect(answer()).toBe("second"));
  });

  test("shows nothing from the previous input while a different input loads", async () => {
    const pending = deferred<EchoOutput>();
    mockApi().action(ECHO, (input): EchoOutput | Promise<EchoOutput> =>
      input.message === "b" ? pending.promise : { echoed: [input.message] },
    );
    const { navigate } = renderWithProviders(<EchoRoute />, { path: "/echo/a" });
    await waitFor(() => expect(answer()).toBe("a"));

    navigate("/echo/b");
    expect(answer()).toBe("pending");

    pending.resolve({ echoed: ["b"] });
    await waitFor(() => expect(answer()).toBe("b"));
  });

  test("asks again on the interval it is given", async () => {
    const api = mockApi().action(PING, { ok: true, message: null });
    renderWithProviders(<PingEvery refetchInterval={5} />);
    await waitFor(() => expect(api.sent(PING).length).toBeGreaterThanOrEqual(3));
  });

  test("refuses to derive a service from a path that is not an action", () => {
    const notAnAction: string = "/api/services/gallery/config";
    const misrouted = notAnAction as typeof PING;
    expect(() => renderWithProviders(<LivePath path={misrouted} />)).toThrow(
      "not an action path: /api/services/gallery/config",
    );
  });
});
