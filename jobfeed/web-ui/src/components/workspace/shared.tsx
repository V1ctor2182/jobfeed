import ColumnLayout from "@cloudscape-design/components/column-layout";
import Link from "@cloudscape-design/components/link";
import { useNavigate } from "react-router";
import "./workspace.css";

export function timestamp(value: string | number | null | undefined) {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "Invalid timestamp" : date.toLocaleString();
}

export function label(value: string | null | undefined) {
  return value ? value.replaceAll("_", " ") : "Not recorded";
}

export function RouteLink({ href, children }: { href: string; children: React.ReactNode }) {
  const navigate = useNavigate();
  return <Link href={href} onFollow={(event) => { event.preventDefault(); navigate(href); }}>{children}</Link>;
}

export function Metrics({ items }: { items: { label: string; value: number | null | undefined }[] }) {
  return <ColumnLayout columns={items.length} minColumnWidth={100} borders="vertical">
    {items.map((item) => <div key={item.label}>
      <div className="jf-figure-label">{item.label}</div>
      <div className="jf-figure-value">{item.value?.toLocaleString() ?? "Unknown"}</div>
    </div>)}
  </ColumnLayout>;
}

export function LoadingSnapshot({ label: text }: { label: string }) {
  return <div className="workspace-loading" aria-busy="true" aria-label={text} role="status">
    <span>{text}</span><div /><div /><div />
  </div>;
}
