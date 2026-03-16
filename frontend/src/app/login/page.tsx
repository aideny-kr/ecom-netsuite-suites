"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/providers/auth-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/hooks/use-toast";
import { Zap } from "lucide-react";
import { GoogleLogin } from "@react-oauth/google";
import { apiClient } from "@/lib/api-client";

export default function LoginPage() {
  const { login } = useAuth();
  const { toast } = useToast();
  const router = useRouter();
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
            Streamline your
            <br />
            e-commerce operations
          </h2>
          <p className="mt-4 text-[15px] leading-relaxed text-white/50">
            Connect Shopify, Stripe, and NetSuite in one unified platform.
            Automate data syncing, reconciliation, and journal postings.
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
              Sign in
            </h1>
            <p className="mt-1.5 text-[15px] text-muted-foreground">
              Enter your credentials to access your account
            </p>
          </div>

          <form onSubmit={onSubmit} className="space-y-5">
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
                className="h-11"
              />
            </div>
            <Button
              type="submit"
              className="h-11 w-full text-[14px] font-medium"
              disabled={isLoading}
            >
              {isLoading ? "Signing in..." : "Sign in"}
            </Button>
          </form>

          <div className="relative my-6">
            <div className="absolute inset-0 flex items-center">
              <span className="w-full border-t" />
            </div>
            <div className="relative flex justify-center text-xs uppercase">
              <span className="bg-background px-2 text-muted-foreground">or</span>
            </div>
          </div>

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
                  router.push("/chat");
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
              width={380}
            />
          </div>

          <p className="mt-6 text-center text-[13px] text-muted-foreground">
            Don&apos;t have an account?{" "}
            <Link
              href="/register"
              className="font-medium text-primary hover:underline"
            >
              Create one
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
