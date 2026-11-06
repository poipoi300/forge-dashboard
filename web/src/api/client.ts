// Typed HTTP access to the API. Paths and bodies come from the generated
// OpenAPI types, so a backend model change that breaks a caller fails tsc.
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

type JsonBody<Operation> = Operation extends {
  requestBody: { content: { "application/json": infer Body } };
}
  ? Body
  : Operation extends { requestBody?: { content: { "application/json": infer Body } } }
    ? Body
    : never;
type JsonResponse<Operation> = Operation extends {
  responses: { 200: { content: { "application/json": infer Response } } };
}
  ? Response
  : never;

export type ActionPath = Extract<
  {
    [P in keyof paths]: paths[P] extends { post: unknown } ? P : never;
  }[keyof paths],
  `/api/services/${string}/actions/${string}`
>;
export type ActionInput<P extends ActionPath> = JsonBody<paths[P]["post"]>;
export type ActionOutput<P extends ActionPath> = JsonResponse<paths[P]["post"]>;

function detailOf(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((item) =>
          item && typeof item === "object" && "msg" in item ? String(item.msg) : String(item),
        )
        .join("; ");
    }
  }
  return fallback;
}

/** An error response's JSON body, or null when it is not JSON (a proxy's HTML page, say). */
function errorBody(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch (failure) {
    if (failure instanceof SyntaxError) return null;
    throw failure;
  }
}

export async function request<T>(
  method: "GET" | "POST" | "PUT",
  path: string,
  body?: unknown,
): Promise<T> {
  const init: RequestInit = { method, headers: { Accept: "application/json" } };
  if (body !== undefined) {
    init.headers = { ...init.headers, "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  const text = await response.text();
  if (!response.ok)
    throw new ApiError(
      response.status,
      detailOf(errorBody(text), `${response.status} ${response.statusText}`),
    );
  return (text ? JSON.parse(text) : null) as T;
}

/** Call a service action by its literal route; input and output are typed from OpenAPI. */
export function call<P extends ActionPath>(
  path: P,
  input: ActionInput<P>,
): Promise<ActionOutput<P>> {
  return request<ActionOutput<P>>("POST", path, input);
}

/** Call an action known only at runtime (manifest actions, schema-built dialogs). */
export function callDynamic(service: string, action: string, input: unknown): Promise<unknown> {
  return request<unknown>("POST", `/api/services/${service}/actions/${action}`, input);
}

export function getConfig(service: string): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>("GET", `/api/services/${service}/config`);
}

export function putConfig(service: string, config: unknown): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>("PUT", `/api/services/${service}/config`, config);
}

export function listServices(): Promise<Schemas["ServiceDescriptor"][]> {
  return request<Schemas["ServiceDescriptor"][]>("GET", "/api/services");
}
