"use client";

import { useState } from "react";
import {
  Pencil,
  Trash2,
  Plus,
  Calculator,
  Check,
  X,
  Loader2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useToast } from "@/hooks/use-toast";
import {
  usePricingConfig,
  useUpdatePricingConfig,
  type CurrencyConfig,
  type TenantPricingConfig,
} from "@/hooks/use-pricing-config";

const ROUNDING_LABELS: Record<string, string> = {
  nearest_9: "Charm (.X9)",
  nearest_100: "Nearest 100",
  nearest_990: "Nearest 990/490",
  nearest_50: "Nearest 50",
  no_rounding: "No rounding",
};

const ROUNDING_OPTIONS = [
  { value: "nearest_9", label: "Charm (.X9)" },
  { value: "nearest_100", label: "Nearest 100" },
  { value: "nearest_990", label: "Nearest 990/490" },
  { value: "nearest_50", label: "Nearest 50" },
  { value: "no_rounding", label: "No rounding" },
] as const;

function calculatePrice(
  usdPrice: number,
  currency: CurrencyConfig,
  eurFxRate: number
) {
  const converted =
    currency.tier === "usd_based"
      ? usdPrice * currency.fx_rate
      : usdPrice * eurFxRate * currency.fx_rate;
  const vatAmount = currency.vat_rate ? converted * currency.vat_rate : 0;
  const preRound = converted + vatAmount;
  let final = preRound;
  if (currency.rounding_rule === "nearest_9")
    final = Math.ceil(preRound / 10) * 10 - 1;
  else if (currency.rounding_rule === "nearest_100")
    final = Math.round(preRound / 100) * 100;
  else if (currency.rounding_rule === "nearest_50")
    final = Math.round(preRound / 50) * 50;
  return { converted, vatAmount, preRound, final };
}

function TierBadge({ tier }: { tier: CurrencyConfig["tier"] }) {
  if (tier === "usd_based") {
    return (
      <span className="inline-flex items-center rounded-md px-2 py-0.5 text-[11px] font-medium bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300">
        USD-based
      </span>
    );
  }
  return (
    <span className="inline-flex items-center rounded-md px-2 py-0.5 text-[11px] font-medium bg-purple-50 text-purple-700 dark:bg-purple-950 dark:text-purple-300">
      EUR-based
    </span>
  );
}

interface EditRowState {
  fx_rate: string;
  tier: CurrencyConfig["tier"];
  vat_rate: string;
  rounding_rule: CurrencyConfig["rounding_rule"];
}

interface AddCurrencyState {
  code: string;
  fx_rate: string;
  tier: CurrencyConfig["tier"];
  vat_rate: string;
  rounding_rule: CurrencyConfig["rounding_rule"];
}

const DEFAULT_ADD_STATE: AddCurrencyState = {
  code: "",
  fx_rate: "",
  tier: "usd_based",
  vat_rate: "",
  rounding_rule: "no_rounding",
};

export function PricingConfigSection() {
  const { data, isLoading, error } = usePricingConfig();
  const updateConfig = useUpdatePricingConfig();
  const { toast } = useToast();

  const [editingCurrency, setEditingCurrency] = useState<string | null>(null);
  const [editRow, setEditRow] = useState<EditRowState | null>(null);
  const [editingEurRate, setEditingEurRate] = useState(false);
  const [eurRateValue, setEurRateValue] = useState("");
  const [showAddForm, setShowAddForm] = useState(false);
  const [addState, setAddState] = useState<AddCurrencyState>(DEFAULT_ADD_STATE);

  // Test calculator
  const [calcUsd, setCalcUsd] = useState("");
  const [calcCurrency, setCalcCurrency] = useState("");

  function buildUpdatedConfig(
    base: TenantPricingConfig,
    currencies: Record<string, CurrencyConfig>
  ): TenantPricingConfig {
    return { ...base, currencies };
  }

  function startEdit(code: string, cfg: CurrencyConfig) {
    setEditingCurrency(code);
    setEditRow({
      fx_rate: String(cfg.fx_rate),
      tier: cfg.tier,
      vat_rate: cfg.vat_rate != null ? String(cfg.vat_rate) : "",
      rounding_rule: cfg.rounding_rule,
    });
  }

  function cancelEdit() {
    setEditingCurrency(null);
    setEditRow(null);
  }

  async function saveEdit(code: string) {
    if (!data || !editRow) return;
    const updatedCurrencies: Record<string, CurrencyConfig> = {
      ...data.config.currencies,
      [code]: {
        fx_rate: parseFloat(editRow.fx_rate) || 0,
        tier: editRow.tier,
        vat_rate: editRow.vat_rate !== "" ? parseFloat(editRow.vat_rate) : null,
        rounding_rule: editRow.rounding_rule,
      },
    };
    const fullConfig = buildUpdatedConfig(data.config, updatedCurrencies);
    updateConfig.mutate(fullConfig, {
      onSuccess: () => {
        toast({ title: `${code} updated` });
        cancelEdit();
      },
      onError: () => {
        toast({ title: "Failed to save", variant: "destructive" });
      },
    });
  }

  async function deleteCurrency(code: string) {
    if (!data) return;
    const updatedCurrencies = { ...data.config.currencies };
    delete updatedCurrencies[code];
    const fullConfig = buildUpdatedConfig(data.config, updatedCurrencies);
    updateConfig.mutate(fullConfig, {
      onSuccess: () => {
        toast({ title: `${code} removed` });
      },
      onError: () => {
        toast({ title: "Failed to delete", variant: "destructive" });
      },
    });
  }

  async function saveEurRate() {
    if (!data) return;
    const fullConfig: TenantPricingConfig = {
      ...data.config,
      eur_fx_rate: parseFloat(eurRateValue) || data.config.eur_fx_rate,
    };
    updateConfig.mutate(fullConfig, {
      onSuccess: () => {
        toast({ title: "EUR rate updated" });
        setEditingEurRate(false);
      },
      onError: () => {
        toast({ title: "Failed to save EUR rate", variant: "destructive" });
      },
    });
  }

  async function addCurrency() {
    if (!data || !addState.code.trim()) return;
    const code = addState.code.trim().toUpperCase();
    const updatedCurrencies: Record<string, CurrencyConfig> = {
      ...data.config.currencies,
      [code]: {
        fx_rate: parseFloat(addState.fx_rate) || 1,
        tier: addState.tier,
        vat_rate: addState.vat_rate !== "" ? parseFloat(addState.vat_rate) : null,
        rounding_rule: addState.rounding_rule,
      },
    };
    const fullConfig = buildUpdatedConfig(data.config, updatedCurrencies);
    updateConfig.mutate(fullConfig, {
      onSuccess: () => {
        toast({ title: `${code} added` });
        setShowAddForm(false);
        setAddState(DEFAULT_ADD_STATE);
      },
      onError: () => {
        toast({ title: "Failed to add currency", variant: "destructive" });
      },
    });
  }

  const currencies = data?.config.currencies ?? {};
  const currencyList = Object.entries(currencies);
  const eurFxRate = data?.config.eur_fx_rate ?? 1;

  // Calculator result
  const calcResult =
    calcUsd && calcCurrency && currencies[calcCurrency]
      ? calculatePrice(parseFloat(calcUsd), currencies[calcCurrency], eurFxRate)
      : null;

  if (isLoading) {
    return (
      <div className="rounded-xl border bg-card p-5 shadow-soft space-y-4">
        <Skeleton className="h-6 w-48" />
        <Skeleton className="h-4 w-72" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-xl border bg-card p-5 shadow-soft">
        <p className="text-[13px] text-muted-foreground">
          Failed to load pricing configuration.
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-xl border bg-card p-5 shadow-soft space-y-6">
      {/* Header */}
      <div>
        <h2 className="text-[15px] font-semibold text-foreground">
          Pricing Configuration
        </h2>
        <p className="text-[13px] text-muted-foreground mt-0.5">
          Manage currency FX rates, VAT, and rounding rules for international pricing.
        </p>
      </div>

      {/* EUR Base Rate */}
      <div className="flex items-center gap-4">
        <div>
          <p className="text-[11px] text-muted-foreground uppercase tracking-wide mb-1">
            EUR / USD Base Rate
          </p>
          {editingEurRate ? (
            <div className="flex items-center gap-2">
              <Input
                className="h-8 w-32 text-[13px]"
                value={eurRateValue}
                onChange={(e) => setEurRateValue(e.target.value)}
                type="number"
                step="0.0001"
                placeholder="e.g. 1.08"
              />
              <Button
                size="sm"
                variant="ghost"
                className="h-8 w-8 p-0"
                onClick={saveEurRate}
                disabled={updateConfig.isPending}
              >
                {updateConfig.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Check className="h-3.5 w-3.5" />
                )}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                className="h-8 w-8 p-0"
                onClick={() => setEditingEurRate(false)}
              >
                <X className="h-3.5 w-3.5" />
              </Button>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <span className="text-[15px] font-medium text-foreground">
                {data?.config.eur_fx_rate ?? "—"}
              </span>
              <Button
                size="sm"
                variant="ghost"
                className="h-7 w-7 p-0"
                onClick={() => {
                  setEurRateValue(String(data?.config.eur_fx_rate ?? ""));
                  setEditingEurRate(true);
                }}
              >
                <Pencil className="h-3 w-3" />
              </Button>
            </div>
          )}
        </div>
        <div className="ml-6">
          <p className="text-[11px] text-muted-foreground uppercase tracking-wide mb-1">
            Base Currency
          </p>
          <span className="text-[15px] font-medium text-foreground">
            {data?.config.base_currency ?? "USD"}
          </span>
        </div>
        <div className="ml-6">
          <p className="text-[11px] text-muted-foreground uppercase tracking-wide mb-1">
            Config Version
          </p>
          <span className="text-[15px] font-medium text-foreground">
            v{data?.config.version ?? 1}
          </span>
        </div>
      </div>

      {/* Currency Table */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <p className="text-[13px] font-medium text-foreground">Currencies</p>
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-[12px] gap-1.5"
            onClick={() => {
              setShowAddForm(!showAddForm);
              setAddState(DEFAULT_ADD_STATE);
            }}
          >
            <Plus className="h-3.5 w-3.5" />
            Add Currency
          </Button>
        </div>

        {/* Add Currency Form */}
        {showAddForm && (
          <div className="mb-4 rounded-lg border bg-muted/30 p-4 space-y-3">
            <p className="text-[13px] font-medium text-foreground">New Currency</p>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
              <div className="space-y-1">
                <Label className="text-[11px]">Code</Label>
                <Input
                  className="h-8 text-[13px] uppercase"
                  placeholder="EUR"
                  value={addState.code}
                  onChange={(e) =>
                    setAddState((s) => ({ ...s, code: e.target.value.toUpperCase() }))
                  }
                  maxLength={3}
                />
              </div>
              <div className="space-y-1">
                <Label className="text-[11px]">FX Rate</Label>
                <Input
                  className="h-8 text-[13px]"
                  placeholder="1.08"
                  type="number"
                  step="0.0001"
                  value={addState.fx_rate}
                  onChange={(e) =>
                    setAddState((s) => ({ ...s, fx_rate: e.target.value }))
                  }
                />
              </div>
              <div className="space-y-1">
                <Label className="text-[11px]">Tier</Label>
                <Select
                  value={addState.tier}
                  onValueChange={(v) =>
                    setAddState((s) => ({
                      ...s,
                      tier: v as CurrencyConfig["tier"],
                    }))
                  }
                >
                  <SelectTrigger className="h-8 text-[13px]">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="usd_based" className="text-[13px]">
                      USD-based
                    </SelectItem>
                    <SelectItem value="eur_based" className="text-[13px]">
                      EUR-based
                    </SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1">
                <Label className="text-[11px]">VAT Rate</Label>
                <Input
                  className="h-8 text-[13px]"
                  placeholder="0.20"
                  type="number"
                  step="0.01"
                  value={addState.vat_rate}
                  onChange={(e) =>
                    setAddState((s) => ({ ...s, vat_rate: e.target.value }))
                  }
                />
              </div>
              <div className="space-y-1">
                <Label className="text-[11px]">Rounding</Label>
                <Select
                  value={addState.rounding_rule}
                  onValueChange={(v) =>
                    setAddState((s) => ({
                      ...s,
                      rounding_rule: v as CurrencyConfig["rounding_rule"],
                    }))
                  }
                >
                  <SelectTrigger className="h-8 text-[13px]">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {ROUNDING_OPTIONS.map((opt) => (
                      <SelectItem
                        key={opt.value}
                        value={opt.value}
                        className="text-[13px]"
                      >
                        {opt.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>
            <div className="flex items-center gap-2 pt-1">
              <Button
                size="sm"
                className="h-7 text-[12px]"
                onClick={addCurrency}
                disabled={updateConfig.isPending || !addState.code.trim()}
              >
                {updateConfig.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin mr-1" />
                ) : null}
                Save Currency
              </Button>
              <Button
                size="sm"
                variant="ghost"
                className="h-7 text-[12px]"
                onClick={() => {
                  setShowAddForm(false);
                  setAddState(DEFAULT_ADD_STATE);
                }}
              >
                Cancel
              </Button>
            </div>
          </div>
        )}

        {/* Table */}
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-[13px]">
            <thead>
              <tr className="border-b bg-muted/40">
                <th className="px-4 py-2.5 text-left text-[11px] font-medium text-muted-foreground uppercase tracking-wide">
                  Code
                </th>
                <th className="px-4 py-2.5 text-left text-[11px] font-medium text-muted-foreground uppercase tracking-wide">
                  FX Rate
                </th>
                <th className="px-4 py-2.5 text-left text-[11px] font-medium text-muted-foreground uppercase tracking-wide">
                  Tier
                </th>
                <th className="px-4 py-2.5 text-left text-[11px] font-medium text-muted-foreground uppercase tracking-wide">
                  VAT %
                </th>
                <th className="px-4 py-2.5 text-left text-[11px] font-medium text-muted-foreground uppercase tracking-wide">
                  Rounding
                </th>
                <th className="px-4 py-2.5 text-right text-[11px] font-medium text-muted-foreground uppercase tracking-wide">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody>
              {currencyList.length === 0 && (
                <tr>
                  <td
                    colSpan={6}
                    className="px-4 py-6 text-center text-[13px] text-muted-foreground"
                  >
                    No currencies configured. Add one above.
                  </td>
                </tr>
              )}
              {currencyList.map(([code, cfg], idx) => {
                const isEditing = editingCurrency === code;
                return (
                  <tr
                    key={code}
                    className={`border-b last:border-0 ${idx % 2 === 0 ? "" : "bg-muted/20"} ${isEditing ? "bg-accent/30" : ""}`}
                  >
                    <td className="px-4 py-3 font-medium text-foreground">
                      {code}
                    </td>
                    {isEditing && editRow ? (
                      <>
                        <td className="px-4 py-2">
                          <Input
                            className="h-7 w-28 text-[13px]"
                            value={editRow.fx_rate}
                            onChange={(e) =>
                              setEditRow((r) =>
                                r ? { ...r, fx_rate: e.target.value } : r
                              )
                            }
                            type="number"
                            step="0.0001"
                          />
                        </td>
                        <td className="px-4 py-2">
                          <Select
                            value={editRow.tier}
                            onValueChange={(v) =>
                              setEditRow((r) =>
                                r
                                  ? { ...r, tier: v as CurrencyConfig["tier"] }
                                  : r
                              )
                            }
                          >
                            <SelectTrigger className="h-7 w-28 text-[13px]">
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              <SelectItem value="usd_based" className="text-[13px]">
                                USD-based
                              </SelectItem>
                              <SelectItem value="eur_based" className="text-[13px]">
                                EUR-based
                              </SelectItem>
                            </SelectContent>
                          </Select>
                        </td>
                        <td className="px-4 py-2">
                          <Input
                            className="h-7 w-24 text-[13px]"
                            value={editRow.vat_rate}
                            onChange={(e) =>
                              setEditRow((r) =>
                                r ? { ...r, vat_rate: e.target.value } : r
                              )
                            }
                            type="number"
                            step="0.01"
                            placeholder="0.20"
                          />
                        </td>
                        <td className="px-4 py-2">
                          <Select
                            value={editRow.rounding_rule}
                            onValueChange={(v) =>
                              setEditRow((r) =>
                                r
                                  ? {
                                      ...r,
                                      rounding_rule:
                                        v as CurrencyConfig["rounding_rule"],
                                    }
                                  : r
                              )
                            }
                          >
                            <SelectTrigger className="h-7 w-36 text-[13px]">
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              {ROUNDING_OPTIONS.map((opt) => (
                                <SelectItem
                                  key={opt.value}
                                  value={opt.value}
                                  className="text-[13px]"
                                >
                                  {opt.label}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </td>
                        <td className="px-4 py-2 text-right">
                          <div className="flex items-center justify-end gap-1">
                            <Button
                              size="sm"
                              variant="ghost"
                              className="h-7 w-7 p-0"
                              onClick={() => saveEdit(code)}
                              disabled={updateConfig.isPending}
                            >
                              {updateConfig.isPending ? (
                                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                              ) : (
                                <Check className="h-3.5 w-3.5 text-green-600" />
                              )}
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              className="h-7 w-7 p-0"
                              onClick={cancelEdit}
                            >
                              <X className="h-3.5 w-3.5" />
                            </Button>
                          </div>
                        </td>
                      </>
                    ) : (
                      <>
                        <td className="px-4 py-3 text-foreground">
                          {cfg.fx_rate}
                        </td>
                        <td className="px-4 py-3">
                          <TierBadge tier={cfg.tier} />
                        </td>
                        <td className="px-4 py-3 text-foreground">
                          {cfg.vat_rate != null
                            ? `${(cfg.vat_rate * 100).toFixed(0)}%`
                            : "—"}
                        </td>
                        <td className="px-4 py-3 text-muted-foreground">
                          {ROUNDING_LABELS[cfg.rounding_rule] ?? cfg.rounding_rule}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <div className="flex items-center justify-end gap-1">
                            <Button
                              size="sm"
                              variant="ghost"
                              className="h-7 w-7 p-0"
                              onClick={() => startEdit(code, cfg)}
                            >
                              <Pencil className="h-3.5 w-3.5" />
                            </Button>
                            <AlertDialog>
                              <AlertDialogTrigger asChild>
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  className="h-7 w-7 p-0 text-destructive hover:text-destructive"
                                >
                                  <Trash2 className="h-3.5 w-3.5" />
                                </Button>
                              </AlertDialogTrigger>
                              <AlertDialogContent>
                                <AlertDialogHeader>
                                  <AlertDialogTitle>
                                    Remove {code}?
                                  </AlertDialogTitle>
                                  <AlertDialogDescription>
                                    This will remove {code} from your pricing
                                    configuration. This cannot be undone.
                                  </AlertDialogDescription>
                                </AlertDialogHeader>
                                <AlertDialogFooter>
                                  <AlertDialogCancel>Cancel</AlertDialogCancel>
                                  <AlertDialogAction
                                    onClick={() => deleteCurrency(code)}
                                    className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                                  >
                                    Remove
                                  </AlertDialogAction>
                                </AlertDialogFooter>
                              </AlertDialogContent>
                            </AlertDialog>
                          </div>
                        </td>
                      </>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Test Calculator */}
      <div className="rounded-lg border bg-muted/30 p-4 space-y-4">
        <div className="flex items-center gap-2">
          <Calculator className="h-4 w-4 text-muted-foreground" />
          <p className="text-[13px] font-medium text-foreground">
            Price Test Calculator
          </p>
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <div className="space-y-1">
            <Label className="text-[11px]">USD Price</Label>
            <Input
              className="h-8 w-36 text-[13px]"
              placeholder="99.00"
              type="number"
              step="0.01"
              value={calcUsd}
              onChange={(e) => setCalcUsd(e.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label className="text-[11px]">Target Currency</Label>
            <Select value={calcCurrency} onValueChange={setCalcCurrency}>
              <SelectTrigger className="h-8 w-32 text-[13px]">
                <SelectValue placeholder="Select..." />
              </SelectTrigger>
              <SelectContent>
                {currencyList.map(([code]) => (
                  <SelectItem key={code} value={code} className="text-[13px]">
                    {code}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        {calcResult && calcCurrency && currencies[calcCurrency] && (
          <div className="rounded-lg border bg-background p-4 space-y-2 text-[13px]">
            <div className="flex items-center justify-between">
              <span className="text-muted-foreground">Converted (pre-VAT)</span>
              <span className="font-medium text-foreground">
                {calcResult.converted.toFixed(2)} {calcCurrency}
              </span>
            </div>
            {calcResult.vatAmount > 0 && (
              <div className="flex items-center justify-between">
                <span className="text-muted-foreground">
                  VAT ({((currencies[calcCurrency].vat_rate ?? 0) * 100).toFixed(0)}%)
                </span>
                <span className="font-medium text-foreground">
                  +{calcResult.vatAmount.toFixed(2)} {calcCurrency}
                </span>
              </div>
            )}
            <div className="flex items-center justify-between">
              <span className="text-muted-foreground">Pre-rounding</span>
              <span className="font-medium text-foreground">
                {calcResult.preRound.toFixed(2)} {calcCurrency}
              </span>
            </div>
            <div className="border-t pt-2 flex items-center justify-between">
              <span className="font-semibold text-foreground">Final Price</span>
              <span className="text-base font-bold text-foreground">
                {calcResult.final.toFixed(2)} {calcCurrency}
              </span>
            </div>
            <p className="text-[11px] text-muted-foreground pt-1">
              Rounding:{" "}
              {ROUNDING_LABELS[currencies[calcCurrency].rounding_rule]} &bull;
              Tier: {currencies[calcCurrency].tier === "usd_based" ? "USD × FX" : "USD × EUR × FX"}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
