"use client";

import { useState } from "react";
import Link from "next/link";
import { useAuth } from "@/providers/auth-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/hooks/use-toast";
import { Zap } from "lucide-react";

export default function RegisterPage() {
  const { register } = useAuth();
  const { toast } = useToast();
  const [tenantName, setTenantName] = useState("");
  const [tenantSlug, setTenantSlug] = useState("");
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [isLoading, setIsLoading] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setIsLoading(true);
    try {
      await register({
        tenant_name: tenantName,
        tenant_slug: tenantSlug,
        email,
        password,
        full_name: fullName,
      });
    } catch (err) {
      toast({
        title: "Registration failed",
        description:
          err instanceof Error ? err.message : "Something went wrong",
        variant: "destructive",
      });
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen">
      {/* Left Panel - Branding */}
      <div className="hidden lg:flex lg:w-[480px] lg:flex-col lg:justify-between bg-[hsl(240_11%_4%)] p-10 text-white">
        <div className="flex items-center gap-2.5">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary">
            <Zap className="h-5 w-5 text-white" />
          </div>
          <span className="text-lg font-semibold tracking-tight">
            Suite Studio AI
          </span>
        </div>
        <div>
          <h2 className="text-3xl font-semibold leading-tight tracking-tight">
            Get started in
            <br />
            minutes, not days
          </h2>
          <p className="mt-4 text-[15px] leading-relaxed text-white/50">
            Set up your organization, connect your platforms, and start
            syncing data with NetSuite right away.
          </p>
        </div>
        <p className="text-xs text-white/30">
          Suite Studio AI
        </p>
      </div>

      {/* Right Panel - Form */}
      <div className="flex flex-1 items-center justify-center px-6">
        <div className="w-full max-w-[380px]">
          {/* Mobile logo */}
          <div className="mb-8 flex items-center gap-2.5 lg:hidden">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary">
              <Zap className="h-5 w-5 text-white" />
            </div>
            <span className="text-lg font-semibold tracking-tight">
              Suite Studio AI
            </span>
          </div>

          <div className="mb-8">
            <h1 className="text-2xl font-semibold tracking-tight">
              Create account
            </h1>
            <p className="mt-1.5 text-[15px] text-muted-foreground">
              Register your organization to get started
            </p>
          </div>

          <form onSubmit={onSubmit} className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2">
                <Label htmlFor="tenantName" className="text-[13px] font-medium">
                  Organization
                </Label>
                <Input
                  id="tenantName"
                  placeholder="Acme Inc"
                  value={tenantName}
                  onChange={(e) => setTenantName(e.target.value)}
                  required
                  className="h-11"
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="tenantSlug" className="text-[13px] font-medium">
                  Slug
                </Label>
                <Input
                  id="tenantSlug"
                  placeholder="acme-inc"
                  value={tenantSlug}
                  onChange={(e) => setTenantSlug(e.target.value)}
                  required
                  className="h-11"
                />
              </div>
            </div>
            <div className="space-y-2">
              <Label htmlFor="fullName" className="text-[13px] font-medium">
                Full Name
              </Label>
              <Input
                id="fullName"
                placeholder="John Doe"
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
                required
                className="h-11"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="email" className="text-[13px] font-medium">
                Email
              </Label>
              <Input
                id="email"
                type="email"
                placeholder="you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                className="h-11"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="password" className="text-[13px] font-medium">
                Password
              </Label>
              <Input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                minLength={8}
                className="h-11"
              />
            </div>
            <Button
              type="submit"
              className="h-11 w-full text-[14px] font-medium"
              disabled={isLoading}
            >
              {isLoading ? "Creating account..." : "Create account"}
            </Button>
          </form>

          <p className="mt-6 text-center text-[13px] text-muted-foreground">
            Already have an account?{" "}
            <Link
              href="/login"
              className="font-medium text-primary hover:underline"
            >
              Sign in
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
