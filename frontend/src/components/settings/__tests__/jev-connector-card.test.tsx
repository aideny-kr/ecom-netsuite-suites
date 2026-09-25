import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { JevStatus } from "@/hooks/use-jev";

const mocks = vi.hoisted(() => ({
  status: vi.fn(),
  setMode: vi.fn(),
  saveKey: vi.fn(),
  removeKey: vi.fn(),
  test: vi.fn(),
  toast: vi.fn(),
  manage: true,
}));

vi.mock("@/hooks/use-jev", () => ({
  useJevStatus: () => ({ data: mocks.status(), isLoading: false, isError: false }),
  useJevSetMode: () => ({ mutateAsync: mocks.setMode, isPending: false }),
  useJevSaveKey: () => ({ mutateAsync: mocks.saveKey, isPending: false }),
  useJevRemoveKey: () => ({ mutateAsync: mocks.removeKey, isPending: false }),
  useJevTest: () => ({ mutateAsync: mocks.test, isPending: false }),
}));
vi.mock("@/hooks/use-permissions", () => ({
  usePermissions: () => ({ hasPermission: () => mocks.manage, isAdmin: mocks.manage, permissions: new Set<string>() }),
}));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast: mocks.toast }) }));

function status(over: Partial<JevStatus> = {}): JevStatus {
  return {
    mode: "live",
    effective_mode: "live",
    deployment_cap: "live",
    key_source: "platform",
    key_hint: null,
    problem: null,
    ...over,
  };
}

async function renderCard() {
  const { default: Card } = await import("../jev-connector-card");
  return render(<Card />);
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.manage = true;
  mocks.setMode.mockResolvedValue(status());
  mocks.saveKey.mockResolvedValue(status({ key_source: "tenant", key_hint: "1234" }));
  mocks.removeKey.mockResolvedValue(status());
  mocks.test.mockResolvedValue({ success: true, key_source: "platform", error: null });
});

describe("JevConnectorCard", () => {
  it("distinguishes live Inc hybrid from shadow resolution", async () => {
    mocks.status.mockReturnValue(status({effective_mode: "shadow", deployment_cap: "shadow", transaction_ops_mode: "live", transaction_ops_config_count: 1}));
    await renderCard();
    expect(screen.getByText(/Resolution workflow: Shadow. Inc exception workflow: Live/)).toBeVisible();
    expect(screen.getByText(/1 enabled configuration/)).toBeVisible();
  });
  it("is on by default with the deployment's key and says what Jev sees", async () => {
    mocks.status.mockReturnValue(status());
    await renderCard();
    expect(screen.getByRole("heading", { name: "TypeSafe Jev" })).toBeVisible();
    expect(screen.getByRole("status", { name: "Resolution workflow is live" })).toBeVisible();
    expect(screen.getByText(/using this deployment's key/i)).toBeVisible();
    expect(screen.getByText(/without customer identifiers or free-form source text/i)).toBeVisible();
    expect(screen.getByRole("radio", { name: /live/i })).toHaveAttribute("aria-checked", "true");
  });

  it("switches the mode", async () => {
    mocks.status.mockReturnValue(status());
    await renderCard();
    fireEvent.click(screen.getByRole("radio", { name: /shadow/i }));
    await waitFor(() => expect(mocks.setMode).toHaveBeenCalledWith("shadow"));
  });

  it("saves the workspace's own key and clears the field", async () => {
    mocks.status.mockReturnValue(status());
    await renderCard();
    const input = screen.getByLabelText(/your typesafe key/i);
    fireEvent.change(input, { target: { value: "ts-own-key-1234" } });
    fireEvent.click(screen.getByRole("button", { name: /save key/i }));
    await waitFor(() => expect(mocks.saveKey).toHaveBeenCalledWith("ts-own-key-1234"));
    await waitFor(() => expect(input).toHaveValue(""));
  });

  it("shows only the last four characters of the workspace's key and can return to the deployment's", async () => {
    mocks.status.mockReturnValue(status({ key_source: "tenant", key_hint: "1234" }));
    await renderCard();
    expect(screen.getByText(/your typesafe key ending 1234/i)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /remove key/i }));
    await waitFor(() => expect(mocks.removeKey).toHaveBeenCalled());
  });

  it("says so when there is no key anywhere", async () => {
    mocks.status.mockReturnValue(status({ key_source: "none", effective_mode: "off" }));
    await renderCard();
    expect(screen.getByText(/no jev key is configured/i)).toBeVisible();
    expect(screen.getByRole("status", { name: "Resolution workflow is off" })).toBeVisible();
  });

  it("explains when the deployment limits the mode", async () => {
    mocks.status.mockReturnValue(status({ deployment_cap: "shadow", effective_mode: "shadow" }));
    await renderCard();
    expect(screen.getByText(/resolution workflow is limited to shadow/i)).toBeVisible();
  });

  it("flags a stored key that cannot be read", async () => {
    mocks.status.mockReturnValue(status({ key_source: "tenant", effective_mode: "off", problem: "unreadable_key" }));
    await renderCard();
    expect(screen.getByText(/could not be read/i)).toBeVisible();
  });

  it("tests the key in use", async () => {
    mocks.status.mockReturnValue(status());
    await renderCard();
    fireEvent.click(screen.getByRole("button", { name: /test key/i }));
    await waitFor(() => expect(mocks.test).toHaveBeenCalledWith(undefined));
    await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith(expect.objectContaining({ title: "Jev key works" })));
  });

  it("shows viewers the state without controls", async () => {
    mocks.manage = false;
    mocks.status.mockReturnValue(status());
    await renderCard();
    expect(screen.getByRole("status", { name: "Resolution workflow is live" })).toBeVisible();
    expect(screen.queryByRole("radio", { name: /shadow/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /save key/i })).not.toBeInTheDocument();
  });
});
