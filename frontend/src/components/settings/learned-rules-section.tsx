"use client";

import { useState } from "react";

import { Plus, Trash2 } from "lucide-react";

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
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/hooks/use-toast";
import {
  useCreateLearnedRule,
  useDeleteLearnedRule,
  useLearnedRules,
  useUpdateLearnedRule,
  type LearnedRule,
} from "@/hooks/use-learned-rules";

export function LearnedRulesSection() {
  const { data, isLoading, error } = useLearnedRules();
  const createRule = useCreateLearnedRule();
  const updateRule = useUpdateLearnedRule();
  const deleteRule = useDeleteLearnedRule();
  const { toast } = useToast();

  const [adding, setAdding] = useState(false);
  const [newDescription, setNewDescription] = useState("");
  const [newCategory, setNewCategory] = useState("");

  const rules = (data ?? []) as LearnedRule[];

  async function handleToggle(rule: LearnedRule) {
    try {
      await updateRule.mutateAsync({ id: rule.id, is_active: !rule.is_active });
      toast({ title: rule.is_active ? "Rule deactivated" : "Rule activated" });
    } catch {
      toast({ title: "Failed to update rule", variant: "destructive" });
    }
  }

  async function handleAdd() {
    const description = newDescription.trim();
    if (!description) return;
    try {
      await createRule.mutateAsync({
        rule_description: description,
        rule_category: newCategory.trim() || undefined,
      });
      toast({ title: "Rule added" });
      setNewDescription("");
      setNewCategory("");
      setAdding(false);
    } catch {
      toast({ title: "Failed to add rule", variant: "destructive" });
    }
  }

  async function handleDelete(rule: LearnedRule) {
    try {
      await deleteRule.mutateAsync(rule.id);
      toast({ title: "Rule deleted" });
    } catch {
      toast({ title: "Failed to delete rule", variant: "destructive" });
    }
  }

  return (
    <div className="space-y-4 rounded-xl border bg-card p-5 shadow-soft">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="text-[15px] font-medium">Learned Rules</h3>
          <p className="text-[13px] text-muted-foreground">
            Tenant-specific business rules injected into every chat turn. Deactivate or remove a rule if it skews
            answers.
          </p>
        </div>
        {!isLoading && !error && (
          <Button size="sm" variant="outline" onClick={() => setAdding((v) => !v)}>
            <Plus className="mr-1 h-3.5 w-3.5" />
            Add Rule
          </Button>
        )}
      </div>

      {isLoading ? (
        <div data-testid="learned-rules-loading" className="space-y-2">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
        </div>
      ) : error ? (
        <p className="text-[13px] text-destructive">Failed to load learned rules.</p>
      ) : (
        <>
          {adding && (
            <div className="space-y-2 rounded-lg border bg-muted/30 p-3">
              <textarea
                className="min-h-[72px] w-full rounded-md border bg-background p-2 text-[13px]"
                placeholder="Describe the rule, e.g. 'Laptop 13' = item class Laptop AND display name LIKE 'Laptop 13'"
                value={newDescription}
                onChange={(e) => setNewDescription(e.target.value)}
              />
              <Input
                className="h-8 text-[13px]"
                placeholder="Category (e.g. term_definition, query_logic) — optional"
                value={newCategory}
                onChange={(e) => setNewCategory(e.target.value)}
              />
              <div className="flex justify-end gap-2">
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setAdding(false);
                    setNewDescription("");
                    setNewCategory("");
                  }}
                >
                  Cancel
                </Button>
                <Button size="sm" onClick={handleAdd} disabled={createRule.isPending || !newDescription.trim()}>
                  Save
                </Button>
              </div>
            </div>
          )}

          <table className="w-full text-[13px]">
            <thead>
              <tr className="text-left text-muted-foreground">
                <th className="py-1 font-medium">Category</th>
                <th className="py-1 font-medium">Rule</th>
                <th className="py-1 font-medium">Status</th>
                <th className="py-1 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {rules.length === 0 ? (
                <tr>
                  <td colSpan={4} className="py-4 text-center text-muted-foreground">
                    No learned rules yet.
                  </td>
                </tr>
              ) : (
                rules.map((rule, idx) => (
                  <tr key={rule.id} className={idx % 2 ? "bg-muted/20" : ""}>
                    <td className="py-2 align-top">
                      <Badge variant="secondary">{rule.rule_category ?? "general"}</Badge>
                    </td>
                    <td className="max-w-md py-2 align-top">{rule.rule_description}</td>
                    <td className="py-2 align-top">
                      <Button
                        size="sm"
                        variant={rule.is_active ? "outline" : "ghost"}
                        onClick={() => handleToggle(rule)}
                        disabled={updateRule.isPending}
                        title={rule.is_active ? "Active — click to deactivate" : "Inactive — click to activate"}
                      >
                        {rule.is_active ? "Active" : "Inactive"}
                      </Button>
                    </td>
                    <td className="py-2 text-right align-top">
                      <AlertDialog>
                        <AlertDialogTrigger asChild>
                          <Button size="sm" variant="ghost" className="text-destructive">
                            <Trash2 className="mr-1 h-3.5 w-3.5" />
                            Delete
                          </Button>
                        </AlertDialogTrigger>
                        <AlertDialogContent>
                          <AlertDialogHeader>
                            <AlertDialogTitle>Delete this learned rule?</AlertDialogTitle>
                            <AlertDialogDescription>
                              This permanently removes the rule from every future chat turn. This can&apos;t be undone.
                            </AlertDialogDescription>
                          </AlertDialogHeader>
                          <AlertDialogFooter>
                            <AlertDialogCancel>Cancel</AlertDialogCancel>
                            <AlertDialogAction onClick={() => handleDelete(rule)}>Delete</AlertDialogAction>
                          </AlertDialogFooter>
                        </AlertDialogContent>
                      </AlertDialog>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
