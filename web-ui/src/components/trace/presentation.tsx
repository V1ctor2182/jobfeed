import type { ReactNode } from "react";
import type { GroupRole, LaneSegment } from "./model";

type Fill = "solid" | "tint" | "hatch" | "hatch-tint";

interface LaneStyle {
  label: string;
  color: string;
  fill: Fill;
  icon: ReactNode;
}

function TraceIcon({ children }: { children: ReactNode }) {
  return <svg className="trace-icon" viewBox="0 0 16 16" aria-hidden="true">{children}</svg>;
}

export const ICONS = {
  run: <TraceIcon><path d="M8 2.5a5.5 5.5 0 1 1-5.5 5.5" /></TraceIcon>,
  failed: <TraceIcon><circle cx="8" cy="8" r="5.5" /><path d="M6.2 6.2l3.6 3.6M9.8 6.2l-3.6 3.6" /></TraceIcon>,
  cross: <TraceIcon><path d="M4.5 4.5l7 7M11.5 4.5l-7 7" /></TraceIcon>,
  chevron: <TraceIcon><path d="M6 4l4 4-4 4" /></TraceIcon>,
  person: <TraceIcon><circle cx="8" cy="5.5" r="2.5" /><path d="M3.5 13.5c.6-2.4 2.3-3.5 4.5-3.5s3.9 1.1 4.5 3.5" /></TraceIcon>,
  tree: <TraceIcon><circle cx="8" cy="3.5" r="1.5" /><circle cx="4" cy="12.5" r="1.5" /><circle cx="12" cy="12.5" r="1.5" /><path d="M8 5v3M4 11V9.5h8V11" /></TraceIcon>,
};

export const LANE_STYLES: Record<LaneSegment["type"], LaneStyle> = {
  model: {
    label: "Model step", color: "var(--jf-trace-model)", fill: "solid",
    icon: <TraceIcon><path d="M8 2.5l1.3 3.2 3.2 1.3-3.2 1.3L8 11.5 6.7 8.3 3.5 7l3.2-1.3z" /></TraceIcon>,
  },
  browser: {
    label: "Browser action", color: "var(--jf-trace-browser)", fill: "tint",
    icon: <TraceIcon><circle cx="8" cy="8" r="5.5" /><path d="M2.5 8h11M8 2.5c1.8 2 1.8 9 0 11M8 2.5c-1.8 2-1.8 9 0 11" /></TraceIcon>,
  },
  backend: {
    label: "Backend receipt", color: "var(--jf-trace-backend)", fill: "tint",
    icon: <TraceIcon><ellipse cx="8" cy="4.5" rx="4.5" ry="2" /><path d="M3.5 4.5v7c0 1.1 2 2 4.5 2s4.5-.9 4.5-2v-7M3.5 8c0 1.1 2 2 4.5 2s4.5-.9 4.5-2" /></TraceIcon>,
  },
  shell: {
    label: "Worker command", color: "var(--jf-trace-shell)", fill: "tint",
    icon: <TraceIcon><rect x="2.5" y="3" width="11" height="10" rx="1.5" /><path d="M5 6.5 7 8 5 9.5M8.5 10H11" /></TraceIcon>,
  },
  human: {
    label: "Waiting on you", color: "var(--jf-trace-human)", fill: "hatch-tint",
    icon: <TraceIcon><rect x="2.5" y="2.5" width="11" height="11" rx="2" /><path d="M5.5 8.2 7.2 10l3.3-4" /></TraceIcon>,
  },
  wait: { label: "Waiting on Applier", color: "var(--jf-trace-hatch)", fill: "hatch", icon: null },
};

export const ROLE_STYLES: Record<GroupRole, { label: string; color: string; icon: ReactNode }> = {
  worker: {
    label: "Python worker", color: "var(--jf-trace-worker)",
    icon: <TraceIcon><rect x="2.5" y="3" width="11" height="4" rx="1" /><rect x="2.5" y="9" width="11" height="4" rx="1" /><path d="M5 5h0M5 11h0" /></TraceIcon>,
  },
  applier: {
    label: "Claude Applier session", color: "var(--jf-trace-applier)",
    icon: <TraceIcon><rect x="3" y="5" width="10" height="8" rx="2" /><path d="M8 2.5V5M6.2 9h0M9.8 9h0" /></TraceIcon>,
  },
};

export function fillClass(type: LaneSegment["type"]) {
  return `trace-fill--${LANE_STYLES[type].fill}`;
}
