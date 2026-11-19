import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import { checklistKey } from "@/api/checklist";

export interface PersonalDetails {
  legal_name: string;
  preferred_name: string;
  primary_email: string;
  phone: string;
  address: string;
  linkedin_url: string;
  portfolio_url: string;
  github_url: string;
  work_authorization: string;
  sponsorship_requirement: string;
  relocation: string;
  availability: string;
  education: string;
}

export interface MyProfile {
  personal_details: PersonalDetails;
  revision: string;
  reviewed: boolean;
  resumes: ResumeEntry[];
  application_resumes: { id: string; label: string; filename: string; available: boolean; format?: string; application_supported?: boolean; status?: string }[];
}

export interface ResumeEntry {
  id: string; filename: string; active: boolean; format: string;
  application_supported: boolean; status: string;
  kind?: "legacy" | "master" | "variant";
  label?: string; role?: string; google_doc_url?: string; page_count?: number | null;
  download_url?: string;
  pdf_export?: { source_google_doc_url: string; exported_at: string | null;
    page_count: number; provenance: "user_declared" | "observed_google_docs_export" } | null;
}

export interface ResumeMetadata {
  kind: "master" | "variant"; label: string; role: string; google_doc_url: string;
  exported_at?: string | null; provenance?: "user_declared" | null;
}

export function useUploadLibraryResume() {
  return useProfileMutation(({ file, metadata, identifier }: {
    file: File; metadata: ResumeMetadata; identifier?: string;
  }) => {
    const body = new FormData();
    body.append("file", file);
    body.append("metadata", JSON.stringify(metadata));
    if (identifier) body.append("identifier", identifier);
    return apiFetch<MyProfile>("/api/my-profile/resumes/upload", { method: "POST", body });
  });
}

export function useUpdateResumeDetails() {
  return useProfileMutation(({ id, metadata }: { id: string; metadata: ResumeMetadata }) =>
    apiFetch<MyProfile>(`/api/my-profile/resumes/${encodeURIComponent(id)}/details`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(metadata),
    }));
}

export const myProfileKey = ["my-profile"] as const;

export function useMyProfile() {
  return useQuery({
    queryKey: myProfileKey,
    queryFn: () => apiFetch<MyProfile>("/api/my-profile"),
    refetchOnWindowFocus: false,
    retry: false,
  });
}

export function useSavePersonalDetails() {
  return useProfileMutation((body: { personal_details: PersonalDetails; revision: string }) =>
    apiFetch<MyProfile>("/api/my-profile/personal", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }));
}

export function useSetResumeActive() {
  return useProfileMutation(({ id, active }: { id: string; active: boolean }) =>
    apiFetch<MyProfile>(`/api/my-profile/resumes/${encodeURIComponent(id)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ active }),
    }));
}

function useProfileMutation<T>(mutationFn: (value: T) => Promise<MyProfile>) {
  const client = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: (state) => {
      client.setQueryData(myProfileKey, state);
      void client.invalidateQueries({ queryKey: checklistKey });
    },
  });
}
