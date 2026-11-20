import { useRef, useState } from "react";
import { useSearchParams } from "react-router";

import { isApplicationsNotInstalled } from "@/api/client";
import { useTracker, useTrackerMessage, type MailStatus, type TrackerApplication, type TrackerMessage } from "@/api/tracker";
import { Board, STAGES } from "@/components/jobs/ApplicationBoard";
import { ApplicationPanel } from "@/components/jobs/ApplicationPanel";
import { mailFor, matches, progressCards } from "@/components/jobs/boards";
import { ApplicationsNotSetUp } from "@/components/shell/ApplicationsNotSetUp";
import { formatLocalDateTime, formatRelativeAge } from "@/lib/dates";
import "@/routes/jobs.css";
import "@/routes/tracker.css";

const STATUS_LABEL: Record<MailStatus, string> = {
  applied: "Received", action: "Needs action", update: "Update", interviewing: "Interview", rejected: "Rejected", offer: "Offer",
};
const INBOX_FILTERS: (MailStatus | "all")[] = ["all", "interviewing", "offer", "action", "rejected", "update", "applied"];
const PAGE = 20;

/** Where every application stands: its email in the Inbox, and one card per application by stage in the Pipeline. */
export default function TrackerPage() {
  const tracker = useTracker();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "pipeline" ? "pipeline" : "inbox";
  const [search, setSearch] = useState("");
  const sync = tracker.data?.last_sync;
  const label = tab === "inbox" ? "Search messages" : "Search companies";
  if (isApplicationsNotInstalled(tracker.error)) return <div className="jf-tracker"><ApplicationsNotSetUp className="jf-tracker-empty" /></div>;

  return (
    <div className="jf-tracker">
      <div className="jf-tracker-bar">
        <div className="jf-pills" role="tablist" aria-label="Tracker view">
          {(["inbox", "pipeline"] as const).map((id) => (
            <button key={id} type="button" role="tab" aria-selected={tab === id} aria-pressed={tab === id} onClick={() => { setSearch(""); setParams(id === "inbox" ? {} : { tab: id }); }}>
              {id === "inbox" ? "Inbox" : "Pipeline"}
            </button>
          ))}
        </div>
        <input className="jf-tracker-search" type="search" aria-label={label} placeholder={label} value={search} onChange={(event) => setSearch(event.target.value)} />
      </div>
      <p className="jf-tracker-sync">
        {sync ? `Mail last checked ${formatRelativeAge(sync.finished_at)} ago · ${sync.new_messages} new` : "Mail has not been checked yet."}
        {sync?.error && <span className="warn"> · The last check failed, so newer mail may be missing.</span>}
      </p>
      {tracker.isPending && <p role="status" className="jf-tracker-empty">Loading application email.</p>}
      {tracker.isError && <p role="alert" className="jf-tracker-empty">Application email could not be loaded.</p>}
      {tracker.data && (tab === "inbox"
        ? <Inbox messages={tracker.data.messages} search={search} />
        : <Pipeline applications={tracker.data.applications} messages={tracker.data.messages} search={search} />)}
    </div>
  );
}

function Pipeline({ applications, messages, search }: { applications: TrackerApplication[]; messages: TrackerMessage[]; search: string }) {
  const [openKey, setOpenKey] = useState<string | null>(null);
  const cards = progressCards(applications);
  const open = openKey === null ? undefined : applications[Number(openKey)];
  const recorded = applications.filter((application) => application.source === "jobfeed");
  return (
    <>
      {recorded.length > 0 && <p className="jf-tracker-sync">{recorded.filter((application) => application.confirmed_by_email).length} of {recorded.length} recorded applications have a confirmation email.</p>}
      <Board columns={STAGES} cards={cards.filter((card) => matches(search, card.company, card.title))} onOpen={setOpenKey} />
      {open && <ApplicationPanel application={open} messages={mailFor(open, messages)} onClose={() => setOpenKey(null)} />}
    </>
  );
}

function Inbox({ messages, search }: { messages: TrackerMessage[]; search: string }) {
  const [filter, setFilter] = useState<MailStatus | "all">("all");
  const [page, setPage] = useState(0);
  const [openId, setOpenId] = useState<string | null>(null);
  const needle = search.trim().toLowerCase();
  const matching = messages.filter((message) => !needle || `${message.company ?? ""} ${message.sender_name ?? ""} ${message.subject} ${message.snippet}`.toLowerCase().includes(needle));
  const shown = matching.filter((message) => filter === "all" || message.status === filter);
  const pages = Math.max(1, Math.ceil(shown.length / PAGE));
  const current = Math.min(page, pages - 1);
  const rows = shown.slice(current * PAGE, current * PAGE + PAGE);
  const open = messages.find((message) => message.id === openId) ?? null;
  const barRef = useRef<HTMLDivElement>(null);
  const inboxRef = useRef<HTMLDivElement>(null);

  // The list stays put while a long email scrolls, so a new pick may land with the reader's top out of view.
  const select = (id: string) => {
    setOpenId(id);
    const inbox = inboxRef.current;
    if (inbox && inbox.getBoundingClientRect().top < (barRef.current?.getBoundingClientRect().bottom ?? 0)) inbox.scrollIntoView({ block: "start" });
  };

  return (
    <>
      <div ref={barRef} className="jf-tracker-sticky">
        <div className="jf-pills jf-tracker-filters" role="group" aria-label="Filter messages">
          {INBOX_FILTERS.map((id) => (
            <button key={id} type="button" aria-pressed={filter === id} onClick={() => { setFilter(id); setPage(0); }}>
              {id === "all" ? "All" : STATUS_LABEL[id]} <b>{id === "all" ? matching.length : matching.filter((message) => message.status === id).length}</b>
            </button>
          ))}
        </div>
      </div>
      <div ref={inboxRef} className="jf-inbox">
        <div className="jf-inbox-list">
          {shown.length === 0 ? <p className="jf-inbox-none">No application email matches.</p> : (
            <ul className="jf-mail" aria-label="Application email">
              {rows.map((message) => (
                <li key={message.id}>
                  <button type="button" aria-current={message.id === openId ? "true" : undefined} onClick={() => select(message.id)}>
                    <span className="jf-mail-avatar" aria-hidden="true">{initials(message.company ?? message.sender_name ?? message.sender)}</span>
                    <span className="jf-mail-body">
                      <span className="jf-mail-top"><b>{message.company ?? message.sender_name ?? message.sender}</b><span>{formatRelativeAge(message.received_at)}</span></span>
                      <span className="jf-mail-subject"><i className={`dot ${message.status}`} title={STATUS_LABEL[message.status]} /><span className="jf-sr">{STATUS_LABEL[message.status]}: </span>{message.subject}</span>
                      <span className="jf-mail-snippet">{message.snippet}</span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {shown.length > PAGE && (
            <div className="jf-tracker-pager">
              <span>{current * PAGE + 1} to {current * PAGE + rows.length} of {shown.length}</span>
              <button type="button" disabled={current === 0} onClick={() => setPage(current - 1)}>Previous</button>
              <button type="button" disabled={current >= pages - 1} onClick={() => setPage(current + 1)}>Next</button>
            </div>
          )}
        </div>
        <Reader message={open} />
      </div>
    </>
  );
}

function Reader({ message }: { message: TrackerMessage | null }) {
  const body = useTrackerMessage(message?.id ?? null);
  if (message === null) return <section className="jf-reader empty" aria-label="Message"><p>Select a message to read</p></section>;
  return (
    <section className="jf-reader" aria-label="Message">
      <header>
        <h2>{message.subject}</h2>
        <div className="jf-reader-meta">
          <div>
            <b>{message.sender_name ?? message.company ?? message.sender}</b> <span>&lt;{message.sender}&gt;</span>
            {(body.data?.recipients ?? []).length > 0 && <div className="to">to {(body.data?.recipients ?? []).join(", ")}</div>}
          </div>
          <div className="when">
            <span>{formatLocalDateTime(message.received_at)}</span>
            <span className={`jf-reader-status ${message.status}`}>{STATUS_LABEL[message.status]}</span>
            {message.view_url && <a href={message.view_url} target="_blank" rel="noreferrer">Open in Gmail ↗</a>}
          </div>
        </div>
      </header>
      {body.isPending && <p role="status" className="jf-reader-note">Fetching this email from Gmail. The first time takes a few seconds.</p>}
      {body.isError && <p role="alert" className="jf-reader-note">This email could not be fetched. The snippet is: {message.snippet}</p>}
      {body.data && <div className="jf-reader-body">{body.data.body || "This email has no plain-text body. Open it in Gmail."}</div>}
    </section>
  );
}

function initials(name: string): string {
  const words = name.replace(/[^\p{L}\p{N} ]/gu, " ").split(/\s+/).filter(Boolean);
  return ((words[0]?.[0] ?? "?") + (words[1]?.[0] ?? "")).toUpperCase();
}
