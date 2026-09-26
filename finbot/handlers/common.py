"""Простые экраны: баланс, справка, мотивация."""

from __future__ import annotations

import random

from .. import motivation, reports, texts
from ..callbacks import GoalCb, Nav
from ..db import Database
from ..ui import Event, btn, kb, render


async def show_dashboard(event: Event, db: Database) -> None:
    text = await reports.dashboard_text(db)
    markup = kb(
        [btn("🎯 Цели", Nav(to="goals")), btn("📅 Календарь", Nav(to="calendar"))],
        [btn("🧾 История", Nav(to="history")), btn("⚙️ Настройки", Nav(to="settings"))],
    )
    await render(event, text, markup)


async def show_help(event: Event, db: Database) -> None:
    await render(event, texts.HELP.format(pct=db.settings.goal_pct), kb([btn("💼 Баланс", Nav(to="balance"))]))


async def show_motivation(event: Event, db: Database) -> None:
    line = await reports.personal_line(db)
    quote = motivation.quote(random.choice(("general", "general", "speech")), db.settings.harsh)
    parts = ["🔥 <b>МОТИВАЦИЯ</b>", "", quote]
    if line:
        parts += ["", line]
    if "stay hard" not in quote.lower():
        parts += ["", "<b>Stay hard.</b> 💀"]
    await render(event, "\n".join(parts), kb([btn("🔥 Ещё", Nav(to="motivation"))]))


async def show_onboarding(event: Event) -> None:
    await render(
        event,
        texts.ONBOARDING,
        kb(
            [btn("🎯 Создать первую цель", GoalCb(action="new"))],
            [btn("💼 Указать деньги на расходы", Nav(to="wallet"))],
            [btn("📖 Как пользоваться", Nav(to="help"))],
        ),
    )
