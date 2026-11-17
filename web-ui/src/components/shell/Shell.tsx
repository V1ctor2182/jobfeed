import SpaceBetween from "@cloudscape-design/components/space-between";
import { Link, Outlet, useLocation } from "react-router";

import { isMonitoringPath, zoneForPath } from "@/components/shell/zones";
import { Sidebar } from "@/components/shell/Sidebar";
import { PersonalMLBanner } from "@/components/shell/PersonalMLBanner";
import { TopBar } from "@/components/shell/TopBar";
import { Toaster } from "@/components/ui/toaster";
import { useDensity } from "@/lib/density";
import "@/components/shell/shell.css";
import "@/routes/jobs.css";

/** The two views of Jobs: applications by stage, and postings with the decisions and queue that lead to them. */
function JobsTabs({ pathname }: { pathname: string }) {
  const board = pathname.replace(/\/+$/, "") === "/jobs";
  return (
    <nav className="jf-subnav" aria-label="Jobs views">
      <Link to="/jobs" aria-current={board ? "page" : undefined}>Board</Link>
      <Link to="/runs?tab=postings" aria-current={board ? undefined : "page"}>Postings</Link>
    </nav>
  );
}

/** Sidebar frame shared by all configured routes. */
export function Shell() {
  const { density } = useDensity();
  const location = useLocation();
  const zone = zoneForPath(location.pathname, location.search);

  return (
    <div className="jf-app" data-testid="jobfeed-workspace-layout">
      <Sidebar />
      <div className="jf-main-col">
        <div className="jf-crumbs"><span>jobfeed</span><i>/</i><span>workspace</span><i>/</i><b>{zone?.label ?? "Jobfeed"}</b></div>
        <main className="jf-main">
          <Toaster />
          <div data-testid="jobfeed-route-surface" data-density={density}>
            <SpaceBetween size="l">
              {!isMonitoringPath(location.pathname, location.search) && location.pathname !== "/profile" && <PersonalMLBanner />}
              {location.pathname !== "/profile" && <TopBar />}
              {zone?.path === "/jobs" && <JobsTabs pathname={location.pathname} />}
              <Outlet />
            </SpaceBetween>
          </div>
        </main>
      </div>
    </div>
  );
}
