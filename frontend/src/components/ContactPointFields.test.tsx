import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { readContactPoints } from "../lib/contact-points";
import { ContactPointFields } from "./ContactPointFields";

afterEach(cleanup);

describe("ContactPointFields", () => {
  it("adds email rows, focuses the new row, and includes them in form submission", async () => {
    const user = userEvent.setup();
    const { container } = render(<form><ContactPointFields kind="email" /></form>);
    await user.type(screen.getByRole("textbox", { name: "Email" }), "first@example.test");
    await user.click(screen.getByRole("button", { name: "Добавить email" }));
    const second = screen.getByRole("textbox", { name: "Email 2" });
    expect(second).toHaveFocus();
    await user.type(second, "second@example.test");

    expect(readContactPoints(new FormData(container.querySelector("form")!), "email"))
      .toEqual(["first@example.test", "second@example.test"]);
    expect(screen.getByText(/Первое заполненное значение — основное/)).toBeInTheDocument();
  });

  it("keeps edited remaining values and input identity when an earlier row is removed", async () => {
    const user = userEvent.setup();
    const { container } = render(<form><ContactPointFields kind="phone" defaultValues={["+7 000 000-00-01", "+7 000 000-00-02", "+7 000 000-00-03"]} /></form>);
    const second = screen.getByRole("textbox", { name: "Телефон 2" });
    const third = screen.getByRole("textbox", { name: "Телефон 3" });
    await user.clear(second);
    await user.type(second, "+7 000 000-00-04");
    await user.click(screen.getByRole("button", { name: "Удалить телефон 1" }));

    expect(screen.getByRole("textbox", { name: "Телефон" })).toBe(second);
    expect(screen.getByRole("textbox", { name: "Телефон 2" })).toBe(third);
    expect(readContactPoints(new FormData(container.querySelector("form")!), "phone"))
      .toEqual(["+7 000 000-00-04", "+7 000 000-00-03"]);
  });

  it("clears the final row while leaving an editable empty input", async () => {
    const user = userEvent.setup();
    const { container } = render(<form><ContactPointFields kind="email" defaultValues={["test@example.test"]} /></form>);
    const input = screen.getByRole("textbox", { name: "Email" });
    await user.click(screen.getByRole("button", { name: "Удалить email 1" }));

    expect(screen.getAllByRole("textbox")).toHaveLength(1);
    expect(input).toHaveValue("");
    expect(input).toHaveFocus();
    expect(readContactPoints(new FormData(container.querySelector("form")!), "email")).toEqual([]);
  });

  it("limits adding rows to 20 and allows adding again after removal", async () => {
    const user = userEvent.setup();
    const defaults = Array.from({ length: 19 }, (_, index) => `test${index}@example.test`);
    render(<ContactPointFields kind="email" defaultValues={defaults} />);
    const addButton = screen.getByRole("button", { name: "Добавить email" });
    await user.click(addButton);

    expect(screen.getAllByRole("textbox")).toHaveLength(20);
    expect(addButton).toBeDisabled();
    expect(screen.getByText(/До 20 адресов email/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Удалить email 20" }));
    expect(addButton).toBeEnabled();
  });

  it("renders every persisted value even if legacy data exceeds the entry limit", () => {
    const defaults = Array.from({ length: 21 }, (_, index) => `test${index}@example.test`);
    const { container } = render(<form><ContactPointFields kind="email" defaultValues={defaults} /></form>);

    expect(screen.getAllByRole("textbox")).toHaveLength(21);
    expect(screen.getByRole("button", { name: "Добавить email" })).toBeDisabled();
    expect(readContactPoints(new FormData(container.querySelector("form")!), "email")).toEqual(defaults);
  });
});
