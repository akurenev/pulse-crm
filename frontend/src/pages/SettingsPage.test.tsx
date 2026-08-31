import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../state/auth-store";
import { CrmProvider } from "../state/crm-store";
import type { Pipeline } from "../types/crm";
import SettingsPage, { ChannelEditor, ChannelsPanel, HtmlFormEditor, NotificationEditor, WebhookEditor } from "./SettingsPage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <AuthProvider>
          <CrmProvider>
            <SettingsPage />
          </CrmProvider>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderEditor(node: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}>{node}</QueryClientProvider>);
}

const routingPipelines: Pipeline[] = [
  {
    id: "pipeline-sales",
    name: "Продажи",
    stages: [
      { id: "sales-new", name: "Новая", color: "blue", stageType: "open" },
      { id: "sales-won", name: "Успешно", color: "green", stageType: "won" },
    ],
  },
  {
    id: "pipeline-renewals",
    name: "Продления",
    stages: [
      { id: "renewals-new", name: "На согласовании", color: "violet", stageType: "open" },
      { id: "renewals-lost", name: "Не продлено", color: "amber", stageType: "lost" },
    ],
  },
];

describe("SettingsPage custom fields", () => {
  it("removes a deal field from the active catalog after confirmation", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    renderPage();

    expect(screen.getByText("Сегмент клиента")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Удалить поле сделки Сегмент клиента" }));

    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("сохранённые значения сделок останутся в базе"));
    expect(screen.queryByText("Сегмент клиента")).not.toBeInTheDocument();
    expect(screen.getByText("Договор подписан")).toBeInTheDocument();
  });
});

describe("SettingsPage pipeline routing", () => {
  it("keeps source creation unavailable while remote routing is loading", async () => {
    const user = userEvent.setup();
    renderEditor(<ChannelsPanel pipelines={[]} currentPipelineId="pipeline-stale" routingLoading requireLoadedRouting />);

    expect(screen.getByRole("status")).toHaveTextContent("Загружаем доступные воронки и этапы");
    for (const name of ["Подключить канал", "Добавить канал", "Webhook", "HTML-форма"]) {
      expect(screen.getByRole("button", { name })).toBeDisabled();
    }

    await user.click(screen.getByRole("button", { name: "Добавить канал" }));
    expect(screen.queryByRole("region", { name: "Подключение корпоративного канала" })).not.toBeInTheDocument();
  });

  it("explains why source creation is unavailable when remote routing is empty", () => {
    renderEditor(<ChannelsPanel pipelines={[]} currentPipelineId="pipeline-stale" routingLoading={false} requireLoadedRouting />);

    expect(screen.getByRole("alert")).toHaveTextContent("Нет доступных воронок");
    expect(screen.getByRole("button", { name: "Подключить канал" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Добавить канал" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Webhook" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "HTML-форма" })).toBeDisabled();
  });

  it("selects any pipeline and resets the channel start stage to that pipeline", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    renderEditor(<ChannelEditor pipelines={routingPipelines} defaultPipelineId="pipeline-sales" onCancel={vi.fn()} onCreated={onCreated} />);

    const pipelineSelect = screen.getByRole("combobox", { name: "Воронка" });
    const stageSelect = screen.getByRole("combobox", { name: "Начальный этап" });
    expect(within(pipelineSelect).getAllByRole("option").map((option) => option.textContent)).toEqual(["Продажи", "Продления"]);
    expect(stageSelect).toHaveValue("sales-new");
    expect(within(stageSelect).queryByRole("option", { name: "Успешно" })).not.toBeInTheDocument();

    await user.selectOptions(pipelineSelect, "pipeline-renewals");
    expect(stageSelect).toHaveValue("renewals-new");
    expect(within(stageSelect).getAllByRole("option").map((option) => option.textContent)).toEqual(["На согласовании"]);

    await user.type(screen.getByRole("textbox", { name: "Название" }), "Почта продлений");
    await user.click(screen.getByRole("button", { name: "Подключить" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(onCreated.mock.calls[0][0]).toMatchObject({
      default_pipeline_id: "pipeline-renewals",
      default_stage_id: "renewals-new",
    });
  });

  it.each([
    {
      editor: "channel",
      build: (onCreated: ReturnType<typeof vi.fn>) => <ChannelEditor pipelines={[]} defaultPipelineId="demo-or-stale-pipeline" onCancel={vi.fn()} onCreated={onCreated} />,
    },
    {
      editor: "webhook",
      build: (onCreated: ReturnType<typeof vi.fn>) => <WebhookEditor pipelines={[]} defaultPipelineId="demo-or-stale-pipeline" onCancel={vi.fn()} onCreated={onCreated} />,
    },
    {
      editor: "HTML form",
      build: (onCreated: ReturnType<typeof vi.fn>) => <HtmlFormEditor pipelines={[]} defaultPipelineId="demo-or-stale-pipeline" onCancel={vi.fn()} onCreated={onCreated} />,
    },
  ])("does not submit demo or stale route ids from the $editor editor", async ({ build }) => {
    const onCreated = vi.fn();
    renderEditor(build(onCreated));

    const editor = screen.getByRole("region");
    const form = editor.querySelector("form");
    const submit = form?.querySelector<HTMLButtonElement>('button[type="submit"]');
    expect(form).not.toBeNull();
    expect(submit).toBeDisabled();

    fireEvent.submit(form!);

    await waitFor(() => expect(within(editor).getByRole("alert")).toHaveTextContent("Дождитесь загрузки воронок"));
    expect(onCreated).not.toHaveBeenCalled();
  });

  it("offers all pipelines and matching stages for a notification filter", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    renderEditor(<NotificationEditor pipelines={routingPipelines} currentUserId="user-test" onCancel={vi.fn()} onCreated={onCreated} />);

    const pipelineSelect = screen.getByRole("combobox", { name: "Воронка" });
    const stageSelect = screen.getByRole("combobox", { name: "Этап" });
    expect(within(pipelineSelect).getAllByRole("option").map((option) => option.textContent)).toEqual(["Все воронки", "Продажи", "Продления"]);
    expect(stageSelect).toBeDisabled();

    await user.selectOptions(pipelineSelect, "pipeline-renewals");
    expect(stageSelect).toBeEnabled();
    expect(within(stageSelect).getAllByRole("option").map((option) => option.textContent)).toEqual(["Все этапы", "На согласовании", "Не продлено"]);
    await user.selectOptions(stageSelect, "renewals-lost");
    await user.type(screen.getByRole("textbox", { name: "Название правила" }), "Неуспешное продление");
    await user.click(screen.getByRole("button", { name: "Создать правило" }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(onCreated.mock.calls[0][0]).toMatchObject({
      pipeline_id: "pipeline-renewals",
      stage_id: "renewals-lost",
    });
  });
});

describe("SettingsPage mobile actions", () => {
  it("opens accessible editors from floating add buttons and restores trigger focus", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("tab", { name: /Каналы/ }));
    const channelFab = screen.getByRole("button", { name: "Добавить канал" });
    expect(channelFab).toHaveClass("mobile-fab");
    expect(channelFab).toHaveAttribute("aria-controls", "channel-editor");
    await user.click(channelFab);
    const channelEditor = screen.getByRole("region", { name: "Подключение корпоративного канала" });
    expect(within(channelEditor).getByRole("heading", { level: 3, name: "Подключение корпоративного канала" })).toBeInTheDocument();
    await waitFor(() => expect(channelEditor).toHaveFocus());
    await user.click(within(channelEditor).getByRole("button", { name: "Закрыть форму" }));
    await waitFor(() => expect(channelFab).toHaveFocus());

    const desktopTrigger = screen.getByRole("button", { name: "Подключить канал" });
    await user.click(desktopTrigger);
    const reopenedEditor = screen.getByRole("region", { name: "Подключение корпоративного канала" });
    await waitFor(() => expect(reopenedEditor).toHaveFocus());
    await user.click(within(reopenedEditor).getByRole("button", { name: "Закрыть форму" }));
    await waitFor(() => expect(desktopTrigger).toHaveFocus());

    await user.click(screen.getByRole("tab", { name: /Оповещения/ }));
    const ruleFab = screen.getByRole("button", { name: "Добавить правило оповещения" });
    expect(ruleFab).toHaveClass("mobile-fab");
    expect(ruleFab).toHaveAttribute("aria-controls", "notification-rule-editor");
    await user.click(ruleFab);
    const ruleEditor = screen.getByRole("region", { name: "Новое правило и шаблон" });
    expect(within(ruleEditor).getByRole("heading", { level: 3, name: "Новое правило и шаблон" })).toBeInTheDocument();
    await waitFor(() => expect(ruleEditor).toHaveFocus());
    await user.click(within(ruleEditor).getByRole("button", { name: "Закрыть форму" }));
    await waitFor(() => expect(ruleFab).toHaveFocus());
  });
});
