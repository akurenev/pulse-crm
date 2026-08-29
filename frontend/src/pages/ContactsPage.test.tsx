import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiCompany, ApiContact, CursorPage } from "../types/api";
import ContactsPage from "./ContactsPage";

const { getMock, postMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  api: { get: getMock, post: postMock },
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

beforeEach(() => {
  getMock.mockReset();
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
    return Promise.reject(new Error(`Unexpected GET ${path}`));
  });
});

afterEach(cleanup);

describe("ContactsPage pagination", () => {
  it("moves through independent contact and company cursor pages and resets on search", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("Первый Контакт")).toBeInTheDocument();
    expect(getMock).toHaveBeenCalledWith("/contacts?limit=25");

    await user.click(screen.getByRole("button", { name: "Следующая страница" }));
    expect(await screen.findByText("Второй Контакт")).toBeInTheDocument();
    expect(screen.getByText("Страница 2")).toBeInTheDocument();
    expect(getMock).toHaveBeenCalledWith("/contacts?limit=25&cursor=contacts-page-2");

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
    expect(getMock).toHaveBeenCalledWith("/companies?limit=25&cursor=companies-page-2");

    await user.click(screen.getByRole("button", { name: "Контакты" }));
    expect(await screen.findByText("Второй Контакт")).toBeInTheDocument();
    expect(screen.getByText("Страница 2")).toBeInTheDocument();

    await user.type(screen.getByPlaceholderText("Имя, компания, тег, телефон или email"), "Первый");
    expect(screen.queryByText("Страница 2")).not.toBeInTheDocument();
    expect(await screen.findByText("Первый найденный Контакт")).toBeInTheDocument();
    await waitFor(() => {
      expect(getMock.mock.calls.some(([path]) => path === "/contacts?limit=25&search=%D0%BF%D0%B5%D1%80%D0%B2%D1%8B%D0%B9")).toBe(true);
    });
  });

  it("opens the shared creation dialog from the mobile floating action", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "Добавить контакт" }));

    const dialog = await screen.findByRole("dialog", { name: "Новый контакт" });
    expect(dialog).toHaveAttribute("id", "new-client-dialog");
  });
});
