"use client";

import { useState } from "react";
import {
  useJevRemoveKey,
  useJevSaveKey,
  useJevSetMode,
  useJevStatus,
  useJevTest,
  type JevMode,
} from "@/hooks/use-jev";
import { usePermissions } from "@/hooks/use-permissions";
import { useToast } from "@/hooks/use-toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { AlertTriangle, FlaskConical, KeyRound, Loader2, ShieldCheck, Trash2, Zap } from "lucide-react";

const MODES: { value: JevMode; label: string; detail: string }[] = [
  { value: "live", label: "Live", detail: "Jev decides when it is confident; otherwise the model does." },
  { value: "shadow", label: "Shadow", detail: "The model decides; Jev's answer is recorded beside it." },
  { value: "off", label: "Off", detail: "Only the model is used." },
];

const MODE_LABEL: Record<JevMode, string> = { live: "Live", shadow: "Shadow", off: "Off" };

const BADGE_CLASS: Record<JevMode, string> = {
  live: "border-green-500/50 bg-green-500/10 text-green-700 dark:text-green-400",
  shadow: "border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-400",
  off: "text-muted-foreground",
};

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : "Unknown error";
}

export default function JevConnectorCard() {
  const { data: status, isLoading, isError } = useJevStatus();
  const setMode = useJevSetMode();
  const saveKey = useJevSaveKey();
  const removeKey = useJevRemoveKey();
  const testKey = useJevTest();
  const { hasPermission } = usePermissions();
  const { toast } = useToast();
  const canManage = hasPermission("connections.manage");
  const [key, setKey] = useState("");
  const busy = setMode.isPending || saveKey.isPending || removeKey.isPending || testKey.isPending;

  async function chooseMode(mode: JevMode) {
    try {
      await setMode.mutateAsync(mode);
      toast({ title: `Jev set to ${MODE_LABEL[mode]}` });
    } catch (err) {
      toast({ title: "Could not change the mode", description: errorMessage(err), variant: "destructive" });
    }
  }

  async function save() {
    try {
      await saveKey.mutateAsync(key.trim());
      setKey("");
      toast({ title: "Jev key saved" });
    } catch (err) {
      // The backend's detail never echoes the key.
      toast({ title: "Key not saved", description: errorMessage(err), variant: "destructive" });
    }
  }

  async function remove() {
    try {
      await removeKey.mutateAsync();
      toast({ title: "Jev key removed" });
    } catch (err) {
      toast({ title: "Could not remove the key", description: errorMessage(err), variant: "destructive" });
    }
  }

  async function test() {
    try {
      const candidate = key.trim();
      const result = await testKey.mutateAsync(candidate || undefined);
      toast(
        result.success
          ? { title: "Jev key works" }
          : { title: "Jev key check failed", description: result.error ?? undefined, variant: "destructive" },
      );
    } catch (err) {
      toast({ title: "Test failed", description: errorMessage(err), variant: "destructive" });
    }
  }

  if (isLoading) {
    return (
      <div id="jev" className="scroll-mt-6 rounded-xl border bg-card p-5 shadow-soft animate-pulse">
        <div className="h-6 w-40 rounded bg-muted" />
        <div className="mt-3 h-4 w-64 rounded bg-muted" />
      </div>
    );
  }

  if (isError || !status) {
    return (
      <div id="jev" role="alert" className="scroll-mt-6 rounded-xl border bg-card p-5 text-[13px] shadow-soft">
        Jev settings could not be loaded.
      </div>
    );
  }

  const capped = status.deployment_cap !== "live" && status.mode !== "off" && status.effective_mode !== status.mode;

  return (
    <div id="jev" className="scroll-mt-6 space-y-4 rounded-xl border bg-card p-5 shadow-soft">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Zap className="h-4 w-4 text-violet-500" />
          <h3 className="text-[15px] font-semibold">TypeSafe Jev</h3>
        </div>
        <Badge
          variant="outline"
          role="status"
          aria-label={`Jev is ${MODE_LABEL[status.effective_mode].toLowerCase()}`}
          className={`text-[11px] ${BADGE_CLASS[status.effective_mode]}`}
        >
          {MODE_LABEL[status.effective_mode]}
        </Badge>
      </div>

      <p className="text-[13px] text-muted-foreground">
        Classifies reconciliation exceptions in about a third of a second, beside the model.
      </p>
      <div className="flex items-start gap-1.5 text-[12px] text-muted-foreground">
        <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-green-600" />
        <span>
          Jev receives only yes/no and category facts, with no amounts, text or identifiers. Every proposal still
          needs human approval, and nothing is posted to NetSuite.
        </span>
      </div>

      {canManage && (
        <div role="radiogroup" aria-label="Jev mode" className="grid gap-2 sm:grid-cols-3">
          {MODES.map((option) => {
            const checked = status.mode === option.value;
            return (
              <button
                key={option.value}
                type="button"
                role="radio"
                aria-checked={checked}
                disabled={busy}
                onClick={() => !checked && void chooseMode(option.value)}
                className={`rounded-lg border p-3 text-left transition-colors ${
                  checked ? "border-primary bg-primary/5" : "hover:bg-muted/50"
                }`}
              >
                <span className="block text-[13px] font-medium">{option.label}</span>
                <span className="mt-1 block text-[12px] text-muted-foreground">{option.detail}</span>
              </button>
            );
          })}
        </div>
      )}

      {capped && (
        <p className="text-[12px] text-muted-foreground">
          This deployment limits Jev to {MODE_LABEL[status.deployment_cap]}.
        </p>
      )}

      {status.problem === "unreadable_key" && (
        <div className="flex gap-2 rounded-lg border border-destructive/50 bg-destructive/5 p-3">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
          <p className="text-[12px]">
            The stored key could not be read, so Jev is off for this workspace. Remove it or save a new key.
          </p>
        </div>
      )}

      <div className="space-y-3 border-t pt-4">
        <div className="flex items-center gap-1.5 text-[13px]">
          <KeyRound className="h-3.5 w-3.5 text-muted-foreground" />
          {status.key_source === "tenant" && status.key_hint ? (
            <span>Using your TypeSafe key ending {status.key_hint}.</span>
          ) : status.key_source === "platform" ? (
            <span>Using this deployment&apos;s key.</span>
          ) : status.key_source === "none" ? (
            <span className="text-amber-700 dark:text-amber-400">
              No Jev key is configured. Add your TypeSafe key to turn Jev on.
            </span>
          ) : (
            <span>Using your TypeSafe key.</span>
          )}
        </div>

        {canManage && (
          <>
            <div className="space-y-1.5">
              <Label htmlFor="jev-key" className="text-[13px]">
                Your TypeSafe key{" "}
                <span className="font-normal text-muted-foreground">
                  {status.key_source === "platform" ? "(optional, replaces the deployment's key)" : ""}
                </span>
              </Label>
              <Input
                id="jev-key"
                type="password"
                autoComplete="off"
                placeholder="ts_..."
                value={key}
                onChange={(e) => setKey(e.target.value)}
                className="font-mono text-[13px]"
              />
              <p className="text-[12px] text-muted-foreground">Checked with TypeSafe, then stored encrypted.</p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" onClick={() => void save()} disabled={!key.trim() || busy}>
                {saveKey.isPending && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
                Save key
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => void test()}
                disabled={busy || (!key.trim() && status.key_source === "none")}
              >
                {testKey.isPending ? (
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                ) : (
                  <FlaskConical className="mr-1.5 h-3.5 w-3.5" />
                )}
                Test key
              </Button>
              {status.key_source === "tenant" && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="text-destructive hover:text-destructive"
                  onClick={() => void remove()}
                  disabled={busy}
                >
                  <Trash2 className="mr-1.5 h-3.5 w-3.5" />
                  Remove key
                </Button>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
