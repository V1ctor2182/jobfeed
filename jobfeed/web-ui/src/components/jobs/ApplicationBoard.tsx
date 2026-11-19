import type { Stage } from "@/api/tracker";

export const STAGES: { id: Stage; label: string }[] = [
  { id: "applied", label: "Applied" }, { id: "ghosted", label: "Ghosted" }, { id: "interviewing", label: "Interviewing" },
  { id: "rejected", label: "Rejected" }, { id: "offer", label: "Offer" },
];
const COLUMN_LIMIT = 40;

export interface BoardColumn {
  id: string;
  label: string;
  /** The true size of the column when not every card is loaded. Defaults to the number of cards. */
  count?: number;
}

export interface BoardCard {
  key: string;
  column: string;
  company: string;
  title: string;
  meta: string;
  /** A short flag shown after the meta line, such as a missing confirmation email. */
  flag?: string;
}

export function initials(name: string): string {
  const words = name.replace(/[^\p{L}\p{N} ]/gu, " ").split(/\s+/).filter(Boolean);
  return ((words[0]?.[0] ?? "?") + (words[1]?.[0] ?? "")).toUpperCase();
}

/** One column per stage, one card per job. A card opens that job. */
export function Board({ columns, cards, onOpen }: { columns: BoardColumn[]; cards: BoardCard[]; onOpen: (key: string) => void }) {
  return (
    <div className="jf-board" style={{ gridTemplateColumns: `repeat(${columns.length}, minmax(210px, 1fr))` }}>
      {columns.map((column) => {
        const own = cards.filter((card) => card.column === column.id);
        const count = column.count ?? own.length;
        return (
          <section key={column.id} className={`jf-column ${column.id}`} aria-label={`${column.label}: ${count}`}>
            <h2>{column.label}<span>{count}</span></h2>
            {own.length === 0 && <p className="jf-column-empty">{count > 0 ? "None of these match or are loaded." : "No jobs"}</p>}
            {own.slice(0, COLUMN_LIMIT).map((card) => (
              <button key={card.key} type="button" className="jf-app-card" onClick={() => onOpen(card.key)}>
                <span className="jf-avatar" aria-hidden="true">{initials(card.company)}</span>
                <span className="jf-app-card-text">
                  <b>{card.company}</b>
                  <span className="title">{card.title}</span>
                  <span className="meta">{card.meta}{card.flag && <em> · {card.flag}</em>}</span>
                </span>
              </button>
            ))}
            {Math.max(count, own.length) > Math.min(own.length, COLUMN_LIMIT) && own.length > 0 && (
              <p className="jf-column-empty">{Math.max(count, own.length) - Math.min(own.length, COLUMN_LIMIT)} more. Search to narrow.</p>
            )}
          </section>
        );
      })}
    </div>
  );
}
