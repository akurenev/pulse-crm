import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { demoMailAccount, demoMailMessages } from "./demo";
import { MailCrmActions } from "./MailCrmActions";

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("../../lib/api", () => ({ remoteEnabled: true, api: mocks, ApiError: class extends Error {} }));
vi.mock("../../state/auth-store", () => ({ useAuth: () => ({ session: { user: { id: "employee-test" } } }) }));
vi.mock("../../state/crm-store", () => ({ useCrm: () => ({ pipeline: { id: "pipeline-test" }, pipelines: [{ id: "pipeline-test", name: "Тестовая воронка", stages: [{ id: "stage-test", stageType: "open" }] }], addDeal: vi.fn() }) }));

const message = demoMailMessages[0];
const onChanged = vi.fn().mockResolvedValue(undefined);

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter><MailCrmActions message={message} account={demoMailAccount} onChanged={onChanged} /></MemoryRouter></QueryClientProvider>);
}

afterEach(cleanup);

describe("explicit mail to CRM actions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.get.mockImplementation(async (path: string) => path === "/sources" ? [{ id: "source-email", key: "email" }] : { items: [], next_cursor: null });
  });

  it("creates an employee-owned contact only after confirmation", async () => {
    mocks.post.mockResolvedValue({ id: "contact-test" });
    mount();
    const createContact = await screen.findByRole("button", { name: "Создать контакт" });
    await waitFor(() => expect(createContact).toBeEnabled());
    expect(mocks.post).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Создать контакт" }));
    fireEvent.change(screen.getByLabelText("Имя и фамилия"), { target: { value: "Тестовый Клиент" } });
    fireEvent.click(screen.getByRole("button", { name: /^Создать$/ }));
    await waitFor(() => expect(mocks.post).toHaveBeenCalledWith("/contacts", expect.objectContaining({ first_name: "Тестовый", last_name: "Клиент", primary_email: message.from_address, assignee_id: "employee-test" })));
    expect(await screen.findByRole("link", { name: "Открыть" })).toHaveAttribute("href", "/contacts?contact=contact-test");
  });

  it("reuses a created deal when sharing its snapshot needs a retry", async () => {
    mocks.post.mockImplementation(async (path: string) => {
      if (path === "/deals") return { id: "deal-test" };
      throw new Error("test snapshot unavailable");
    });
    mount();
    fireEvent.click(screen.getByRole("button", { name: "Создать сделку" }));
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: /^Создать$/ }));
    await screen.findByRole("alert");
    expect(mocks.post).toHaveBeenCalledWith(`/mail/messages/${message.id}/link`, { deal_id: "deal-test" });
    mocks.post.mockResolvedValue({ deal_id: "deal-test", note_id: "note-test" });
    fireEvent.click(screen.getByRole("button", { name: "Прикрепить письмо" }));
    await screen.findByText(/Сделка создана, письмо прикреплено/);
    expect(mocks.post.mock.calls.filter(([path]) => path === "/deals")).toHaveLength(1);
  });

  it("keeps email body out of a task unless explicitly selected", async () => {
    mocks.post.mockResolvedValue({ id: "task-test" });
    mount();
    fireEvent.click(screen.getByRole("button", { name: "Создать задачу" }));
    fireEvent.click(screen.getByRole("button", { name: /^Создать$/ }));
    await waitFor(() => expect(mocks.post).toHaveBeenCalledWith("/tasks", expect.objectContaining({ assignee_id: "employee-test", description: null })));
    expect(mocks.post).not.toHaveBeenCalledWith(expect.stringContaining("/link"), expect.anything());
  });
});
