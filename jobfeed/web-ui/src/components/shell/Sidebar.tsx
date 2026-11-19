import { useQuery } from "@tanstack/react-query";
import { Link, useLocation } from "react-router";

import { profileAnswers, useAnswerBank } from "@/api/answer-bank";
import { apiFetch } from "@/api/client";
import { actionCount, useReview } from "@/api/review";
import { ZoneIcon } from "@/components/shell/ZoneIcon";
import { ZONES, isMonitoringPath, zoneForPath, type Zone } from "@/components/shell/zones";

/** Navigation for the application's product zones. */
export function Sidebar() {
  const location = useLocation();
  const current = zoneForPath(location.pathname, location.search);
  const spam = useQuery({
    queryKey: ["spam-review", "pending-count"],
    queryFn: () => apiFetch<{ total: number }>("/api/spam-review?status=pending&limit=1"),
    refetchInterval: 60_000,
    staleTime: 30_000,
    retry: false,
    enabled: !isMonitoringPath(location.pathname, location.search),
  });
  const answers = useAnswerBank();
  const review = useReview();
  const counts: Record<NonNullable<Zone["count"]>, number> = {
    review: review.data ? actionCount(review.data) : 0,
    spam: spam.data?.total ?? 0,
    answers: answers.data ? profileAnswers(answers.data).waiting.length : 0,
  };
  const links = (group: Zone["group"]) => ZONES.filter((zone) => zone.group === group).map((zone) => {
    const count = zone.count ? counts[zone.count] : 0;
    return (
      <Link key={zone.path} to={zone.path} title={zone.label} aria-current={zone === current ? "page" : undefined}>
        <ZoneIcon name={zone.icon} />
        <span className="jf-nav-label">{zone.label}</span>
        {count > 0 && <span className={zone.count !== "spam" ? "jf-nav-count hot" : "jf-nav-count"}>{count}</span>}
      </Link>
    );
  });

  return (
    <aside className="jf-side">
      <Link className="jf-brand" to="/dashboard" aria-label="Jobfeed">
        <span className="jf-brand-logo" aria-hidden="true">J</span>
        <span><b>jobfeed</b><small>local workspace</small></span>
      </Link>
      <nav aria-label="Zones">
        <div className="jf-nav">{links("primary")}</div>
        <div className="jf-side-label">Tools</div>
        <div className="jf-nav">{links("tools")}</div>
      </nav>
    </aside>
  );
}
