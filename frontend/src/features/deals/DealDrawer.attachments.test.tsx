import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";

import { initialDeals, pipeline, users } from "../../data/demo";
import type { ApiActivity, CursorPage } from "../../types/api";
import { DealDrawer } from "./DealDrawer";

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  upload: vi.fn(),
}));

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    remoteEnabled: true,
    api: {
      ...actual.api,
      get: apiMocks.get,
      post: apiMocks.post,
      upload: apiMocks.upload,
    },
  };
});

const noteActivity: ApiActivity = {
  id: "note-event-1",
  event_type: "deal.note.created",
  entity_type: "deal",
  entity_id: initialDeals[0].id,
  actor_id: "user-ak",
  payload: { body: "Клиент прислал договор" },
  occurred_at: "2026-08-31T08:00:00+05:00",
  attachments: [{
    id: "note-file-1",
    activity_event_id: "note-event-1",
    position: 0,
    original_filename: "brief.pdf",
    content_type: "application/pdf",
    size_bytes: 2048,
    sha256: "a".repeat(64),
    created_at: "2026-08-31T08:00:00+05:00",
  }],
};

const dealCreatedActivity: ApiActivity = {
  id: "deal-event-1",
  event_type: "deal.created",
  entity_type: "deal",
  entity_id: initialDeals[0].id,
  actor_id: "user-ak",
  payload: {},
  occurred_at: "2026-08-30T08:00:00+05:00",
  attachments: [],
};

function renderDrawer() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const props: ComponentProps<typeof DealDrawer> = {
    deal: initialDeals[0],
    pipeline,
    assignees: Object.values(users),
    mutationPending: false,
    onClose: vi.fn(),
    onMove: vi.fn().mockResolvedValue(undefined),
    onSetNextPurchase: vi.fn().mockResolvedValue(undefined),
    onSetContact: vi.fn().mockResolvedValue(undefined),
    onSetCompany: vi.fn().mockResolvedValue(undefined),
    onSetAssignee: vi.fn().mockResolvedValue(undefined),
    onSetTags: vi.fn().mockResolvedValue(undefined),
    onSetCustomFields: vi.fn().mockResolvedValue(undefined),
    onSendMessage: vi.fn().mockResolvedValue(undefined),
    onRetryMessage: vi.fn().mockResolvedValue(undefined),
    onToggleTask: vi.fn().mockResolvedValue(undefined),
    onDelete: vi.fn().mockResolvedValue(undefined),
  };
  return render(<QueryClientProvider client={queryClient}><DealDrawer {...props} /></QueryClientProvider>);
}

beforeEach(() => {
  apiMocks.get.mockImplementation(async (path: string) => {
    if (path.includes("/activity?")) {
      return { items: [noteActivity, dealCreatedActivity], next_cursor: null } satisfies CursorPage<ApiActivity>;
    }
    if (path === "/note-attachments/note-file-1/download") {
      return { url: "https://files.example.test/brief.pdf", expires_in: 300 };
    }
    throw new Error(`Unexpected GET ${path}`);
  });
  apiMocks.post.mockResolvedValue(noteActivity);
  apiMocks.upload.mockResolvedValue(noteActivity);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  apiMocks.get.mockReset();
  apiMocks.post.mockReset();
  apiMocks.upload.mockReset();
});

describe("DealDrawer note attachments", () => {
  it("keeps notes in Details and leaves the event history separate", async () => {
    const user = userEvent.setup();
    renderDrawer();
    const dialog = await screen.findByRole("dialog", { name: initialDeals[0].title });

    expect(await within(dialog).findByText("Клиент прислал договор")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: /brief\.pdf/ })).toBeInTheDocument();
    expect(within(dialog).getByText(/не отправляются клиенту в переписке/i)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("tab", { name: "История" }));
    expect(within(dialog).queryByText("Клиент прислал договор")).not.toBeInTheDocument();
    expect(within(dialog).getByText("Сделка создана")).toBeInTheDocument();
  });

  it("uploads all selected note files through the dedicated endpoint", async () => {
    const user = userEvent.setup();
    renderDrawer();
    const dialog = await screen.findByRole("dialog", { name: initialDeals[0].title });
    const input = within(dialog).getByLabelText("Добавить файлы к заметке");
    const first = new File(["one"], "summary.txt", { type: "text/plain" });
    const second = new File(["two"], "photo.jpg", { type: "image/jpeg" });

    await user.upload(input, [first, second]);
    await user.type(within(dialog).getByRole("textbox", { name: "Заметка о сделке" }), "Итоги встречи");
    await user.click(within(dialog).getByRole("button", { name: "Добавить заметку" }));

    await waitFor(() => expect(apiMocks.upload).toHaveBeenCalledTimes(1));
    const [path, form] = apiMocks.upload.mock.calls[0] as [string, FormData];
    expect(path).toBe(`/deals/${initialDeals[0].id}/notes/with-attachments`);
    expect(form.get("body")).toBe("Итоги встречи");
    expect(form.getAll("files")).toEqual([first, second]);
    expect(within(dialog).queryByText("summary.txt")).not.toBeInTheDocument();
  });

  it("rejects more than five files before upload", async () => {
    const user = userEvent.setup();
    renderDrawer();
    const dialog = await screen.findByRole("dialog", { name: initialDeals[0].title });
    const files = Array.from({ length: 6 }, (_, index) => new File(
      [`file-${index}`],
      `file-${index}.txt`,
      { type: "text/plain" },
    ));

    await user.upload(within(dialog).getByLabelText("Добавить файлы к заметке"), files);

    expect(within(dialog).getByRole("alert")).toHaveTextContent("не больше 5 файлов");
    expect(within(dialog).getByRole("button", { name: "Добавить заметку" })).toBeDisabled();
    expect(apiMocks.upload).not.toHaveBeenCalled();
  });

  it("removes a selected file before the note is uploaded", async () => {
    const user = userEvent.setup();
    renderDrawer();
    const dialog = await screen.findByRole("dialog", { name: initialDeals[0].title });
    const input = within(dialog).getByLabelText("Добавить файлы к заметке");
    const keep = new File(["one"], "keep.txt", { type: "text/plain" });
    const remove = new File(["two"], "remove.txt", { type: "text/plain" });

    await user.upload(input, [keep, remove]);
    await user.click(within(dialog).getByRole("button", { name: "Убрать файл remove.txt" }));
    expect(within(dialog).queryByText("remove.txt")).not.toBeInTheDocument();

    await user.type(within(dialog).getByRole("textbox", { name: "Заметка о сделке" }), "Оставить один файл");
    await user.click(within(dialog).getByRole("button", { name: "Добавить заметку" }));

    await waitFor(() => expect(apiMocks.upload).toHaveBeenCalledTimes(1));
    const form = apiMocks.upload.mock.calls[0]?.[1] as FormData;
    expect(form.getAll("files")).toEqual([keep]);
  });

  it("rejects a file larger than 20 MB before upload", async () => {
    const user = userEvent.setup();
    renderDrawer();
    const dialog = await screen.findByRole("dialog", { name: initialDeals[0].title });
    const oversized = new File(["oversized"], "archive.pdf", { type: "application/pdf" });
    Object.defineProperty(oversized, "size", { value: 20 * 1024 * 1024 + 1 });

    await user.upload(within(dialog).getByLabelText("Добавить файлы к заметке"), oversized);

    expect(within(dialog).getByRole("alert")).toHaveTextContent("превышает лимит 20 МБ");
    expect(within(dialog).getByRole("button", { name: "Добавить заметку" })).toBeDisabled();
    expect(apiMocks.upload).not.toHaveBeenCalled();
  });

  it("shows an activity loading error and retries instead of displaying an empty state", async () => {
    const user = userEvent.setup();
    apiMocks.get.mockRejectedValue(new Error("network unavailable"));
    renderDrawer();
    const dialog = await screen.findByRole("dialog", { name: initialDeals[0].title });

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Не удалось загрузить заметки");
    expect(within(dialog).queryByText("Договорённости и приложенные документы появятся здесь.")).not.toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Повторить" }));
    await waitFor(() => expect(apiMocks.get.mock.calls.filter(([path]) => String(path).includes("/activity?")).length).toBeGreaterThan(1));
  });

  it("requests a short-lived link when an attachment is opened", async () => {
    const user = userEvent.setup();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    renderDrawer();
    const dialog = await screen.findByRole("dialog", { name: initialDeals[0].title });

    await user.click(await within(dialog).findByRole("button", { name: /brief\.pdf/ }));

    await waitFor(() => expect(apiMocks.get).toHaveBeenCalledWith("/note-attachments/note-file-1/download"));
    expect(click).toHaveBeenCalledTimes(1);
  });
});
