import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiChannelConnection, ApiNotificationRule, ApiNotificationTemplate, ApiUser } from "../types/api";
import { ChannelEditor, NotificationEditor, UserEditor } from "./SettingsPage";

const { getMock, patchMock, postMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  patchMock: vi.fn(),
  postMock: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  ApiError: class ApiError extends Error {
    constructor(message: string, public readonly status: number, public readonly details?: unknown) {
      super(message);
    }
  },
  api: { get: getMock, patch: patchMock, post: postMock },
  remoteEnabled: true,
}));

const users: ApiUser[] = [
  { id: "owner-1", email: "owner@example.com", full_name: "Владелец", role: "owner", version: 5 },
  { id: "employee-1", email: "employee@example.com", full_name: "Сотрудник", role: "employee", version: 3 },
];
const pipelines = [{
  id: "pipeline-1",
  name: "Продажи",
  stages: [{ id: "stage-1", name: "Новая", color: "blue" as const, stageType: "open" as const }],
}];

function renderEditor(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

beforeEach(() => {
  getMock.mockReset();
  patchMock.mockReset();
  postMock.mockReset();
});

afterEach(cleanup);

describe("Settings remote editors", () => {
  it("patches a channel with expected_version and leaves credentials untouched when blank", async () => {
    const user = userEvent.setup();
    const onSaved = vi.fn();
    const channel: ApiChannelConnection = {
      id: "channel-1",
      kind: "email",
      name: "Почта",
      status: "active",
      settings: {},
      default_pipeline_id: "pipeline-1",
      default_stage_id: "stage-1",
      default_assignee_id: null,
      has_credentials: true,
      last_healthcheck_at: null,
      last_error: null,
      version: 7,
      created_at: "2026-08-01T10:00:00Z",
      updated_at: "2026-08-01T10:00:00Z",
    };
    patchMock.mockResolvedValue({ ...channel, name: "Новая почта", version: 8 });
    renderEditor(<ChannelEditor channel={channel} pipelines={pipelines} defaultPipelineId="pipeline-1" availableUsers={users} onCancel={vi.fn()} onCreated={onSaved} />);

    const name = screen.getByRole("textbox", { name: "Название" });
    await user.clear(name);
    await user.type(name, "Новая почта");
    await user.click(screen.getByRole("button", { name: "Сохранить канал" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(patchMock).toHaveBeenCalledWith("/admin/integrations/channels/channel-1", expect.objectContaining({
      expected_version: 7,
      name: "Новая почта",
    }));
    expect(patchMock.mock.calls[0][1]).not.toHaveProperty("credentials");
  });

  it("refreshes the editor and explains an optimistic-lock conflict", async () => {
    const { ApiError } = await import("../lib/api");
    const user = userEvent.setup();
    const onConflict = vi.fn().mockResolvedValue(undefined);
    const target = users[1];
    patchMock.mockRejectedValue(new ApiError("conflict", 409, { detail: { code: "version_conflict", message: "record was modified" } }));
    renderEditor(<UserEditor user={target} actor={users[0]} onCancel={vi.fn()} onSaved={vi.fn()} onConflict={onConflict} />);

    await user.click(screen.getByRole("button", { name: "Сохранить пользователя" }));

    await waitFor(() => expect(onConflict).toHaveBeenCalledWith(target.id));
    expect(screen.getByRole("alert")).toHaveTextContent("Запись уже изменена другим пользователем");
    expect(patchMock).toHaveBeenCalledWith(`/users/${target.id}`, expect.objectContaining({ expected_version: 3 }));
  });

  it("patches the linked template before patching an edited notification rule", async () => {
    const user = userEvent.setup();
    const onSaved = vi.fn();
    const template: ApiNotificationTemplate = {
      id: "template-1",
      name: "Лид — шаблон",
      channel: "in_app",
      subject_template: null,
      body_template: "Старый текст",
      is_active: true,
      version: 4,
      created_at: "2026-08-01T10:00:00Z",
      updated_at: "2026-08-01T10:00:00Z",
    };
    const rule: ApiNotificationRule = {
      id: "rule-1",
      template_id: template.id,
      name: "Лид",
      event_type: "lead.created",
      audience: "employee",
      channel: "in_app",
      pipeline_id: null,
      stage_id: null,
      source_id: null,
      filters: {},
      recipients: [{ address: "employee-1", recipient_id: "employee-1" }],
      delay_seconds: 0,
      require_client_consent: true,
      is_enabled: true,
      version: 6,
      created_at: "2026-08-01T10:00:00Z",
      updated_at: "2026-08-01T10:00:00Z",
    };
    patchMock.mockImplementation((path: string) => path.includes("notification-templates")
      ? Promise.resolve({ ...template, body_template: "Новый текст", version: 5 })
      : Promise.resolve({ ...rule, version: 7 }));
    renderEditor(<NotificationEditor rule={rule} template={template} pipelines={pipelines} currentUserId="owner-1" currentUser={users[0]} availableUsers={users} onCancel={vi.fn()} onCreated={onSaved} />);

    const body = screen.getByRole("textbox", { name: "Текст шаблона" });
    await user.clear(body);
    await user.type(body, "Новый текст");
    await user.click(screen.getByRole("button", { name: "Сохранить правило" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(patchMock.mock.calls[0]).toEqual([
      "/admin/integrations/notification-templates/template-1",
      expect.objectContaining({ expected_version: 4, body_template: "Новый текст" }),
    ]);
    expect(patchMock.mock.calls[1]).toEqual([
      "/admin/integrations/notification-rules/rule-1",
      expect.objectContaining({ expected_version: 6, template_id: "template-1" }),
    ]);
  });
});
