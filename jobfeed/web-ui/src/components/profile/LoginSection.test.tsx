import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { LoginSection } from "@/components/profile/LoginSection";

let state = { password_set: true, accounts: 9 };
let puts: unknown[];
let refuse: string | null;

beforeEach(() => {
  state = { password_set: true, accounts: 9 }; puts = []; refuse = null;
  vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "PUT") {
      puts.push(JSON.parse(String(init.body)));
      if (refuse) return Response.json({ error: { code: "account_password_invalid", message: refuse, request_id: "r1" } }, { status: 422 });
      state = { ...state, password_set: true };
    }
    return Response.json(state);
  }));
});
afterEach(() => { vi.unstubAllGlobals(); });

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><LoginSection /></QueryClientProvider>);
}

test("it says a password is set without ever showing one, and warns what a change breaks", async () => {
  renderSection();
  expect(await screen.findByText("A password is set. 9 accounts use it.")).toBeVisible();
  expect(screen.getByLabelText("Account password")).toHaveAttribute("type", "password");
  expect(screen.getByLabelText("Account password")).toHaveValue("");
  expect(screen.getByText(/does not change the 9 accounts already made/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Save password" })).toBeDisabled();
});

test("saving sends the typed password once and clears the field", async () => {
  state = { password_set: false, accounts: 0 };
  renderSection();
  expect(await screen.findByText("No password is set.")).toBeVisible();
  fireEvent.change(screen.getByLabelText("Account password"), { target: { value: "Tr1cky-pass!" } });
  fireEvent.click(screen.getByRole("button", { name: "Save password" }));
  await waitFor(() => expect(puts).toEqual([{ password: "Tr1cky-pass!" }]));
  expect(await screen.findByText("Saved.")).toBeVisible();
  expect(screen.getByLabelText("Account password")).toHaveValue("");
  expect(screen.getByText("A password is set. 0 accounts use it.")).toBeVisible();
});

test("a refused password shows the rule it broke and keeps what was typed", async () => {
  refuse = "The password was not saved: it needs a digit.";
  renderSection();
  await screen.findByText("A password is set. 9 accounts use it.");
  fireEvent.change(screen.getByLabelText("Account password"), { target: { value: "NoDigits!!" } });
  fireEvent.click(screen.getByRole("button", { name: "Save password" }));
  expect(await screen.findByText("The password was not saved: it needs a digit.")).toBeVisible();
  expect(screen.getByLabelText("Account password")).toHaveValue("NoDigits!!");
});
