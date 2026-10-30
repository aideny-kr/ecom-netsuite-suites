import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ValidationControl } from "../validation-control";
import { ChangesetPanel } from "../changeset-panel";
import { RunsPanel } from "../runs-panel";
import type { ChangeSet, Connection, WorkspaceRun } from "@/lib/types";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("@/lib/api-client", () => ({ apiClient: api }));
vi.mock("@/providers/auth-provider", () => ({ useAuth: () => ({ user: { tenant_id: "company", roles: ["admin"] } }) }));

const connection = (id: string, account: string, status = "active") => ({
  id, label: id, provider: "netsuite", status, metadata_json: { account_id: account },
  tenant_id: "company", auth_type: null, credentials_set: true, last_sync_at: null,
  last_health_check_at: null, error_reason: null, created_at: "", updated_at: "",
}) as Connection;
const targets = [connection("sandbox", "123_SB1"), connection("second", "456-sb2", "healthy"),
  connection("production", "123"), connection("revoked", "123_SB2", "revoked"),
  connection("unknown", ""), { ...connection("stripe", "123_SB3"), provider: "stripe" }];

function mount(element = <ValidationControl changesetId="change" />) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}>{element}</QueryClientProvider>);
  return client;
}
async function select(id = "sandbox") {
  await screen.findByRole("option", { name: /sandbox · 123_SB1/ });
  fireEvent.change(screen.getByRole("combobox"), { target: { value: id } });
}
beforeEach(() => {
  api.get.mockReset().mockImplementation((path: string) => Promise.resolve(path === "/api/v1/connections" ? targets : []));
  api.post.mockReset().mockResolvedValue({ id: "saved", status: "queued" });
});

describe("explicit validation target", () => {
  it("requires an explicit sandbox choice and sends only its connection ID", async () => {
    mount();
    await screen.findByRole("option", { name: /production · 123/ });
    expect(screen.getByRole("button", { name: "Validate" })).toBeDisabled();
    expect(screen.getByRole("option", { name: /production/ })).toBeDisabled();
    expect(screen.getByRole("option", { name: /revoked/ })).toBeDisabled();
    expect(screen.getByRole("option", { name: /unknown/ })).toBeDisabled();
    expect(screen.queryByRole("option", { name: /stripe/ })).not.toBeInTheDocument();
    await select("second");
    fireEvent.click(screen.getByRole("button", { name: "Validate" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/changesets/change/validate", { connection_id: "second" }));
    expect(await screen.findByRole("status")).toHaveTextContent("request saved");
    expect(screen.queryByText(/validation passed/i)).not.toBeInTheDocument();
  });
  it("shows server refusal and clears it when the target changes", async () => {
    api.post.mockRejectedValue(new Error("SDK validation requires current authorized project and sandbox connection"));
    mount(); await select();
    fireEvent.click(screen.getByRole("button", { name: "Validate" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("current authorized project");
    await select("second");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(api.post).toHaveBeenCalledTimes(1);
  });
  it("blocks stale cached connections after refetch failure and permits reload", async () => {
    const client = mount(); await select();
    api.get.mockRejectedValueOnce(new Error("network"));
    await client.invalidateQueries({ queryKey: ["connections"] });
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load");
    expect(screen.getByRole("button", { name: "Validate" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Reload connections" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Validate" })).toBeEnabled());
    expect(api.post).not.toHaveBeenCalled();
  });
  it("invalidates a selection when its connection is revoked", async () => {
    const client = mount(); await select();
    client.setQueryData(["connections", "company"], [connection("sandbox", "123_SB1", "revoked")]);
    await waitFor(() => expect(screen.getByRole("combobox")).toHaveValue(""));
    expect(screen.getByRole("button", { name: "Validate" })).toBeDisabled();
  });
  it("requires reselection when a connection account identity changes", async () => {
    const client = mount(); await select();
    client.setQueryData(["connections", "company"], [connection("sandbox", "999_SB1")]);
    await waitFor(() => expect(screen.getByRole("combobox")).toHaveValue(""));
    expect(screen.getByRole("button", { name: "Validate" })).toBeDisabled();
    expect(api.post).not.toHaveBeenCalled();
  });
  it("explains missing targets and does not send a body-less request", async () => {
    api.get.mockResolvedValue([]); mount();
    expect(await screen.findByText(/No active sandbox connection/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Validate" })).toBeDisabled();
    expect(api.post).not.toHaveBeenCalled();
  });
  it("does not request validation before changeset approval", async () => {
    mount(<ValidationControl changesetId="change" disabled />); await select();
    expect(screen.getByRole("button", { name: "Validate" })).toBeDisabled();
    expect(screen.getByRole("combobox")).toBeDisabled();
  });
  it("locks selection and action while the request is pending", async () => {
    api.post.mockReturnValue(new Promise(() => {})); mount(); await select();
    fireEvent.click(screen.getByRole("button", { name: "Validate" }));
    expect(await screen.findByRole("button", { name: "Requesting validation…" })).toBeDisabled();
    expect(screen.getByRole("combobox")).toBeDisabled();
  });
  it("wires the real expanded changeset panel to explicit selection", async () => {
    mount(<ChangesetPanel changesets={[{ id: "change", title: "Reviewed change", status: "approved" } as ChangeSet]} onViewDiff={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /Reviewed change/ })); await select();
    fireEvent.click(screen.getByRole("button", { name: "Validate" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/changesets/change/validate", { connection_id: "sandbox" }));
  });
  it("requires a fresh explicit choice for a new validation from run history", async () => {
    mount(<RunsPanel runs={[{ id: "old-run", changeset_id: "change", run_type: "suitecloud_validate", status: "failed", duration_ms: null } as WorkspaceRun]} />);
    fireEvent.click(screen.getByRole("button", { name: /SuiteCloud Validate/ }));
    await screen.findByRole("option", { name: /sandbox · 123_SB1/ });
    expect(screen.getByRole("button", { name: "Run validation again" })).toBeDisabled();
    await select(); fireEvent.click(screen.getByRole("button", { name: "Run validation again" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/changesets/change/validate", { connection_id: "sandbox" }));
    expect(screen.getByText(/Creates a new validation run/)).toBeInTheDocument();
  });
});
