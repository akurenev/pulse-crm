# Pulse CRM design system

The desktop and mobile concept images in this directory are the visual source of
truth for the MVP interface. Product UI remains code-native.

## Direction

- Cool blue-gray canvas with true-white surfaces.
- Deep navy text and indigo as the only primary accent.
- Thin neutral borders and restrained shadows; no gradients or glass effects.
- Open rails and lists are preferred over nested card grids.
- Desktop uses a fixed navigation rail, a four-column Kanban canvas, and a
  400–420 px deal drawer. Mobile uses a vertical stage list, a full-screen deal
  sheet, and a five-item bottom navigation.

## Tokens

| Token | Value |
| --- | --- |
| canvas | `#f6f8fb` |
| surface | `#ffffff` |
| text | `#111827` |
| muted | `#697386` |
| border | `#dce1e9` |
| accent | `#4f46f5` |
| accent-soft | `#eeedff` |
| success | `#18a66a` |
| warning | `#f59e0b` |
| danger | `#f05a3c` |
| radius-sm/md/lg | `8 / 10 / 12 px` |
| shadow | `0 10px 30px rgba(25, 35, 60, .08)` |

Typography uses Inter with system fallbacks. Page titles are 28–32 px at
700–750 weight; card titles 15–17 px at 650; UI chrome 13–14 px; metadata
12–13 px. Controls have a 44 px minimum interactive height on touch layouts.

## Reusable families

- Sidebar and bottom navigation share the same outline icon family.
- Deal cards share one border, spacing, metadata row, avatar, and due-date
  treatment; selection adds an indigo border and soft surface.
- Stages use a 4 px top rail with blue, violet, emerald, and amber variants.
- Drawers, dialogs, and mobile sheets use the same header, tabs, field rows,
  task rows, message bubbles, and footer composer.

## Copy lock for the primary viewport

`Pulse CRM`, `Главная`, `Сделки`, `Клиенты`, `Задачи`, `Активность`,
`Настройки`, `Повторные продажи`, `Поиск по сделкам`, `Новая сделка`,
`Новый лид`, `Связались`, `Предложение`, `Ожидаем оплату`.

