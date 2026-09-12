"""
auto_flipper/keyboards.py — Interactive inline keyboards for the Auto-Flipper Telegram Bot
Supports multi-category controls, category toggles, P&L category breakdown, and settings.
"""
from typing import Any, Dict, List, Optional
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_dashboard_keyboard(status: Dict[str, Any]) -> InlineKeyboardMarkup:
    dry_run = status.get("dry_run", True)
    auto_buy = status.get("auto_buy", False)
    stopped = status.get("is_emergency_stopped", False)
    turbo = status.get("turbo_mode", False)

    dry_text = "🧪 DRY RUN: ВКЛ" if dry_run else "🟢 LIVE: РЕАЛЬНЫЙ"
    buy_text = "🤖 АВТО-ВЫКУП: ВКЛ" if auto_buy else "⏸️ АВТО-ВЫКУП: ВЫКЛ"
    turbo_text = "⚡ ТУРБО: ВКЛ" if turbo else "⏳ ТУРБО: ВЫКЛ"

    buttons = [
        [
            InlineKeyboardButton(text=dry_text, callback_data="flip_toggle_dry"),
            InlineKeyboardButton(text=buy_text, callback_data="flip_toggle_buy"),
        ],
        [
            InlineKeyboardButton(text="📈 Финансовый P&L", callback_data="flip_pnl"),
            InlineKeyboardButton(text="📦 Склад и лоты", callback_data="flip_inventory"),
        ],
        [
            InlineKeyboardButton(text="🚀 Буст лотов (/boost)", callback_data="flip_boost"),
            InlineKeyboardButton(text="🎯 Цель прибыли (/goal)", callback_data="flip_goal"),
        ],
        [
            InlineKeyboardButton(text="🌐 Сессия FunPay (/browser)", callback_data="flip_browser"),
            InlineKeyboardButton(text=turbo_text, callback_data="flip_toggle_turbo"),
        ],
        [
            InlineKeyboardButton(text="📁 Категории (/categories)", callback_data="flip_categories"),
            InlineKeyboardButton(text="⚙️ Настройки бота", callback_data="flip_settings"),
        ],
        [
            InlineKeyboardButton(text="🔄 Обновить дашборд", callback_data="flip_refresh_dash"),
        ],
    ]

    if stopped:
        buttons.append([InlineKeyboardButton(text="▶️ ВОЗОБНОВИТЬ ПОСЛЕ СТОП", callback_data="flip_resume")])
    else:
        buttons.append([InlineKeyboardButton(text="🛑 ЭКСТРЕННЫЙ СТОП", callback_data="flip_emergency_stop")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def pnl_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Обновить P&L", callback_data="flip_pnl"),
                InlineKeyboardButton(text="🎯 Цель прибыли", callback_data="flip_goal"),
            ],
            [
                InlineKeyboardButton(text="📁 По категориям", callback_data="flip_pnl_categories"),
                InlineKeyboardButton(text="📦 Перейти к складу", callback_data="flip_inventory"),
            ],
            [
                InlineKeyboardButton(text="« Главное меню", callback_data="flip_main"),
            ],
        ]
    )


def pnl_categories_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="flip_pnl_categories"),
                InlineKeyboardButton(text="📈 Общий P&L", callback_data="flip_pnl"),
            ],
            [
                InlineKeyboardButton(text="📁 Настроить категории", callback_data="flip_categories"),
                InlineKeyboardButton(text="« Главное меню", callback_data="flip_main"),
            ],
        ]
    )


def inventory_keyboard(items: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    cat_badges = {
        "steam": "🎮",
        "discord": "⚡",
        "cursor": "🤖",
        "exitlag": "🚀",
        "tg_premium": "⭐",
        "chatgpt": "🧠",
        "cs2_prime": "🎯",
        "valorant_ranked": "🔥",
        "minecraft_pc": "⛏️",
    }
    buttons = []
    for it in items[:6]:
        status_icon = (
            "⏳" if it["status"] == "bought"
            else "📋" if it["status"] == "ready_for_sale"
            else "🏷️" if it["status"] == "listed"
            else "🛑" if it["status"] == "emergency_paused"
            else "✅"
        )
        c_id = it.get("category_id", "chatgpt")
        cat_icon = cat_badges.get(c_id, "📦")
        price_str = f"{int(it['buy_price'])}➔{int(it['sell_price'])} ₽"
        short_title = it.get("title", "")[:12]
        buttons.append([
            InlineKeyboardButton(
                text=f"{status_icon}{cat_icon} {price_str} | {short_title}",
                callback_data=f"flip_item_{it['item_uuid']}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(text="🔄 Обновить склад", callback_data="flip_inventory"),
        InlineKeyboardButton(text="📁 Категории", callback_data="flip_categories"),
    ])
    buttons.append([
        InlineKeyboardButton(text="« В главное меню", callback_data="flip_main"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def item_detail_keyboard(item_uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="« Назад к складу", callback_data="flip_inventory")],
            [InlineKeyboardButton(text="« Главное меню", callback_data="flip_main")],
        ]
    )


def boost_keyboard(turbo_mode: bool, remaining_turbo: int = 0) -> InlineKeyboardMarkup:
    if turbo_mode and remaining_turbo > 0:
        mins = max(1, remaining_turbo // 60)
        turbo_label = f"⚡ Турбо активен ({mins} мин) | Выкл"
    elif turbo_mode:
        turbo_label = "⚡ Выключить Турбо-режим"
    else:
        turbo_label = "⚡ Включить Турбо-режим (6 сек)"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Поднять лоты сейчас (Raise)", callback_data="flip_raise_now")],
            [InlineKeyboardButton(text=turbo_label, callback_data="flip_toggle_turbo")],
            [InlineKeyboardButton(text="🔄 Обновить статус буста", callback_data="flip_boost")],
            [InlineKeyboardButton(text="« В главное меню", callback_data="flip_main")],
        ]
    )


def goal_keyboard(current_goal: float) -> InlineKeyboardMarkup:
    presets = [1000, 3000, 5000, 10000, 25000, 50000]
    row1 = []
    row2 = []
    for i, p in enumerate(presets):
        check = "✓ " if int(current_goal) == p else ""
        btn = InlineKeyboardButton(text=f"{check}{p:,} ₽".replace(",", " "), callback_data=f"flip_set_goal_{p}")
        if i < 3:
            row1.append(btn)
        else:
            row2.append(btn)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            row1,
            row2,
            [InlineKeyboardButton(text="✍️ Ввести свою цель вручную", callback_data="flip_custom_goal")],
            [
                InlineKeyboardButton(text="📈 Отчёт P&L", callback_data="flip_pnl"),
                InlineKeyboardButton(text="« В главное меню", callback_data="flip_main"),
            ],
        ]
    )


def browser_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Проверить сессию снова", callback_data="flip_browser"),
                InlineKeyboardButton(text="🚀 Буст лотов", callback_data="flip_boost"),
            ],
            [InlineKeyboardButton(text="« В главное меню", callback_data="flip_main")],
        ]
    )


def settings_keyboard(status: Dict[str, Any]) -> InlineKeyboardMarkup:
    dry_text = "🧪 DRY RUN" if status.get("dry_run") else "🟢 LIVE"
    buy_text = "ВКЛ ✅" if status.get("auto_buy") else "ВЫКЛ ⏸️"
    budget = int(status.get("max_budget", 350))
    min_p = int(status.get("min_profit", 150))
    mode_text = status.get("mode", "OBSERVE")

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"⚙️ Режим: {mode_text}", callback_data="flip_mode_menu"),
                InlineKeyboardButton(text=f"Среда: {dry_text}", callback_data="flip_toggle_dry"),
            ],
            [
                InlineKeyboardButton(text=f"Авто-выкуп: {buy_text}", callback_data="flip_toggle_buy"),
                InlineKeyboardButton(text="📁 Категории (/categories)", callback_data="flip_categories"),
            ],
            [
                InlineKeyboardButton(text=f"💰 Бюджет: {budget} ₽ ✏️", callback_data="flip_edit_budget"),
                InlineKeyboardButton(text=f"🎯 Мин. профит: {min_p} ₽ ✏️", callback_data="flip_edit_profit"),
            ],
            [
                InlineKeyboardButton(text="🔑 Golden Key (инфо)", callback_data="flip_edit_key"),
                InlineKeyboardButton(text="« В главное меню", callback_data="flip_main"),
            ],
        ]
    )


def mode_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    modes = [
        ("OBSERVE", "👁️ OBSERVE (Наблюдение)"),
        ("ASSIST", "🤝 ASSIST (Полуавтомат)"),
        ("LIMITED_AUTO", "🤖 LIMITED_AUTO (Авто-лимиты)"),
        ("PAUSED", "⏸️ PAUSED (Пауза)"),
    ]
    buttons = []
    for mode_key, mode_label in modes:
        check = "✓ " if current_mode == mode_key else ""
        buttons.append([InlineKeyboardButton(text=f"{check}{mode_label}", callback_data=f"flip_set_mode_{mode_key}")])
    buttons.append([
        InlineKeyboardButton(text="⚙️ Настройки", callback_data="flip_settings"),
        InlineKeyboardButton(text="« Главное меню", callback_data="flip_main"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def categories_keyboard(enabled_categories: List[str]) -> InlineKeyboardMarkup:
    """Renders interactive toggle grid for all target arbitrage categories."""
    from auto_flipper.categories import CATEGORY_REGISTRY
    buttons = []
    row = []
    short_names = {
        "steam": "Steam (89)",
        "discord": "Discord (923)",
        "cursor": "Cursor (3734)",
        "exitlag": "ExitLag (1568)",
        "tg_premium": "TG Prem (1391)",
        "chatgpt": "ChatGPT (1355)",
        "cs2_prime": "CS2 Prime (1350)",
        "valorant_ranked": "Valorant (612)",
        "minecraft_pc": "Minecraft (221)",
    }

    for cat_id, cat in CATEGORY_REGISTRY.items():
        is_on = cat_id in enabled_categories
        mark = "✅" if is_on else "❌"
        disp = short_names.get(cat_id, cat.name)
        row.append(InlineKeyboardButton(text=f"{disp} {mark}", callback_data=f"flip_toggle_cat_{cat_id}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton(text="⚡ Включить все категории", callback_data="flip_cat_enable_all"),
    ])
    buttons.append([
        InlineKeyboardButton(text="⚙️ Настройки", callback_data="flip_settings"),
        InlineKeyboardButton(text="« Главное меню", callback_data="flip_main"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def budget_keyboard(current_budget: float) -> InlineKeyboardMarkup:
    presets = [250, 300, 350, 400, 500, 750]
    row1 = []
    row2 = []
    for i, p in enumerate(presets):
        check = "✓ " if int(current_budget) == p else ""
        btn = InlineKeyboardButton(text=f"{check}{p} ₽", callback_data=f"flip_preset_budget_{p}")
        if i < 3:
            row1.append(btn)
        else:
            row2.append(btn)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            row1,
            row2,
            [InlineKeyboardButton(text="✍️ Ввести сумму вручную", callback_data="flip_custom_budget")],
            [InlineKeyboardButton(text="« Назад в настройки", callback_data="flip_settings")],
        ]
    )


def emergency_stop_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Возобновить работу", callback_data="flip_resume")],
            [InlineKeyboardButton(text="« В главное меню", callback_data="flip_main")],
        ]
    )
