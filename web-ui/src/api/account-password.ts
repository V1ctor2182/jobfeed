import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { components } from "@/api/types.gen";

export type AccountPasswordState = components["schemas"]["AccountPasswordState"];
const key = ["my-profile", "account-password"] as const;

/** Whether a shared account password is set. The password itself is never sent back. */
export function useAccountPassword() {
  return useQuery({ queryKey: key, queryFn: () => apiFetch<AccountPasswordState>("/api/my-profile/account-password"), retry: false });
}

export function useSaveAccountPassword() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (password: string) => apiFetch<AccountPasswordState>("/api/my-profile/account-password", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password }),
    }),
    onSuccess: (state) => client.setQueryData(key, state),
  });
}
