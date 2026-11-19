import Button from "@cloudscape-design/components/button";
import { type JobStar, useJobStars, useSetJobStar } from "@/api/job-stars";

type StarStatus = ReturnType<typeof useJobStars>;

export function StarButton({ sourceId, label, stars }: {
  sourceId: string | number;
  label: string;
  stars: StarStatus;
}) {
  const item: JobStar | undefined = stars.byId.get(String(sourceId));
  const save = useSetJobStar();
  if (item === undefined) {
    return <Button variant="icon" formAction="none" iconName="refresh"
      loading={stars.loading} loadingText={`Loading star status for ${label}`}
      disabled={!stars.failed}
      ariaLabel={stars.failed ? `Retry star status for ${label}` : `Star status unavailable for ${label}`}
      onClick={() => stars.retry()} />;
  }
  return <>
    <Button variant="icon" formAction="none" ariaLabel={`${item.starred ? "Unstar" : "Star"} ${label}`}
      nativeButtonAttributes={{ "aria-pressed": item.starred }}
      loading={save.isPending} loadingText={`Saving star for ${label}`}
      iconSvg={<svg viewBox="0 0 16 16" focusable="false"><path
        d="m8 1.5 2 4.1 4.5.7-3.3 3.2.8 4.5L8 11.9l-4 2.1.8-4.5L1.5 6.3l4.5-.7Z"
        stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"
        fill={item.starred ? "currentColor" : "none"} /></svg>}
      onClick={() => save.mutate({ sourceId: String(sourceId), starred: !item.starred })} />
    {save.error && <span role="alert">Star not saved. {save.error.message}</span>}
  </>;
}

export function DetailStarButton({ sourceId, label }: { sourceId: string; label: string }) {
  const stars = useJobStars([sourceId]);
  return <StarButton sourceId={sourceId} label={label} stars={stars} />;
}
