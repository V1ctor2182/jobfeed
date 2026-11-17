import { useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { createPortal } from "react-dom";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SegmentedControl from "@cloudscape-design/components/segmented-control";
import SpaceBetween from "@cloudscape-design/components/space-between";
import TextFilter from "@cloudscape-design/components/text-filter";

import {
  formatClock, formatTick, nodePhase, tickValues, traceRows, traceSummary, type ApplierTrace, type SessionState,
} from "./model";
import { fillClass, ICONS, LANE_STYLES } from "./presentation";
import { RowTooltip, useRowTooltip } from "./RowTooltip";
import { cssVars, TraceRowView } from "./TraceRow";
import { SPEEDS, useTraceClock, type Speed } from "./use-trace-clock";
import "./trace.css";

interface TraceWaterfallProps {
  trace: ApplierTrace;
  initialNow?: number;
  autoPlay?: boolean;
  /** A run still in progress: the clock follows its end and replay is off. */
  live?: boolean;
}

const FOLLOW_SLACK_PX = 24;
const KEYBOARD_SEEK_SECONDS = 5;

export function TraceWaterfall({ trace, initialNow, autoPlay, live = false }: TraceWaterfallProps) {
  const traceRef = useRef<HTMLDivElement>(null);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);
  const end = trace.root.end;
  const clock = useTraceClock(traceRef, {
    end, initialNow, autoPlay, live,
    onFrame: (state) => {
      const scroller = scrollerRef.current;
      if (scroller && followRef.current && (state.playing || live)) scroller.scrollTop = scroller.scrollHeight;
    },
  });
  const { now, domain, playing, scrubbing, speed } = clock.view;
  const finished = now >= end;

  const rows = useMemo(() => traceRows(trace.root), [trace.root]);
  const summary = traceSummary(trace, now);
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());
  const [query, setQuery] = useState("");
  const term = query.trim().toLowerCase();
  const tooltip = useRowTooltip();

  const fractionAt = (event: PointerEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    return rect.width > 0 ? (event.clientX - rect.left) / rect.width : 0;
  };
  const restart = () => {
    followRef.current = true;
    if (scrollerRef.current) scrollerRef.current.scrollTop = 0;
    clock.restart();
  };
  const toggleGroup = (id: string) => setCollapsed((previous) => {
    const next = new Set(previous);
    if (!next.delete(id)) next.add(id);
    return next;
  });
  const onAxisKey = (event: KeyboardEvent<HTMLDivElement>) => {
    const delta = { ArrowLeft: -KEYBOARD_SEEK_SECONDS, ArrowRight: KEYBOARD_SEEK_SECONDS }[event.key];
    if (delta === undefined) return;
    event.preventDefault();
    clock.seekBy(delta);
  };

  return (
    <>
      <SpaceBetween size="s">
        <Container
          disableContentPaddings
          header={
            <Header
              variant="h2"
              description={trace.subtitle}
              actions={
                <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                  {!live && <>
                    <Button iconName="refresh" ariaLabel="Restart" onClick={restart} />
                    <Button
                      iconName={playing ? "pause" : finished ? "refresh" : "play"}
                      ariaLabel={playing ? "Pause" : finished ? "Replay" : "Play"}
                      onClick={() => (finished ? restart() : clock.togglePlay())}
                    />
                    <SegmentedControl
                      label="Playback speed"
                      selectedId={String(speed)}
                      options={SPEEDS.map((value) => ({ id: String(value), text: `${value}×` }))}
                      onChange={({ detail }) => clock.setSpeed(Number(detail.selectedId) as Speed)}
                    />
                  </>}
                  <div className="trace-search">
                    <TextFilter
                      filteringText={query}
                      filteringPlaceholder="Search steps"
                      filteringAriaLabel="Search steps"
                      onChange={({ detail }) => setQuery(detail.filteringText)}
                    />
                  </div>
                </SpaceBetween>
              }
            >
              {trace.title}
            </Header>
          }
        >
          <div ref={traceRef} className={`trace${finished && !scrubbing ? " is-finished" : ""}`}>
            <dl className="trace-kpis" aria-label="Run summary">
              <Kpi label="Elapsed" value={formatClock(summary.elapsed)} />
              <Kpi label="Session" value={summary.session} state={summary.session} />
              <Kpi label="Fields filled" value={trace.fieldsTotal == null ? String(summary.fieldsFilled) : `${summary.fieldsFilled}/${trace.fieldsTotal}`} />
              <Kpi label="Waiting on you" value={formatClock(summary.waitingOnYou)} />
              <Kpi label="Tool calls" value={String(summary.toolCalls)} />
              <Kpi label="Errors" value={String(summary.errors)} state={summary.errors > 0 ? "bad" : undefined} />
            </dl>
            <div
              ref={scrollerRef}
              className="trace-scroller"
              onScroll={(event) => {
                const element = event.currentTarget;
                followRef.current = element.scrollTop + element.clientHeight >= element.scrollHeight - FOLLOW_SLACK_PX;
              }}
            >
              <div className="trace-thead">
                <div className="trace-steps-cell">{ICONS.tree}Steps</div>
                <div
                  className="trace-axis"
                  role="slider"
                  tabIndex={0}
                  aria-label="Timeline. Drag to scrub."
                  aria-valuemin={0}
                  aria-valuemax={Math.round(end)}
                  aria-valuenow={Math.round(now)}
                  aria-valuetext={formatClock(now)}
                  onKeyDown={onAxisKey}
                  onPointerDown={(event) => {
                    event.currentTarget.setPointerCapture(event.pointerId);
                    clock.scrubStart(fractionAt(event));
                  }}
                  onPointerMove={(event) => { if (scrubbing) clock.scrubMove(fractionAt(event)); }}
                  onPointerUp={clock.scrubEnd}
                  onPointerCancel={clock.scrubEnd}
                >
                  <TickMarks domain={domain} part="label" />
                  <span className="trace-playhead-dot" />
                </div>
              </div>
              <div className="trace-tbody">
                <div className="trace-grid" aria-hidden="true">
                  <TickMarks domain={domain} part="line" />
                  <span className="trace-playhead-line" />
                </div>
                <ul className="trace-rows" aria-label="Steps">
                  {rows.map((row) => (
                    <TraceRowView
                      key={row.node.id}
                      row={row}
                      now={now}
                      isOpen={nodePhase(row.node, now) !== "pending" && row.ancestorIds.every((id) => !collapsed.has(id))}
                      isExpanded={!collapsed.has(row.node.id)}
                      isDimmed={term !== "" && !`${row.node.name} ${row.node.meta}`.toLowerCase().includes(term)}
                      isSlowest={summary.slowestStepId === row.node.id}
                      onToggle={toggleGroup}
                      onPointerMove={(id, event) => { if (event.pointerType === "mouse") tooltip.show(id, event.clientX, event.clientY); }}
                      onFocus={(id, event) => {
                        const rect = event.currentTarget.getBoundingClientRect();
                        tooltip.show(id, rect.left + 40, rect.bottom - 10);
                      }}
                      onLeave={tooltip.hide}
                    />
                  ))}
                </ul>
              </div>
            </div>
          </div>
        </Container>
        <Legend />
      </SpaceBetween>
      {createPortal(
        <RowTooltip
          tooltipRef={tooltip.ref}
          node={rows.find((row) => row.node.id === tooltip.id)?.node}
          now={now}
        />,
        document.body,
      )}
    </>
  );
}

function Kpi({ label, value, state }: { label: string; value: string; state?: SessionState | "bad" }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd data-state={state}><RollingText value={value} /></dd>
    </div>
  );
}

/** Positions, counted from the right, whose character differs between the two strings. */
function changedPositions(previous: string, value: string) {
  const positions: number[] = [];
  for (let fromRight = 0; fromRight < value.length; fromRight++) {
    if (previous[previous.length - 1 - fromRight] !== value[value.length - 1 - fromRight]) positions.push(fromRight);
  }
  return positions;
}

/** Rolls in only the characters that changed, aligned from the right so digits keep their place. */
function RollingText({ value }: { value: string }) {
  const [shown, setShown] = useState({ value, version: 0, changed: [] as number[], rolling: [] as number[] });
  if (shown.value !== value) {
    const changed = changedPositions(shown.value, value);
    // A digit that also changed on the previous update ticks faster than the roll
    // lasts (seconds at 30x), so it swaps in place instead of staying mid-animation.
    const rolling = changed.filter((position) => !shown.changed.includes(position));
    setShown({ value, version: shown.version + 1, changed, rolling });
  }
  const { version, rolling } = shown;
  return (
    <>
      <span className="trace-sr-only">{value}</span>
      <span aria-hidden="true">
        {[...value].map((char, index) => {
          const isRolling = rolling.includes(value.length - 1 - index);
          return (
            <span key={isRolling ? `${version}:${index}` : index} className={isRolling ? "trace-roll-char is-rolling" : "trace-roll-char"}>
              {char}
            </span>
          );
        })}
      </span>
    </>
  );
}

interface TickMark {
  value: number;
  leaving: boolean;
}

const TICK_EXIT_MS = 220;

/** Axis ticks fade in when the step changes and fade out before they unmount. */
function TickMarks({ domain, part }: { domain: number; part: "label" | "line" }) {
  const values = tickValues(domain);
  const key = values.join(",");
  const [seenKey, setSeenKey] = useState(key);
  const [marks, setMarks] = useState<TickMark[]>(() => values.map((value) => ({ value, leaving: false })));
  if (key !== seenKey) {
    setSeenKey(key);
    const wanted = new Set(values);
    const kept = marks.map((mark) => ({ value: mark.value, leaving: !wanted.has(mark.value) }));
    const added = values.filter((value) => !marks.some((mark) => mark.value === value)).map((value) => ({ value, leaving: false }));
    setMarks([...kept, ...added]);
  }
  useLayoutEffect(() => {
    if (!marks.some((mark) => mark.leaving)) return;
    const timer = setTimeout(() => setMarks((current) => current.filter((mark) => !mark.leaving)), TICK_EXIT_MS);
    return () => clearTimeout(timer);
  }, [marks]);
  return marks.map(({ value, leaving }) => (
    <span
      key={value}
      className={`trace-tick-${part}${leaving ? " is-out" : ""}`}
      style={{ left: `calc(${value} / var(--trace-domain) * 100%)` }}
    >
      {part === "label" ? formatTick(value) : null}
    </span>
  ));
}

function Legend() {
  const entries = Object.entries(LANE_STYLES).map(([type, style]) => ({
    label: style.label, color: style.color, fill: fillClass(type as keyof typeof LANE_STYLES),
  }));
  return (
    <div className="trace-legend" aria-hidden="true">
      {[...entries, { label: "Failed", color: "var(--jf-trace-failed)", fill: "trace-fill--solid" }].map((entry) => (
        <span key={entry.label}>
          <i className={`trace-swatch ${entry.fill}`} style={cssVars({ "--trace-bar-color": entry.color })} />
          {entry.label}
        </span>
      ))}
    </div>
  );
}
