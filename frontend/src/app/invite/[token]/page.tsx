"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { apiClient } from "@/lib/api-client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Loader2, Lock, User, ArrowRight, Zap } from "lucide-react";

interface InviteInfo {
  email: string;
  role_name: string;
  role_display_name: string;
  tenant_name: string;
  status: string;
  expired: boolean;
}

interface AcceptResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

type PageState =
  | { kind: "loading" }
  | { kind: "valid"; invite: InviteInfo }
  | { kind: "expired" }
  | { kind: "accepted" }
  | { kind: "invalid" }
  | { kind: "error"; message: string };

export default function InviteAcceptPage() {
  const params = useParams();
  const router = useRouter();
  const token = params.token as string;

  const [state, setState] = useState<PageState>({ kind: "loading" });
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) {
      setState({ kind: "invalid" });
      return;
    }

    apiClient
      .get<InviteInfo>(`/api/v1/invites/accept/${token}`)
      .then((data) => {
        if (data.expired) {
          setState({ kind: "expired" });
        } else if (data.status === "accepted") {
          setState({ kind: "accepted" });
        } else {
          setState({ kind: "valid", invite: data });
        }
      })
      .catch((err) => {
        const msg = err instanceof Error ? err.message : "Unknown error";
        if (msg.includes("404") || msg.toLowerCase().includes("not found") || msg.toLowerCase().includes("invalid")) {
          setState({ kind: "invalid" });
        } else {
          setState({ kind: "error", message: msg });
        }
      });
  }, [token]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);

    if (password.length < 8) {
      setFormError("Password must be at least 8 characters.");
      return;
    }
    if (password !== confirmPassword) {
      setFormError("Passwords do not match.");
      return;
    }

    setIsSubmitting(true);
    try {
      const data = await apiClient.post<AcceptResponse>(
        `/api/v1/invites/accept/${token}`,
        { full_name: fullName, password },
      );

      // Store auth tokens and redirect
      localStorage.setItem("access_token", data.access_token);
      if (data.refresh_token) {
        localStorage.setItem("refresh_token", data.refresh_token);
      }
      document.cookie = `access_token=${data.access_token}; path=/; max-age=${60 * 60 * 24 * 7}; samesite=lax`;

      router.push("/chat");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to create account.";
      setFormError(msg);
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <div className="w-full max-w-md">
        {/* Logo */}
        <div className="mb-8 flex items-center justify-center gap-2.5">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary">
            <Zap className="h-5 w-5 text-white" />
          </div>
          <span className="text-lg font-semibold tracking-tight">
            Suite Studio AI
          </span>
        </div>

        {/* Loading */}
        {state.kind === "loading" && (
          <div className="rounded-xl border bg-card p-8 shadow-soft">
            <div className="flex flex-col items-center gap-4 py-8">
              <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
              <p className="text-[15px] text-muted-foreground">
                Loading invitation...
              </p>
            </div>
          </div>
        )}

        {/* Expired */}
        {state.kind === "expired" && (
          <div className="rounded-xl border bg-card p-8 shadow-soft text-center">
            <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-destructive/10">
              <Lock className="h-6 w-6 text-destructive" />
            </div>
            <h2 className="text-xl font-semibold tracking-tight">
              Invitation Expired
            </h2>
            <p className="mt-2 text-[15px] text-muted-foreground">
              This invitation has expired. Contact your admin for a new one.
            </p>
          </div>
        )}

        {/* Already accepted */}
        {state.kind === "accepted" && (
          <div className="rounded-xl border bg-card p-8 shadow-soft text-center">
            <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-primary/10">
              <User className="h-6 w-6 text-primary" />
            </div>
            <h2 className="text-xl font-semibold tracking-tight">
              Already Accepted
            </h2>
            <p className="mt-2 text-[15px] text-muted-foreground">
              This invitation has already been used.
            </p>
            <Link href="/login">
              <Button className="mt-6 h-11 w-full text-[14px] font-medium">
                Go to Login
                <ArrowRight className="ml-2 h-4 w-4" />
              </Button>
            </Link>
          </div>
        )}

        {/* Invalid token */}
        {state.kind === "invalid" && (
          <div className="rounded-xl border bg-card p-8 shadow-soft text-center">
            <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-destructive/10">
              <Lock className="h-6 w-6 text-destructive" />
            </div>
            <h2 className="text-xl font-semibold tracking-tight">
              Invalid Link
            </h2>
            <p className="mt-2 text-[15px] text-muted-foreground">
              Invalid invitation link.
            </p>
          </div>
        )}

        {/* Error */}
        {state.kind === "error" && (
          <div className="rounded-xl border bg-card p-8 shadow-soft text-center">
            <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-destructive/10">
              <Lock className="h-6 w-6 text-destructive" />
            </div>
            <h2 className="text-xl font-semibold tracking-tight">
              Something Went Wrong
            </h2>
            <p className="mt-2 text-[15px] text-muted-foreground">
              {state.message}
            </p>
          </div>
        )}

        {/* Valid invite — signup form */}
        {state.kind === "valid" && (
          <div className="rounded-xl border bg-card p-8 shadow-soft">
            <div className="mb-6 text-center">
              <h2 className="text-xl font-semibold tracking-tight">
                You&apos;ve been invited to join
              </h2>
              <p className="mt-1 text-lg font-medium text-primary">
                {state.invite.tenant_name}
              </p>
              <p className="mt-1 text-[13px] text-muted-foreground">
                on Suite Studio AI
              </p>
            </div>

            <div className="mb-6 rounded-lg border bg-muted/50 p-3 text-center">
              <p className="text-[13px] text-muted-foreground">
                Role:{" "}
                <span className="font-medium text-foreground">
                  {state.invite.role_display_name}
                </span>
              </p>
              <p className="mt-1 text-[13px] text-muted-foreground">
                Email:{" "}
                <span className="font-medium text-foreground">
                  {state.invite.email}
                </span>
              </p>
            </div>

            <form onSubmit={onSubmit} className="space-y-5">
              <div className="space-y-2">
                <Label htmlFor="fullName" className="text-[13px] font-medium">
                  Full Name
                </Label>
                <div className="relative">
                  <User className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    id="fullName"
                    type="text"
                    placeholder="Your full name"
                    value={fullName}
                    onChange={(e) => setFullName(e.target.value)}
                    required
                    className="h-11 pl-10"
                  />
                </div>
              </div>

              <div className="space-y-2">
                <Label htmlFor="password" className="text-[13px] font-medium">
                  Password
                </Label>
                <div className="relative">
                  <Lock className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    id="password"
                    type="password"
                    placeholder="Min. 8 characters"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    required
                    minLength={8}
                    className="h-11 pl-10"
                  />
                </div>
              </div>

              <div className="space-y-2">
                <Label
                  htmlFor="confirmPassword"
                  className="text-[13px] font-medium"
                >
                  Confirm Password
                </Label>
                <div className="relative">
                  <Lock className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    id="confirmPassword"
                    type="password"
                    placeholder="Re-enter password"
                    value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    required
                    minLength={8}
                    className="h-11 pl-10"
                  />
                </div>
              </div>

              {formError && (
                <p className="text-[13px] text-destructive">{formError}</p>
              )}

              <Button
                type="submit"
                className="h-11 w-full text-[14px] font-medium"
                disabled={isSubmitting}
              >
                {isSubmitting ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    Creating account...
                  </>
                ) : (
                  <>
                    Create Account
                    <ArrowRight className="ml-2 h-4 w-4" />
                  </>
                )}
              </Button>

              {/* Divider */}
              <div className="relative flex items-center py-1">
                <div className="flex-1 border-t" />
                <span className="px-3 text-[13px] text-muted-foreground">
                  or
                </span>
                <div className="flex-1 border-t" />
              </div>

              {/* Google SSO stub */}
              <div className="relative group">
                <Button
                  type="button"
                  variant="outline"
                  className="h-11 w-full text-[14px] font-medium"
                  disabled
                >
                  <svg
                    className="mr-2 h-4 w-4"
                    viewBox="0 0 24 24"
                    xmlns="http://www.w3.org/2000/svg"
                  >
                    <path
                      d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z"
                      fill="#4285F4"
                    />
                    <path
                      d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"
                      fill="#34A853"
                    />
                    <path
                      d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"
                      fill="#FBBC05"
                    />
                    <path
                      d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"
                      fill="#EA4335"
                    />
                  </svg>
                  Continue with Google
                </Button>
                <div className="absolute -top-8 left-1/2 -translate-x-1/2 whitespace-nowrap rounded bg-foreground px-2 py-1 text-[11px] text-background opacity-0 transition-opacity group-hover:opacity-100">
                  Coming soon — Phase 2
                </div>
              </div>
            </form>

            <p className="mt-6 text-center text-[13px] text-muted-foreground">
              Already have an account?{" "}
              <Link
                href="/login"
                className="font-medium text-primary hover:underline"
              >
                Log in
              </Link>
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
