import { describe, expect, test } from "bun:test";
import { screen, waitFor, within } from "@testing-library/react";
import { ActionButton } from "@/schema/ActionButton";
import {
  formField,
  installHarness,
  MockApiError,
  mockApi,
  renderWithProviders,
  settle,
} from "./harness";

installHarness();

const PING = "/api/services/gallery/actions/ping" as const;
const ECHO = "/api/services/gallery/actions/echo" as const;
const RESET = "/api/services/gallery/actions/reset" as const;
const SLOWLY = "/api/services/gallery/actions/count-slowly" as const;

function button(name: string): HTMLButtonElement {
  return screen.getByRole("button", { name }) as HTMLButtonElement;
}

describe("ActionButton", () => {
  test("runs an action with no inputs immediately and toasts its message", async () => {
    const api = mockApi().action(PING, { ok: true, message: "pong" });
    const { user } = renderWithProviders(<ActionButton service="gallery" action="ping" />);
    await user.click(await screen.findByRole("button", { name: "Ping" }));
    expect(await screen.findByText("pong")).toBeTruthy();
    expect(api.sent(PING)).toEqual([{}]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("says the action is done when the response carries no message", async () => {
    mockApi().action(PING, { ok: true, message: null });
    const { user } = renderWithProviders(<ActionButton service="gallery" action="ping" />);
    await user.click(await screen.findByRole("button", { name: "Ping" }));
    expect(await screen.findByText("Ping: done")).toBeTruthy();
  });

  test("stays disabled until the service descriptors have loaded", async () => {
    mockApi();
    renderWithProviders(<ActionButton service="gallery" action="ping" />);
    expect(button("ping").disabled).toBe(true);
    await screen.findByRole("button", { name: "Ping" });
    expect(button("Ping").disabled).toBe(false);
  });

  test("reports a failed action as a toast with the server reason", async () => {
    mockApi().action(PING, () => {
      throw new MockApiError(500, "disk is full");
    });
    const { user } = renderWithProviders(<ActionButton service="gallery" action="ping" />);
    await user.click(await screen.findByRole("button", { name: "Ping" }));
    expect(await screen.findByText("Ping failed")).toBeTruthy();
    expect(await screen.findByText("disk is full")).toBeTruthy();
  });

  test("never calls the API for an action the services do not describe", async () => {
    const api = mockApi();
    const { user } = renderWithProviders(
      <ActionButton service="gallery" action="vanish" label="Vanish" />,
    );
    await settle();
    expect(button("Vanish").disabled).toBe(true);
    await user.click(button("Vanish"));
    expect(api.callsTo("POST", "/api/services/gallery/actions/vanish")).toEqual([]);
  });

  test("announces a started job with a link to it", async () => {
    mockApi().action(SLOWLY, { job_id: "job-7", kind: "gallery", title: "Counting" });
    const { user, location } = renderWithProviders(
      <ActionButton service="gallery" action="count-slowly" />,
    );
    await user.click(await screen.findByRole("button", { name: "Count slowly" }));
    expect(await screen.findByText("Started: Counting")).toBeTruthy();
    await user.click(await screen.findByRole("button", { name: "View job" }));
    expect(location()).toBe("/jobs/job-7");
  });

  test("opens a dialog for inputs and sends the edited values", async () => {
    const api = mockApi().action(ECHO, { echoed: ["hi"] });
    const { user } = renderWithProviders(<ActionButton service="gallery" action="echo" />);
    await user.click(await screen.findByRole("button", { name: "Echo" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByPlaceholderText("Say something"), "hello");
    await user.click(formField("shout", dialog).getByRole("switch"));
    await user.click(within(dialog).getByRole("button", { name: "Echo" }));
    await waitFor(() => expect(api.sent(ECHO)).toHaveLength(1));
    expect(api.sent(ECHO)[0]).toEqual({ message: "hello", times: 1, shout: true });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  test("starts the dialog from editable defaults and skips fields the caller fixed", async () => {
    const api = mockApi().action(ECHO, { echoed: [] });
    const { user } = renderWithProviders(
      <ActionButton
        service="gallery"
        action="echo"
        defaults={{ message: "preset" }}
        input={{ times: 3 }}
      />,
    );
    await user.click(await screen.findByRole("button", { name: "Echo" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog.querySelector('[data-field="times"]')).toBeNull();
    const message = within(dialog).getByPlaceholderText("Say something") as HTMLTextAreaElement;
    expect(message.value).toBe("preset");
    await user.click(within(dialog).getByRole("button", { name: "Echo" }));
    await waitFor(() => expect(api.sent(ECHO)).toHaveLength(1));
    expect(api.sent(ECHO)[0]).toEqual({ message: "preset", times: 3, shout: false });
  });

  test("keeps the dialog open and shows the server error when the action fails", async () => {
    mockApi().action(ECHO, () => {
      throw new MockApiError(422, [{ msg: "message too short" }, { msg: "times too low" }]);
    });
    const { user } = renderWithProviders(<ActionButton service="gallery" action="echo" />);
    await user.click(await screen.findByRole("button", { name: "Echo" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByPlaceholderText("Say something"), "x");
    await user.click(within(dialog).getByRole("button", { name: "Echo" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert.textContent).toBe("message too short; times too low");
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  test("asks for typed confirmation before a dangerous action and sends nothing until it matches", async () => {
    const api = mockApi().action(RESET, { ok: true, message: "reset" });
    const { user } = renderWithProviders(<ActionButton service="gallery" action="reset" />);
    await user.click(await screen.findByRole("button", { name: "Reset counter" }));
    const dialog = await screen.findByRole("dialog");
    const submit = within(dialog).getByRole("button", {
      name: "Reset counter",
    }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    await user.type(within(dialog).getByRole("textbox"), "confirm");
    expect(submit.disabled).toBe(false);
    expect(api.sent(RESET)).toEqual([]);
    await user.click(submit);
    await waitFor(() => expect(api.sent(RESET)).toEqual([{}]));
  });

  test("expects the project id as the confirmation text when the action is bound to a project", async () => {
    mockApi().action(RESET, { ok: true, message: null });
    const { user } = renderWithProviders(
      <ActionButton service="gallery" action="reset" context={{ projectId: "alpha" }} />,
    );
    await user.click(await screen.findByRole("button", { name: "Reset counter" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("alpha")).toBeTruthy();
  });

  test("cancelling the dialog sends nothing", async () => {
    const api = mockApi();
    const { user } = renderWithProviders(<ActionButton service="gallery" action="echo" />);
    await user.click(await screen.findByRole("button", { name: "Echo" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.callsTo("POST", ECHO)).toEqual([]);
  });
});
