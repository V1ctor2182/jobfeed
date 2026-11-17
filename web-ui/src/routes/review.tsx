import { useState } from "react";
import { Link } from "react-router";

import { profileAnswers, useAnswerBank } from "@/api/answer-bank";
import { isQuestion, useResumeSite, useReview, useReviewIgnore, useReviewReply, type ReviewAction, type ReviewItem, type ReviewSite } from "@/api/review";
import { formatRelativeAge } from "@/lib/dates";
import "@/routes/review.css";

type Filter = "question" | "blocked";
const FILTERS: { id: Filter; label: string }[] = [{ id: "question", label: "Questions" }, { id: "blocked", label: "Blocked jobs" }];

/** Everything Appliers are waiting on the user for. Each button sends one reply. Submit is confirmed elsewhere. */
export default function ReviewPage() {
  const review = useReview();
  const answers = useAnswerBank();
  const [filter, setFilter] = useState<Filter>("question");
  const sites = review.data?.sites ?? [];
  // A job stopped by a job board's sign-in is shown once, under that board's card, not as a blocked job of its own.
  const grouped = new Set(sites.flatMap((site) => site.request_ids ?? []));
  const items = (review.data?.items ?? []).filter((item) => !grouped.has(item.id));
  const shown = items.filter((item) => isQuestion(item) === (filter === "question"));
  const count = (id: Filter) => items.filter((item) => isQuestion(item) === (id === "question")).length + (id === "question" ? sites.length : 0);
  const drafted = answers.data ? profileAnswers(answers.data).waiting.length : 0;

  return (
    <div className="jf-review">
      <div className="jf-review-bar">
        <div className="jf-pills" role="group" aria-label="Filter">
          {FILTERS.map(({ id, label }) => (
            <button key={id} type="button" aria-pressed={filter === id} onClick={() => setFilter(id)}>
              {label} <b>{count(id)}</b>
            </button>
          ))}
        </div>
        <span className="jf-review-note">Answering never submits an application.</span>
      </div>
      {review.isPending && <p role="status" className="jf-review-empty">Loading open questions.</p>}
      {review.isError && <p role="alert" className="jf-review-empty">Open questions could not be loaded.</p>}
      {filter === "blocked" && shown.length > 0 && <p className="jf-review-note">These are not questions. Each Applier stopped at a step only you can do, such as signing in. Do the step and press Done, or ignore the job.</p>}
      {filter === "question" && sites.map((site) => <SiteCard key={site.site} site={site} jobs={(review.data?.items ?? []).filter((item) => grouped.has(item.id) && (site.request_ids ?? []).includes(item.id))} />)}
      {review.data && shown.length === 0 && !(filter === "question" && sites.length > 0) && <p className="jf-review-empty">{filter === "question" ? "No questions are waiting on you." : "No jobs are blocked."}</p>}
      {shown.map((item) => <ReviewCard key={item.id} item={item} />)}
      {(review.data?.ended ?? 0) > 0 && <p className="jf-review-note">{review.data?.ended} older requests are hidden because their Applier has ended and cannot take a reply.</p>}
      {drafted > 0 && (
        <Link className="jf-review-more" to="/profile?section=answers">
          <span>{drafted} drafted {drafted === 1 ? "answer waits" : "answers wait"} for your approval</span><span>Answer Bank ↗</span>
        </Link>
      )}
    </div>
  );
}

/** One action for a whole job board: sign in there once, and every job it stopped can go on. */
function SiteCard({ site, jobs }: { site: ReviewSite; jobs: ReviewItem[] }) {
  const resume = useResumeSite();
  const [open, setOpen] = useState(false);
  const name = site.label.replace(/^Sign in to /, "");
  return (
    <article className="jf-review-card site" aria-label={site.label}>
      <header>
        <div>
          <h2>{site.held ? site.label : `Retry ${jobs.length} ${name} ${jobs.length === 1 ? "job" : "jobs"}`}</h2>
          <div className="jf-review-meta">
            {jobs.length} {jobs.length === 1 ? "job" : "jobs"} stopped at a sign-in · {site.held ? `${site.queued} queued ${name} jobs paused` : `${name} is not paused`}
            {site.latest_stop_at && ` · last stop ${formatRelativeAge(site.latest_stop_at)} ago`}
          </div>
        </div>
        <div className="jf-review-links">{site.page_url && <a href={site.page_url} target="_blank" rel="noreferrer">Sign-in page ↗</a>}</div>
      </header>
      <p className="jf-review-text">
        {site.held
          ? `${name} asks for a sign-in before its Apply button works, and an Applier never signs in or creates an account for you. No new ${name} job starts until you resume it. Jobs from other sites go on. Sign in to ${name} in the applying browser, then resume.`
          : `These Appliers stopped at a ${name} sign-in some time ago and are still waiting. No ${name} job has stopped there recently, so new ${name} jobs are running normally. If the applying browser is signed in to ${name}, tell them to try again.`}
      </p>
      <div className="jf-review-actions">
        <button type="button" className="primary" disabled={resume.isPending} onClick={() => resume.mutate(site.site)}>{site.held ? `I signed in, resume ${name}` : `I'm signed in, retry them`}</button>
        {jobs.length > 0 && <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "Hide" : "Show"} the {jobs.length} {jobs.length === 1 ? "job" : "jobs"}</button>}
      </div>
      {resume.error && <p role="alert" className="jf-review-warn">{resume.error.message}</p>}
      {open && <div className="jf-review-nested">{jobs.map((item) => <ReviewCard key={item.id} item={item} />)}</div>}
    </article>
  );
}

function ReviewCard({ item }: { item: ReviewItem }) {
  const reply = useReviewReply();
  const ignore = useReviewIgnore();
  const [typing, setTyping] = useState<ReviewAction | null>(null);
  const [text, setText] = useState("");
  const [confirmIgnore, setConfirmIgnore] = useState(false);
  const busy = reply.isPending || ignore.isPending;
  const send = (action: ReviewAction, value?: string) => reply.mutate({ request_id: item.id, action, text: value });
  const open = (action: ReviewAction, initial = "") => { setTyping(action); setText(initial); };
  const error = reply.error ?? ignore.error;
  const options = item.options ?? [];
  const quick = item.quick_replies ?? [];
  const name = [item.company, item.title].filter(Boolean).join(" · ") || `Job ${item.job_id}`;

  return (
    <article className="jf-review-card" aria-label={name}>
      <header>
        <div>
          <h2>{name}</h2>
          <div className="jf-review-meta">job #{item.job_id} · {item.kind === "blocker" ? "blocker" : "question"}{item.field ? ` · ${item.field}` : ""} · {formatRelativeAge(item.created_at)}</div>
        </div>
        <div className="jf-review-links">
          {item.posting_url && <a href={item.posting_url} target="_blank" rel="noreferrer">Posting ↗</a>}
          {item.page_url && <a href={item.page_url} target="_blank" rel="noreferrer">Form page ↗</a>}
        </div>
      </header>
      <p className="jf-review-text">{item.text}</p>
      {item.draft && <blockquote className="jf-review-draft"><span>Draft answer</span>{item.draft}</blockquote>}

      {typing === null ? (
        <div className="jf-review-actions">
          {options.map((option) => <button key={option} type="button" className="primary" disabled={busy} onClick={() => send("pick", option)}>{option}</button>)}
          {quick.map((word) => <button key={word} type="button" className="primary" disabled={busy} onClick={() => send("quick", word)}>{word.charAt(0).toUpperCase() + word.slice(1)}</button>)}
          {item.draft && <>
            <button type="button" className="primary" disabled={busy} onClick={() => send("approve")}>Approve draft</button>
            <button type="button" disabled={busy} onClick={() => open("change_request")}>Request changes</button>
            <button type="button" disabled={busy} onClick={() => open("answer", item.draft ?? "")}>Write my own</button>
          </>}
          {!item.draft && <button type="button" disabled={busy} onClick={() => open("answer")}>Other</button>}
          {confirmIgnore
            ? <button type="button" className="danger" disabled={busy} onClick={() => ignore.mutate(item.job_id)}>Yes, ignore job {item.job_id}</button>
            : <button type="button" className="quiet" disabled={busy} onClick={() => setConfirmIgnore(true)}>Ignore job</button>}
        </div>
      ) : (
        <form className="jf-review-form" onSubmit={(event) => { event.preventDefault(); if (text.trim()) send(typing, text.trim()); }}>
          <label htmlFor={`reply-${item.id}`}>{typing === "change_request" ? "What should change?" : "Your answer"}</label>
          <textarea id={`reply-${item.id}`} value={text} rows={3} autoFocus onChange={(event) => setText(event.target.value)} />
          <div className="jf-review-actions">
            <button type="submit" className="primary" disabled={busy || !text.trim()}>{typing === "change_request" ? "Send change request" : "Send answer"}</button>
            <button type="button" disabled={busy} onClick={() => setTyping(null)}>Cancel</button>
          </div>
        </form>
      )}
      {confirmIgnore && typing === null && <p className="jf-review-warn">Ignoring stops this job's Applier, cancels the job in the queue, and marks it ignored in Jobfeed.</p>}
      {error && <p role="alert" className="jf-review-warn">{error.message}</p>}
    </article>
  );
}
