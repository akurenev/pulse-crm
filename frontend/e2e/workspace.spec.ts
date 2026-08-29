import { expect, test } from "@playwright/test";

test("creates a contact and switches to companies", async ({ page }) => {
  await page.goto("/contacts");
  const importedContact = page.getByRole("button", { name: /Анна Смирнова/ });
  await expect(importedContact).toContainText("VIP");
  await importedContact.click();
  const importedDialog = page.getByRole("dialog", { name: "Анна Смирнова" });
  await expect(importedDialog.getByText("Повторная покупка, VIP", { exact: true })).toBeVisible();
  await importedDialog.getByRole("button", { name: "Закрыть" }).click();

  await page.getByRole("button", { name: "Новый контакт" }).click();
  const dialog = page.getByRole("dialog", { name: "Новый контакт" });
  await dialog.getByLabel("Имя").fill("Мария");
  await dialog.getByLabel("Фамилия").fill("Орлова");
  await dialog.getByLabel("Email").fill("maria@example.com");
  await dialog.getByLabel("Телефон").fill("+7 000 000-00-06");
  await dialog.getByRole("button", { name: "Сохранить" }).click();
  await expect(page.getByText("Мария Орлова", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Компании" }).click();
  await expect(page.getByRole("region", { name: "Список компаний" })).toBeVisible();
});

test("creates and completes a task", async ({ page }) => {
  await page.goto("/tasks");
  await page.getByRole("button", { name: "Новая задача" }).click();
  const dialog = page.getByRole("dialog", { name: "Новая задача" });
  await dialog.getByLabel("Название").fill("Проверить договор");
  await dialog.getByLabel("Срок").fill("2030-09-01T12:30");
  await dialog.getByRole("button", { name: "Создать задачу" }).click();
  const task = page.getByRole("button", { name: /Проверить договор/ });
  await expect(task).toBeVisible();
  await task.click();
  await expect(task).toHaveClass(/task-table-row--done/);
});

test("creates a disabled client notification rule with recorded consent", async ({ page }) => {
  await page.goto("/settings");
  await page.getByRole("tab", { name: /Оповещения/ }).click();
  await page.getByRole("button", { name: "Новое правило" }).click();
  const editor = page.locator(".settings-editor");
  await editor.getByLabel("Название правила").fill("Повторная покупка — Анне");
  await editor.getByLabel("Получатель").selectOption("client");
  await editor.getByLabel("Клиент", { exact: true }).selectOption("c-1");
  await editor.getByLabel("Основание согласия").fill("Checkbox формы заказа, 28.08.2026");
  await editor.getByLabel(/Подтверждаю, что согласие/).check();
  await editor.getByRole("button", { name: "Создать правило" }).click();
  const rule = page.getByText("Повторная покупка — Анне", { exact: true });
  await expect(rule).toBeVisible();
  await expect(page.getByRole("button", { name: "Включить правило Повторная покупка — Анне" })).toHaveAttribute("aria-pressed", "false");
});

test("creates a deal field and makes it required for a pipeline stage", async ({ page }) => {
  await page.goto("/settings");
  await page.getByRole("button", { name: "Новое поле сделки" }).click();
  let editor = page.locator(".settings-editor");
  await editor.getByLabel("Название").fill("Причина обращения");
  await editor.getByLabel("Системный ключ").fill("request_reason");
  await editor.getByLabel("Тип").selectOption("select");
  await editor.getByLabel("Варианты списка").fill("Повторная покупка\nРекомендация");
  await editor.getByRole("button", { name: "Создать поле" }).click();
  await expect(page.getByText("Причина обращения", { exact: true })).toBeVisible();

  const stage = page.getByRole("button", { name: "Обязательные поля этапа Новый лид" });
  await stage.click();
  editor = page.locator(".settings-editor");
  await editor.getByLabel(/^Название/).check();
  await editor.getByLabel(/^Причина обращения/).check();
  await editor.getByRole("button", { name: "Сохранить обязательность" }).click();
  await expect(stage).toContainText("2 обязательных полей");

  await stage.click();
  editor = page.locator(".settings-editor");
  await expect(editor.getByLabel(/^Название/)).toBeChecked();
  await expect(editor.getByLabel(/^Причина обращения/)).toBeChecked();
});

test("renames a pipeline and manages its open stages", async ({ page }) => {
  await page.goto("/settings");
  const pipeline = page.locator(".settings-section").first();

  await pipeline.getByRole("button", { name: "Переименовать воронку Повторные продажи" }).click();
  let editor = page.locator(".settings-editor");
  await editor.getByLabel("Название воронки").fill("Продажи и продления");
  await editor.getByRole("button", { name: "Сохранить название" }).click();
  await expect(pipeline.getByText("Продажи и продления", { exact: true })).toBeVisible();

  await pipeline.getByRole("button", { name: "Добавить этап в воронку Продажи и продления" }).click();
  editor = page.locator(".settings-editor");
  await editor.getByLabel("Название этапа").fill("Согласование договора");
  await editor.getByRole("button", { name: "Добавить этап" }).click();
  await expect(pipeline.getByRole("button", { name: "Обязательные поля этапа Согласование договора" })).toBeVisible();

  await pipeline.getByRole("button", { name: "Переименовать этап Согласование договора" }).click();
  editor = page.locator(".settings-editor");
  await editor.getByLabel("Название этапа").fill("Договор согласован");
  await editor.getByRole("button", { name: "Сохранить этап" }).click();
  await expect(pipeline.getByRole("button", { name: "Обязательные поля этапа Договор согласован" })).toBeVisible();

  page.once("dialog", (dialog) => void dialog.accept());
  await pipeline.getByRole("button", { name: "Удалить этап Договор согласован" }).click();
  await expect(pipeline.getByRole("button", { name: "Обязательные поля этапа Договор согласован" })).toHaveCount(0);

  await page.getByRole("button", { name: "Новая воронка" }).click();
  editor = page.locator(".settings-editor");
  await editor.getByLabel("Название воронки").fill("Воронка для удаления");
  await editor.getByRole("button", { name: "Создать воронку" }).click();
  const removablePipeline = page.locator(".settings-section").filter({ hasText: "Воронка для удаления" });
  page.once("dialog", (dialog) => void dialog.accept());
  await removablePipeline.getByRole("button", { name: "Удалить воронку Воронка для удаления" }).click();
  await expect(removablePipeline).toHaveCount(0);
});
