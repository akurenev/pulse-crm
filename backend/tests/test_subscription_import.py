from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

import httpx
import pytest
from openpyxl import Workbook

from app.integrations.subscription_import import (
    default_call_due_at,
    parse_frontol_endlic,
    renewal_groups,
)


def csrf(auth: dict[str, object]) -> dict[str, str]:
    return {"X-CSRF-Token": str(auth["csrf_token"])}


def workbook_bytes() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "TDSheet"
    sheet.append(["Партнер", "ИНН партнера"])
    sheet.append(["Тестовый партнер"])
    sheet.append([])
    sheet.append(
        [
            "LID",
            "Дата активации",
            "Дата окончания",
            "Наименование пользователя",
            "ИНН Пользователя",
            "Телефон пользователя",
            "Электронная почта пользователя",
        ]
    )
    sheet.append(["ПО Frontol - Тариф Базовый"])
    first = [
        "LICENSE-001",
        "01.10.2025",
        "15.10.2026",
        "ООО Тестовая организация",
        "7700000000",
        "+7 900 000-00-01; ",
        "first@example.com",
    ]
    sheet.append(first)
    sheet.append(first)
    sheet.append(["ПО Frontol Mark Unit"])
    sheet.append(
        [
            "LICENSE-002",
            "02.10.2025",
            "15.10.2026",
            "ООО Тестовая организация",
            "7700000000",
            "+7 900 000-00-01; ",
            "second@example.com",
        ]
    )
    sheet.append(
        [
            "LICENSE-001",
            "03.10.2025",
            "20.10.2026",
            "ООО Тестовая организация",
            "7700000000",
            "+7 900 000-00-01; ",
            "first@example.com",
        ]
    )
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def test_parser_groups_licenses_by_inn_and_expiration() -> None:
    parsed = parse_frontol_endlic(workbook_bytes())
    groups = renewal_groups(parsed.licenses)

    assert len(parsed.licenses) == 3
    assert parsed.duplicate_rows == 1
    assert len(groups) == 2
    assert [len(group.licenses) for group in groups] == [2, 1]
    assert groups[0].products == ["ПО Frontol - Тариф Базовый", "ПО Frontol Mark Unit"]


def test_default_call_due_time_uses_workspace_day_boundary() -> None:
    before_cutoff = default_call_due_at(
        timezone_name="Asia/Yekaterinburg",
        now=datetime(2026, 10, 1, 8, 0, tzinfo=UTC),
    )
    after_cutoff_friday = default_call_due_at(
        timezone_name="Asia/Yekaterinburg",
        now=datetime(2026, 10, 2, 13, 0, tzinfo=UTC),
    )
    saturday = default_call_due_at(
        timezone_name="Asia/Yekaterinburg",
        now=datetime(2026, 10, 3, 7, 0, tzinfo=UTC),
    )

    assert before_cutoff == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    assert after_cutoff_friday == datetime(2026, 10, 5, 5, 0, tzinfo=UTC)
    assert saturday == datetime(2026, 10, 5, 5, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_subscription_import_preview_create_and_repeat_are_idempotent(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
) -> None:
    headers = csrf(owner_auth)
    pipeline = (await client.get("/api/v1/pipelines")).json()[0]
    owner = (await client.get("/api/v1/users")).json()[0]
    content = workbook_bytes()

    def files() -> dict[str, tuple[str, bytes, str]]:
        return {
            "file": (
                "EndLic_test.xlsx",
                content,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        }

    form = {
        "pipeline_id": pipeline["id"],
        "assignee_id": owner["id"],
        "dry_run": "true",
    }
    preview = await client.post(
        "/api/v1/admin/integrations/subscription-imports",
        headers=headers,
        data=form,
        files=files(),
    )
    assert preview.status_code == 200, preview.text
    assert {
        "licenses": 3,
        "groups": 2,
        "duplicate_rows": 1,
        "companies_created": 1,
        "contacts_created": 1,
        "deals_created": 2,
        "tasks_created": 2,
    }.items() <= preview.json()["counts"].items()
    assert (await client.get("/api/v1/deals", params={"pipeline_id": pipeline["id"]})).json()[
        "items"
    ] == []

    form["dry_run"] = "false"
    created = await client.post(
        "/api/v1/admin/integrations/subscription-imports",
        headers=headers,
        data=form,
        files=files(),
    )
    assert created.status_code == 200, created.text
    assert created.json()["counts"]["deals_created"] == 2
    assert created.json()["counts"]["tasks_created"] == 2

    deals = (
        await client.get(
            "/api/v1/deals",
            params={"pipeline_id": pipeline["id"], "limit": 100},
        )
    ).json()["items"]
    assert len(deals) == 2
    grouped_deal = next(
        deal for deal in deals if deal["custom_fields"]["subscription_license_count"] == 2
    )
    assert len(grouped_deal["custom_fields"]["subscription_licenses"]) == 2
    assert grouped_deal["company"]["inn"] == "7700000000"
    tasks = (await client.get("/api/v1/tasks", params={"limit": 100})).json()["items"]
    assert len(tasks) == 2
    assert all(task["task_type"] == "call" for task in tasks)

    repeated = await client.post(
        "/api/v1/admin/integrations/subscription-imports",
        headers=headers,
        data=form,
        files=files(),
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["counts"]["deals_created"] == 0
    assert repeated.json()["counts"]["deals_updated"] == 2
    assert repeated.json()["counts"]["tasks_created"] == 0
    assert repeated.json()["counts"]["tasks_reused"] == 2
    repeated_deals = (
        await client.get(
            "/api/v1/deals",
            params={"pipeline_id": pipeline["id"], "limit": 100},
        )
    ).json()["items"]
    repeated_tasks = (await client.get("/api/v1/tasks", params={"limit": 100})).json()["items"]
    assert len(repeated_deals) == 2
    assert len(repeated_tasks) == 2
