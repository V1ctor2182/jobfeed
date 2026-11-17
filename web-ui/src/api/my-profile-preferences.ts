import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { JobProfile } from "@/api/onboarding-resume";
import { checklistKey } from "@/api/checklist";
import { configurationKey } from "@/api/configuration";
import { resumeOnboardingKey } from "@/api/onboarding-resume";
import { onboardingSearchesKey } from "@/api/onboarding-searches";

export interface ProfilePreferences {
  profile: JobProfile;
  searches_need_review: boolean;
  scoring_resume_active: boolean;
}

export const preferencesKey = ["my-profile-preferences"] as const;

export function useProfilePreferences() {
  return useQuery({
    queryKey: preferencesKey,
    queryFn: () => apiFetch<ProfilePreferences>("/api/my-profile/preferences"),
    refetchOnWindowFocus: false,
    retry: false,
  });
}

export function useSaveProfilePreferences() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (profile: JobProfile) => apiFetch<ProfilePreferences>("/api/my-profile/preferences", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(profile),
    }),
    onSuccess: (state) => {
      client.setQueryData(preferencesKey, state);
      for (const queryKey of [configurationKey, checklistKey, resumeOnboardingKey, onboardingSearchesKey]) {
        void client.invalidateQueries({ queryKey });
      }
    },
  });
}
