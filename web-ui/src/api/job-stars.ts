import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";

export interface JobStar {
  source_job_id: string;
  real_job_id: string | null;
  starred: boolean;
}

interface JobStarsResponse { stars: JobStar[] }

const starKeys = ["job-stars"] as const;
const READ_BATCH_SIZE = 100;

export function useJobStars(sourceIds: (string | number)[]) {
  const ids = [...new Set(sourceIds.map(String).filter(id => /^[1-9]\d*$/.test(id)))].sort();
  const query = useQuery({
    queryKey: [...starKeys, ids],
    queryFn: async () => {
      const batches: string[][] = [];
      for (let offset = 0; offset < ids.length; offset += READ_BATCH_SIZE) {
        batches.push(ids.slice(offset, offset + READ_BATCH_SIZE));
      }
      const responses = await Promise.all(batches.map(batch => apiFetch<JobStarsResponse>(
        `/api/job-stars?${new URLSearchParams({ job_ids: batch.join(",") })}`,
      )));
      return { stars: responses.flatMap(response => response.stars ?? []) };
    },
    enabled: ids.length > 0,
    retry: false,
  });
  return {
    byId: new Map((query.data?.stars ?? []).map(star => [String(star.source_job_id), star])),
    loading: ids.length > 0 && query.isPending,
    failed: query.isError,
    retry: () => { void query.refetch(); },
  };
}

export function useSetJobStar() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ sourceId, starred }: { sourceId: string; starred: boolean }) => apiFetch<JobStar>(
      `/api/jobs/${encodeURIComponent(sourceId)}/star`,
      { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ starred }) },
    ),
    onSuccess: async updated => {
      await client.cancelQueries({ queryKey: starKeys });
      client.setQueriesData<JobStarsResponse>({ queryKey: starKeys }, previous => previous && ({
        stars: previous.stars.map(star => String(star.source_job_id) === String(updated.source_job_id)
          || (updated.real_job_id !== null && star.real_job_id === updated.real_job_id)
          ? { ...star, starred: updated.starred }
          : star),
      }));
      void client.invalidateQueries({ queryKey: starKeys });
    },
  });
}
