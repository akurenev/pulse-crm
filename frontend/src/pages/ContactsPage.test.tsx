import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import type { ApiCompany, ApiContact, ApiDeal, ApiUser, CursorPage } from "../types/api";
import ContactsPage from "./ContactsPage";

const { deleteMock, getMock, patchMock, postMock, permissionState } = vi.hoisted(() => ({
  deleteMock: vi.fn(),
  getMock: vi.fn(),
  patchMock: vi.fn(),
  postMock: vi.fn(),
  permissionState: { isEmployee: false },
}));

vi.mock("../lib/api", () => ({
  ApiError: class ApiError extends Error {
    constructor(message: string, public readonly status: number, public readonly details?: unknown) {
      super(message);
    }
  },
  api: { delete: deleteMock, get: getMock, patch: patchMock, post: postMock },
  remoteEnabled: true,
}));

const currentUser = {
  id: "user-current",
  name: "Текущий Сотрудник",
  initials: "ТС",
  tone: "violet" as const,
};

const responsibleUser: ApiUser = {
  id: "user-responsible",
  email: "responsible@example.com",
  full_name: "Мария Ответственная",
  role: "employee",
  version: 1,
};

vi.mock("../state/crm-store", () => ({
  useCrm: () => ({ currentUser, isEmployee: permissionState.isEmployee }),
}));

const contact = (id: string, firstName: string, assigneeId: string | null = null): ApiContact => ({
  id,
  first_name: firstName,
  last_name: "Контакт",
  company_id: null,
  assignee_id: assigneeId,
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

const deal = (id: string, title: string): ApiDeal => ({
  id,
  title,
  pipeline_id: "pipeline-1",
  stage_id: "stage-1",
  company_id: null,
  company: null,
  contact_ids: [],
  primary_contact: null,
  assignee_id: null,
  source_id: null,
  amount: "12500",
  currency: "RUB",
  tags: [],
  custom_fields: {},
  next_purchase_at: null,
  last_activity_at: "2026-08-29T10:00:00Z",
  version: 1,
  created_at: "2026-08-29T10:00:00Z",
  updated_at: "2026-08-29T10:00:00Z",
});

function renderPage(initialEntry = "/contacts") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const rendered = render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <QueryClientProvider client={queryClient}>
        <ContactsPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { ...rendered, queryClient };
}

function requestedPaths() {
  return getMock.mock.calls.map(([path]) => path as string);
}

beforeEach(() => {
  permissionState.isEmployee = false;
  deleteMock.mockReset();
  getMock.mockReset();
  patchMock.mockReset();
  postMock.mockReset();
  getMock.mockImplementation((path: string) => {
    if (path === "/users") return Promise.resolve([responsibleUser]);
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
    if (
      pathname === "/activity"
      || (pathname.startsWith("/contacts/") && (pathname.endsWith("/purchases") || pathname.endsWith("/deals")))
      || (pathname.startsWith("/companies/") && pathname.endsWith("/contacts"))
    ) {
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
      if (path === "/users") return Promise.resolve([responsibleUser]);
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
    expect(within(dialog).getByRole("combobox", { name: "Ответственный" })).toHaveValue(currentUser.id);
  });

  it("maps the contact assignee returned by the API through workspace users", async () => {
    const assignedContact = contact("contact-assigned", "Назначенный", responsibleUser.id);
    getMock.mockImplementation((path: string) => {
      if (path === "/users") return Promise.resolve([responsibleUser]);
      if (path.startsWith("/contacts?")) return Promise.resolve({ items: [assignedContact], next_cursor: null });
      return Promise.resolve({ items: [], next_cursor: null });
    });
    renderPage();

    expect(await screen.findByText("Мария Ответственная")).toBeInTheDocument();
  });
});

describe("ContactsPage linked records", () => {
  it("opens a contact company and its deals, then navigates back from the company contact list", async () => {
    const user = userEvent.setup();
    const linkedCompany = company("company-linked", "Связанная компания");
    const linkedContact = { ...contact("contact-linked", "Связанный"), company_id: linkedCompany.id };
    const linkedDeal = deal("deal-linked", "Связанная сделка");
    getMock.mockImplementation((path: string) => {
      if (path === "/users") return Promise.resolve([responsibleUser]);
      const pathname = path.split("?")[0];
      if (pathname === "/contacts") return Promise.resolve({ items: [linkedContact], next_cursor: null });
      if (pathname === `/companies/${linkedCompany.id}`) return Promise.resolve(linkedCompany);
      if (pathname === `/contacts/${linkedContact.id}/deals`) return Promise.resolve({ items: [linkedDeal], next_cursor: null });
      if (pathname === `/contacts/${linkedContact.id}/purchases`) return Promise.resolve({ items: [linkedDeal], next_cursor: null });
      if (pathname === `/companies/${linkedCompany.id}/contacts`) return Promise.resolve({ items: [linkedContact], next_cursor: null });
      if (pathname === "/activity") return Promise.resolve({ items: [], next_cursor: null });
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });
    renderPage();

    await user.click(await screen.findByText("Связанный Контакт"));
    const contactDetail = await screen.findByRole("dialog", { name: "Связанный Контакт" });
    const dealLink = await within(contactDetail).findByRole("link", { name: "Открыть сделку Связанная сделка" });
    expect(dealLink).toHaveAttribute("href", "/deals?deal=deal-linked");
    await user.click(await within(contactDetail).findByRole("button", { name: "Связанная компания" }));

    const companyDetail = await screen.findByRole("dialog", { name: "Связанная компания" });
    expect(within(companyDetail).queryByText("У компании пока нет связанных контактов.")).not.toBeInTheDocument();
    await user.click(await within(companyDetail).findByRole("button", { name: "Открыть контакт Связанный Контакт" }));
    expect(await screen.findByRole("dialog", { name: "Связанный Контакт" })).toBeInTheDocument();
  });

  it("opens contact and company deep links even when records are outside the current page", async () => {
    const deepContact = contact("contact-deep", "Глубокий");
    getMock.mockImplementation((path: string) => {
      if (path === "/users") return Promise.resolve([responsibleUser]);
      if (path === "/contacts?limit=25") return Promise.resolve({ items: [], next_cursor: null });
      if (path === `/contacts/${deepContact.id}`) return Promise.resolve(deepContact);
      const pathname = path.split("?")[0];
      if (pathname === "/activity" || pathname.endsWith("/purchases") || pathname.endsWith("/deals")) {
        return Promise.resolve({ items: [], next_cursor: null });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage(`/contacts?contact=${deepContact.id}`);

    expect(await screen.findByRole("dialog", { name: "Глубокий Контакт" })).toBeInTheDocument();
    expect(getMock).toHaveBeenCalledWith(`/contacts/${deepContact.id}`, expect.objectContaining({ signal: expect.any(AbortSignal) }));
  });

  it("opens a company deep link outside the current company page", async () => {
    const deepCompany = company("company-deep", "Глубокая компания");
    getMock.mockImplementation((path: string) => {
      if (path === "/users") return Promise.resolve([responsibleUser]);
      if (path === "/contacts?limit=25") return Promise.resolve({ items: [], next_cursor: null });
      if (path === `/companies/${deepCompany.id}`) return Promise.resolve(deepCompany);
      const pathname = path.split("?")[0];
      if (pathname === "/activity" || pathname.endsWith("/contacts")) {
        return Promise.resolve({ items: [], next_cursor: null });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage(`/contacts?company=${deepCompany.id}`);

    expect(await screen.findByRole("dialog", { name: "Глубокая компания" })).toBeInTheDocument();
    expect(getMock).toHaveBeenCalledWith(`/companies/${deepCompany.id}`, expect.objectContaining({ signal: expect.any(AbortSignal) }));
  });

  it("keeps server-matched company search results visible for normalized phone queries", async () => {
    const user = userEvent.setup();
    const matchedCompany = { ...company("company-phone", "Телефонная компания"), phone: "+7 (000) 123-45-67" };
    getMock.mockImplementation((path: string) => {
      if (path === "/users") return Promise.resolve([responsibleUser]);
      if (path.startsWith("/contacts?")) return Promise.resolve({ items: [], next_cursor: null });
      if (path === "/companies?limit=25") return Promise.resolve({ items: [], next_cursor: null });
      if (path.includes("/companies?limit=25&search=70001234567")) return Promise.resolve({ items: [matchedCompany], next_cursor: null });
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });
    renderPage();

    await user.click(screen.getByRole("button", { name: "Компании" }));
    await user.type(screen.getByPlaceholderText("Имя, компания, тег, телефон или email"), "70001234567");

    expect(await screen.findByText("Телефонная компания")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Телефонная компания")).toBeVisible());
  });
});

describe("ContactsPage contact management", () => {
  it("edits a contact with its current version and refreshes the list cache", async () => {
    const user = userEvent.setup();
    let storedContact = contact("contact-edit", "Анна");
    getMock.mockImplementation((path: string) => {
      if (path === "/users") return Promise.resolve([responsibleUser]);
      const pathname = path.split("?")[0];
      if (pathname === "/contacts") return Promise.resolve({ items: [storedContact], next_cursor: null });
      if (pathname === "/activity" || pathname.endsWith("/purchases") || pathname.endsWith("/deals")) return Promise.resolve({ items: [], next_cursor: null });
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
    const assigneeSelect = within(detail).getByRole("combobox", { name: "Ответственный" });
    await user.clear(firstName);
    await user.type(firstName, "Мария");
    await user.clear(lastName);
    await user.type(lastName, "Новая");
    await user.clear(email);
    await user.type(email, "maria@example.com");
    await user.clear(phone);
    await user.type(phone, "+7 999 123-45-67");
    await user.type(tags, " VIP, Партнёр, vip ");
    await user.selectOptions(assigneeSelect, responsibleUser.id);
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
      assignee_id: responsibleUser.id,
    }));
    expect(await within(detail).findByRole("heading", { name: "Мария Новая" })).toBeInTheDocument();
    expect(screen.getByText("Контакт обновлён")).toBeInTheDocument();
    await user.click(within(detail).getByRole("button", { name: "Закрыть" }));
    expect(await screen.findByText("Мария Новая")).toBeInTheDocument();
  });

  it("edits a company with optimistic versioning and refreshes the visible record", async () => {
    const user = userEvent.setup();
    let storedCompany: ApiCompany = { ...company("company-edit", "Старая компания"), phone: "+7 000 000-00-00" };
    getMock.mockImplementation((path: string) => {
      if (path === "/users") return Promise.resolve([responsibleUser]);
      const pathname = path.split("?")[0];
      if (pathname === "/contacts") return Promise.resolve({ items: [], next_cursor: null });
      if (pathname === "/companies") return Promise.resolve({ items: [storedCompany], next_cursor: null });
      if (pathname === "/activity" || pathname.endsWith("/contacts")) return Promise.resolve({ items: [], next_cursor: null });
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });
    patchMock.mockImplementation((_path: string, payload: Record<string, unknown>) => {
      storedCompany = {
        ...storedCompany,
        name: String(payload.name),
        email: payload.email as string | null,
        phone: payload.phone as string | null,
        website: payload.website as string | null,
        tags: payload.tags as string[],
        version: 2,
      };
      return Promise.resolve(storedCompany);
    });
    renderPage();

    await user.click(screen.getByRole("button", { name: "Компании" }));
    await user.click(await screen.findByText("Старая компания"));
    const detail = await screen.findByRole("dialog", { name: "Старая компания" });
    await user.click(within(detail).getByRole("button", { name: "Редактировать компанию" }));
    await user.clear(within(detail).getByLabelText("Название"));
    await user.type(within(detail).getByLabelText("Название"), "Новая компания");
    await user.clear(within(detail).getByLabelText("Email"));
    await user.type(within(detail).getByLabelText("Email"), "new-company@example.com");
    await user.clear(within(detail).getByLabelText("Телефон"));
    await user.type(within(detail).getByLabelText("Телефон"), "+7 000 111-22-33");
    await user.type(within(detail).getByLabelText("Сайт"), "https://company.example.com");
    await user.type(within(detail).getByLabelText("Теги"), " Партнёр, VIP, партнёр ");
    await user.click(within(detail).getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(patchMock).toHaveBeenCalledWith("/companies/company-edit", {
      expected_version: 1,
      name: "Новая компания",
      email: "new-company@example.com",
      phone: "+7 000 111-22-33",
      website: "https://company.example.com",
      tags: ["Партнёр", "VIP"],
    }));
    expect(await within(detail).findByRole("heading", { name: "Новая компания" })).toBeInTheDocument();
    expect(screen.getByText("Компания обновлена")).toBeInTheDocument();
    await user.click(within(detail).getByRole("button", { name: "Закрыть" }));
    expect(await within(screen.getByRole("region", { name: "Список компаний" })).findByText("Новая компания")).toBeInTheDocument();
  });

  it("restricts employee contacts to the current assignee and removes company and delete actions", async () => {
    permissionState.isEmployee = true;
    postMock.mockResolvedValue(contact("contact-created", "Новый", currentUser.id));
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByText("Первый Контакт"));
    const detail = await screen.findByRole("dialog", { name: "Первый Контакт" });
    expect(within(detail).queryByRole("button", { name: "Удалить контакт" })).not.toBeInTheDocument();
    expect(within(detail).queryByText("Компания", { exact: true })).not.toBeInTheDocument();
    await user.click(within(detail).getByRole("button", { name: "Закрыть" }));

    expect(screen.queryByRole("button", { name: "Компании" })).not.toBeInTheDocument();
    expect(requestedPaths().some((path) => path.startsWith("/companies?"))).toBe(false);
    await user.click(screen.getByRole("button", { name: "Добавить контакт" }));
    const createDialog = await screen.findByRole("dialog", { name: "Новый контакт" });
    expect(within(createDialog).queryByRole("combobox", { name: "Ответственный" })).not.toBeInTheDocument();
    expect(within(createDialog).getByLabelText("Ответственный")).toHaveTextContent(currentUser.name);
    await user.type(within(createDialog).getByLabelText("Имя"), "Новый");
    await user.click(within(createDialog).getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(postMock).toHaveBeenCalledOnce());
    expect(postMock.mock.calls[0][1]).not.toHaveProperty("assignee_id");
  });

  it("fails closed for an employee contact while the refreshed list is pending", async () => {
    permissionState.isEmployee = true;
    let contactRequests = 0;
    const pendingRefresh = new Promise<CursorPage<ApiContact>>(() => undefined);
    getMock.mockImplementation((path: string) => {
      if (path === "/users") return Promise.resolve([responsibleUser]);
      if (path.startsWith("/contacts?")) {
        contactRequests += 1;
        return contactRequests === 1
          ? Promise.resolve({ items: [contact("contact-1", "Первый", currentUser.id)], next_cursor: null })
          : pendingRefresh;
      }
      if (path.startsWith("/activity?") || path.endsWith("/purchases?limit=100") || path.endsWith("/deals?limit=100")) {
        return Promise.resolve({ items: [], next_cursor: null });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });
    const user = userEvent.setup();
    const { queryClient } = renderPage();

    await user.click(await screen.findByText("Первый Контакт"));
    expect(await screen.findByRole("dialog", { name: "Первый Контакт" })).toBeInTheDocument();
    await waitFor(() => expect(queryClient.getQueryData(["activity", "contact", "contact-1"])).toBeDefined());
    await waitFor(() => expect(queryClient.getQueryData(["contact-purchases", "contact-1"])).toBeDefined());
    await waitFor(() => expect(queryClient.getQueryData(["contact-deals", "contact-1"])).toBeDefined());

    act(() => window.dispatchEvent(new Event("pulse:access-changed")));

    expect(screen.queryByRole("dialog", { name: "Первый Контакт" })).not.toBeInTheDocument();
    expect(queryClient.getQueryData(["activity", "contact", "contact-1"])).toBeUndefined();
    expect(queryClient.getQueryData(["contact-purchases", "contact-1"])).toBeUndefined();
    expect(queryClient.getQueryData(["contact-deals", "contact-1"])).toBeUndefined();
    expect(screen.queryByText("Первый Контакт")).not.toBeInTheDocument();
    await waitFor(() => expect(contactRequests).toBe(2));
  });

  it("requires confirmation before deleting and removes the contact from cached pages", async () => {
    const user = userEvent.setup();
    let deleted = false;
    const storedContact = contact("contact-delete", "Удаляемый");
    getMock.mockImplementation((path: string) => {
      if (path === "/users") return Promise.resolve([responsibleUser]);
      const pathname = path.split("?")[0];
      if (pathname === "/contacts") return Promise.resolve({ items: deleted ? [] : [storedContact], next_cursor: null });
      if (pathname === "/activity" || pathname.endsWith("/purchases") || pathname.endsWith("/deals")) return Promise.resolve({ items: [], next_cursor: null });
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });
    deleteMock.mockImplementation(() => {
      deleted = true;
      return Promise.resolve(undefined);
    });
    renderPage();

    await user.click(await screen.findByText("Удаляемый Контакт"));
    const detail = await screen.findByRole("dialog", { name: "Удаляемый Контакт" });
    const deleteButton = within(detail).getByRole("button", { name: "Удалить контакт" });
    expect(deleteButton).toHaveClass("icon-button", "record-delete-button");
    expect(deleteButton).not.toHaveTextContent("Удалить");
    await user.click(deleteButton);
    const confirmation = await screen.findByRole("dialog", { name: "Удалить контакт?" });
    expect(confirmation).toHaveTextContent("Это действие нельзя отменить");
    expect(deleteMock).not.toHaveBeenCalled();

    await user.click(within(confirmation).getByRole("button", { name: "Удалить контакт" }));

    await waitFor(() => expect(deleteMock).toHaveBeenCalledWith("/contacts/contact-delete?expected_version=1"));
    await waitFor(() => expect(screen.queryByText("Удаляемый Контакт")).not.toBeInTheDocument());
    expect(screen.getByText("Контакт удалён")).toBeInTheDocument();
  });

  it("deletes a company with the same icon and confirmation pattern", async () => {
    const user = userEvent.setup();
    let deleted = false;
    const storedCompany = company("company-delete", "Удаляемая компания");
    getMock.mockImplementation((path: string) => {
      if (path === "/users") return Promise.resolve([responsibleUser]);
      const pathname = path.split("?")[0];
      if (pathname === "/contacts") return Promise.resolve({ items: [], next_cursor: null });
      if (pathname === "/companies") return Promise.resolve({ items: deleted ? [] : [storedCompany], next_cursor: null });
      if (pathname === "/activity" || pathname.endsWith("/contacts")) return Promise.resolve({ items: [], next_cursor: null });
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });
    deleteMock.mockImplementation(() => {
      deleted = true;
      return Promise.resolve(undefined);
    });
    renderPage();

    await user.click(screen.getByRole("button", { name: "Компании" }));
    await user.click(await screen.findByText("Удаляемая компания"));
    const detail = await screen.findByRole("dialog", { name: "Удаляемая компания" });
    const deleteButton = within(detail).getByRole("button", { name: "Удалить компанию" });
    expect(deleteButton).toHaveClass("icon-button", "record-delete-button");
    expect(deleteButton).not.toHaveTextContent("Удалить");
    await user.click(deleteButton);

    const confirmation = await screen.findByRole("dialog", { name: "Удалить компанию?" });
    expect(confirmation).toHaveTextContent("без возможности восстановления");
    await user.click(within(confirmation).getByRole("button", { name: "Удалить компанию" }));

    await waitFor(() => expect(deleteMock).toHaveBeenCalledWith("/companies/company-delete?expected_version=1"));
    await waitFor(() => expect(screen.queryByText("Удаляемая компания")).not.toBeInTheDocument());
    expect(screen.getByText("Компания удалена")).toBeInTheDocument();
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
