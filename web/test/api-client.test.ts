import { afterEach, describe, expect, mock, spyOn, test } from "bun:test";
import {
  ApiError,
  call,
  callDynamic,
  getConfig,
  listServices,
  putConfig,
  request,
} from "@/api/client";
import services from "./fixtures/services.json";
import { installHarness, MockApiError, mockApi } from "./harness";

installHarness();
afterEach(() => mock.restore());

const ECHO = "/api/services/gallery/actions/echo" as const;
const ECHO_INPUT = { message: "hi", times: 2, shout: true };

async function failureOf(pending: Promise<unknown>): Promise<ApiError> {
  try {
    await pending;
  } catch (failure) {
    if (failure instanceof ApiError) return failure;
    throw failure;
  }
  throw new Error("the request was expected to fail");
}

function responseWith(status: number, statusText: string, body: string): Response {
  return new Response(body, { status, statusText });
}

describe("typed calls", () => {
  test("call posts the input as JSON to the action and returns its output", async () => {
    const api = mockApi().action(ECHO, (input) => ({ echoed: [input.message] }));
    const output = await call(ECHO, ECHO_INPUT);
    expect(output).toEqual({ echoed: ["hi"] });
    expect(api.callsTo("POST", ECHO)).toEqual([{ method: "POST", path: ECHO, body: ECHO_INPUT }]);
  });

  test("callDynamic builds the action path from runtime names", async () => {
    const api = mockApi().action(ECHO, { echoed: [] });
    await callDynamic("gallery", "echo", ECHO_INPUT);
    expect(api.sent(ECHO)).toEqual([ECHO_INPUT]);
  });

  test("getConfig reads a service's configuration with no body", async () => {
    const api = mockApi().config("gallery", { title: "Demo" });
    expect(await getConfig("gallery")).toEqual({ title: "Demo" });
    expect(api.callsTo("GET", "/api/services/gallery/config")).toEqual([
      { method: "GET", path: "/api/services/gallery/config", body: undefined },
    ]);
  });

  test("putConfig replaces a service's configuration and returns what the service stored", async () => {
    const api = mockApi().route("PUT", "/api/services/gallery/config", (input) => input);
    const stored = await putConfig("gallery", { title: "Renamed", interval_s: 9 });
    expect(stored).toEqual({ title: "Renamed", interval_s: 9 });
    expect(api.callsTo("PUT", "/api/services/gallery/config")).toEqual([
      {
        method: "PUT",
        path: "/api/services/gallery/config",
        body: { title: "Renamed", interval_s: 9 },
      },
    ]);
  });

  test("listServices fetches every service descriptor in the server's order", async () => {
    const api = mockApi();
    const listed = await listServices();
    expect(listed.map((descriptor) => descriptor.id)).toEqual(
      services.map((descriptor) => descriptor.id),
    );
    expect(api.callsTo("GET", "/api/services")).toHaveLength(1);
  });

  test("marks JSON bodies with a JSON content type and sends reads without one", async () => {
    mockApi().action(ECHO, { echoed: [] }).config("gallery", { title: "Demo" });
    const fetchSpy = spyOn(globalThis, "fetch");
    await call(ECHO, ECHO_INPUT);
    await getConfig("gallery");
    expect(fetchSpy.mock.calls[0]?.[1]).toEqual({
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(ECHO_INPUT),
    });
    expect(fetchSpy.mock.calls[1]?.[1]).toEqual({
      method: "GET",
      headers: { Accept: "application/json" },
    });
  });
});

describe("request failures", () => {
  test("carries the server's detail string as the error message", async () => {
    mockApi().action(ECHO, () => {
      throw new MockApiError(409, "a run is already active");
    });
    const failure = await failureOf(call(ECHO, ECHO_INPUT));
    expect(failure.status).toBe(409);
    expect(failure.detail).toBe("a run is already active");
    expect(failure.message).toBe("a run is already active");
  });

  test("joins a validation list into one line, one message per problem", async () => {
    mockApi().action(ECHO, () => {
      throw new MockApiError(422, [{ msg: "message too short" }, { msg: "times too low" }]);
    });
    const failure = await failureOf(call(ECHO, ECHO_INPUT));
    expect(failure.status).toBe(422);
    expect(failure.detail).toBe("message too short; times too low");
  });

  test("uses the text of list items that are not objects", async () => {
    mockApi().action(ECHO, () => {
      throw new MockApiError(422, ["first problem", "second problem"]);
    });
    const failure = await failureOf(call(ECHO, ECHO_INPUT));
    expect(failure.detail).toBe("first problem; second problem");
  });

  test("falls back to the status line when the detail is not text or a list", async () => {
    spyOn(globalThis, "fetch").mockResolvedValue(
      responseWith(422, "Unprocessable Entity", JSON.stringify({ detail: { code: "bad" } })),
    );
    const failure = await failureOf(call(ECHO, ECHO_INPUT));
    expect(failure.status).toBe(422);
    expect(failure.detail).toBe("422 Unprocessable Entity");
  });

  test("falls back to the status line when the error body is not JSON, such as a proxy's HTML page", async () => {
    spyOn(globalThis, "fetch").mockResolvedValue(
      responseWith(502, "Bad Gateway", "<html><body>bad gateway</body></html>"),
    );
    const failure = await failureOf(getConfig("gallery"));
    expect(failure.status).toBe(502);
    expect(failure.detail).toBe("502 Bad Gateway");
  });

  test("falls back to the status line when the error body is empty", async () => {
    spyOn(globalThis, "fetch").mockResolvedValue(responseWith(503, "Service Unavailable", ""));
    const failure = await failureOf(getConfig("gallery"));
    expect(failure.status).toBe(503);
    expect(failure.detail).toBe("503 Service Unavailable");
  });
});

describe("request responses", () => {
  test("fails loudly when a successful response is not JSON", async () => {
    spyOn(globalThis, "fetch").mockResolvedValue(responseWith(200, "OK", "<html></html>"));
    await expect(request<null>("GET", "/api/services/gallery/config")).rejects.toBeInstanceOf(
      SyntaxError,
    );
  });

  test("resolves to null for a successful response with an empty body", async () => {
    spyOn(globalThis, "fetch").mockResolvedValue(responseWith(200, "OK", ""));
    expect(await request<null>("POST", "/api/services/gallery/actions/reset")).toBeNull();
  });
});
