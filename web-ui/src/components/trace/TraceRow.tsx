import { useState, type CSSProperties, type FocusEvent, type PointerEvent } from "react";
import { formatDuration, nodePhase, rowSegments, spanPhase, type LaneSegment, type Phase, type TraceRow } from "./model";
import { fillClass, ICONS, LANE_STYLES, ROLE_STYLES } from "./presentation";

const cx = (...names: (string | false | undefined)[]) => names.filter(Boolean).join(" ");
export const cssVars = (vars: Record<string, string | number>) => vars as CSSProperties;

/** Lane positions are CSS expressions over the clock variables, so they move without re-rendering. */
const lanePosition = (time: number | string) => `calc((${time} - var(--trace-start)) / var(--trace-domain) * 100% + 1px)`;
const laneWidth = (start: number, end: number | string) => `calc((${end} - ${start}) / var(--trace-domain) * 100% - 2px)`;
const clampedNow = (end: number) => `min(var(--trace-now), ${end})`;

interface TraceRowProps {
  row: TraceRow;
  now: number;
  isOpen: boolean;
  isExpanded: boolean;
  isDimmed: boolean;
  isSlowest: boolean;
  onToggle: (id: string) => void;
  onPointerMove: (id: string, event: PointerEvent<HTMLDivElement>) => void;
  onFocus: (id: string, event: FocusEvent<HTMLDivElement>) => void;
  onLeave: () => void;
}

export function TraceRowView({ row, now, isOpen, isExpanded, isDimmed, isSlowest, onToggle, onPointerMove, onFocus, onLeave }: TraceRowProps) {
  const { node, depth } = row;
  const phase = nodePhase(node, now);
  const isGroup = node.kind === "group";
  const isHuman = node.kind === "step" && node.category === "human";
  const shaking = useFailureShake(phase);
  const finished = phase === "done" || phase === "failed";
  const label = `${formatDuration(Math.max(0, (finished ? node.end : now) - node.start))}${isHuman && phase === "running" ? " · waiting" : ""}`;
  const icon = node.kind === "group" ? ROLE_STYLES[node.role].icon : LANE_STYLES[node.category].icon;
  const rowVars: Record<string, string | number> = { "--depth": depth };
  if (node.kind === "group") rowVars["--trace-role-color"] = ROLE_STYLES[node.role].color;

  return (
    <li
      className={cx("trace-row", isGroup && "trace-row--group", isHuman && "trace-row--human", isOpen && "is-open", isExpanded && "is-expanded", isDimmed && "is-dimmed")}
      style={cssVars(rowVars)}
      aria-hidden={!isOpen}
      inert={!isOpen}
    >
      <div className="trace-row-clip">
        <div
          className="trace-row-inner"
          tabIndex={0}
          onPointerMove={(event) => onPointerMove(node.id, event)}
          onPointerLeave={onLeave}
          onFocus={(event) => onFocus(node.id, event)}
          onBlur={onLeave}
        >
          <div className="trace-steps-cell">
            {isGroup ? (
              <button
                type="button"
                className="trace-chevron"
                aria-expanded={isExpanded}
                aria-label={`${isExpanded ? "Collapse" : "Expand"} ${node.name}`}
                onClick={() => onToggle(node.id)}
              >{ICONS.chevron}</button>
            ) : <span className="trace-chevron-spacer" />}
            <span className="trace-status" data-state={phase === "failed" ? "err" : finished ? "ok" : "run"}>
              <span className="trace-status-run">{isHuman ? ICONS.person : ICONS.run}</span>
              <span className="trace-status-ok">{icon}</span>
              <span className="trace-status-err">{ICONS.failed}</span>
            </span>
            <span className={cx("trace-name", phase === "running" && "is-running", shaking && "is-shaking")}>{node.name}</span>
            <span className="trace-sr-only">{phaseText(phase, isHuman)}</span>
            <span className="trace-meta">{node.meta}</span>
          </div>
          <div className="trace-lane">
            {rowSegments(node).map((segment) => <LaneBar key={segment.start} segment={segment} now={now} />)}
            <span className={cx("trace-label", phase === "pending" && "is-off")} style={{ left: lanePosition(clampedNow(node.end)) }}>
              <span>{label}</span>
              <span className={cx("trace-badge", !isSlowest && "is-off")} aria-hidden={!isSlowest}>Slowest</span>
            </span>
          </div>
        </div>
      </div>
    </li>
  );
}

export function phaseText(phase: Phase, isHuman: boolean) {
  if (phase === "running") return isHuman ? "Waiting on you" : "Running";
  return phase === "failed" ? "Failed" : phase === "done" ? "Done" : "Pending";
}

function LaneBar({ segment, now }: { segment: LaneSegment; now: number }) {
  const phase = spanPhase(segment, now);
  return (
    <span
      className={cx("trace-bar", fillClass(segment.type), phase === "running" && "is-running", phase === "failed" && "is-failed")}
      hidden={phase === "pending"}
      style={cssVars({
        "--trace-bar-color": LANE_STYLES[segment.type].color,
        left: lanePosition(segment.start),
        width: laneWidth(segment.start, clampedNow(segment.end)),
      })}
    >
      {phase === "failed" && <span className="trace-bar-cross">{ICONS.cross}</span>}
    </span>
  );
}

/** True right after a step goes from running to failed, so the name shakes once. */
function useFailureShake(phase: Phase) {
  const [seen, setSeen] = useState(phase);
  const [shaking, setShaking] = useState(false);
  if (phase !== seen) {
    setSeen(phase);
    setShaking(seen === "running" && phase === "failed");
  }
  return shaking;
}
