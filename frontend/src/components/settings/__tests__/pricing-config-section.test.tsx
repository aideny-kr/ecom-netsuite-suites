import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { PricingConfigSection } from "../pricing-config-section";

vi.mock("@/hooks/use-pricing-config", () => ({
  usePricingConfig: () => ({
    data: {
      id: "1",
      tenant_id: "t",
      config: {
        version: 1,
        base_currency: "USD",
        eur_fx_rate: 0.93,
        currencies: {
          GBP: { fx_rate: 0.8, tier: "usd_based", vat_rate: 0.2, rounding_rule: "nearest_9" },
        },
      },
    },
    isLoading: false,
    error: null,
  }),
  useUpdatePricingConfig: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

function wrap(children: React.ReactNode) {
  return <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>;
}

describe("PricingConfigSection", () => {
  it("renders the EUR base rate, base currency, and a configured currency row", () => {
    render(wrap(<PricingConfigSection />));
    expect(screen.getByText(/Base Rate/i)).toBeInTheDocument();
    expect(screen.getByText(/Base Currency/i)).toBeInTheDocument();
    expect(screen.getByText("GBP")).toBeInTheDocument();
  });

  it("offers an Add Currency control", () => {
    render(wrap(<PricingConfigSection />));
    expect(screen.getByText(/Add Currency/i)).toBeInTheDocument();
  });
});
