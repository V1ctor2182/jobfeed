import { useMutation, useQueryClient } from "@tanstack/react-query";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import {
  applicationTabsKey, getApplicationTabs, openExistingApplicationTab, useApplicationTabs,
} from "@/api/application-tabs";
import { applierOwnerValid } from "./run-model";

const openResult = {
  focused: "Application form opened in Chrome.",
  tab_closed: "The application form tab is closed. Open the applier chat to recover it.",
  page_changed: "The recorded Chrome tab has changed page. Open the applier chat to check it.",
  focus_failed: "Chrome could not bring the application form forward. Try again.",
};

export function ApplicationTabJump({ jobId, owner }: { jobId: number; owner?: string | null }) {
  const queryClient = useQueryClient();
  const ownerAvailable = applierOwnerValid(owner);
  const tabs = useApplicationTabs(ownerAvailable);
  const open = useMutation({
    mutationFn: async (target: { jobId: number; owner: string }) => {
      const current = await queryClient.fetchQuery({
        queryKey: applicationTabsKey, queryFn: getApplicationTabs, staleTime: 0,
      });
      return openExistingApplicationTab(target.jobId, target.owner, current.csrf_token);
    },
    retry: false,
  });
  const currentRequest = open.variables?.jobId === jobId && open.variables.owner === owner;
  return <>
    <Button variant="inline-link" disabled={!ownerAvailable || tabs.isPending || tabs.isError} loading={currentRequest && open.isPending}
      loadingText="Opening application form" onClick={() => open.mutate({ jobId, owner: owner! })}>
      Open application form
    </Button>
    {!ownerAvailable && <Box variant="small" color="text-body-secondary">Applier unavailable.</Box>}
    {currentRequest && open.data && !open.isPending && !open.error && <div role="status">{openResult[open.data.status]}</div>}
    {currentRequest && open.error && <div role="alert">Could not confirm the jump. Check Chrome before retrying. {open.error.message}</div>}
    {tabs.isError && ownerAvailable && <div role="alert">
      Chrome connection unavailable. <Button variant="inline-link" onClick={() => void tabs.refetch()}>Retry connection check</Button>
    </div>}
  </>;
}
