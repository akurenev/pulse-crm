import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../state/auth-store";
import { CrmProvider } from "../state/crm-store";
import type { ApiChannelConnection, ApiNotificationRule, ApiNotificationTemplate, ApiUser } from "../types/api";
import type { Pipeline } from "../types/crm";
import SettingsPage, { ChannelEditor, ChannelsPanel, HtmlFormEditor, NotificationEditor, UserEditor, WebhookEditor } from "./SettingsPage";

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

const workspaceUsers: ApiUser[] = [
  { id: "user-test", email: "admin@example.com", full_name: "Администратор", role: "admin", version: 1 },
  { id: "employee-b", email: "employee@example.com", full_name: "Сотрудник Б", role: "employee", version: 1 },
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

describe("SettingsPage user roles", () => {
  it("offers the employee role with its user-facing label", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("tab", { name: /Пользователи/ }));
    const role = screen.getByRole("combobox", { name: "Роль" });
    expect(within(role).getByRole("option", { name: "Сотрудник" })).toHaveValue("employee");
    expect(role).toHaveValue("employee");
  });

  it("edits a user name and role without making the login email writable", async () => {
    const user = userEvent.setup();
    const onSaved = vi.fn();
    renderEditor(<UserEditor user={workspaceUsers[1]} actor={{ ...workspaceUsers[0], role: "owner" }} onCancel={vi.fn()} onSaved={onSaved} />);

    const email = screen.getByRole("textbox", { name: "Email для входа" });
    expect(email).toHaveValue("employee@example.com");
    expect(email).toHaveAttribute("readonly");
    const name = screen.getByRole("textbox", { name: "Имя" });
    await user.clear(name);
    await user.type(name, "Новый сотрудник");
    await user.selectOptions(screen.getByRole("combobox", { name: "Роль" }), "manager");
    await user.click(screen.getByRole("button", { name: "Сохранить пользователя" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(onSaved.mock.calls[0][0]).toMatchObject({
      id: "employee-b",
      full_name: "Новый сотрудник",
      role: "manager",
      version: 2,
    });
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

  it("edits a channel without exposing or clearing its saved credentials", async () => {
    const user = userEvent.setup();
    const onSaved = vi.fn();
    const channel = { ...demoChannelForTest(), default_pipeline_id: "pipeline-sales", default_stage_id: "sales-new" };
    renderEditor(<ChannelEditor channel={channel} pipelines={routingPipelines} defaultPipelineId="pipeline-renewals" availableUsers={workspaceUsers} onCancel={vi.fn()} onCreated={onSaved} />);

    const credentials = screen.getByRole("textbox", { name: "Новые реквизиты (необязательно)" });
    expect(credentials).toHaveValue("");
    expect(credentials).toHaveAttribute("placeholder", "Оставьте пустым, чтобы сохранить текущие реквизиты");
    const name = screen.getByRole("textbox", { name: "Название" });
    await user.clear(name);
    await user.type(name, "Основная почта");
    await user.click(screen.getByRole("button", { name: "Сохранить канал" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(onSaved.mock.calls[0][0]).toMatchObject({
      id: channel.id,
      name: "Основная почта",
      has_credentials: true,
      version: 2,
    });
  });

  it.each([
    {
      editor: "channel",
      build: (onCreated: ReturnType<typeof vi.fn>) => <ChannelEditor pipelines={routingPipelines} defaultPipelineId="pipeline-sales" availableUsers={workspaceUsers} onCancel={vi.fn()} onCreated={onCreated} />,
      fields: [["Название", "Входящая почта"]],
      submitLabel: "Подключить",
      assigneeKey: "default_assignee_id",
    },
    {
      editor: "webhook",
      build: (onCreated: ReturnType<typeof vi.fn>) => <WebhookEditor pipelines={routingPipelines} defaultPipelineId="pipeline-sales" availableUsers={workspaceUsers} onCancel={vi.fn()} onCreated={onCreated} />,
      fields: [["Название", "Заявки партнёров"]],
      submitLabel: "Создать endpoint",
      assigneeKey: "assignee_id",
    },
    {
      editor: "HTML form",
      build: (onCreated: ReturnType<typeof vi.fn>) => <HtmlFormEditor pipelines={routingPipelines} defaultPipelineId="pipeline-sales" availableUsers={workspaceUsers} onCancel={vi.fn()} onCreated={onCreated} />,
      fields: [["Название формы", "Заявка"], ["Slug", "lead-request"]],
      submitLabel: "Создать форму",
      assigneeKey: "assignee_id",
    },
  ])("submits and resets the assignee for the $editor editor", async ({ build, fields, submitLabel, assigneeKey }) => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    renderEditor(build(onCreated));

    for (const [label, value] of fields) await user.type(screen.getByRole("textbox", { name: label }), value);
    const assignee = screen.getByRole("combobox", { name: "Ответственный" });
    expect(within(assignee).getAllByRole("option").map((option) => option.textContent)).toEqual(["Не назначен", "Администратор", "Сотрудник Б"]);
    await user.selectOptions(assignee, "employee-b");
    const submit = screen.getByRole("button", { name: submitLabel });
    await user.click(submit);

    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(onCreated.mock.calls[0][0]).toMatchObject({ [assigneeKey]: "employee-b" });

    await waitFor(() => expect(submit).toBeEnabled());
    await user.selectOptions(assignee, "");
    await user.click(submit);
    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(2));
    expect(onCreated.mock.calls[1][0]).toMatchObject({ [assigneeKey]: null });
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

  it("limits employee notifications to the in-app channel and binds the recipient id", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    renderEditor(<NotificationEditor pipelines={routingPipelines} currentUserId="user-test" availableUsers={workspaceUsers} onCancel={vi.fn()} onCreated={onCreated} />);

    const audience = screen.getByRole("combobox", { name: "Получатель" });
    const channel = screen.getByRole("combobox", { name: "Канал" });
    expect(within(channel).getAllByRole("option").map((option) => option.textContent)).toEqual(["В приложении"]);

    await user.selectOptions(audience, "client");
    expect(channel).toHaveValue("email");
    expect(within(channel).getAllByRole("option").map((option) => option.textContent)).toEqual(["В приложении", "Email", "Telegram", "MAX"]);
    await user.selectOptions(channel, "telegram");
    await user.selectOptions(audience, "employee");

    expect(channel).toHaveValue("in_app");
    expect(within(channel).getAllByRole("option").map((option) => option.textContent)).toEqual(["В приложении"]);
    const employeeRecipient = screen.getByRole("combobox", { name: "Сотрудник" });
    expect(within(employeeRecipient).getAllByRole("option").map((option) => option.textContent)).toEqual(["Администратор", "Сотрудник Б"]);
    await user.selectOptions(employeeRecipient, "employee-b");
    await user.type(screen.getByRole("textbox", { name: "Название правила" }), "Назначение сотруднику");
    await user.click(screen.getByRole("button", { name: "Создать правило" }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(onCreated.mock.calls[0][0]).toMatchObject({
      audience: "employee",
      channel: "in_app",
      recipients: [{ address: "employee-b", recipient_id: "employee-b" }],
    });
  });

  it("updates the existing template when editing notification text", async () => {
    const user = userEvent.setup();
    const onSaved = vi.fn();
    const rule: ApiNotificationRule = {
      id: "rule-edit",
      template_id: "template-edit",
      name: "Старая формулировка",
      event_type: "lead.created",
      audience: "employee",
      channel: "in_app",
      pipeline_id: null,
      stage_id: null,
      source_id: null,
      filters: {},
      recipients: [{ address: "user-test", recipient_id: "user-test" }],
      delay_seconds: 0,
      require_client_consent: true,
      is_enabled: true,
      version: 3,
      created_at: "2026-08-01T10:00:00Z",
      updated_at: "2026-08-01T10:00:00Z",
    };
    const template: ApiNotificationTemplate = {
      id: "template-edit",
      name: "Старая формулировка — шаблон",
      channel: "in_app",
      subject_template: null,
      body_template: "Старый текст",
      is_active: true,
      version: 4,
      created_at: "2026-08-01T10:00:00Z",
      updated_at: "2026-08-01T10:00:00Z",
    };
    renderEditor(<NotificationEditor rule={rule} template={template} pipelines={routingPipelines} currentUserId="user-test" availableUsers={workspaceUsers} onCancel={vi.fn()} onCreated={onSaved} />);

    const body = screen.getByRole("textbox", { name: "Текст шаблона" });
    await user.clear(body);
    await user.type(body, "Новый понятный текст");
    await user.click(screen.getByRole("button", { name: "Сохранить правило" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(onSaved.mock.calls[0][0]).toMatchObject({ id: "rule-edit", template_id: "template-edit", version: 4 });
    expect(onSaved.mock.calls[0][1]).toMatchObject({ id: "template-edit", body_template: "Новый понятный текст", version: 5 });
  });

  it("creates a replacement template before changing a rule channel", async () => {
    const user = userEvent.setup();
    const onSaved = vi.fn();
    const rule: ApiNotificationRule = {
      id: "rule-client-edit",
      template_id: "template-email",
      name: "Письмо клиенту",
      event_type: "purchase.due_soon",
      audience: "client",
      channel: "email",
      pipeline_id: null,
      stage_id: null,
      source_id: null,
      filters: {},
      recipients: [{ address: "anna@example.com", contact_id: "c-1", normalized_address: "anna@example.com" }],
      delay_seconds: 0,
      require_client_consent: true,
      is_enabled: false,
      version: 2,
      created_at: "2026-08-01T10:00:00Z",
      updated_at: "2026-08-01T10:00:00Z",
    };
    const template: ApiNotificationTemplate = {
      id: "template-email",
      name: "Письмо клиенту — шаблон",
      channel: "email",
      subject_template: "Событие в Pulse CRM",
      body_template: "Проверьте заказ",
      is_active: true,
      version: 2,
      created_at: "2026-08-01T10:00:00Z",
      updated_at: "2026-08-01T10:00:00Z",
    };
    renderEditor(<NotificationEditor rule={rule} template={template} pipelines={routingPipelines} currentUserId="user-test" onCancel={vi.fn()} onCreated={onSaved} />);

    await user.selectOptions(screen.getByRole("combobox", { name: "Канал" }), "telegram");
    const address = screen.getByRole("textbox", { name: "ID чата получателя" });
    await user.clear(address);
    await user.type(address, "chat-42");
    await user.type(screen.getByRole("textbox", { name: "Основание согласия" }), "Согласие в тестовой форме");
    await user.click(screen.getByRole("checkbox", { name: /Подтверждаю/ }));
    await user.click(screen.getByRole("button", { name: "Сохранить правило" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    const [savedRule, savedTemplate] = onSaved.mock.calls[0] as [ApiNotificationRule, ApiNotificationTemplate];
    expect(savedTemplate.channel).toBe("telegram");
    expect(savedTemplate.id).not.toBe(template.id);
    expect(savedRule).toMatchObject({
      id: rule.id,
      channel: "telegram",
      template_id: savedTemplate.id,
      recipients: [{ address: "chat-42", contact_id: "c-1", normalized_address: "chat-42" }],
      is_enabled: false,
    });
  });

  it("keeps a verified client rule enabled during safe text edits", async () => {
    const user = userEvent.setup();
    const onSaved = vi.fn();
    const rule: ApiNotificationRule = {
      id: "rule-client-safe-edit",
      template_id: "template-client-safe",
      name: "Напоминание клиенту",
      event_type: "purchase.due_soon",
      audience: "client",
      channel: "email",
      pipeline_id: null,
      stage_id: null,
      source_id: null,
      filters: {},
      recipients: [{ address: "anna@example.com", contact_id: "c-1", normalized_address: "anna@example.com" }],
      delay_seconds: 0,
      require_client_consent: true,
      is_enabled: true,
      version: 2,
      created_at: "2026-08-01T10:00:00Z",
      updated_at: "2026-08-01T10:00:00Z",
    };
    const template: ApiNotificationTemplate = {
      id: rule.template_id,
      name: `${rule.name} — шаблон`,
      channel: "email",
      subject_template: "Событие в Pulse CRM",
      body_template: "Старый текст",
      is_active: true,
      version: 2,
      created_at: rule.created_at,
      updated_at: rule.updated_at,
    };
    renderEditor(<NotificationEditor rule={rule} template={template} pipelines={routingPipelines} currentUserId="user-test" onCancel={vi.fn()} onCreated={onSaved} />);

    expect(screen.queryByRole("textbox", { name: "Основание согласия" })).not.toBeInTheDocument();
    const body = screen.getByRole("textbox", { name: "Текст шаблона" });
    await user.clear(body);
    await user.type(body, "Обновлённый текст");
    await user.click(screen.getByRole("button", { name: "Сохранить правило" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(onSaved.mock.calls[0][0]).toMatchObject({
      id: rule.id,
      template_id: template.id,
      is_enabled: true,
    });
  });
});

function demoChannelForTest(): ApiChannelConnection {
  return {
    id: "channel-edit",
    kind: "email",
    name: "Почта",
    status: "active",
    settings: {},
    default_pipeline_id: "pipeline-sales",
    default_stage_id: "sales-new",
    default_assignee_id: null,
    has_credentials: true,
    last_healthcheck_at: null,
    last_error: null,
    version: 1,
    created_at: "2026-08-01T10:00:00Z",
    updated_at: "2026-08-01T10:00:00Z",
  };
}

describe("SettingsPage mobile actions", () => {
  it("keeps rule toggling and editing as sibling native buttons", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("tab", { name: /Оповещения/ }));

    const edit = screen.getByRole("button", { name: /Изменить правило Новый лид/ });
    const toggle = screen.getByRole("button", { name: /Выключить правило Новый лид/ });
    expect(edit.parentElement).toBe(toggle.parentElement);
    expect(edit.contains(toggle)).toBe(false);

    await user.click(edit);
    expect(await screen.findByRole("region", { name: /Изменение правила/ })).toBeInTheDocument();
  });

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
