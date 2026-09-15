"use client";

import { useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useCreateConnection } from "@/hooks/use-connections";
import { useToast } from "@/hooks/use-toast";
import { Plus } from "lucide-react";

type Provider = "shopify" | "stripe" | "netsuite" | "solidus" | "api";

const credentialFields: Record<Provider, { key: string; label: string }[]> = {
  solidus: [],
  api: [],
  shopify: [
    { key: "shop_domain", label: "Shop Domain" },
    { key: "api_key", label: "API Key" },
    { key: "api_secret", label: "API Secret" },
    { key: "access_token", label: "Access Token" },
  ],
  stripe: [
    { key: "api_key", label: "Secret Key" },
    { key: "webhook_secret", label: "Webhook Secret" },
  ],
  netsuite: [
    { key: "account_id", label: "Account ID" },
    { key: "consumer_key", label: "Consumer Key" },
    { key: "consumer_secret", label: "Consumer Secret" },
    { key: "token_id", label: "Token ID" },
    { key: "token_secret", label: "Token Secret" },
  ],
};

export function AddConnectionDialog() {
  const [open, setOpen] = useState(false);
  const [provider, setProvider] = useState<Provider | "">("");
  const [label, setLabel] = useState("");
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const createConnection = useCreateConnection();
  const { toast } = useToast();
  const isHttp = provider === "solidus" || provider === "api";
  function setCredential(key: string, value: string) {
    setCredentials((current) => ({ ...current, [key]: value }));
  }

  function resetForm() {
    setProvider("");
    setLabel("");
    setCredentials({});
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!provider) return;

    try {
      const result = await createConnection.mutateAsync({
        provider,
        label,
        credentials,
      });
      toast({ title: result.status === "error" ? "Saved — read access needs attention" : "Connection saved", description: result.status === "error" ? "Check the credential and endpoint, then test the connection." : undefined });
      setOpen(false);
      resetForm();
    } catch (err) {
      toast({
        title: "Failed to create connection",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { setOpen(next); if (!next) resetForm(); }}>
      <DialogTrigger asChild>
        <Button className="text-[13px] font-medium">
          <Plus className="mr-2 h-4 w-4" />
          Add Connection
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="text-lg">Add Connection</DialogTitle>
          <DialogDescription className="text-[13px]">
            Add a platform or API. Solidus and custom APIs are verified with a read request.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={onSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label className="text-[13px] font-medium">Provider</Label>
            <Select
              value={provider}
              onValueChange={(v) => {
                setProvider(v as Provider);
                setCredentials(v === "solidus" ? { auth_type: "bearer", api_profile: "solidus_rest" } : v === "api" ? { auth_type: "bearer" } : {});
              }}
            >
              <SelectTrigger className="h-10 text-[13px]">
                <SelectValue placeholder="Select a provider" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="shopify">Shopify</SelectItem>
                <SelectItem value="stripe">Stripe</SelectItem>
                <SelectItem value="netsuite">NetSuite</SelectItem>
                <SelectItem value="solidus">Solidus</SelectItem>
                <SelectItem value="api">Custom API</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <Label htmlFor="conn-label" className="text-[13px] font-medium">
              Label
            </Label>
            <Input
              id="conn-label"
              placeholder="e.g., Framework Solidus"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              required
              className="h-10 text-[13px]"
            />
          </div>

          {isHttp && (
            <>
              {provider === "solidus" && (
                <label className="block space-y-2 text-[13px]">
                  API profile
                  <select className="h-10 w-full rounded-md border bg-background px-3" value={credentials.api_profile} onChange={(e) => setCredentials((current) => ({ ...current, api_profile: e.target.value, base_url: e.target.value === "framework_sync" ? "https://private-direct-access.frame.work/api/" : "" }))}>
                    <option value="solidus_rest">Solidus REST</option>
                    <option value="framework_sync">Framework Sync — order investigations</option>
                  </select>
                </label>
              )}
              <div className="space-y-2">
                <Label htmlFor="api-base-url">API base URL</Label>
                <Input id="api-base-url" type="url" value={credentials.base_url || ""} onChange={(e) => setCredential("base_url", e.target.value)} placeholder="https://store.example.com/api/" required />
              </div>
              <label className="block space-y-2 text-[13px]">
                Authentication
                <select className="h-10 w-full rounded-md border bg-background px-3" value={credentials.auth_type} onChange={(e) => setCredentials((current) => ({ ...current, auth_type: e.target.value, token: "", header_name: "" }))}>
                  <option value="bearer">Bearer token</option>
                  <option value="api_key">API key in header</option>
                  {provider === "api" && <option value="none">None</option>}
                </select>
              </label>
              {credentials.auth_type === "api_key" && <div className="space-y-2"><Label htmlFor="api-header">Authentication header</Label><Input id="api-header" value={credentials.header_name || ""} onChange={(e) => setCredential("header_name", e.target.value)} placeholder="X-API-Key" /></div>}
              {credentials.auth_type !== "none" && <div className="space-y-2"><Label htmlFor="api-token">API token</Label><Input id="api-token" type="password" autoComplete="new-password" value={credentials.token || ""} onChange={(e) => setCredential("token", e.target.value)} required /></div>}
              {provider === "api" && <div className="space-y-2"><Label htmlFor="api-test-path">Read endpoint for verification</Label><Input id="api-test-path" value={credentials.test_path || ""} onChange={(e) => setCredential("test_path", e.target.value)} placeholder="v1/health" required /><p className="text-[13px] text-muted-foreground">A relative GET endpoint returning JSON. No URL credentials or query tokens.</p></div>}
              {provider === "solidus" && <p className="text-[13px] text-muted-foreground">Use a credential with order read access. Framework Sync supports order investigations; standard REST is saved and tested here. Select the authentication configured by your store.</p>}
            </>
          )}

          {provider &&
            credentialFields[provider].map((field) => (
              <div key={field.key} className="space-y-2">
                <Label htmlFor={field.key} className="text-[13px] font-medium">
                  {field.label}
                </Label>
                <Input
                  id={field.key}
                  type="password"
                  value={credentials[field.key] || ""}
                  onChange={(e) =>
                    setCredentials((prev) => ({
                      ...prev,
                      [field.key]: e.target.value,
                    }))
                  }
                  required
                  className="h-10 text-[13px]"
                />
              </div>
            ))}

          <DialogFooter className="gap-2 pt-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => setOpen(false)}
              className="text-[13px]"
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={!provider || createConnection.isPending}
              className="text-[13px]"
            >
              {createConnection.isPending ? (isHttp ? "Verifying…" : "Creating…") : isHttp ? "Save and verify" : "Create"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
