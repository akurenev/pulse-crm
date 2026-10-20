import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { demoDraft, demoMailAccount, demoMailFolders, demoMailMessages } from "../features/mail/demo";
import type { MailMessage } from "../types/mail";
import MailPage from "./MailPage";

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn(), upload: vi.fn() }));
vi.mock("../lib/api", () => ({
  remoteEnabled: true, api: mocks,
  ApiError: class ApiError extends Error { constructor(message: string, public readonly status: number) { super(message); } },
}));
vi.mock("../features/mail/MailCrmActions", () => ({ MailCrmActions: () => null }));
vi.mock("../state/crm-store", () => ({ useCrm: () => ({ deals: [{ id: "deal-test", title: "Тестовая сделка" }], currentUser: { id: "employee-test", name: "Тестовый сотрудник" }, pipelines: [] }) }));

const storageSet = vi.fn();
let messages: MailMessage[];
let record: MailMessage;
const account = { ...demoMailAccount, has_credentials: true };

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<MemoryRouter><QueryClientProvider client={client}><MailPage /></QueryClientProvider></MemoryRouter>);
}

beforeEach(() => {
  storageSet.mockReset();
  vi.stubGlobal("localStorage", { setItem: storageSet });
  Object.values(mocks).forEach((mock) => mock.mockReset());
  record = { ...demoMailMessages[0], is_read: true, body_html: '<img src="https://example.com/tracker" onerror="alert(1)">', version: 3 };
  messages = [record];
  mocks.get.mockImplementation((path: string) => {
    if (path === "/mail/accounts") return Promise.resolve({ items: [account] });
    if (path.endsWith("/folders")) return Promise.resolve({ items: demoMailFolders });
    if (path.includes("/messages?")) return Promise.resolve({ items: messages, next_cursor: null });
    if (path.startsWith("/mail/messages/")) return Promise.resolve(record);
    if (path.startsWith("/deals?")) return Promise.resolve({ items: [{ id: "deal-test", title: "Тестовая сделка" }], next_cursor: null });
    return Promise.reject(new Error(`Unexpected GET ${path}`));
  });
  mocks.patch.mockImplementation((_path: string, body: Partial<MailMessage>) => { record = { ...record, ...body, version: record.version + 1 }; return Promise.resolve(record); });
  mocks.delete.mockResolvedValue(undefined);
  mocks.post.mockImplementation((path: string) => path.endsWith("/link") ? Promise.resolve({ deal_id: "deal-test", note_id: "note-test" }) : Promise.reject(new Error(`Unexpected POST ${path}`)));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("personal mailbox", () => {
  it("opens a related thread message through its full detail endpoint", async () => {
    const user = userEvent.setup();
    const related = { ...record, id: "related-message", subject: "Ответ по срокам", body_text: "Полный текст связанного письма, загруженный отдельным запросом.", received_at: "2026-10-20T09:00:00Z" };
    const defaultGet = mocks.get.getMockImplementation()!;
    mocks.get.mockImplementation((path: string, init?: RequestInit) => {
      if (path === "/mail/messages/related-message") return Promise.resolve(related);
      if (path.includes("/messages?") && new URL(path, "https://crm.example.com").searchParams.has("thread_key")) {
        return Promise.resolve({ items: [record, { ...related, body_text: "Сокращённый текст списка" }], next_cursor: null });
      }
      return defaultGet(path, init);
    });
    renderPage();
    await user.click(await screen.findByRole("button", { name: /Тестовый клиент/ }));
    const chain = await screen.findByRole("region", { name: "Письма цепочки" });
    await user.click(within(chain).getByRole("button", { name: /Ответ по срокам/ }));
    const reader = screen.getByRole("article", { name: "Просмотр письма" });
    expect(await within(reader).findByRole("heading", { level: 2, name: related.subject })).toBeVisible();
    expect(within(reader).getByText(related.body_text)).toBeVisible();
    expect(within(reader).queryByText("Сокращённый текст списка")).not.toBeInTheDocument();
    expect(mocks.get).toHaveBeenCalledWith("/mail/messages/related-message");
  });

  it("shows a received-message attachment warning as safe text", async () => {
    const user = userEvent.setup();
    const warning = "Некоторые вложения не загружены: файл превышает лимит.";
    record = { ...record, last_error: warning };
    messages = [record];
    renderPage();
    await user.click(await screen.findByRole("button", { name: /Тестовый клиент/ }));
    const reader = screen.getByRole("article", { name: "Просмотр письма" });
    expect(await within(reader).findByRole("status")).toHaveTextContent(warning);
    expect(within(reader).getByText(/возможную дату поставки/)).toBeVisible();
  });

  it("renders safe text without loading message HTML and applies versioned flags", async () => {
    const user = userEvent.setup();
    const rendered = renderPage();
    await user.click(await screen.findByRole("button", { name: /Тестовый клиент/ }));
    expect(await within(screen.getByRole("article", { name: "Просмотр письма" })).findByText(/возможную дату поставки/)).toBeVisible();
    expect(rendered.container.querySelector('img[src*="tracker"]')).toBeNull();
    await user.click(screen.getByRole("button", { name: "Убрать звезду" }));
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledWith(`/mail/messages/${record.id}`, { expected_version: 3, is_starred: false }));
    await user.click(screen.getByRole("button", { name: "Отметить непрочитанным" }));
    await waitFor(() => expect(mocks.patch).toHaveBeenLastCalledWith(`/mail/messages/${record.id}`, { expected_version: 4, is_read: false }));
  });

  it("requires a chosen accessible deal and explicit sharing consent", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: /Тестовый клиент/ }));
    await user.click(await screen.findByRole("button", { name: "Прикрепить к сделке" }));
    const dialog = screen.getByRole("dialog", { name: "Прикрепить письмо к сделке" });
    const attach = within(dialog).getByRole("button", { name: "Прикрепить письмо" });
    expect(attach).toBeDisabled();
    await within(dialog).findByRole("option", { name: "Тестовая сделка" });
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Сделка" }), "deal-test");
    expect(attach).toBeDisabled();
    await user.click(within(dialog).getByRole("checkbox"));
    await user.click(attach);
    await waitFor(() => expect(mocks.post).toHaveBeenCalledWith(`/mail/messages/${record.id}/link`, { deal_id: "deal-test" }));
  });

  it("retains the saved draft and pending files when upload fails; retry sends only after upload", async () => {
    const { ApiError } = await import("../lib/api");
    const user = userEvent.setup();
    const steps: string[] = [];
    mocks.post.mockImplementation((path: string, body: unknown) => {
      if (path.endsWith("/drafts")) { steps.push("draft"); record = demoDraft(account, body as Parameters<typeof demoDraft>[1], "draft-test"); return Promise.resolve(record); }
      if (path.endsWith("/send")) { steps.push("send"); record = { ...record, status: "queued", version: record.version + 1 }; return Promise.resolve(record); }
      return Promise.reject(new Error(`Unexpected POST ${path}`));
    });
    mocks.upload.mockRejectedValueOnce(new ApiError("unsupported", 415)).mockImplementationOnce(() => { steps.push("upload"); record = { ...record, attachments: [{ id: "file-test", message_id: "draft-test", original_filename: "test.txt", size_bytes: 4, content_type: "text/plain", sha256: "test" }] }; return Promise.resolve(record.attachments[0]); });
    renderPage();
    await screen.findByRole("combobox", { name: "Мой ящик" });
    await user.click(screen.getByRole("button", { name: "Написать письмо" }));
    const dialog = screen.getByRole("dialog", { name: "Новое письмо" });
    await user.type(within(dialog).getByRole("textbox", { name: "Кому" }), "client@example.com");
    await user.type(within(dialog).getByRole("textbox", { name: "Тема" }), "Тест отправки");
    await user.type(within(dialog).getByRole("textbox", { name: "Текст письма" }), "Текст не теряется");
    await user.upload(within(dialog).getByLabelText("Прикрепить файлы"), new File(["test"], "test.txt", { type: "text/plain" }));
    await user.click(within(dialog).getByRole("button", { name: "Отправить" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("тип вложения не поддерживается");
    expect(within(dialog).getByRole("textbox", { name: "Текст письма" })).toHaveValue("Текст не теряется");
    expect(steps).toEqual(["draft"]);
    await user.click(within(dialog).getByRole("button", { name: "Отправить" }));
    await waitFor(() => expect(steps).toEqual(["draft", "upload", "send"]));
    expect(mocks.post.mock.calls.filter(([path]) => (path as string).endsWith("/drafts"))).toHaveLength(1);
    expect(mocks.patch).toHaveBeenCalledWith("/mail/messages/draft-test/draft", expect.objectContaining({ expected_version: 1, body_text: "Текст не теряется" }));
    expect(await screen.findByRole("status")).toHaveTextContent("очередь отправки");
  });

  it("preserves typed text after a draft conflict and retries with the refreshed version", async () => {
    const { ApiError } = await import("../lib/api");
    const user = userEvent.setup();
    record = { ...record, status: "draft", direction: "outbound" };
    messages = [record];
    mocks.patch.mockImplementationOnce(() => { record = { ...record, version: 7, body_text: "Другая версия" }; return Promise.reject(new ApiError("conflict", 409)); }).mockImplementationOnce((_path: string, body: Partial<MailMessage>) => Promise.resolve({ ...record, ...body, version: 8 }));
    renderPage();
    await user.click(await screen.findByRole("button", { name: /Кому: employee@example.com/ }));
    await user.click(await screen.findByRole("button", { name: "Редактировать черновик" }));
    const dialog = screen.getByRole("dialog");
    const body = within(dialog).getByRole("textbox", { name: "Текст письма" });
    await user.clear(body); await user.type(body, "Мой новый текст");
    await user.click(within(dialog).getByRole("button", { name: "Сохранить черновик" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Текст редактора сохранён");
    expect(body).toHaveValue("Мой новый текст");
    await user.click(within(dialog).getByRole("button", { name: "Сохранить черновик" }));
    await waitFor(() => expect(mocks.patch).toHaveBeenLastCalledWith(`/mail/messages/${record.id}/draft`, expect.objectContaining({ expected_version: 7, body_text: "Мой новый текст" })));
  });

  it("reconnects a disabled account with a new password and active status", async () => {
    const user = userEvent.setup();
    mocks.get.mockImplementation((path: string) => {
      if (path === "/mail/accounts") return Promise.resolve({ items: [{ ...account, status: "disabled", has_credentials: false, version: 9 }] });
      if (path.endsWith("/folders")) return Promise.resolve({ items: demoMailFolders });
      return Promise.resolve({ items: [], next_cursor: null });
    });
    mocks.patch.mockResolvedValue({ ...account, version: 10 });
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Подключить повторно" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("Пароль для повторного подключения"), "test-only-password");
    await user.click(within(dialog).getByRole("button", { name: "Подключить повторно" }));
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledWith(`/mail/accounts/${account.id}`, expect.objectContaining({ expected_version: 9, password: "test-only-password", status: "active" })));
    expect(storageSet).not.toHaveBeenCalled();
  });
});
