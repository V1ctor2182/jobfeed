import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ApplicationSubmitButton } from "./ApplicationSubmitButton";

let eligible: boolean;
let fail: boolean;
let posts: RequestInit[];
beforeEach(() => {
  sessionStorage.clear(); eligible = true; fail = false; posts = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") { posts.push(init); if (fail) throw new Error("lost"); return Response.json({request_id: "id", state: "queued"}); }
    if (url.includes("/status?")) return Response.json({state:"unknown",request_id:"id"});
    return Response.json({ csrf_token: "csrf", eligible, reason: eligible ? null : "Already submitted", company: "Rilla", title: "Engineer", job_id: 42, owner: "owner", scope:"manifest", bank_revision: 4, warnings: ["Batch is stopped"], answers:[{entry_id:"a",version:2,question:"Why Rilla?",answer:"Exact reviewed prose.",state:"pending_review",scope:{kind:"job",job_id:"42"},valid_until:null}] });
  }));
});
afterEach(() => vi.unstubAllGlobals());
function mount() { return render(<QueryClientProvider client={new QueryClient({ defaultOptions:{queries:{retry:false}} })}><ApplicationSubmitButton batchId="b" jobId={42}/></QueryClientProvider>); }
async function open() { await waitFor(() => expect(screen.getByRole("button",{name:"Submit application"})).toBeEnabled()); fireEvent.click(screen.getByRole("button",{name:"Submit application"})); return screen.findByRole("dialog"); }
it("shows full exact answer and warnings, and cancel sends nothing",async()=>{mount();const dialog=await open(); expect(within(dialog).getByText("Exact reviewed prose.")).toBeVisible(); expect(within(dialog).getByText("Batch is stopped")).toBeVisible();fireEvent.click(within(dialog).getByRole("button",{name:"Cancel"})); expect(posts).toHaveLength(0);});
it("only explicit confirm sends pinned preview and same-origin token once",async()=>{mount();const dialog=await open();fireEvent.click(within(dialog).getByRole("button",{name:"Confirm and submit"}));await waitFor(()=>expect(posts).toHaveLength(1));const body=JSON.parse(String(posts[0]!.body));expect(body.confirmed).toBe(true);expect(body.expected_preview.answers[0].answer).toBe("Exact reviewed prose.");expect(body.expected_preview).not.toHaveProperty("csrf_token");expect(posts[0]!.headers).toMatchObject({"X-Application-Submissions-CSRF":"csrf"});expect(screen.getByRole("button",{name:"Submit application"})).toBeDisabled();});
it("keeps uncertain request identity across remount and never resends",async()=>{fail=true;const view=mount();fireEvent.click(within(await open()).getByRole("button",{name:"Confirm and submit"}));await waitFor(()=>expect(posts).toHaveLength(1));view.unmount();mount();expect(screen.getByRole("button",{name:"Submit application"})).toBeDisabled();fireEvent.click(screen.getByRole("button",{name:"Check submission status"}));expect(posts).toHaveLength(1);});
it("disables completed application",async()=>{eligible=false;mount();expect(await screen.findByText("Already submitted")).toBeVisible();expect(screen.getByRole("button",{name:"Submit application"})).toBeDisabled();});
it("allows a fresh review only after definitive rejection and durable no-record proof", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if(init?.method === "POST") { posts.push(init); return Response.json({error:{code:"application_submission_conflict",message:"Preview changed",request_id:"trace"}}, {status:409}); }
    if(url.includes("/status?")) return Response.json({request_id:"request",state:"not_found",accepted:false});
    return Response.json({csrf_token:"csrf",eligible:true,scope:"manifest",job_id:42,owner:"owner",company:"Rilla",title:"Engineer",bank_revision:1,warnings:[],answers:[]});
  }));
  mount(); fireEvent.click(within(await open()).getByRole("button",{name:"Confirm and submit"}));
  await waitFor(()=>expect(screen.getByRole("button",{name:"Submit application"})).toBeEnabled());
  expect(posts).toHaveLength(1); expect(sessionStorage.length).toBe(0);
});
it.each([false, true])("distinguishes website success from Jobfeed sync %s", async synced => {
  sessionStorage.setItem('["application-submission","b",42]', 'existing');
  vi.stubGlobal("fetch", vi.fn(async (url: string) => Response.json(url.includes("/status?") ? {request_id:"existing",state:synced ? "submitted" : "sync_pending",submitted:true,synced,reason:synced ? null : "Formal sync failed; reconcile."} : {eligible:false,reason:"Already submitted"})));
  mount();
  expect(await screen.findByText(synced ? "Website submission verified. Jobfeed sync verified." : "Website submission verified. Jobfeed sync pending. Formal sync failed; reconcile.")).toBeVisible();
  expect(screen.getByRole("button",{name:"Submit application"})).toBeDisabled();
  expect(screen.getByRole("button",{name:"Check submission status"})).toBeEnabled();
  expect(posts).toHaveLength(0);
});

it.each([
  ["preparing", "Current application is still being checked."],
  ["queued", "Submission request queued for your application agent."],
  ["running", "Your application agent is checking and submitting this application."],
  ["unknown", "Delivery is unconfirmed. Check this request; do not submit again."],
  ["turn_ended", "The application agent finished its turn. Submission still needs verification."],
])("explains %s without offering another submission", async (state, message) => {
  sessionStorage.setItem('["application-submission","b",42]', 'existing');
  vi.stubGlobal("fetch", vi.fn(async (url: string) => Response.json(url.includes("/status?")
    ? { request_id: "existing", state, reason: "Current application is still being checked.", accepted: true }
    : { eligible: true, warnings: [], answers: [] })));
  mount();
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(message));
  expect(screen.getByRole("status")).toHaveTextContent("Current application is still being checked.");
  expect(screen.getByRole("button", { name: "Submit application" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Check submission status" }));
  expect(sessionStorage.getItem('["application-submission","b",42]')).toBe('existing');
  expect(posts).toHaveLength(0);
});

it.each([null, "Confirmation saved. Waiting for an application slot."])("shows the preparing reason once with a fallback for %s", async reason => {
  sessionStorage.setItem('["application-submission","b",42]', 'existing');
  vi.stubGlobal("fetch", vi.fn(async (url: string) => Response.json(url.includes("/status?")
    ? { request_id: "existing", state: "preparing", reason, accepted: true }
    : { eligible: true, warnings: [], answers: [] })));
  mount();
  const expected = reason ?? "Confirmation saved. Waiting to send to your application agent.";
  await waitFor(() => expect(screen.getByRole("status").textContent).toBe(expected));
  expect(screen.getByRole("button", { name: "Submit application" })).toBeDisabled();
  expect(posts).toHaveLength(0);
});

it.each([undefined, false])("keeps a blocked request reserved without explicit no-delivery evidence %s", async deliveryNotStarted => {
  sessionStorage.setItem('["application-submission","b",42]', 'existing');
  vi.stubGlobal("fetch", vi.fn(async (url: string) => Response.json(url.includes("/status?")
    ? { request_id: "existing", state: "blocked", delivery_not_started: deliveryNotStarted, reason: "Tab is controlled by another chat." }
    : { eligible: true, warnings: [], answers: [] })));
  mount();
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Tab is controlled by another chat."));
  expect(screen.queryByRole("button", { name: "Review current application" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Submit application" })).toBeDisabled();
  expect(sessionStorage.getItem('["application-submission","b",42]')).toBe('existing');
});

it("requires a fresh review and explicit confirmation after proven unsent rejection", async () => {
  sessionStorage.setItem('["application-submission","b",42]', 'existing');
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") { posts.push(init); return Response.json({ request_id: JSON.parse(String(init.body)).request_id, state: "preparing" }); }
    if (url.includes("/status?")) return Response.json({ request_id: "existing", state: "blocked", delivery_not_started: true, accepted: true, reason: "The answer version changed." });
    return Response.json({ csrf_token: "csrf", eligible: true, scope: "manifest", job_id: 42, owner: "owner", company: "Rilla", title: "Engineer", bank_revision: 2, warnings: [], answers: [{ entry_id: "a", version: 3, question: "Why Rilla?", answer: "New answer for review.", state: "pending_review", scope: { kind: "job", job_id: "42" }, valid_until: null }] });
  }));
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Review current application" }));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText("New answer for review.")).toBeVisible();
  expect(posts).toHaveLength(0);
  expect(within(dialog).queryByText(/stopped workflow/)).not.toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", { name: "Confirm and submit" }));
  await waitFor(() => expect(posts).toHaveLength(1));
  const payload = JSON.parse(String(posts[0]!.body));
  expect(payload.request_id).not.toBe("existing");
  expect(payload.expected_preview.answers[0]).toMatchObject({ version: 3, answer: "New answer for review." });
});

it.each(["unknown", "unavailable", "ineligible"])("preserves the original request when fresh review becomes %s", async outcome => {
  sessionStorage.setItem('["application-submission","b",42]', 'existing');
  let statusReads = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url.includes("/status?")) {
      statusReads += 1;
      if (statusReads > 1 && outcome === "unavailable") throw new Error("Status unavailable");
      return Response.json({ request_id: "existing", state: statusReads > 1 && outcome === "unknown" ? "unknown" : "blocked", delivery_not_started: true });
    }
    return Response.json({ csrf_token: "csrf", eligible: outcome !== "ineligible", reason: outcome === "ineligible" ? "Current owner changed" : null, warnings: [], answers: [] });
  }));
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Review current application" }));
  await waitFor(() => expect(statusReads).toBe(2));
  await waitFor(() => expect(screen.getByRole("button", { name: "Submit application" })).toBeDisabled());
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(sessionStorage.getItem('["application-submission","b",42]')).toBe('existing');
  expect(posts).toHaveLength(0);
});
