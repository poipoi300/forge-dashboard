import { describe, expect, test } from "bun:test";
import { screen } from "@testing-library/react";
import { asSchema, useActionDescriptor, useDescriptor, useDescriptors } from "@/api/descriptors";
import services from "./fixtures/services.json";
import { installHarness, mockApi, renderWithProviders } from "./harness";

installHarness();

function ServiceIds() {
  const descriptors = useDescriptors();
  return <p>{descriptors ? [...descriptors.keys()].join(",") : "loading"}</p>;
}

function ResetDescriptor() {
  const action = useActionDescriptor("gallery", "reset");
  return (
    <p>{action ? `${action.title}|danger ${action.danger}|confirm ${action.confirm}` : "none"}</p>
  );
}

function ServiceAndAction({ service, action }: { service: string; action: string }) {
  const descriptor = useDescriptor(service);
  const actionDescriptor = useActionDescriptor(service, action);
  return (
    <p>
      {descriptor?.title ?? "no service"} / {actionDescriptor?.title ?? "no action"}
    </p>
  );
}

describe("useDescriptors", () => {
  test("is undefined until the service list arrives, then maps every service by id", async () => {
    mockApi();
    renderWithProviders(<ServiceIds />);
    expect(screen.getByText("loading")).toBeTruthy();
    const ids = services.map((descriptor) => descriptor.id).join(",");
    expect(await screen.findByText(ids)).toBeTruthy();
  });
});

describe("useDescriptor", () => {
  test("gives a service its own descriptor by id", async () => {
    mockApi();
    renderWithProviders(<ServiceAndAction service="gallery" action="ping" />);
    expect(await screen.findByText("Schema gallery / Ping")).toBeTruthy();
  });

  test("gives nothing for a service the server does not describe", async () => {
    mockApi();
    renderWithProviders(
      <>
        <ServiceIds />
        <ServiceAndAction service="vanished" action="ping" />
      </>,
    );
    await screen.findByText(services.map((descriptor) => descriptor.id).join(","));
    expect(screen.getByText("no service / no action")).toBeTruthy();
  });
});

describe("useActionDescriptor", () => {
  test("gives an action's descriptor with its danger and confirmation flags", async () => {
    mockApi();
    renderWithProviders(<ResetDescriptor />);
    expect(await screen.findByText("Reset counter|danger true|confirm true")).toBeTruthy();
  });

  test("gives nothing for an action the service does not declare", async () => {
    mockApi();
    renderWithProviders(<ServiceAndAction service="gallery" action="vanish" />);
    expect(await screen.findByText("Schema gallery / no action")).toBeTruthy();
  });
});

describe("descriptor sharing", () => {
  test("fetches the service list once for every consumer on the page", async () => {
    const api = mockApi();
    renderWithProviders(
      <>
        <ServiceIds />
        <ServiceAndAction service="gallery" action="echo" />
      </>,
    );
    expect(await screen.findByText("Schema gallery / Echo")).toBeTruthy();
    expect(api.callsTo("GET", "/api/services")).toHaveLength(1);
    expect(api.unrouted).toEqual([]);
  });
});

describe("asSchema", () => {
  test("passes a descriptor's schema through unchanged", () => {
    const schema = { type: "object", properties: {} };
    expect(asSchema(schema)).toBe(schema);
  });
});
