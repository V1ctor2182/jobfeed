import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";

import type { components } from "@/api/types.gen";

export type SetupCheck = components["schemas"]["ChecklistCheck"];
export type SetupSection = components["schemas"]["ChecklistSection"];
type Snapshot = components["schemas"]["OnboardingChecklist"];

export const checklistKey = ["workspace", "onboarding-checklist"] as const;

export function useSetupChecklist() {
  return useQuery({
    queryKey: checklistKey,
    queryFn: ({ signal }) => apiFetch<Snapshot>("/api/onboarding/checklist", { signal }),
    retry: false,
    refetchInterval: 30_000,
  });
}
