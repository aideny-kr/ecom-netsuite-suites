"use client";

import { useState } from "react";
import Link from "next/link";
import { useAuth } from "@/providers/auth-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/hooks/use-toast";
import { OrbitalLoginShell } from "@/components/auth/orbital-login-shell";
import dynamic from "next/dynamic";
const GoogleLogin = dynamic(
  () => import("@react-oauth/google").then((m) => m.GoogleLogin),
  { ssr: false },
);
import { apiClient } from "@/lib/api-client";
import { Loader2 } from "lucide-react";

export default function LoginPage() {
  const { login } = useAuth();
  const { toast } = useToast();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [isGoogleLoading, setIsGoogleLoading] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setIsLoading(true);
    try {
      await login({ email, password });
    } catch (err) {
      toast({
        title: "Login failed",
        description:
          err instanceof Error ? err.message : "Invalid credentials",
        variant: "destructive",
      });
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <OrbitalLoginShell>
          <div className="mb-8">
            <h1 className="text-3xl font-medium tracking-tight text-foreground">
              Sign in
            </h1>
            <p className="mt-1.5 text-sm text-muted-foreground">
              Welcome back. Your workspace is ready.
            </p>
          </div>

          <form onSubmit={onSubmit} className="space-y-5">
            <div className="space-y-2">
              <Label htmlFor="email" className="text-[13px] font-medium text-foreground">
                Email
              </Label>
              <Input
                id="email"
                type="email"
                autoComplete="username"
                placeholder="you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                className="h-12 rounded-md border-input bg-background text-foreground placeholder:text-muted-foreground focus-visible:ring-ring focus-visible:border-primary"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="password" className="text-[13px] font-medium text-foreground">
                Password
              </Label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                className="h-12 rounded-md border-input bg-background text-foreground placeholder:text-muted-foreground focus-visible:ring-ring focus-visible:border-primary"
              />
            </div>
            <button
              type="submit"
              className="h-12 w-full rounded-md bg-primary text-[14px] font-semibold text-primary-foreground hover:bg-primary/85 transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-primary disabled:opacity-50"
              disabled={isLoading}
            >
              {isLoading ? "Signing in..." : "Sign in"}
            </button>
          </form>

          {process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID && <>
          <div className="relative my-6">
            <div className="absolute inset-0 flex items-center">
              <span className="w-full border-t border-border" />
            </div>
            <div className="relative flex justify-center text-xs uppercase">
              <span className="bg-card px-3 text-muted-foreground tracking-widest">or</span>
            </div>
          </div>

          {isGoogleLoading ? (
            <Button
              variant="outline"
              className="h-11 w-full text-[14px] font-medium"
              disabled
            >
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              Signing in with Google...
            </Button>
          ) : (
            <div className="flex justify-center">
              <GoogleLogin
                onSuccess={async (credentialResponse) => {
                  if (!credentialResponse.credential) return;
                  setIsGoogleLoading(true);
                  try {
                    const res = await apiClient.post<{ access_token: string; refresh_token: string }>(
                      "/api/v1/auth/google",
                      { google_id_token: credentialResponse.credential },
                    );
                    localStorage.setItem("access_token", res.access_token);
                    document.cookie = `access_token=${res.access_token}; path=/; max-age=604800; samesite=lax`;
                    window.location.href = "/chat";
                  } catch (err) {
                    toast({
                      title: "Google sign-in failed",
                      description: err instanceof Error ? err.message : "Could not sign in with Google",
                      variant: "destructive",
                    });
                  } finally {
                    setIsGoogleLoading(false);
                  }
                }}
                onError={() => {
                  toast({
                    title: "Google sign-in failed",
                    description: "Google authentication was cancelled or failed",
                    variant: "destructive",
                  });
                }}
                text="signin_with"
                shape="rectangular"
              />
            </div>
          )}

          </>}

          <p className="mt-6 text-center text-[13px] text-muted-foreground">
            Don&apos;t have an account?{" "}
            <Link
              href="/register"
              className="font-medium text-primary hover:underline"
            >
              Create one
            </Link>
          </p>
    </OrbitalLoginShell>
  );
}
