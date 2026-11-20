import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch, apiPost } from "@/api/client";
import type { components } from "@/api/types.gen";

export type AnswerBank = components["schemas"]["AnswerBankState"];
export type AnswerEntry = components["schemas"]["AnswerEntry"];
export type AnswerVersion = components["schemas"]["AnswerVersion"];
export type AnswerDraft = components["schemas"]["AnswerDraft"];
export type AnswerMutation = components["schemas"]["AnswerBankMutation"];
export type CommonQuestion = components["schemas"]["CommonQuestion"];
export const answerBankKey = ["answer-bank"] as const;

export function useAnswerBank() {
  return useQuery({ queryKey: answerBankKey, queryFn: () => apiFetch<AnswerBank>("/api/answer-bank"), retry: false });
}

export function useMutateAnswer() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: AnswerMutation) => apiPost<AnswerBank>("/api/answer-bank", body),
    onSuccess: (state) => client.setQueryData(answerBankKey, state),
  });
}

export function currentAnswer(entry: AnswerEntry): AnswerVersion {
  return entry.versions.find((version) => version.version === entry.current_version)!;
}

export function scopeLabel(scope: AnswerDraft["scope"]): string {
  if (scope.kind === "company") return scope.company ?? "One company";
  if (scope.kind === "job") return `${scope.company}, job ${scope.job_id}`;
  return "All companies";
}

/** Answers the user sees in the profile: drafts waiting for them, and saved answers not shown as a common question. */
export function profileAnswers(bank: AnswerBank) {
  const common = new Set(bank.common_questions?.map((item) => item.entry_id).filter(Boolean));
  const waiting: AnswerEntry[] = [];
  const saved: AnswerEntry[] = [];
  for (const entry of bank.entries) {
    const state = currentAnswer(entry).state;
    if (state === "pending_review") waiting.push(entry);
    else if (state === "approved" && !common.has(entry.id)) saved.push(entry);
  }
  return { waiting, saved };
}

/** Saving in the profile is the user's approval of exactly this answer. */
export function saveRequest(bank: AnswerBank, answer: string, target: { entry: AnswerEntry } | { entryId: string; question: string }): AnswerMutation {
  if ("entry" in target) {
    const current = currentAnswer(target.entry);
    const expired = current.valid_until ? Date.parse(current.valid_until) <= Date.now() : false;
    return { action: "save", expected_revision: bank.revision, entry_id: target.entry.id, expected_version: target.entry.current_version,
      draft: { question: current.question, answer, aliases: current.aliases ?? [], scope: current.scope, valid_until: expired ? null : current.valid_until ?? null } };
  }
  return { action: "save", expected_revision: bank.revision, entry_id: target.entryId,
    draft: { question: target.question, answer, aliases: [], scope: { kind: "global" }, valid_until: null } };
}

export function deleteRequest(bank: AnswerBank, entry: AnswerEntry): AnswerMutation {
  return { action: "revoke", expected_revision: bank.revision, entry_id: entry.id, expected_version: entry.current_version };
}

export type ScopeFilter = "all" | AnswerDraft["scope"]["kind"];
export type SourceFilter = "all" | "user_reply" | "applier_auto" | "dashboard";

/** Who approved the answer: the user's reply to an Applier, an Applier under auto submit, or the Dashboard. */
export function answerSource(version: AnswerVersion): string | undefined {
  const source = version.approval?.source as { kind?: string } | undefined;
  return source?.kind;
}

/** Whether the answer's question, other wordings or answer contain the search text, ignoring case. */
export function matchesSearch(version: AnswerVersion, text: string): boolean {
  const needle = text.trim().toLowerCase();
  return !needle || [version.question, ...(version.aliases ?? []), version.answer].some((value) => value.toLowerCase().includes(needle));
}

function scopeKey(scope: AnswerDraft["scope"]): string {
  return [scope.kind, (scope.company ?? "").trim().toLowerCase(), scope.job_id ?? ""].join("\u0000");
}

/** Answers merge only when they apply to the same companies or job, as the answer store requires. */
export function sameScope(entries: AnswerEntry[]): boolean {
  return new Set(entries.map((entry) => scopeKey(currentAnswer(entry).scope))).size <= 1;
}

/** Keeps one answer and folds the others' wordings into it. The others are removed from use. */
export function mergeRequest(bank: AnswerBank, keep: AnswerEntry, others: AnswerEntry[]): AnswerMutation {
  return { action: "merge", expected_revision: bank.revision, entry_id: keep.id, merge_entry_ids: others.map((entry) => entry.id) };
}
