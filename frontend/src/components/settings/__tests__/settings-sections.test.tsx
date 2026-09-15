import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it } from "vitest";
import { SettingsSections, SettingsSection } from "../settings-sections";

beforeEach(() => {
  window.history.replaceState(null, "", "/settings");
});
function view(initialSection = "workspace") {
  return render(
    <SettingsSections initialSection={initialSection}>
      <SettingsSection id="workspace" label="Workspace">
        <input aria-label="Draft company name" defaultValue="Original" />
      </SettingsSection>
      <SettingsSection id="connections" label="Connections">
        <button id="celigo">Test connection</button>
      </SettingsSection>
      <SettingsSection id="agent" label="Agent">
        <button>Save configuration</button>
      </SettingsSection>
    </SettingsSections>,
  );
}
it("keeps unsaved fields mounted across section switches", () => {
  view();
  fireEvent.change(screen.getByLabelText("Draft company name"), {
    target: { value: "Unsaved draft" },
  });
  fireEvent.click(screen.getByRole("link", { name: /^Connections/ }));
  expect(screen.getByLabelText("Draft company name")).not.toBeVisible();
  expect(screen.getByRole("button", { name: "Test connection" })).toBeVisible();
  fireEvent.click(screen.getByRole("link", { name: /^Workspace/ }));
  expect(screen.getByLabelText("Draft company name")).toHaveValue(
    "Unsaved draft",
  );
});
it("resolves direct hashes and keeps the legacy connections entry point", () => {
  window.history.replaceState(null, "", "/settings#agent");
  view("connections");
  expect(
    screen.getByRole("button", { name: "Save configuration" }),
  ).toBeVisible();
});
it("searches system names without discarding the current form", () => {
  view();
  fireEvent.change(screen.getByRole("searchbox", { name: "Find a setting" }), {
    target: { value: "Metabase" },
  });
  expect(screen.getByRole("link", { name: /Connections/ })).toBeVisible();
  expect(
    screen.queryByRole("link", { name: /Workspace/ }),
  ).not.toBeInTheDocument();
  expect(screen.getByLabelText("Draft company name")).toBeVisible();
});

it("opens legacy connector hash links in the Connections group", () => {
  HTMLElement.prototype.scrollIntoView = () => {};
  window.history.replaceState(null, "", "/settings#celigo");
  view();
  expect(screen.getByRole("button", { name: "Test connection" })).toBeVisible();
});
