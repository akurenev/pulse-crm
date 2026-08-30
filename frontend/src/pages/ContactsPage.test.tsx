import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiCompany, ApiContact, CursorPage } from "../types/api";
import ContactsPage from "./ContactsPage";

const { deleteMock, getMock, patchMock, postMock } = vi.hoisted(() => ({
  deleteMock: vi.fn(),
  getMock: vi.fn(),
  patchMock: vi.fn(),
  postMock: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  api: { delete: deleteMock, get: getMock, patch: patchMock, post: postMock },
  remoteEnabled: true,
}));

const contact = (id: string, firstName: string): ApiContact => ({
  id,
  first_name: firstName,
  last_name: "Контакт",
  company_id: null,
  primary_email: `${id}@example.com`,
  primary_phone: "+7 000 000-00-00",
  emails: [],
  phones: [],
  tags: [],
  custom_fields: {},
  version: 1,
  created_at: "2026-08-29T10:00:00Z",
  updated_at: "2026-08-29T10:00:00Z",
});

const company = (id: string, name: string): ApiCompany => ({
  id,
  name,
  website: null,
  phone: null,
  email: `${id}@example.com`,
  tags: [],
  custom_fields: {},
  version: 1,
  created_at: "2026-08-29T10:00:00Z",
  updated_at: "2026-08-29T10:00:00Z",
});

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ContactsPage />
    </QueryClientProvider>,
  );
}

function requestedPaths() {
  return getMock.mock.calls.map(([path]) => path as string);
}

beforeEach(() => {
  deleteMock.mockReset();
  getMock.mockReset();
  patchMock.mockReset();
  postMock.mockReset();
  getMock.mockImplementation((path: string) => {
    const [pathname, query = ""] = path.split("?");
    const params = new URLSearchParams(query);
    const search = params.get("search");

    if (pathname === "/contacts") {
      const page: CursorPage<ApiContact> = params.get("cursor") === "contacts-page-2"
        ? { items: [contact("contact-2", "Второй")], next_cursor: null }
        : { items: [contact("contact-1", search ? "Первый найденный" : "Первый")], next_cursor: search ? null : "contacts-page-2" };
      return Promise.resolve(page);
    }
    if (pathname === "/companies") {
      const page: CursorPage<ApiCompany> = params.get("cursor") === "companies-page-2"
        ? { items: [company("company-2", "Вторая компания")], next_cursor: null }
        : { items: [company("company-1", "Первая компания")], next_cursor: "companies-page-2" };
      return Promise.resolve(page);
    }
    if (pathname === "/activity" || (pathname.startsWith("/contacts/") && pathname.endsWith("/purchases"))) {
      return Promise.resolve({ items: [], next_cursor: null });
    }
    return Promise.reject(new Error(`Unexpected GET ${path}`));
  });
});

afterEach(cleanup);

describe("ContactsPage pagination", () => {
  it("moves through independent contact and company cursor pages and resets on search", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("Первый Контакт")).toBeInTheDocument();
    expect(requestedPaths()).toContain("/contacts?limit=25");
    expect(requestedPaths().some((path) => path.startsWith("/companies?"))).toBe(false);

    await user.click(screen.getByRole("button", { name: "Следующая страница" }));
    expect(await screen.findByText("Второй Контакт")).toBeInTheDocument();
    expect(screen.getByText("Страница 2")).toBeInTheDocument();
    expect(requestedPaths()).toContain("/contacts?limit=25&cursor=contacts-page-2");

    await user.click(screen.getByRole("button", { name: "Предыдущая страница" }));
    expect(await screen.findByText("Первый Контакт")).toBeInTheDocument();
    expect(screen.getByText("Страница 1")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Следующая страница" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Следующая страница" }));
    expect(await screen.findByText("Второй Контакт")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Компании" }));
    expect(await screen.findByText("Первая компания")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Следующая страница" }));
    expect(await screen.findByText("Вторая компания")).toBeInTheDocument();
    expect(requestedPaths()).toContain("/companies?limit=25&cursor=companies-page-2");

    await user.click(screen.getByRole("button", { name: "Контакты" }));
    expect(await screen.findByText("Второй Контакт")).toBeInTheDocument();
    expect(screen.getByText("Страница 2")).toBeInTheDocument();

    await user.type(screen.getByPlaceholderText("Имя, компания, тег, телефон или email"), "Первый");
    expect(screen.queryByText("Страница 2")).not.toBeInTheDocument();
    expect(await screen.findByText("Первый найденный Контакт")).toBeInTheDocument();
    await waitFor(() => {
      expect(getMock.mock.calls.some(([path]) => path === "/contacts?limit=25&search=%D0%BF%D0%B5%D1%80%D0%B2%D1%8B%D0%B9")).toBe(true);
    });
    expect(requestedPaths().filter((path) => path.includes("/contacts?") && path.includes("search=")).length).toBe(1);
  });

  it("shows no invented assignee for remote contacts and retries a failed request", async () => {
    const user = userEvent.setup();
    let contactAttempts = 0;
    getMock.mockImplementation((path: string) => {
      if (path.startsWith("/contacts?")) {
        contactAttempts += 1;
        if (contactAttempts === 1) return Promise.reject(new TypeError("Network request failed"));
        return Promise.resolve({ items: [contact("contact-retry", "После повтора")], next_cursor: null });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage();

    expect(await screen.findByRole("alert")).toHaveTextContent("Проверьте соединение");
    expect(screen.queryByText("Алексей Кузнецов")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Повторить" }));

    expect(await screen.findByText("После повтора Контакт")).toBeInTheDocument();
    expect(screen.getByText("Не назначен")).toBeInTheDocument();
    expect(contactAttempts).toBe(2);
  });

  it("opens the shared creation dialog from the mobile floating action", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "Добавить контакт" }));

    const dialog = await screen.findByRole("dialog", { name: "Новый контакт" });
    expect(dialog).toHaveAttribute("id", "new-client-dialog");
  });
});

describe("ContactsPage contact management", () => {
  it("edits a contact with its current version and refreshes the list cache", async () => {
    const user = userEvent.setup();
    let storedContact = contact("contact-edit", "Анна");
    getMock.mockImplementation((path: string) => {
      const pathname = path.split("?")[0];
      if (pathname === "/contacts") return Promise.resolve({ items: [storedContact], next_cursor: null });
      if (pathname === "/activity" || pathname.endsWith("/purchases")) return Promise.resolve({ items: [], next_cursor: null });
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });
    patchMock.mockImplementation((_path: string, payload: Record<string, unknown>) => {
      storedContact = {
        ...storedContact,
        first_name: String(payload.first_name),
        last_name: String(payload.last_name),
        primary_email: String(payload.primary_email),
        primary_phone: String(payload.primary_phone),
        emails: payload.emails as string[],
        phones: payload.phones as string[],
        tags: payload.tags as string[],
        version: 2,
      };
      return Promise.resolve(storedContact);
    });
    renderPage();

    await user.click(await screen.findByText("Анна Контакт"));
    const detail = await screen.findByRole("dialog", { name: "Анна Контакт" });
    await user.click(within(detail).getByRole("button", { name: "Редактировать контакт" }));
    const firstName = within(detail).getByLabelText("Имя");
    const lastName = within(detail).getByLabelText("Фамилия");
    const email = within(detail).getByLabelText("Email");
    const phone = within(detail).getByLabelText("Телефон");
    const tags = within(detail).getByLabelText("Теги");
    await user.clear(firstName);
    await user.type(firstName, "Мария");
    await user.clear(lastName);
    await user.type(lastName, "Новая");
    await user.clear(email);
    await user.type(email, "maria@example.com");
    await user.clear(phone);
    await user.type(phone, "+7 999 123-45-67");
    await user.type(tags, " VIP, Партнёр, vip ");
    await user.click(within(detail).getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(patchMock).toHaveBeenCalledWith("/contacts/contact-edit", {
      expected_version: 1,
      first_name: "Мария",
      last_name: "Новая",
      primary_email: "maria@example.com",
      primary_phone: "+7 999 123-45-67",
      emails: ["maria@example.com"],
      phones: ["+7 999 123-45-67"],
      tags: ["VIP", "Партнёр"],
    }));
    expect(await within(detail).findByRole("heading", { name: "Мария Новая" })).toBeInTheDocument();
    expect(screen.getByText("Контакт обновлён")).toBeInTheDocument();
    await user.click(within(detail).getByRole("button", { name: "Закрыть" }));
    expect(await screen.findByText("Мария Новая")).toBeInTheDocument();
  });

  it("requires confirmation before deleting and removes the contact from cached pages", async () => {
    const user = userEvent.setup();
    let deleted = false;
    const storedContact = contact("contact-delete", "Удаляемый");
    getMock.mockImplementation((path: string) => {
      const pathname = path.split("?")[0];
      if (pathname === "/contacts") return Promise.resolve({ items: deleted ? [] : [storedContact], next_cursor: null });
      if (pathname === "/activity" || pathname.endsWith("/purchases")) return Promise.resolve({ items: [], next_cursor: null });
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });
    deleteMock.mockImplementation(() => {
      deleted = true;
      return Promise.resolve(undefined);
    });
    renderPage();

    await user.click(await screen.findByText("Удаляемый Контакт"));
    const detail = await screen.findByRole("dialog", { name: "Удаляемый Контакт" });
    await user.click(within(detail).getByRole("button", { name: "Удалить контакт" }));
    const confirmation = await screen.findByRole("dialog", { name: "Удалить контакт?" });
    expect(confirmation).toHaveTextContent("Это действие нельзя отменить");
    expect(deleteMock).not.toHaveBeenCalled();

    await user.click(within(confirmation).getByRole("button", { name: "Удалить контакт" }));

    await waitFor(() => expect(deleteMock).toHaveBeenCalledWith("/contacts/contact-delete?expected_version=1"));
    await waitFor(() => expect(screen.queryByText("Удаляемый Контакт")).not.toBeInTheDocument());
    expect(screen.getByText("Контакт удалён")).toBeInTheDocument();
  });

  it("keeps the confirmation open and shows an error when deletion fails", async () => {
    const user = userEvent.setup();
    deleteMock.mockRejectedValue(new Error("Conflict"));
    renderPage();

    await user.click(await screen.findByText("Первый Контакт"));
    const detail = await screen.findByRole("dialog", { name: "Первый Контакт" });
    await user.click(within(detail).getByRole("button", { name: "Удалить контакт" }));
    const confirmation = await screen.findByRole("dialog", { name: "Удалить контакт?" });
    await user.click(within(confirmation).getByRole("button", { name: "Удалить контакт" }));

    expect(await within(confirmation).findByRole("alert")).toHaveTextContent("Не удалось удалить контакт");
    await user.click(within(confirmation).getByRole("button", { name: "Отмена" }));
    expect(await screen.findByRole("dialog", { name: "Первый Контакт" })).toBeInTheDocument();
  });
});
