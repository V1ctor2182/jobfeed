import Button from "@cloudscape-design/components/button";
import Box from "@cloudscape-design/components/box";
import Container from "@cloudscape-design/components/container";
import SpaceBetween from "@cloudscape-design/components/space-between";
import type { ReactNode } from "react";

import { useBulkTransition, useRealJobBulkTransition, type TransitionStatus } from "@/api/queries";
import { toast } from "@/components/ui/use-toast";

interface BulkBarProps {
  canonical?: boolean;
  currentDecision: "wait" | "applied" | "ignored";
  selectedIds: string[];
  /** True total matching the current filters (the API's `total`). */
  total: number;
  /** False while the first-page fast path is still resolving the exact total. */
  totalIsExact: boolean;
  isSelectingAll: boolean;
  onSelectPage: () => void;
  onSelectAllMatching: () => void;
  onClear: () => void;
  /** Receives failures so successful rows can leave the active list while
   * failed rows remain selected for retry. */
  onBulkResult: (failedIds: string[]) => void;
  additionalActions?: (pending: boolean) => ReactNode;
  actionPending?: boolean;
}

interface BulkAction {
  decision: "wait" | "ignored";
  label: string;
  to: TransitionStatus;
}

const ACTIONS: BulkAction[] = [
  { decision: "wait", label: "Move selected to Wait", to: "shortlisted" },
  { decision: "ignored", label: "Ignore selected", to: "ignored" },
];

/** Appears while rows are checkbox-selected; runs the bulk endpoint. */
export function BulkBar({
  canonical = false,
  currentDecision,
  selectedIds,
  total,
  totalIsExact,
  isSelectingAll,
  onSelectPage,
  onSelectAllMatching,
  onClear,
  onBulkResult,
  additionalActions,
  actionPending = false,
}: BulkBarProps) {
  const sourceBulk = useBulkTransition();
  const realBulk = useRealJobBulkTransition();
  const bulk = canonical ? realBulk : sourceBulk;

  if (selectedIds.length === 0) {
    return null;
  }

  const run = ({ to }: BulkAction) => {
    bulk.mutate(
      { items: selectedIds.map((id) => ({ id, to })) },
      {
        onSuccess: (result) => {
          const failedIds = result.failed.map((failure) => failure.id);
          toast({
            variant: failedIds.length > 0 ? "destructive" : "default",
            title: failedIds.length > 0
              ? "Some selected jobs could not be updated"
              : "Selected jobs updated",
            description:
              `${result.succeeded} succeeded · ${result.skipped} skipped · ` +
              `${failedIds.length} failed · ${result.cascaded} cascaded` +
              (failedIds.length > 0 ? ` · Failed IDs: ${failedIds.join(", ")}` : ""),
          });
          onBulkResult(failedIds);
        },
        onError: (error) => {
          toast({
            variant: "destructive",
            title: "Selected jobs could not be updated",
            description: error.message,
          });
        },
      },
    );
  };

  return (
    <div role="toolbar" aria-label="Bulk actions">
      <Container>
        <SpaceBetween size="xs">
          <SpaceBetween direction="horizontal" size="xs" alignItems="center">
            <Box variant="strong">{selectedIds.length} selected</Box>
            <div role="group" aria-label="Decision actions">
              <SpaceBetween direction="horizontal" size="xs">
                {ACTIONS.filter(
                  (action) => action.decision !== currentDecision,
                ).map((action) => (
                  <Button
                    key={action.to}
                    disabled={bulk.isPending || isSelectingAll || actionPending}
                    onClick={() => run(action)}
                  >
                    {action.label}
                  </Button>
                ))}
                {additionalActions?.(bulk.isPending)}
              </SpaceBetween>
            </div>
          </SpaceBetween>
          <div role="group" aria-label="Selection controls">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                disabled={bulk.isPending || isSelectingAll || actionPending}
                onClick={onSelectPage}
              >
                Select this page
              </Button>
              <Button
                variant="link"
                loading={isSelectingAll}
                disabled={bulk.isPending || !totalIsExact || actionPending}
                onClick={onSelectAllMatching}
              >
                {totalIsExact
                  ? `Select all ${total} ${total === 1 ? "result" : "results"}`
                  : "Loading all results"}
              </Button>
              <Button
                variant="link"
                disabled={bulk.isPending || isSelectingAll || actionPending}
                onClick={onClear}
              >
                Clear selection
              </Button>
            </SpaceBetween>
          </div>
        </SpaceBetween>
      </Container>
    </div>
  );
}
