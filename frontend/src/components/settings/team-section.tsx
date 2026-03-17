"use client";

import { useState } from "react";
import { useAuth } from "@/providers/auth-provider";
import { usePermissions } from "@/hooks/use-permissions";
import {
  useTeamMembers,
  useTeamInvites,
  useCreateInvite,
  useRevokeInvite,
  useChangeUserRole,
  useDeactivateUser,
} from "@/hooks/use-team";
import type { TeamMember, TeamInvite } from "@/hooks/use-team";
import { ROLE_DISPLAY_NAMES } from "@/lib/types";
import type { RoleName } from "@/lib/types";
import { useToast } from "@/hooks/use-toast";
import {
  Users,
  Mail,
  MoreVertical,
  Shield,
  UserX,
  UserPlus,
  Loader2,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  DropdownMenuSub,
  DropdownMenuSubTrigger,
  DropdownMenuSubContent,
} from "@/components/ui/dropdown-menu";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

// TODO: MAX_SEATS should come from the API (entitlement_service.get_plan_limits)
const MAX_SEATS = 20;

const ASSIGNABLE_ROLES: { value: RoleName; label: string }[] = [
  { value: "admin", label: "Admin" },
  { value: "finance", label: "User" },
  { value: "ops", label: "Operations Only" },
];

export function TeamSection() {
  const { user } = useAuth();
  const { hasPermission } = usePermissions();
  const { toast } = useToast();

  const { data: members, isLoading: membersLoading } = useTeamMembers();
  const { data: invites, isLoading: invitesLoading } = useTeamInvites();
  const createInvite = useCreateInvite();
  const revokeInvite = useRevokeInvite();
  const changeRole = useChangeUserRole();
  const deactivateUser = useDeactivateUser();

  const [inviteOpen, setInviteOpen] = useState(false);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<RoleName>("finance");
  const [deactivateTarget, setDeactivateTarget] = useState<TeamMember | null>(null);

  if (!hasPermission("users.manage")) return null;

  const activeMembers = members?.filter((m) => m.is_active) ?? [];
  const pendingInvites = invites?.filter((i) => i.status === "pending") ?? [];
  const seatCount = activeMembers.length + pendingInvites.length;

  async function handleInvite() {
    if (!inviteEmail.trim()) return;
    try {
      await createInvite.mutateAsync({ email: inviteEmail.trim(), role_name: inviteRole });
      toast({ title: "Invitation sent", description: `Invited ${inviteEmail.trim()}` });
      setInviteEmail("");
      setInviteRole("finance");
      setInviteOpen(false);
    } catch (err) {
      toast({
        title: "Failed to send invitation",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  async function handleRevoke(invite: TeamInvite) {
    try {
      await revokeInvite.mutateAsync(invite.id);
      toast({ title: "Invitation revoked", description: invite.email });
    } catch (err) {
      toast({
        title: "Failed to revoke invitation",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  async function handleChangeRole(member: TeamMember, newRole: RoleName) {
    try {
      await changeRole.mutateAsync({ userId: member.id, roles: [newRole] });
      toast({ title: "Role updated", description: `${member.full_name} is now ${ROLE_DISPLAY_NAMES[newRole]}` });
    } catch (err) {
      toast({
        title: "Failed to change role",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  async function handleDeactivate() {
    if (!deactivateTarget) return;
    try {
      await deactivateUser.mutateAsync(deactivateTarget.id);
      toast({ title: "User deactivated", description: deactivateTarget.email });
      setDeactivateTarget(null);
    } catch (err) {
      toast({
        title: "Failed to deactivate user",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  function getRoleDisplay(roles: string[]): string {
    if (!roles?.length) return "Unknown";
    const role = roles[0] as RoleName;
    return ROLE_DISPLAY_NAMES[role] ?? role;
  }

  function formatDate(iso: string): string {
    const d = new Date(iso);
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  }

  const isSelf = (memberId: string) => memberId === user?.id;

  return (
    <>
      <div className="rounded-xl border bg-card p-5 shadow-soft space-y-5">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-lg font-semibold flex items-center gap-2">
              <Users className="h-5 w-5 text-muted-foreground" />
              Team
            </h3>
            <p className="mt-0.5 text-[13px] text-muted-foreground">
              Manage your team members and invitations
            </p>
          </div>
          <Button size="sm" onClick={() => setInviteOpen(true)}>
            <UserPlus className="mr-1.5 h-4 w-4" />
            Invite Team Member
          </Button>
        </div>

        {/* Members */}
        <div>
          <p className="text-[13px] font-medium text-muted-foreground mb-2">
            Members ({seatCount} of {MAX_SEATS})
          </p>
          {membersLoading ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
            </div>
          ) : !activeMembers.length ? (
            <p className="text-[13px] text-muted-foreground py-4 text-center">No members found</p>
          ) : (
            <div className="divide-y rounded-lg border">
              {activeMembers.map((member) => (
                <div
                  key={member.id}
                  className="flex items-center justify-between px-4 py-3"
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-muted text-[13px] font-medium text-muted-foreground">
                      {member.full_name?.[0]?.toUpperCase() ?? "?"}
                    </div>
                    <div className="min-w-0">
                      <p className="text-[13px] font-medium text-foreground truncate">
                        {member.full_name}
                      </p>
                      <p className="text-[11px] text-muted-foreground truncate">
                        {member.email}
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge variant="secondary" className="text-[11px]">
                      {getRoleDisplay(member.roles)}
                    </Badge>
                    {isSelf(member.id) && (
                      <Badge variant="outline" className="text-[11px]">you</Badge>
                    )}
                    {!isSelf(member.id) && (
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <Button variant="ghost" size="icon" className="h-7 w-7">
                            <MoreVertical className="h-4 w-4" />
                          </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end">
                          <DropdownMenuSub>
                            <DropdownMenuSubTrigger>
                              <Shield className="mr-2 h-4 w-4" />
                              Change Role
                            </DropdownMenuSubTrigger>
                            <DropdownMenuSubContent>
                              {ASSIGNABLE_ROLES.map((r) => (
                                <DropdownMenuItem
                                  key={r.value}
                                  onClick={() => handleChangeRole(member, r.value)}
                                  disabled={member.roles?.includes(r.value)}
                                >
                                  {r.label}
                                  {member.roles?.includes(r.value) && (
                                    <span className="ml-auto text-[11px] text-muted-foreground">current</span>
                                  )}
                                </DropdownMenuItem>
                              ))}
                            </DropdownMenuSubContent>
                          </DropdownMenuSub>
                          <DropdownMenuItem
                            className="text-destructive focus:text-destructive"
                            onClick={() => setDeactivateTarget(member)}
                          >
                            <UserX className="mr-2 h-4 w-4" />
                            Deactivate
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Pending Invitations */}
        <div>
          <p className="text-[13px] font-medium text-muted-foreground mb-2">
            Pending Invitations ({pendingInvites.length})
          </p>
          {invitesLoading ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
            </div>
          ) : !pendingInvites.length ? (
            <p className="text-[13px] text-muted-foreground py-4 text-center">
              No pending invitations
            </p>
          ) : (
            <div className="divide-y rounded-lg border">
              {pendingInvites.map((invite) => (
                <div
                  key={invite.id}
                  className="flex items-center justify-between px-4 py-3"
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-muted">
                      <Mail className="h-4 w-4 text-muted-foreground" />
                    </div>
                    <div className="min-w-0">
                      <p className="text-[13px] font-medium text-foreground truncate">
                        {invite.email}
                      </p>
                      <p className="text-[11px] text-muted-foreground">
                        {invite.role_display_name ?? invite.role_name} &middot; Sent {formatDate(invite.created_at)}
                      </p>
                    </div>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7 text-muted-foreground hover:text-destructive"
                    onClick={() => handleRevoke(invite)}
                    disabled={revokeInvite.isPending}
                  >
                    <X className="h-4 w-4" />
                  </Button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Invite Dialog */}
      <Dialog open={inviteOpen} onOpenChange={setInviteOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Invite Team Member</DialogTitle>
            <DialogDescription>
              Send an invitation email to add a new team member.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 pt-2">
            <div>
              <label className="text-[13px] font-medium text-foreground">Email</label>
              <input
                type="email"
                placeholder="colleague@company.com"
                value={inviteEmail}
                onChange={(e) => setInviteEmail(e.target.value)}
                className="mt-1.5 flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-[13px] ring-offset-background placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
                onKeyDown={(e) => {
                  if (e.key === "Enter") handleInvite();
                }}
              />
            </div>
            <div>
              <label className="text-[13px] font-medium text-foreground">Role</label>
              <Select
                value={inviteRole}
                onValueChange={(v) => setInviteRole(v as RoleName)}
              >
                <SelectTrigger className="mt-1.5 text-[13px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ASSIGNABLE_ROLES.map((r) => (
                    <SelectItem key={r.value} value={r.value} className="text-[13px]">
                      {r.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex justify-end gap-2 pt-2">
              <Button variant="outline" size="sm" onClick={() => setInviteOpen(false)}>
                Cancel
              </Button>
              <Button
                size="sm"
                onClick={handleInvite}
                disabled={!inviteEmail.trim() || createInvite.isPending}
              >
                {createInvite.isPending && <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />}
                Send Invitation
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      {/* Deactivate Confirm */}
      <AlertDialog open={!!deactivateTarget} onOpenChange={(open) => !open && setDeactivateTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Deactivate user</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to deactivate{" "}
              <span className="font-medium text-foreground">{deactivateTarget?.full_name}</span>?
              They will lose access to this workspace immediately.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDeactivate}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deactivateUser.isPending && <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />}
              Deactivate
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
