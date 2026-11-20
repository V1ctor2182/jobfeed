import type { CSSProperties, ReactNode } from "react";
import { Link } from "react-router";

import { profileAnswers, useAnswerBank } from "@/api/answer-bank";
import { usePerformanceOverview, useRuns, type RunSummary } from "@/api/queries";
import { actionCount, useReview } from "@/api/review";
import { useApplyHealth, useDiscoverySummary } from "@/api/workspace";
import { costByDay, cumulativeApplied, queueGroups, queueTotals } from "@/lib/dashboard-model";
import { zeroFillDaily } from "@/lib/daily-series";
import { dayTick, formatLocalDateTime } from "@/lib/dates";
import "@/components/workspace/dashboard-overview.css";

const SPARK_DAYS = 14;
const COST_WINDOW_DAYS = 30;
const count = new Intl.NumberFormat("en-US");
const money = (value: number) => `$${value.toFixed(2)}`;

/** Headline numbers, the application queue, apply progress, and recent runs. Read-only. */
export function DashboardOverview() {
  const discovery = useDiscoverySummary();
  const apply = useApplyHealth();
  const cost = usePerformanceOverview(COST_WINDOW_DAYS);
  const runs = useRuns({ limit: 100, days: SPARK_DAYS });
  const answers = useAnswerBank();
  const review = useReview();

  const totals = discovery.data?.totals;
  const daily = zeroFillDaily(discovery.data?.daily ?? [], SPARK_DAYS);
  const today = daily[daily.length - 1];
  const yesterday = daily[daily.length - 2];
  const costs = costByDay(runs.data?.runs ?? [], SPARK_DAYS);
  const queue = apply.data?.queue;
  const groups = queueGroups(queue?.counts_by_state ?? {});
  const queueSum = queueTotals(groups);
  const applyVerdicts = discovery.data?.verdict_distribution?.apply ?? 0;
  const waiting = review.data && answers.data ? actionCount(review.data) + profileAnswers(answers.data).waiting.length : null;
  const series = cumulativeApplied(discovery.data?.daily ?? []);
  const first = series[0];
  const latest = series.at(-1);

  const discoveryLoading = discovery.isPending;
  const waitingLoading = review.isPending || answers.isPending;

  return (
    <div className="jf-dash">
      <div className="jf-kstrip">
        <Kpi label="Applied" loading={discoveryLoading} value={totals ? count.format(totals.applied) : "–"} foot={today && <><b className="up">+{today.applied}</b> today</>} spark={daily.map((entry) => entry.applied)} />
        <Kpi label="Discovered" loading={discoveryLoading} value={totals ? count.format(totals.jobs) : "–"} foot={yesterday && <><b>{count.format(yesterday.discovered)}</b> yesterday</>} spark={daily.map((entry) => entry.discovered)} />
        <Kpi label="Evaluated" loading={discoveryLoading} value={totals ? count.format(totals.evaluated) : "–"} foot={totals && totals.jobs > 0 && <><b>{Math.round((100 * totals.evaluated) / totals.jobs)}%</b> of discovered</>} spark={daily.map((entry) => entry.evaluated)} />
        <Kpi label={`Model cost, ${COST_WINDOW_DAYS} days`} loading={cost.isPending || runs.isPending} value={cost.data ? money(cost.data.total_llm_cost_usd) : "–"} foot={runs.data && <><b>{money(costs.at(-2)?.cost ?? 0)}</b> yesterday</>} spark={costs.map((entry) => entry.cost)} />
      </div>

      <div className="jf-dash-grid">
        <section className="jf-card" aria-labelledby="jf-queue-title">
          <div className="jf-card-head">
            <h2 id="jf-queue-title">Application queue</h2>
            {queue?.state === "available" && <span className="jf-meta">{count.format(queue.total ?? 0)} records · {count.format(queueSum.active)} in progress</span>}
            <Link className="jf-chip" to="/runs?tab=queue">Open ↗</Link>
          </div>
          {apply.isPending && <QueueSkeleton />}
          {apply.data && queue?.state !== "available" && <p className="jf-note">The application queue could not be read.</p>}
          {apply.isError && <p className="jf-note">The application queue could not be loaded.</p>}
          {groups.length > 0 && <>
            <div className="jf-dots-wrap">
              {groups.map((group) => (
                <div key={group.label} role="img" aria-label={`${group.label}: ${group.count}`}>
                  <h3>{group.label}<span>{group.count}</span></h3>
                  <div className="jf-dots">{Array.from({ length: group.count }, (_, index) => <i key={index} className={group.kind} />)}</div>
                </div>
              ))}
            </div>
            <div className="jf-legend">
              <span><i className="active" />In progress<b>{queueSum.active}</b></span>
              <span><i className="queued" />Queued<b>{queueSum.queued}</b></span>
              <span><i className="closed" />Closed<b>{queueSum.closed}</b></span>
              <span className="jf-mono">one dot = one job</span>
            </div>
          </>}
        </section>

        <section className="jf-card jf-pad" aria-labelledby="jf-progress-title">
          <h2 id="jf-progress-title">Apply progress</h2>
          <div className="jf-muted jf-gap">{first ? `Applied since ${dayTick(first.day)}` : "Applied"}</div>
          <div className="jf-big">{discoveryLoading ? <Skel width={120} height={42} /> : totals ? count.format(totals.applied) : "–"}</div>
          {discoveryLoading && <div className="jf-progress" aria-hidden="true"><Skel width="100%" height={6} /></div>}
          {totals && applyVerdicts > 0 && (
            <div className="jf-progress">
              <div className="bar"><i style={{ width: `${Math.min(100, (100 * totals.applied) / applyVerdicts)}%` }} /></div>
              <span className="jf-mono">{totals.applied} / {applyVerdicts} Apply verdicts</span>
            </div>
          )}
          <div className="jf-trio">
            <div><span>In progress</span><b>{apply.isPending ? <Skel width={32} height={19} /> : queue ? queueSum.active : "–"}</b></div>
            <div><span>Waiting on you</span><b>{waitingLoading ? <Skel width={56} height={19} /> : <>{waiting ?? "–"} <small>{waiting === 1 ? "item" : "items"}</small></>}</b></div>
            <div><span>Queued</span><b>{apply.isPending ? <Skel width={32} height={19} /> : queue ? queueSum.queued : "–"}</b></div>
          </div>
          <div className="jf-row">
            <Link className="jf-btn primary" to="/review">Open review</Link>
            <Link className="jf-btn" to="/jobs">Open jobs</Link>
          </div>
        </section>
      </div>

      <div className="jf-dash-grid">
        <section className="jf-card" aria-labelledby="jf-applied-title">
          <div className="jf-card-head"><h2 id="jf-applied-title">Applied over time</h2><span className="jf-meta jf-mono">cumulative</span></div>
          {discoveryLoading
            ? <div className="jf-chart-skel" role="status" aria-label="Loading applied over time"><Skel width="100%" height="100%" /></div>
            : latest && series.length > 1 ? <AppliedChart series={series} last={latest} /> : <p className="jf-note">No applications recorded yet.</p>}
        </section>
        <section className="jf-card" aria-labelledby="jf-runs-title">
          <div className="jf-card-head"><h2 id="jf-runs-title">Recent runs</h2><Link className="jf-chip" to="/scanner-runs">All ↗</Link></div>
          <div className="jf-runs">
            {runs.isPending && <RunsSkeleton />}
            {runs.isError && <p className="jf-note">Recent runs could not be loaded.</p>}
            {runs.data?.runs.length === 0 && <p className="jf-note">No runs in the last {SPARK_DAYS} days.</p>}
            {runs.data?.runs.slice(0, 5).map((run) => <RunRow key={run.run_id} run={run} />)}
          </div>
        </section>
      </div>
    </div>
  );
}

/** Pulsing placeholder block; the surrounding region carries the accessible loading label. */
function Skel({ width, height, style }: { width: CSSProperties["width"]; height: CSSProperties["height"]; style?: CSSProperties }) {
  return <span className="jf-skel" aria-hidden="true" style={{ width, height, ...style }} />;
}

const SKEL_SPARK = [14, 22, 18, 28, 20, 32, 24, 30, 18, 26, 34, 22, 28, 36];

function Kpi({ label, value, foot, spark, loading }: { label: string; value: string; foot: ReactNode; spark: number[]; loading: boolean }) {
  const max = Math.max(...spark, 0) || 1;
  if (loading) {
    return (
      <div className="jf-kpi" role="status" aria-busy="true" aria-label={`Loading ${label}`}>
        <div className="lab">{label}</div>
        <div className="val"><Skel width={96} height={32} /></div>
        <div className="foot">
          <Skel width={84} height={14} />
          <div className="jf-spark" aria-hidden="true">
            {SKEL_SPARK.map((height, index) => <i key={index} className="jf-skel" style={{ height, animationDelay: `${index * 60}ms` }} />)}
          </div>
        </div>
      </div>
    );
  }
  return (
    <div className="jf-kpi">
      <div className="lab">{label}</div>
      <div className="val">{value}</div>
      <div className="foot">
        <span>{foot}</span>
        <div className="jf-spark" aria-hidden="true">
          {spark.map((point, index) => <i key={index} className={index === spark.length - 1 ? "hi" : undefined} style={{ height: Math.max(3, Math.round((38 * point) / max)) }} />)}
        </div>
      </div>
    </div>
  );
}

function QueueSkeleton() {
  return (
    <div className="jf-dots-wrap" role="status" aria-busy="true" aria-label="Loading the application queue">
      {[9, 5, 12].map((columns, group) => (
        <div key={group} aria-hidden="true">
          <Skel width={90} height={14} style={{ marginBottom: 10 }} />
          <div className="jf-dots">{Array.from({ length: columns * 4 }, (_, index) => <i key={index} className="jf-skel" />)}</div>
        </div>
      ))}
    </div>
  );
}

function RunsSkeleton() {
  return (
    <div role="status" aria-busy="true" aria-label="Loading recent runs">
      {Array.from({ length: 5 }, (_, index) => (
        <div key={index} className="jf-run" aria-hidden="true">
          <Skel width={36} height={36} style={{ flex: "none", borderRadius: "50%" }} />
          <div className="jf-skel-lines"><Skel width={88} height={14} /><Skel width={180} height={12} /></div>
          <Skel width={56} height={14} style={{ marginLeft: "auto" }} />
        </div>
      ))}
    </div>
  );
}

function RunRow({ run }: { run: RunSummary }) {
  const evaluation = run.source === "evaluate";
  const state = run.status === "failed" || run.status === "running" ? run.status : null;
  return (
    <div className="jf-run">
      <div className={run.status === "failed" ? "ic hot" : "ic"} aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
          {evaluation ? <path d="M4 20V11M10 20V5M16 20v-6" /> : <><circle cx="11" cy="11" r="7" /><path d="m20 20-4-4" /></>}
        </svg>
      </div>
      <div>
        <div className="t">{evaluation ? "Evaluation" : "Scan"}{state && <> · <em>{state}</em></>}</div>
        <div className="m">{formatLocalDateTime(run.started_at)} · {evaluation ? `${count.format(run.jobs_scored)} scored` : `${count.format(run.jobs_discovered)} found`}</div>
      </div>
      <div className="v">{evaluation ? money(run.total_llm_cost_usd) : `+${count.format(run.jobs_inserted)} new`}</div>
    </div>
  );
}

type AppliedPoint = { day: string; total: number };

function AppliedChart({ series, last }: { series: AppliedPoint[]; last: AppliedPoint }) {
  const W = 640, H = 230, L = 6, R = 48, T = 26, B = 24;
  const top = Math.max(last.total, 1);
  const x = (index: number) => L + ((W - L - R) * index) / (series.length - 1);
  const y = (value: number) => T + (H - T - B) * (1 - value / top);
  const points = series.map((point, index) => `${x(index)},${y(point.total)}`).join(" ");
  const ticks = [0, Math.floor((series.length - 1) / 2), series.length - 1];
  return (
    <svg className="jf-chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${last.total} applications by ${dayTick(last.day)}`}>
      {[0, top / 2, top].map((value) => (
        <g key={value}>
          <line x1={L} x2={W - R} y1={y(value)} y2={y(value)} className="grid" />
          <text x={W - R + 10} y={y(value) + 4}>{Math.round(value)}</text>
        </g>
      ))}
      {ticks.map((index, position) => <text key={index} x={x(index)} y={H - 4} textAnchor={position === 0 ? "start" : position === 1 ? "middle" : "end"}>{dayTick(series[index]?.day ?? last.day)}</text>)}
      <polygon points={`${x(0)},${y(0)} ${points} ${x(series.length - 1)},${y(0)}`} className="area" />
      <polyline points={points} className="line" />
      <circle cx={x(series.length - 1)} cy={y(last.total)} r="5" className="end" />
    </svg>
  );
}
