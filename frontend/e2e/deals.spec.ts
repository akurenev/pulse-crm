import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/deals");
});

test("kanban opens a deal and sends a message", async ({ page }, testInfo) => {
  if (testInfo.project.name === "mobile") {
    await expect(page.locator(".mobile-header").getByText("Сделки", { exact: true })).toBeVisible();
  } else {
    await expect(page.getByRole("heading", { name: "Сделки", level: 1 })).toBeVisible();
  }
  await expect(page.getByText("Новый лид", { exact: true })).toBeVisible();
  await expect(page.getByText("Связались", { exact: true }).first()).toBeVisible();

  await page.getByText("Кофейня «Слой»", { exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "Кофейня «Слой»" });
  await expect(drawer).toBeVisible();
  await expect(drawer.getByRole("combobox", { name: "Этап сделки" })).toHaveValue("contacted");

  const message = "Коммерческое предложение отправим сегодня до 17:00.";
  await drawer.getByRole("tab", { name: "Переписка" }).click();
  await drawer.getByPlaceholder("Написать сообщение").fill(message);
  await drawer.getByRole("button", { name: "Отправить", exact: true }).click();
  await expect(drawer.getByText(message, { exact: true })).toBeVisible();
});

test("new deal is created on the first stage", async ({ page }) => {
  await page.getByRole("button", { name: "Новая сделка", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Новая сделка" });
  await dialog.getByLabel("Название").fill("Тестовая кофейня");
  await dialog.getByLabel("Потребность").fill("Зерно и сиропы");
  await dialog.getByLabel("Сумма, ₽").fill("51000");
  await dialog.getByLabel("Источник").selectOption("telegram");
  await dialog.getByRole("button", { name: "Создать сделку" }).click();

  await expect(page.getByRole("dialog", { name: "Тестовая кофейня" })).toBeVisible();
  await expect(page.getByText("51 000 ₽", { exact: true }).first()).toBeVisible();
});

test("list view opens the same deal card", async ({ page }, testInfo) => {
  const switcher = testInfo.project.name === "mobile"
    ? page.locator(".mobile-layout-switch")
    : page.locator(".deals-toolbar").getByLabel("Вид сделок");
  await switcher.getByRole("button", { name: "Список" }).click();
  const list = page.getByRole("region", { name: "Список сделок" });
  await expect(list).toBeVisible();
  await list.getByRole("button", { name: /Кофейня «Слой»/ }).click();
  await expect(page.getByRole("dialog", { name: "Кофейня «Слой»" })).toBeVisible();
});

test("sets the next purchase date from a deal", async ({ page }) => {
  await page.getByText("Кофейня «Слой»", { exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "Кофейня «Слой»" });
  const date = drawer.getByLabel("Дата следующей покупки");
  await date.fill("2030-10-12");
  await drawer.getByRole("button", { name: "Сохранить" }).click();
  await expect(date).toHaveValue("2030-10-12");
});

test("mobile layout has no horizontal page overflow", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "Mobile-only responsive assertion");

  const dimensions = await page.evaluate(() => ({
    viewportWidth: window.innerWidth,
    documentWidth: document.documentElement.scrollWidth,
  }));

  expect(dimensions.documentWidth).toBeLessThanOrEqual(dimensions.viewportWidth);
  await expect(page.getByRole("navigation", { name: "Мобильная навигация" })).toBeVisible();
});
