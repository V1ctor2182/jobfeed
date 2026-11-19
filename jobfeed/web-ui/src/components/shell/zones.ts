/** Product zones shared by sidebar navigation and route titles. */
export interface Zone {
  /** Link target. A query string narrows the zone to one section of a page. */
  path: string;
  label: string;
  group: "primary" | "tools";
  icon: ZoneIconName;
  /** Other paths that belong to this zone and keep it selected. */
  also?: string[];
  /** Which sidebar count this zone shows, if any. */
  count: "review" | "answers" | "spam" | null;
}

export type ZoneIconName =
  | "dashboard" | "review" | "tracker" | "jobs" | "answers" | "profile" | "sources"
  | "trace" | "spam" | "insights" | "settings" | "scan" | "health" | "performance";

export const ZONES: Zone[] = [
  { path: "/dashboard", label: "Dashboard", group: "primary", icon: "dashboard", count: null },
  { path: "/review", label: "Review", group: "primary", icon: "review", count: "review" },
  { path: "/jobs", label: "Jobs", group: "primary", icon: "jobs", count: null, also: ["/library", "/runs", "/application-queue"] },
  { path: "/tracker", label: "Tracker", group: "primary", icon: "tracker", count: null },
  { path: "/profile?section=answers", label: "Answer Bank", group: "primary", icon: "answers", count: "answers" },
  { path: "/profile", label: "Profile", group: "primary", icon: "profile", count: null },
  { path: "/sources", label: "Sources", group: "primary", icon: "sources", count: null },
  { path: "/applier-trace", label: "Applier trace", group: "tools", icon: "trace", count: null },
  { path: "/spam-review", label: "Spam review", group: "tools", icon: "spam", count: "spam" },
  { path: "/insights", label: "Insights", group: "tools", icon: "insights", count: null },
  { path: "/scanner-runs", label: "Scanner runs", group: "tools", icon: "scan", count: null },
  { path: "/apply-health", label: "Apply health", group: "tools", icon: "health", count: null },
  { path: "/performance", label: "Performance", group: "tools", icon: "performance", count: null },
  { path: "/setup", label: "Settings", group: "tools", icon: "settings", count: null },
];

/** The zone a location belongs to. A zone with a query string wins over the bare path it shares. */
export function zoneForPath(pathname: string, search = ""): Zone | undefined {
  pathname = pathname.replace(/\/+$/, "") || "/";
  if (pathname === "/triage" || pathname === "/pipeline") pathname = "/runs";
  const params = new URLSearchParams(search);
  const matches = ZONES.filter((zone) => {
    const [path, query] = zone.path.split("?");
    if (![path, ...(zone.also ?? [])].some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`))) return false;
    return [...new URLSearchParams(query)].every(([key, value]) => params.get(key) === value);
  });
  return matches.find((zone) => zone.path.includes("?")) ?? matches[0];
}

export function isMonitoringPath(pathname: string, search = ""): boolean {
  if (pathname.replace(/\/+$/, "") === "/triage") return isMonitoringPath("/runs", search);
  return ["/", "/dashboard", "/review", "/tracker", "/jobs", "/runs", "/application-queue", "/apply-health", "/applier-trace", "/answer-bank"].includes(pathname.replace(/\/+$/, "") || "/");
}
