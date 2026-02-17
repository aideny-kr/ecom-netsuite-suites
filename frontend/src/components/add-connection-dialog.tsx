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

type Provider = "shopify" | "stripe" | "netsuite";

const credentialFields: Record<Provider, { key: string; label: string }[]> = {
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

  function resetForm() {
    setProvider("");
    setLabel("");
    setCredentials({});
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!provider) return;

    try {
      await createConnection.mutateAsync({
        provider,
        label,
        credentials,
      });
      toast({ title: "Connection created successfully" });
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
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button className="text-[13px] font-medium">
          <Plus className="mr-2 h-4 w-4" />
          Add Connection
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="text-lg">Add Connection</DialogTitle>
          <DialogDescription className="text-[13px]">
            Connect a new platform to sync data.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={onSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label className="text-[13px] font-medium">Provider</Label>
            <Select
              value={provider}
              onValueChange={(v) => {
                setProvider(v as Provider);
                setCredentials({});
              }}
            >
              <SelectTrigger className="h-10 text-[13px]">
                <SelectValue placeholder="Select a provider" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="shopify">Shopify</SelectItem>
                <SelectItem value="stripe">Stripe</SelectItem>
                <SelectItem value="netsuite">NetSuite</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <Label htmlFor="conn-label" className="text-[13px] font-medium">
              Label
            </Label>
            <Input
              id="conn-label"
              placeholder="e.g., Production Shopify"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              required
              className="h-10 text-[13px]"
            />
          </div>

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
              {createConnection.isPending ? "Creating..." : "Create"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
