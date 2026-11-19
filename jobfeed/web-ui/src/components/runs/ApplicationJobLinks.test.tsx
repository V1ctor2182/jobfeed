import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ApplicationJobLinks } from "./ApplicationJobLinks";
import { applicationJobState } from "./run-model";
import type { ObservedJob } from "@/api/workspace";
const job: ObservedJob = {job_id:42,title:"Engineer",company:"Rilla",owner_thread_id:"current-owner",job_url:"https://example.test/rilla",counted:true,closed:true,paused:true,submitted:true,synced:true,terminal:false,user_submitted:true,queue_state:"needs_input"};
afterEach(()=>vi.unstubAllGlobals());
it("shows submission evidence and a read-only form lookup without starting actions", async()=>{
 const fetch=vi.fn<(input: string) => Promise<Response>>(async(input)=>Response.json(input === "/api/application-tabs" ? {csrf_token:"current",bindings:[]} : {status:"missing",reason:"No saved confirmation"}));vi.stubGlobal("fetch",fetch);
 render(<QueryClientProvider client={new QueryClient()}><ApplicationJobLinks batchId="old-observation" job={job}/></QueryClientProvider>);
 expect(screen.getByText("Submitted · Jobfeed sync verified")).toBeVisible();
 expect(screen.getByRole("link",{name:"Open applier chat"})).toHaveAttribute("href","codex://threads/current-owner");
 expect(screen.queryByRole("button",{name:/Connect Chrome|Rerun application|Submit application/})).not.toBeInTheDocument();
 expect(screen.getByRole("button",{name:"Open application form"})).toBeVisible();
 await waitFor(()=>expect(fetch).toHaveBeenCalledTimes(1));
 expect(String(fetch.mock.calls[0]?.[0])).toBe("/api/application-tabs");
 fireEvent.click(screen.getByRole("button",{name:"View saved confirmation"}));
 await waitFor(()=>expect(fetch).toHaveBeenCalledTimes(2));
 expect(String(fetch.mock.calls[1]?.[0])).toBe("/api/apply-health/jobs/42/confirmation");
});
it("a Claude Applier keeps the form jump and shows Discord instead of a Codex chat link", async()=>{
 const fetch=vi.fn<(input: string) => Promise<Response>>(async()=>Response.json({csrf_token:"current",bindings:[]}));vi.stubGlobal("fetch",fetch);
 const claude = {...job, owner_thread_id:"08a3d9d1307249849dc7abcca5be6293"};
 render(<QueryClientProvider client={new QueryClient()}><ApplicationJobLinks batchId="b" job={claude}/></QueryClientProvider>);
 expect(screen.queryByRole("link",{name:"Open applier chat"})).not.toBeInTheDocument();
 expect(screen.getByText("Claude Applier · questions arrive on Discord")).toBeVisible();
 await waitFor(()=>expect(screen.getByRole("button",{name:"Open application form"})).toBeEnabled());
});
it("verified submission takes precedence over obsolete pause and queue markers",()=>{
 expect(applicationJobState(job)).toBe("Submitted");
});
