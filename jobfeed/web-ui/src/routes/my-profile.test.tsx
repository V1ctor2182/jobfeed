import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router";
import type { AnswerBank } from "@/api/answer-bank";
import type { MyProfile } from "@/api/my-profile";
import type { ProfilePreferences } from "@/api/my-profile-preferences";
import type { ResumeState } from "@/api/onboarding-resume";
import MyProfilePage from "./my-profile";
import App from "@/App";
import { configurationKey } from "@/api/configuration";
import { DensityProvider } from "@/lib/density";

const PERSONAL: MyProfile = {
  personal_details: { legal_name: "Example Candidate", preferred_name: "Example", primary_email: "candidate@example.test", phone: "", address: "Example City", linkedin_url: "", portfolio_url: "", github_url: "", work_authorization: "", sponsorship_requirement: "", relocation: "", availability: "", education: "Example University" },
  revision: "revision-1", reviewed: false,
  resumes: [{ id: "original", filename: "original.pdf", active: true, format:"pdf", application_supported:true, status:"Available for applications" }],
  application_resumes: [{ id: "original", label: "Original résumé", filename: "original.pdf", available: true }],
};
const PREFERENCES: ProfilePreferences = {
  profile: { desired_titles: ["Software Engineer"], seniority_levels: [], target_countries: ["US"], target_locations: ["Example City"], work_modes: ["remote"], industries: [], company_sizes: [], work_authorization: "", hiring_timeline: "", excluded_titles: [], excluded_companies: [], excluded_locations: [], excluded_keywords: [], maximum_posting_age_hours: 36, maximum_posting_age_days: null, resume_evidence: [] },
  searches_need_review: false, scoring_resume_active: true,
};
const RESUME: ResumeState = { original_name: "current.pdf", extracted_text: "Example résumé text", profile: PREFERENCES.profile, is_confirmed: true };
const APPROVAL = { version: 1, approved_at: "2026-01-01T00:00:00Z", event_id: "event", source: { kind: "dashboard" } };
const BANK: AnswerBank = {
  schema_version: 1, revision: 3,
  common_questions: [
    { key: "age_18", group: "demographic", question: "Are you at least 18 years old?", aliases: ["Are you over 18?"], options: ["Yes", "No"], entry_id: null },
    { key: "gender", group: "demographic", question: "What is your gender?", aliases: ["Gender"], options: ["Male", "Female", "Decline to self-identify"], entry_id: "gender-entry" },
    { key: "desired_salary", group: "application", question: "What is your desired salary?", aliases: [], options: [], entry_id: null },
  ],
  entries: [
    { id: "gender-entry", current_version: 1, versions: [{ version: 1, question: "Gender", answer: "Decline to self-identify", aliases: [], scope: { kind: "global" }, valid_until: null, state: "approved", created_at: "2026-01-01T00:00:00Z", approval: APPROVAL }] },
    { id: "draft", current_version: 1, versions: [{ version: 1, question: "Why Example Co?", answer: "Drafted synthetic reason.", aliases: [], scope: { kind: "job", company: "Example Co", job_id: "7" }, valid_until: null, state: "pending_review", created_at: "2026-01-01T00:00:00Z", approval: null }] },
    { id: "notice", current_version: 1, versions: [{ version: 1, question: "What is your notice period?", answer: "Two weeks", aliases: [], scope: { kind: "global" }, valid_until: null, state: "approved", created_at: "2026-01-01T00:00:00Z", approval: APPROVAL }] },
  ],
};
let bank: AnswerBank;

/** Applies a save or delete the way the answer store does, so the page shows what was saved. */
function answerBankResponse(body: Record<string, unknown> | undefined) {
  if (!body) return json(bank);
  if (body.expected_revision !== bank.revision) return json({ error: { code: "answer_bank_conflict", message: "Answers changed.", request_id: "test" } }, 409);
  bank.revision++;
  let entry = bank.entries.find(candidate => candidate.id === body.entry_id);
  if (body.action === "save") {
    const draft = body.draft as AnswerBank["entries"][number]["versions"][number];
    const version = { ...draft, version: (entry?.current_version ?? 0) + 1, state: "approved" as const, created_at: "2026-02-01T00:00:00Z", approval: APPROVAL };
    if (entry) { entry.versions.at(-1)!.state = "superseded"; entry.versions.push(version); entry.current_version = version.version; }
    else { entry = { id: String(body.entry_id), current_version: 1, versions: [version] }; bank.entries.push(entry); }
    const item = bank.common_questions!.find(candidate => candidate.question === draft.question);
    if (item) item.entry_id = entry.id;
  } else if (body.action === "revoke" && entry) {
    entry.versions.at(-1)!.state = "revoked";
    for (const item of bank.common_questions!) if (item.entry_id === entry.id) item.entry_id = null;
  }
  return json(bank);
}

let personal: MyProfile;
let preferences: ProfilePreferences;
let resume: ResumeState;
let personalStatus: number;
let preferenceStatus: number;
const requests: { path: string; method: string; body: unknown }[] = [];

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}

function Location() { const location = useLocation(); return <div data-testid="location">{location.pathname}{location.search}</div>; }
function mount(section?: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const entry = section ? `/profile?section=${section}` : "/profile";
  return { ...render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[entry]}><MyProfilePage /><Location /></MemoryRouter></QueryClientProvider>), client };
}
function openSection(name: RegExp) {
  fireEvent.click(within(screen.getByRole("navigation", { name: "Profile sections" })).getByRole("link", { name }));
}

beforeEach(() => {
  personal = structuredClone(PERSONAL);
  bank = structuredClone(BANK);
  preferences = structuredClone(PREFERENCES);
  resume = structuredClone(RESUME);
  personalStatus = 200;
  preferenceStatus = 200;
  requests.length = 0;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : init?.body;
    requests.push({ path, method, body });
    if (path.startsWith("/api/spam-review")) return json({ total: 0 });
    if (path === "/api/answer-bank") return answerBankResponse(body);
    if (path === "/api/my-profile") return json(personal);
    if (path === "/api/my-profile/personal") {
      if (personalStatus !== 200) return json({ error: { code: "conflict", message: "Profile changed", request_id: "test" } }, personalStatus);
      personal = { ...personal, personal_details: body.personal_details, revision: "revision-2", reviewed: true };
      return json(personal);
    }
    if (path === "/api/my-profile/preferences") {
      if (method === "PUT") {
        if (preferenceStatus !== 200) return json({ error: { code: "save_failed", message: "Could not save preferences", request_id: "test" } }, preferenceStatus);
        preferences = { ...preferences, profile: body, searches_need_review: true, scoring_resume_active: true };
        resume = { ...resume, profile: body, is_confirmed: true };
      }
      return json(preferences);
    }
    if (path === "/api/onboarding/resume") {
      if (method === "POST") {
        const file = (body as FormData).get("file") as File;
        resume = { ...resume, original_name: file.name, extracted_text: "Replacement résumé text" };
        preferences.scoring_resume_active = false;
        const previous = personal.resumes.find(entry => entry.filename.toLowerCase() === file.name.toLowerCase());
        const entry = { id: previous?.id ?? `upload-${file.name}`, filename: file.name, active: previous?.active ?? true, format:"pdf", application_supported: file.name.endsWith(".pdf"), status:"Available for applications" };
        personal.resumes = [...personal.resumes.filter(row => row.id !== entry.id), entry];
      }
      return json(resume);
    }
    if (path === "/api/my-profile/resumes/upload") return json(personal);
    if (path === "/api/my-profile/application-resume") {
      personal.application_resumes.push({ id: "uploaded", label: "Uploaded résumé", filename: resume.original_name!, available: true });
      return json(personal);
    }
    if (path === "/api/my-profile/resumes/original") {
      personal.resumes[0]!.active = body.active;
      return json(personal);
    }
    throw new Error(`Unexpected request ${method} ${path}`);
  }));
});

afterEach(() => vi.unstubAllGlobals());

it("separates the complete master from downloadable application variants", async () => {
  personal.resumes = [
    { id:"master", filename:"Master.docx", label:"Complete master", kind:"master", active:false,
      format:"docx", application_supported:false, status:"Complete source material", google_doc_url:"https://docs.google.com/document/d/master/edit", download_url:"/api/my-profile/resumes/master/download" },
    { id:"swe", filename:"SWE.pdf", label:"SWE one page", kind:"variant", role:"Software Engineer", active:true,
      format:"pdf", application_supported:true, status:"Available for applications", page_count:1,
      google_doc_url:"https://docs.google.com/document/d/swe/edit", download_url:"/api/my-profile/resumes/swe/download",
      pdf_export:{ source_google_doc_url:"https://docs.google.com/document/d/swe/edit", exported_at:null, page_count:1, provenance:"user_declared" } },
  ];
  mount("resume");
  expect(await screen.findByText("Complete master")).toBeVisible();
  expect(screen.getByText("SWE one page")).toBeVisible();
  expect(screen.getByRole("link", { name:"Download saved PDF" })).toHaveAttribute("href", "/api/my-profile/resumes/swe/download");
  expect(screen.getAllByRole("checkbox", { name:"Available for future applications" })).toHaveLength(1);
  expect(screen.getByText(/Export time not recorded/)).toBeVisible();
});

it("opens the master editor in a dialog and discards cancelled changes", async () => {
  personal.resumes = [{ id: "master", filename: "Master.docx", label: "Complete master", kind: "master", active: false,
    format: "docx", application_supported: false, status: "Complete source material",
    google_doc_url: "https://docs.google.com/document/d/master/edit" }];
  mount("resume");
  fireEvent.click(await screen.findByRole("button", { name: "Edit or replace master" }));
  const dialog = screen.getByRole("dialog", { name: "Update master résumé" });
  expect(dialog).toBeVisible();
  expect(within(dialog).getByLabelText("Resume name")).toHaveValue("Complete master");
  expect(within(dialog).getByLabelText("Google Docs URL")).toHaveValue("https://docs.google.com/document/d/master/edit");
  fireEvent.change(within(dialog).getByLabelText("Resume name"), { target: { value: "Unsaved change" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Edit or replace master" }));
  expect(within(screen.getByRole("dialog", { name: "Update master résumé" })).getByLabelText("Resume name")).toHaveValue("Complete master");
  fireEvent.click(screen.getByRole("button", { name: "Close résumé editor" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("opens the selected application variant in a dialog", async () => {
  personal.resumes = [{ id: "swe", filename: "SWE.pdf", label: "SWE one page", kind: "variant", role: "Software Engineer", active: true,
    format: "pdf", application_supported: true, status: "Available for applications",
    google_doc_url: "https://docs.google.com/document/d/swe/edit" }];
  mount("resume");
  fireEvent.click(await screen.findByRole("button", { name: "Edit or replace SWE one page" }));
  const dialog = screen.getByRole("dialog", { name: "Update application variant" });
  expect(dialog).toBeVisible();
  expect(within(dialog).getByLabelText("Resume name")).toHaveValue("SWE one page");
  expect(within(dialog).getByLabelText("Target role")).toHaveValue("Software Engineer");
  expect(within(dialog).getByLabelText("Google Docs URL")).toHaveValue("https://docs.google.com/document/d/swe/edit");
});

it("links each resume to its current Google PDF export without replacing the saved agent copy", async () => {
  personal.resumes = ["master", "swe", "ai-infra", "ai-engineer"].map((id, index) => ({
    id, label: id, filename: `${id}.pdf`, kind: index === 0 ? "master" : "variant",
    role: index === 0 ? "" : id, active: index !== 0, format: "pdf", application_supported: index !== 0,
    status: index === 0 ? "Complete source material" : "Available for applications",
    google_doc_url: `https://docs.google.com/document/d/${id}/edit`,
    download_url: `/api/my-profile/resumes/${id}/download`,
    page_count: index === 0 ? 2 : 1,
  }));
  mount("resume");
  for (const id of ["master", "swe", "ai-infra", "ai-engineer"]) {
    const sync = await screen.findByRole("link", { name: `Sync ${id}` });
    expect(sync).toHaveAttribute("href", `https://docs.google.com/document/d/${id}/export?format=pdf`);
    expect(sync).toHaveAttribute("target", "_blank");
    sync.addEventListener("click", event => event.preventDefault(), { once: true });
    fireEvent.click(sync);
  }
  expect(screen.getAllByRole("link", { name: "Download saved PDF" }).map(link => link.getAttribute("href"))).toEqual([
    "/api/my-profile/resumes/master/download", "/api/my-profile/resumes/swe/download",
    "/api/my-profile/resumes/ai-infra/download", "/api/my-profile/resumes/ai-engineer/download",
  ]);
  expect(screen.getByText("Sync downloads the latest Google Docs PDF. Use Edit or replace to update the saved agent copy.")).toBeVisible();
  expect(screen.getAllByRole("checkbox", { name: "Available for future applications" })).toHaveLength(3);
  expect(requests.every(request => request.method === "GET")).toBe(true);
  expect(screen.queryByText(/synced|sync complete/i)).not.toBeInTheDocument();
});

it("requires an explicit PDF source association before uploading a variant", async () => {
  mount("resume");
  fireEvent.click(await screen.findByRole("button", { name:"Add variant" }));
  fireEvent.change(screen.getByLabelText("Resume name"), { target:{ value:"AI Engineer" } });
  fireEvent.change(screen.getByLabelText("Target role"), { target:{ value:"AI Engineer" } });
  fireEvent.change(screen.getByLabelText("Google Docs URL"), { target:{ value:"https://docs.google.com/document/d/ai/edit" } });
  const file = screen.getByRole("dialog", { name: "Add application variant" }).querySelector<HTMLInputElement>('input[type="file"]')!;
  fireEvent.change(file, { target:{ files:[new File(["PDF"], "AI.pdf", { type:"application/pdf" })] } });
  expect(screen.getByRole("button", { name:"Save file and details" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name:"This PDF was exported from the Google Doc linked above." }));
  fireEvent.click(screen.getByRole("button", { name:"Save file and details" }));
  await waitFor(() => expect(requests.some(request => request.path === "/api/my-profile/resumes/upload")).toBe(true));
  const sent = requests.find(request => request.path === "/api/my-profile/resumes/upload")!.body as FormData;
  expect(JSON.parse(sent.get("metadata") as string)).toEqual({ kind:"variant", label:"AI Engineer", role:"AI Engineer", google_doc_url:"https://docs.google.com/document/d/ai/edit", provenance:"user_declared" });
  expect(requests.some(request => request.path === "/api/onboarding/resume" && request.method === "POST")).toBe(false);
});

it("loads existing personal details, résumé and preferences without starting work or requiring AI", async () => {
  mount("details");
  expect(await screen.findByLabelText("Legal name")).toHaveValue("Example Candidate");
  expect(await screen.findByLabelText("Desired job titles")).toHaveValue("Software Engineer");
  expect(screen.getByLabelText("Sponsorship requirement")).toHaveValue("");
  openSection(/^Résumé/);
  expect(screen.getByText(/original.pdf — Active/)).toBeVisible();
  expect(screen.queryByRole("button", { name: /analy[sz]e/i })).not.toBeInTheDocument();
  expect(requests.every(request => request.method === "GET")).toBe(true);
  expect(requests.some(request => /scan|readiness|start|batch/.test(request.path))).toBe(false);
});

it("makes My Profile reachable before setup is complete", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(configurationKey, { configured: false });
  render(<QueryClientProvider client={client}><DensityProvider><MemoryRouter initialEntries={["/profile"]}><App /></MemoryRouter></DensityProvider></QueryClientProvider>);
  expect(await screen.findByLabelText("Legal name")).toHaveValue("Example Candidate");
  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("My Profile");
  expect(screen.getByRole("link", { name: "Profile" })).toHaveAttribute("aria-current", "page");
});

it("saves personal details and keeps unsaved job preferences", async () => {
  mount("details");
  fireEvent.change(await screen.findByLabelText("Desired job titles"), { target: { value: "Research Engineer" } });
  fireEvent.change(screen.getByLabelText("Preferred name"), { target: { value: "Updated name" } });
  fireEvent.click(screen.getByRole("button", { name: "Save personal details" }));
  expect(await screen.findByText("Personal details saved and reviewed.")).toBeVisible();
  expect(screen.getByLabelText("Desired job titles")).toHaveValue("Research Engineer");
  expect(requests.find(request => request.path.endsWith("/personal"))?.body).toEqual({ personal_details: { ...PERSONAL.personal_details, preferred_name: "Updated name" }, revision: "revision-1" });
});

it("preserves edits on a stale personal profile conflict", async () => {
  personalStatus = 409;
  mount("details");
  fireEvent.change(await screen.findByLabelText("Legal name"), { target: { value: "User's latest edit" } });
  fireEvent.click(screen.getByRole("button", { name: "Save personal details" }));
  expect(await screen.findByText("The saved profile changed")).toBeVisible();
  expect(screen.getByLabelText("Legal name")).toHaveValue("User's latest edit");
  expect(screen.queryByText("Personal details saved and reviewed.")).not.toBeInTheDocument();
});

it("explains invalid personal fields before sending a save", async () => {
  mount("details");
  fireEvent.change(await screen.findByLabelText("Email"), { target: { value: "invalid-email" } });
  fireEvent.change(screen.getByLabelText("Portfolio URL"), { target: { value: "example.test" } });
  fireEvent.click(screen.getByRole("button", { name: "Save personal details" }));
  expect(screen.getByText("Enter an email address such as name@example.com.")).toBeVisible();
  expect(screen.getByText("Enter a complete http:// or https:// website URL.")).toBeVisible();
  expect(requests.some(request => request.path.endsWith("/personal"))).toBe(false);
});

it("saves multiline preferences without losing personal edits and shows required search review", async () => {
  mount("preferences");
  fireEvent.change(await screen.findByLabelText("Legal name"), { target: { value: "Unsaved personal edit" } });
  const titles = await screen.findByLabelText("Desired job titles");
  fireEvent.change(titles, { target: { value: "Engineer\n" } });
  expect(titles).toHaveValue("Engineer\n");
  fireEvent.change(titles, { target: { value: "Engineer\nResearcher" } });
  fireEvent.click(screen.getByRole("button", { name: "Save job preferences" }));
  expect(await screen.findByText("Discovery searches need review")).toBeVisible();
  expect(screen.getByLabelText("Legal name")).toHaveValue("Unsaved personal edit");
  expect(requests.find(request => request.path.endsWith("/preferences") && request.method === "PUT")?.body).toEqual({ ...PREFERENCES.profile, desired_titles: ["Engineer", "Researcher"] });
  fireEvent.click(screen.getByRole("button", { name: "Review searches" }));
  await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/setup/searches"));
});

it("keeps preferences when save fails", async () => {
  preferenceStatus = 500;
  mount("preferences");
  fireEvent.change(await screen.findByLabelText("Target locations"), { target: { value: "Another City" } });
  fireEvent.click(screen.getByRole("button", { name: "Save job preferences" }));
  expect(await screen.findByText("Job preferences could not be saved")).toBeVisible();
  expect(screen.getByLabelText("Target locations")).toHaveValue("Another City");
});

it("uploads directly into the single résumé list without separate activation", async () => {
  mount("resume");
  fireEvent.change(await screen.findByLabelText("Desired job titles"), { target: { value: "Unsaved role" } });
  const file = document.querySelector<HTMLInputElement>('input[type="file"]')!;
  fireEvent.change(file, { target: { files: [new File(["sample"], "replacement.pdf", { type: "application/pdf" })] } });
  fireEvent.click(screen.getByRole("button", { name: "Upload résumé" }));
  expect(await screen.findByText("Résumé saved in your list. Save job preferences to use the uploaded text for scoring. Your saved preferences were kept.")).toBeVisible();
  expect(screen.getByText(/replacement.pdf — Active/)).toBeVisible();
  expect(screen.getByLabelText("Desired job titles")).toHaveValue("Unsaved role");
  expect(requests.some(request => request.path === "/api/my-profile/application-resume")).toBe(false);
  expect(screen.queryByRole("button", { name: "Use this PDF for applications" })).not.toBeInTheDocument();
  expect(screen.getByText(/original.pdf — Active/)).toBeVisible();
  expect(requests.filter(request => request.method === "POST").map(request => request.path)).toEqual(["/api/onboarding/resume"]);
});

it("can disable an existing application PDF without starting a batch", async () => {
  mount("resume");
  const checkbox = await screen.findByRole("checkbox", { name: /original.pdf — Active/ });
  fireEvent.click(checkbox);
  await waitFor(() => expect(checkbox).not.toBeChecked());
  expect(requests.filter(request => request.method === "PUT")).toEqual([{ path: "/api/my-profile/resumes/original", method: "PUT", body: { active: false } }]);
});

it("shows a recoverable error for missing personal information and leaves preferences accessible", async () => {
  const originalFetch = globalThis.fetch;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => String(input) === "/api/my-profile" ? Promise.resolve(json({}, 503)) : originalFetch(input, init)));
  mount("preferences");
  expect(await screen.findByText("Personal details could not be loaded")).toBeVisible();
  expect(await screen.findByLabelText("Desired job titles")).toHaveValue("Software Engineer");
  expect(screen.getByRole("button", { name: "Retry personal details" })).toBeVisible();
});

it("replaces one matching filename while retaining other résumés and inactive status", async () => {
  personal.resumes.push({id:"other",filename:"other.pdf",active:true,format:"pdf",application_supported:true,status:"Available for applications"});
  personal.resumes[0]!.active = false;
  mount("resume");
  await screen.findByRole("checkbox", {name:"original.pdf — Inactive"});
  fireEvent.change(document.querySelector<HTMLInputElement>('input[type="file"]')!, {target:{files:[new File(["updated"],"original.pdf",{type:"application/pdf"})]}});
  fireEvent.click(screen.getByRole("button",{name:"Upload résumé"}));
  await screen.findByText("Résumé saved in your list. Save job preferences to use the uploaded text for scoring. Your saved preferences were kept.");
  await waitFor(()=>expect(screen.getAllByRole("checkbox",{name:"original.pdf — Inactive"})).toHaveLength(1));
  expect(screen.getByRole("checkbox",{name:"other.pdf — Active"})).toBeChecked();
  expect(screen.queryByText(/Primary résumé|Application PDFs|Uploaded résumé/)).not.toBeInTheDocument();
});

it("does not attach the latest extracted text to library entries without per-file evidence", async () => {
  resume.original_name="original.pdf";
  personal.resumes.push({id:"other",filename:"other.pdf",active:false,format:"pdf",application_supported:true,status:"Inactive"});
  mount("resume");
  await screen.findByRole("checkbox",{name:"original.pdf — Active"});
  expect(screen.queryByRole("button",{name:"Preview extracted text: original.pdf"})).not.toBeInTheDocument();
  expect(screen.queryByRole("button",{name:"Preview extracted text: other.pdf"})).not.toBeInTheDocument();
});


it("keeps personal answers in one section and preserves saved legacy preference notes", async () => {
  preferences.profile.work_authorization = "Existing search note";
  preferences.profile.hiring_timeline = "Existing hiring note";
  mount("preferences");
  await screen.findByLabelText("Desired job titles");
  expect(screen.queryByLabelText("Job-search authorization notes")).toBeNull();
  expect(screen.queryByLabelText("Hiring window")).toBeNull();
  fireEvent.change(screen.getByLabelText("Target countries"), { target: { value: "United States" } });
  fireEvent.change(screen.getByLabelText("Target locations"), { target: { value: "" } });
  expect(screen.getByRole("button", { name: "Continue to review searches" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Save job preferences" }));
  await screen.findByText("Discovery searches need review");
  const saved = requests.find(request => request.method === "PUT" && request.path.endsWith("/preferences"));
  expect(saved?.body).toEqual({ ...PREFERENCES.profile, target_countries: ["United States"], target_locations: [""], work_authorization: "Existing search note", hiring_timeline: "Existing hiring note" });
  expect(requests.some(request => request.path.includes("/analyze"))).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "Continue to review searches" }));
  await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/setup/searches"));
});


it("sets up a first résumé and confirms preferences in one home without AI", async () => {
  personal.resumes = [];
  personal.application_resumes = [];
  preferences.scoring_resume_active = false;
  resume = { original_name: null, extracted_text: null, profile: null, is_confirmed: false };
  mount("preferences");
  await screen.findByLabelText("Desired job titles");
  expect(screen.getByRole("button", { name: "Continue to review searches" })).toBeDisabled();
  openSection(/^Résumé/);
  const file = document.querySelector<HTMLInputElement>('input[type="file"]')!;
  fireEvent.change(file, { target: { files: [new File(["Example résumé"], "first.pdf", { type: "application/pdf" })] } });
  fireEvent.click(screen.getByRole("button", { name: "Upload résumé" }));
  await screen.findByText(/first.pdf — Active/);
  openSection(/^Job preferences/);
  fireEvent.click(screen.getByRole("button", { name: "Save job preferences" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Continue to review searches" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Continue to review searches" }));
  await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/setup/searches"));
  expect(requests.filter(request => request.method !== "GET").map(request => request.path)).toEqual(["/api/onboarding/resume", "/api/my-profile/preferences"]);
});


it("refreshes pristine preferences after analysis while retaining unsaved edits during refetch", async () => {
  const { client } = mount("preferences");
  expect(await screen.findByLabelText("Desired job titles")).toHaveValue("Software Engineer");
  preferences = { ...preferences, profile: { ...preferences.profile, desired_titles: ["Analyzed role"] } };
  await act(async () => { await client.invalidateQueries({ queryKey: ["my-profile-preferences"] }); });
  await waitFor(() => expect(screen.getByLabelText("Desired job titles")).toHaveValue("Analyzed role"));
  fireEvent.change(screen.getByLabelText("Desired job titles"), { target: { value: "Unsaved personal choice" } });
  preferences = { ...preferences, scoring_resume_active: false, profile: { ...preferences.profile, desired_titles: ["Different server role"] } };
  await act(async () => { await client.invalidateQueries({ queryKey: ["my-profile-preferences"] }); });
  await screen.findByText("The uploaded résumé is not active for scoring yet. Upload a résumé if needed, then save preferences.");
  expect(screen.getByLabelText("Desired job titles")).toHaveValue("Unsaved personal choice");
  expect(screen.getByText("Unsaved changes")).toBeVisible();
});


it("requires saving an AI draft before continuing to search review", async () => {
  resume.is_confirmed = false;
  mount("preferences");
  await screen.findByLabelText("Desired job titles");
  expect(screen.getByRole("button", { name: "Continue to review searches" })).toBeDisabled();
  expect(screen.getByText("Save job preferences to confirm this draft before reviewing searches.")).toBeVisible();
  expect(requests.some(request => request.method !== "GET")).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "Save job preferences" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Continue to review searches" })).toBeEnabled());
  expect(requests.filter(request => request.method !== "GET").map(request => request.path)).toEqual(["/api/my-profile/preferences"]);
});

it("applies a pending server refresh when local edits are undone", async () => {
  const { client } = mount("preferences");
  await screen.findByLabelText("Desired job titles");
  fireEvent.change(screen.getByLabelText("Desired job titles"), { target: { value: "Unsaved local role" } });
  preferences = { ...preferences, scoring_resume_active: false, profile: { ...preferences.profile, desired_titles: ["Current server role"], excluded_companies: ["Current blocked company"] } };
  await act(async () => { await client.invalidateQueries({ queryKey: ["my-profile-preferences"] }); });
  await screen.findByText("The uploaded résumé is not active for scoring yet. Upload a résumé if needed, then save preferences.");
  expect(screen.getByLabelText("Desired job titles")).toHaveValue("Unsaved local role");
  fireEvent.change(screen.getByLabelText("Desired job titles"), { target: { value: "Software Engineer" } });
  expect(screen.getByLabelText("Desired job titles")).toHaveValue("Current server role");
  expect(screen.getByLabelText("Excluded companies")).toHaveValue("Current blocked company");
  fireEvent.click(screen.getByRole("button", { name: "Save job preferences" }));
  await waitFor(() => expect(requests.find(request => request.method === "PUT" && request.path.endsWith("/preferences"))?.body).toEqual(preferences.profile));
});


it("saves preferred states separately while keeping nationwide US eligibility", async () => {
  preferences.profile.target_countries = ["United States"];
  preferences.profile.target_locations = [];
  mount("preferences");
  await screen.findByLabelText("Preferred locations");
  fireEvent.change(screen.getByLabelText("Preferred locations"), { target: { value: "California\nNew York" } });
  expect(screen.getByLabelText("Target countries")).toHaveValue("United States");
  expect(screen.getByLabelText("Target locations")).toHaveValue("");
  fireEvent.click(screen.getByRole("button", { name: "Save job preferences" }));
  await screen.findByText("Discovery searches need review");
  expect(requests.find(request => request.method === "PUT" && request.path.endsWith("/preferences"))?.body).toEqual(expect.objectContaining({
    target_countries: ["United States"], target_locations: [], preferred_locations: ["California", "New York"],
  }));
});

function Back() { const navigate = useNavigate(); return <button onClick={() => navigate(-1)}>Go back</button>; }
function group(question: string) { return within(screen.getByRole("group", { name: question })); }
function answerRequests() { return requests.filter(request => request.path === "/api/answer-bank" && request.method === "POST").map(request => request.body); }

it("switches sections through the URL so links and Back work", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/profile"]}><MyProfilePage /><Location /><Back /></MemoryRouter></QueryClientProvider>);
  expect(await screen.findByRole("heading", { name: "Profile strength" })).toBeVisible();
  openSection(/^Personal info/);
  expect(screen.getByTestId("location")).toHaveTextContent("/profile?section=personal");
  expect(await screen.findByRole("heading", { name: "Personal info" })).toBeVisible();
  expect(screen.queryByRole("heading", { name: "Profile strength" })).toBeNull();
  expect(within(screen.getByRole("navigation", { name: "Profile sections" })).getByRole("link", { name: /^Personal info/ })).toHaveAttribute("aria-current", "page");
  fireEvent.click(screen.getByRole("button", { name: "Go back" }));
  expect(await screen.findByRole("heading", { name: "Profile strength" })).toBeVisible();
  expect(screen.getByTestId("location")).toHaveTextContent(/^\/profile$/);
});

it("scores a partly filled profile and links each missing item to its section", async () => {
  mount();
  expect(await screen.findByText("35%")).toBeVisible();
  const checklist = within(screen.getByRole("list", { name: "Profile checklist" }));
  expect(checklist.getByRole("link", { name: "Add your contact info" }).closest("li")).toHaveTextContent("+15%");
  expect(checklist.queryByRole("link", { name: "Add your education" })).toBeNull();
  expect(checklist.queryByRole("link", { name: "Add a résumé for applications" })).toBeNull();
  fireEvent.click(checklist.getByRole("link", { name: "Answer the demographic questions" }));
  expect(screen.getByTestId("location")).toHaveTextContent("/profile?section=personal");
  expect(await screen.findByRole("heading", { name: "Personal info" })).toBeVisible();
});

it("counts a fully answered profile as complete", async () => {
  personal.personal_details = { ...personal.personal_details, phone: "555-0100", linkedin_url: "https://example.test/in", work_authorization: "Authorized", sponsorship_requirement: "None" };
  for (const item of bank.common_questions!) item.entry_id = "gender-entry";
  mount();
  expect(await screen.findByText("100%")).toBeVisible();
  expect(within(screen.getByRole("list", { name: "Profile checklist" })).queryAllByRole("link")).toHaveLength(0);
});

it("saves a common question chosen from its options as an approved answer", async () => {
  mount("personal");
  const adult = await screen.findByRole("group", { name: "Are you at least 18 years old?" });
  expect(within(adult).getByRole("button", { name: "Save" })).toBeDisabled();
  fireEvent.mouseDown(within(adult).getByRole("button", { name: /Not answered/ }));
  fireEvent.mouseUp(await screen.findByRole("option", { name: "Yes" }));
  fireEvent.click(within(adult).getByRole("button", { name: "Save" }));
  expect(await within(adult).findByText("Saved")).toBeVisible();
  expect(answerRequests()).toEqual([{ action: "save", expected_revision: 3, entry_id: expect.any(String),
    draft: { question: "Are you at least 18 years old?", answer: "Yes", aliases: [], scope: { kind: "global" }, valid_until: null } }]);
  expect(group("What is your gender?").getByRole("button", { name: /Decline to self-identify/ })).toBeVisible();
});

it("saves a free-text common question and keeps matched answers out of the other answers", async () => {
  mount("answers");
  const salary = await screen.findByRole("group", { name: "What is your desired salary?" });
  fireEvent.change(within(salary).getByRole("textbox"), { target: { value: "Open to discussion" } });
  fireEvent.click(within(salary).getByRole("button", { name: "Save" }));
  await within(salary).findByText("Saved");
  expect(answerRequests()[0]).toMatchObject({ action: "save", draft: { question: "What is your desired salary?", answer: "Open to discussion", scope: { kind: "global" } } });
  expect(screen.queryByRole("group", { name: "Gender" })).toBeNull();
  expect(within(screen.getByRole("region", { name: "Application answers" })).queryByText(/version/i)).toBeNull();
});

it("saves an Applier draft with the user's edit, which approves it", async () => {
  mount("answers");
  expect(await screen.findByRole("heading", { name: /Waiting for you/ })).toBeVisible();
  const draft = group("Why Example Co?");
  expect(draft.getByRole("textbox")).toHaveValue("Drafted synthetic reason.");
  expect(draft.getByText("Example Co, job 7")).toBeVisible();
  fireEvent.change(draft.getByRole("textbox"), { target: { value: "My own synthetic reason." } });
  fireEvent.click(draft.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(screen.queryByRole("heading", { name: /Waiting for you/ })).toBeNull());
  expect(answerRequests()).toEqual([{ action: "save", expected_revision: 3, entry_id: "draft", expected_version: 1,
    draft: { question: "Why Example Co?", answer: "My own synthetic reason.", aliases: [], scope: { kind: "job", company: "Example Co", job_id: "7" }, valid_until: null } }]);
  expect(group("Why Example Co?").getByRole("textbox")).toHaveValue("My own synthetic reason.");
});

it("adds a new question for every company", async () => {
  mount("answers");
  fireEvent.click(await screen.findByRole("button", { name: "Add a question" }));
  const added = group("New question");
  fireEvent.change(added.getByLabelText("Question"), { target: { value: "What is your favorite tool?" } });
  fireEvent.change(added.getByLabelText("Answer"), { target: { value: "A synthetic editor." } });
  fireEvent.click(added.getByRole("button", { name: "Save" }));
  expect(await screen.findByRole("group", { name: "What is your favorite tool?" })).toBeVisible();
  expect(screen.queryByRole("group", { name: "New question" })).toBeNull();
  expect(answerRequests()).toEqual([{ action: "save", expected_revision: 3, entry_id: expect.any(String),
    draft: { question: "What is your favorite tool?", answer: "A synthetic editor.", aliases: [], scope: { kind: "global" }, valid_until: null } }]);
});

it("deletes a saved answer", async () => {
  mount("answers");
  const notice = await screen.findByRole("group", { name: "What is your notice period?" });
  fireEvent.click(within(notice).getByRole("button", { name: "Delete" }));
  await waitFor(() => expect(screen.queryByRole("group", { name: "What is your notice period?" })).toBeNull());
  expect(answerRequests()).toEqual([{ action: "revoke", expected_revision: 3, entry_id: "notice", expected_version: 1 }]);
});

it("sends the old answer bank page to application answers", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  client.setQueryData(configurationKey, { configured: false });
  render(<QueryClientProvider client={client}><DensityProvider><MemoryRouter initialEntries={["/answer-bank"]}><App /><Location /></MemoryRouter></DensityProvider></QueryClientProvider>);
  expect(await screen.findByRole("heading", { name: /Waiting for you/ })).toBeVisible();
  expect(screen.getByTestId("location")).toHaveTextContent("/profile?section=answers");
});

it("approves an Applier draft as written when saved without changes", async () => {
  mount("answers");
  const draft = await screen.findByRole("group", { name: "Why Example Co?" });
  fireEvent.click(within(draft).getByRole("button", { name: "Save" }));
  await waitFor(() => expect(answerRequests()).toHaveLength(1));
  expect(answerRequests()[0]).toMatchObject({ action: "save", entry_id: "draft", expected_version: 1, draft: { answer: "Drafted synthetic reason." } });
});
