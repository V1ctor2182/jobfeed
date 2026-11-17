import { useCallback, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { formatDuration, nodePhase, type TraceNode } from "./model";
import { LANE_STYLES, ROLE_STYLES } from "./presentation";
import { phaseText } from "./TraceRow";

/** Hover and focus details. Pointer moves reposition the tooltip directly instead of re-rendering the trace. */
export function useRowTooltip() {
  const ref = useRef<HTMLDivElement>(null);
  const point = useRef({ x: 0, y: 0 });
  const [id, setId] = useState<string | null>(null);
  const place = useCallback(() => {
    const element = ref.current;
    if (!element) return;
    const { x, y } = point.current;
    const { offsetWidth: width, offsetHeight: height } = element;
    element.style.left = `${Math.max(8, Math.min(window.innerWidth - width - 8, x + 14))}px`;
    element.style.top = `${y + 16 + height > window.innerHeight - 8 ? y - height - 12 : y + 16}px`;
  }, []);
  // Content changes size as the clock ticks, so re-place after every render while shown.
  useLayoutEffect(() => { if (id) place(); });
  return {
    ref, id,
    show: (nextId: string, x: number, y: number) => {
      point.current = { x, y };
      if (nextId === id) place();
      else setId(nextId);
    },
    hide: () => setId(null),
  };
}

export function RowTooltip({ tooltipRef, node, now }: {
  tooltipRef: RefObject<HTMLDivElement | null>;
  node: TraceNode | undefined;
  now: number;
}) {
  const phase = node ? nodePhase(node, now) : "pending";
  const finished = phase === "done" || phase === "failed";
  const step = node?.kind === "step" ? node : undefined;
  const type = node?.kind === "group" ? ROLE_STYLES[node.role].label : step ? LANE_STYLES[step.category].label : "";
  const status = phaseText(phase, step?.category === "human");
  return (
    <div ref={tooltipRef} className={`trace-tooltip${node ? " is-on" : ""}`} role="tooltip">
      {node && (
        <>
          <h4>{node.name}</h4>
          <p className="trace-tooltip-meta">{node.meta}</p>
          <dl>
            <dt>Type</dt><dd>{type}</dd>
            <dt>Status</dt><dd>{status}</dd>
            <dt>Started</dt><dd>{formatDuration(node.start)}</dd>
            <dt>Duration</dt><dd>{formatDuration(Math.max(0, (finished ? node.end : now) - node.start))}</dd>
            {step?.fieldsFilled ? <><dt>Fields</dt><dd>{step.fieldsFilled}</dd></> : null}
          </dl>
          {step?.note && <div className={`trace-tooltip-note${phase === "failed" ? " is-bad" : ""}`}>{step.note}</div>}
        </>
      )}
    </div>
  );
}
