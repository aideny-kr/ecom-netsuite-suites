import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const useVersionCheck = vi.fn();
vi.mock("@/hooks/use-version-check", () => ({
  useVersionCheck: () => useVersionCheck(),
}));

import { NewVersionBanner } from "@/components/new-version-banner";

const reload = vi.fn();

beforeEach(() => {
  useVersionCheck.mockReset();
  reload.mockReset();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, reload },
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("NewVersionBanner", () => {
  it("renders nothing when no update is available", () => {
    useVersionCheck.mockReturnValue({ updateAvailable: false });
    const { container } = render(<NewVersionBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the banner and reloads when Refresh is clicked", () => {
    useVersionCheck.mockReturnValue({ updateAvailable: true });
    render(<NewVersionBanner />);
    expect(screen.getByText(/new version is available/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /refresh/i }));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("hides the banner when dismissed", () => {
    useVersionCheck.mockReturnValue({ updateAvailable: true });
    render(<NewVersionBanner />);
    expect(screen.getByText(/new version is available/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /dismiss/i }));
    expect(screen.queryByText(/new version is available/i)).not.toBeInTheDocument();
  });

  it("announces itself on mount via role=status aria-live=polite", () => {
    useVersionCheck.mockReturnValue({ updateAvailable: true });
    render(<NewVersionBanner />);
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveTextContent(/new version is available/i);
  });
});
