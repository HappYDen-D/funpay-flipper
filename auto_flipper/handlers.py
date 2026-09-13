"""
auto_flipper/handlers.py — Telegram bot router & UI handlers for the Auto-Flipper Resale Bot
"""
import logging
import re
from html import escape
from typing import Any, Dict, List, Optional

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from auto_flipper.categories import CATEGORY_REGISTRY, get_category_by_id
from auto_flipper.credential_extractor import CredentialExtractor
from auto_flipper.database import db
from auto_flipper.flipper_engine import flipper_engine
from auto_flipper.economics import decimal
from auto_flipper.keyboards import (
    boost_keyboard,
    browser_keyboard,
    budget_keyboard,
    categories_keyboard,
    emergency_stop_keyboard,
    goal_keyboard,
    inventory_keyboard,
    item_detail_keyboard,
    main_dashboard_keyboard,
    mode_keyboard,
    pnl_categories_keyboard,
    pnl_keyboard,
    settings_keyboard,
)

logger = logging.getLogger("FlipperHandlers")
router = Router()
from auto_flipper.access import AdminAccess
router.message.outer_middleware(AdminAccess())
router.callback_query.outer_middleware(AdminAccess())


def is_admin(user_id: int) -> bool:
    return db.is_user_admin(user_id)


def parse_goal_amount(text):
    """Parse the entire amount, with optional thousands spaces and exact cents."""
    if not isinstance(text, str):
        raise ValueError('Amount required')
    text = text.strip()
    # Keep spaces only as groups of three: do not silently join arbitrary arguments.
    if not re.fullmatch(r'(?:[0-9]+|[0-9]{1,3}(?:[ \u00a0\u2009\u202f][0-9]{3})+)(?:[.,][0-9]{1,2})?', text):
        raise ValueError('Invalid money amount')
    amount = decimal(re.sub(r'[ \u00a0\u2009\u202f]', '', text).replace(',', '.'))
    if not 100 <= amount <= 10_000_000:
        raise ValueError('Goal must be between 100 and 10000000 RUB')
    return amount.quantize(decimal('.01'))


def format_goal_amount(value):
    amount = decimal(value)
    places = 0 if amount == amount.to_integral_value() else 2
    return f'{amount:,.{places}f}'.replace(',', ' ')


async def edit_text_if_changed(message, text, **kwargs):
    """Ignore only Telegram's harmless duplicate edit response."""
    try:
        await message.edit_text(text, **kwargs)
        return True
    except TelegramBadRequest as error:
        if 'message is not modified' not in error.message.lower():
            raise
        return False


def format_boost_result(result):
    """Report only what the action result confirms, including blocked dry runs."""
    raise_info = result.get('raise_info') or {}
    confirmed = result.get('success') is True and raise_info.get('success') is True
    lines = ['🚀 <b>РЕЗУЛЬТАТ БУСТА ЛОТОВ (FunPay Raise):</b>', '───────────────────────────']
    if confirmed:
        lines.append('🧪 <b>DRY RUN:</b> поднятие выполнено в симуляции.' if result.get('dry_run') is True
                     else '✅ <b>Поднятие лотов подтверждено FunPay.</b>')
        if raise_info.get('cooldown'):
            lines.append('⏳ Для части лотов действует кулдаун; их поднятие не подтверждено.')
    elif raise_info.get('cooldown'):
        seconds = raise_info.get('cooldown_seconds') or 0
        wait = f'около {max(1, int(seconds) // 60)} мин' if seconds > 0 else 'неизвестен'
        lines.append(f'⏳ <b>Кулдаун FunPay:</b> поднятие не выполнено. Срок ожидания: {wait}.')
    else:
        reason = result.get('error') or raise_info.get('error') or 'Поднятие не подтверждено'
        lines.append('⚠️ <b>Поднятие не выполнено:</b> ' + escape(str(reason)))
    scheduler = result.get('scheduler') or {}
    remaining = scheduler.get('remaining_turbo_seconds') or 0
    if result.get('turbo_mode') is True:
        suffix = f' (осталось {max(1, int(remaining) // 60)} мин)' if remaining > 0 else ''
        lines.append('⚡ <b>Турбо-режим:</b> включён' + suffix + '.')
    else:
        lines.append('⏳ <b>Турбо-режим:</b> выключен.')
    return '\n'.join(lines)


def format_browser_text(info):
    statuses = {
        'valid': '🟢 Авторизован', 'expired': '🔴 Сессия истекла',
        'cloudflare': '🟡 Защита Cloudflare', 'no_key': '⚪ golden_key не настроен',
    }
    status = statuses.get(info.get('session_status'), '🔴 Состояние сессии не подтверждено')
    currency = info.get('balance_currency') or info.get('currency')
    unit = '₽' if currency == 'RUB' else escape(str(currency)) if currency else '(валюта не подтверждена)'

    def amount(field):
        try:
            value = decimal(info.get(field))
            if value < 0:
                raise ValueError('Negative balance')
            return f'{value:.2f} {unit}'
        except ValueError:
            return 'нет подтверждённых данных'

    username = escape(str(info.get('username') or 'не определён'))
    try:
        latency = decimal(info.get('latency_ms'))
        latency_text = f'{latency} мс' if latency >= 0 else 'нет данных'
    except ValueError:
        latency_text = 'нет данных'
    error = '\n⚠️ ' + escape(str(info['error'])) if info.get('error') else ''
    return (
        '🌐 <b>Сессия FunPay Web &amp; Cloudflare</b>\n'
        '───────────────────────────\n'
        f'👤 <b>Профиль:</b> {username}\n'
        f'🔐 <b>Статус:</b> {status}{error}\n'
        '───────────────────────────\n'
        '<b>Показания страницы баланса:</b>\n'
        f'• Доступно: <code>{amount("balance_available")}</code>\n'
        f'• Удержано: <code>{amount("balance_hold")}</code>\n'
        f'• Всего: <code>{amount("balance_total")}</code>\n'
        f'⏱️ <b>Время ответа:</b> {latency_text}\n'
        '───────────────────────────\n'
        '<i>Допуск к покупке проверяется отдельно: /capital и /candidates.</i>'
    )


# ─────────────────────────────────────────────────────────────
# FSM States
# ─────────────────────────────────────────────────────────────

class GoalInputState(StatesGroup):
    waiting_for_goal = State()


class BudgetInputState(StatesGroup):
    waiting_for_budget = State()


class ProfitInputState(StatesGroup):
    waiting_for_profit = State()


class KeyInputState(StatesGroup):
    waiting_for_key = State()


# ─────────────────────────────────────────────────────────────
# Presentation Formatters
# ─────────────────────────────────────────────────────────────

def format_dashboard_text(status: Dict[str, Any], user_id: int) -> str:
    dry_mode = "🧪 <b>DRY RUN (Симуляция)</b>" if status["dry_run"] else "🟢 <b>LIVE (Реальный баланс)</b>"
    buy_mode = "🟢 <b>АКТИВЕН</b>" if status["auto_buy"] else "⏸️ <b>ПАУЗА</b>"
    
    sched = status.get("scheduler", {})
    rem_turbo = sched.get("remaining_turbo_seconds", 0)
    if status["turbo_mode"] and rem_turbo > 0:
        turbo_mode = f"⚡ <b>ВКЛ ({max(1, rem_turbo // 60)} мин)</b>"
    elif status["turbo_mode"]:
        turbo_mode = "⚡ <b>ВКЛ (6 сек)</b>"
    else:
        turbo_mode = "⏳ <b>Обычный (25 сек)</b>"

    stopped_banner = "\n🛑 <b>АКТИВИРОВАН ЭКСТРЕННЫЙ СТОП! Продажи на паузе.</b>\n" if status["is_emergency_stopped"] else ""

    counts = status["counts"]
    pnl = status["pnl"]
    goal = db.get_goal_progress(user_id)
    enabled_cats = db.get_enabled_categories()
    total_cats = len(CATEGORY_REGISTRY)

    flipper_mode = status.get("mode", "OBSERVE")
    return (
        "🤖 <b>Автономный бот-флипер FunPay (мультикатегорийный)</b>\n"
        "───────────────────────────\n"
        f"⚙️ <b>Режим работы:</b> <code>{flipper_mode}</code> | {dry_mode}\n"
        f"🤖 <b>Авто-выкуп:</b> {buy_mode} | 🚀 <b>Турбо:</b> {turbo_mode}\n"
        f"📁 <b>Категории:</b> <code>{len(enabled_cats)}/{total_cats} активны</code>\n"
        f"💰 <b>Доп. лимит покупки (0 = авто):</b> <code>{int(status['max_budget'])} ₽</code> (Мин. профит: <code>{int(status['min_profit'])} ₽</code>)\n"
        f"🏷️ <b>Цена перепродажи (GPT):</b> <code>~{int(status['target_resale_price'])} ₽</code>\n"
        f"{stopped_banner}"
        "───────────────────────────\n"
        "🎯 <b>ПРОГРЕСС ЦЕЛИ ПРИБЫЛИ:</b>\n"
        f"<code>[{goal['bar']}]</code> <b>{goal['percent']}%</b>\n"
        f"💵 Заработано: <b>+{goal['realized']:.2f} ₽</b> из <b>{int(goal['goal'])} ₽</b>\n"
        f"⏳ Осталось: <b>{goal['remaining']:.2f} ₽</b> (~{goal['flips_needed'] if goal['flips_needed'] is not None else '—'} сделок | ETA: {goal['eta_text']})\n"
        "───────────────────────────\n"
        "📦 <b>Склад и оборот лотов:</b>\n"
        f"• ⏳ В процессе выкупа: <b>{counts['bought']}</b>\n"
        f"• 📋 Готовы к продаже: <b>{counts['ready_for_sale']}</b>\n"
        f"• 🏷️ Активно на FunPay: <b>{counts['listed']}</b>\n"
        + (f"• 🛑 На паузе: <b>{counts['emergency_paused']}</b>\n" if counts.get("emergency_paused", 0) > 0 else "")
        + f"• ✅ Продано покупателям: <b>{counts['sold']}</b>\n\n"
        f"💎 <b>Реализованный P&L:</b> <code>+{pnl['total_net_profit']:.2f} ₽</code> (ROI: +{pnl['roi_pct']:.1f}%)\n"
        "───────────────────────────\n"
        "<i>Используйте кнопки меню для управления:</i>"
    )


def format_pnl_text(pnl: Dict[str, Any], goal: Dict[str, Any]) -> str:
    avg_turnover = f"{pnl['avg_turnover_seconds'] / 60:.1f} мин" if pnl['avg_turnover_seconds'] > 0 else "нет данных"
    by_cat = pnl.get("by_category", {})
    cat_lines = []
    for cid, cstat in by_cat.items():
        if cstat.get("total_sold", 0) > 0 or cstat.get("active_listed", 0) > 0 or cstat.get("holding_count", 0) > 0:
            cname = cstat.get("name", cid)
            sold = cstat.get("total_sold", 0)
            prof = cstat.get("total_net_profit", 0.0)
            roi = cstat.get("roi_pct", 0.0)
            cat_lines.append(f"• <b>{cname}:</b> {sold} прод. | <code>+{prof:.2f} ₽</code> (ROI: +{roi:.1f}%)")

    cat_section = ("\n📁 <b>Результаты по направлениям:</b>\n" + "\n".join(cat_lines) + "\n") if cat_lines else ""

    return (
        "📈 <b>Финансовый отчёт и баланс флипера (P&L)</b>\n"
        "───────────────────────────\n"
        f"💼 <b>Подтверждённые поступления продавцу:</b> <code>{pnl['total_revenue_gross']:.2f} ₽</code>\n"
        f"💸 <b>Затраты на закупку лотов:</b> <code>{pnl['total_spent_on_sold']:.2f} ₽</code>\n"
        "📉 Комиссии уже учтены в подтверждённых поступлениях.\n"
        "───────────────────────────\n"
        f"💎 <b>ЧИСТАЯ ПРИБЫЛЬ:</b> <code>+{pnl['total_net_profit']:.2f} ₽</code>\n"
        f"📊 <b>Рентабельность инвестиций (ROI):</b> <code>+{pnl['roi_pct']:.1f}%</code>\n"
        f"{cat_section}"
        "───────────────────────────\n"
        f"🎯 <b>Цель прибыли:</b> <code>{int(goal['goal'])} ₽</code> (выполнено {goal['percent']}% | ETA: {goal['eta_text']})\n"
        f"📦 <b>Успешных флипов:</b> {pnl['total_sold']} шт.\n"
        f"🏷️ <b>В продаже сейчас:</b> {pnl['active_listed_count']} шт. (на сумму {pnl['active_listed_value']:.2f} ₽)\n"
        f"📦 <b>В наличии на складе:</b> {pnl['holding_count']} шт. (себестоимость {pnl['holding_cost']:.2f} ₽)\n"
        f"⏱️ <b>Среднее время оборота:</b> {avg_turnover}\n"
    )


def format_categories_text(enabled_categories: List[str], category_medians: Dict[str, float]) -> str:
    lines = [
        "📁 <b>Целевые ликвидные позиции FunPay</b>",
        "───────────────────────────",
        "Бот снайпит высокочастотные направления и мгновенно перепродает:",
        "",
    ]
    for cat_id, cat in CATEGORY_REGISTRY.items():
        is_on = cat_id in enabled_categories
        status_icon = "🟢 <b>ВКЛ</b>" if is_on else "⚪ <i>выкл</i>"
        med = category_medians.get(cat_id, cat.market_benchmark)
        target_resale = round(med * cat.markup_discount)
        lines.append(
            f"• <b>{cat.name}</b> (Раздел #{cat.node_id}) — {status_icon}\n"
            f"   Закупка: <code>≤ {int(cat.max_buy_price)} ₽</code> | Продажа: <code>~{int(target_resale)} ₽</code>\n"
            f"   Ожидаемый профит: <code>+{int(cat.min_profit)} ₽</code> (маржа {int(cat.min_margin_pct)}%)\n"
        )
    lines.append("───────────────────────────")
    lines.append("<i>Нажимайте кнопки ниже для включения/выключения направлений:</i>")
    return "\n".join(lines)


def format_pnl_by_categories_text(pnl: Dict[str, Any]) -> str:
    by_cat = pnl.get("by_category", {})
    lines = [
        "📁 <b>Финансовый отчёт по категориям товаров</b>",
        "───────────────────────────",
    ]
    has_activity = False
    for cid, cstat in by_cat.items():
        cname = cstat.get("name", cid)
        sold = cstat.get("total_sold", 0)
        profit = cstat.get("total_net_profit", 0.0)
        rev = cstat.get("total_revenue_gross", 0.0)
        roi = cstat.get("roi_pct", 0.0)
        listed = cstat.get("active_listed", 0)
        holding = cstat.get("holding_count", 0)

        lines.append(
            f"• <b>{cname}</b>:\n"
            f"   Продано: <b>{sold} шт.</b> | Оборот: <code>{rev:.2f} ₽</code>\n"
            f"   Чистая прибыль: <code>+{profit:.2f} ₽</code> (ROI: +{roi:.1f}%)\n"
            f"   В продаже: <b>{listed}</b> | На складе: <b>{holding}</b>\n"
        )
        if sold > 0 or listed > 0 or holding > 0:
            has_activity = True

    if not has_activity:
        lines.append("<i>Пока нет завершённых продаж по направлениям. Запустите авто-выкуп!</i>\n")

    lines.append("───────────────────────────")
    lines.append(f"💎 <b>Итого чистая прибыль:</b> <code>+{pnl['total_net_profit']:.2f} ₽</code> (всего {pnl['total_sold']} сделок)")
    return "\n".join(lines)


def format_goal_text(goal_data: Dict[str, Any]) -> str:
    avg_calc_label = "по истории продаж" if goal_data.get("has_dynamic_avg") else "нет подтверждённых продаж"
    milestones = goal_data.get("milestones", {})
    return (
        "🎯 <b>Цель прибыли и прогресс (Profit Goal)</b>\n"
        "───────────────────────────\n"
        f"🎯 <b>Текущая цель:</b> <code>{format_goal_amount(goal_data['goal'])} ₽</code>\n"
        f"💰 <b>Реализовано:</b> <code>+{goal_data['realized']:.2f} ₽</code>\n"
        f"📊 <b>Прогресс:</b> <code>[{goal_data['bar']}]</code> <b>{goal_data['percent']}%</b>\n"
        f"⏳ <b>Осталось заработать:</b> <code>{goal_data['remaining']:.2f} ₽</code>\n"
        f"📦 <b>Осталось сделок:</b> <b>~{goal_data['flips_needed'] if goal_data['flips_needed'] is not None else '—'}</b> шт. (профит ~{goal_data['avg_per_flip']:.1f} ₽, {avg_calc_label})\n"
        f"⏱️ <b>Оценка времени (ETA):</b> <b>{goal_data['eta_text']}</b>\n"
        "───────────────────────────\n"
        "🏁 <b>Контрольные рубежи:</b>\n"
        f"• 25%: {'✅ Достигнут' if milestones.get(25) else '⏳ В процессе'}\n"
        f"• 50%: {'✅ Достигнут' if milestones.get(50) else '⏳ В процессе'}\n"
        f"• 75%: {'✅ Достигнут' if milestones.get(75) else '⏳ В процессе'}\n"
        f"• 100%: {'🏆 Выполнен!' if milestones.get(100) else '⏳ В процессе'}\n"
        "───────────────────────────\n"
        "Выберите быструю предустановку цели ниже или введите сумму вручную:"
    )


# ─────────────────────────────────────────────────────────────
# Bot Command Handlers
# ─────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    db.get_or_create_user(
        user_id=user_id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )
    status = flipper_engine.get_status_summary()
    text = format_dashboard_text(status, user_id)
    kb = main_dashboard_keyboard(status)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("status"))
async def cmd_status(message: Message):
    status = flipper_engine.get_status_summary()
    text = format_dashboard_text(status, message.from_user.id)
    kb = main_dashboard_keyboard(status)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("pnl"))
async def cmd_pnl(message: Message):
    pnl = db.get_pnl_stats()
    goal = db.get_goal_progress(message.from_user.id)
    text = format_pnl_text(pnl, goal)
    kb = pnl_keyboard()
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("inventory"))
async def cmd_inventory(message: Message):
    items = db.get_inventory_list(limit=10)
    if not items:
        text = "📦 <b>Склад пуст.</b> Пока не совершено ни одной покупки."
    else:
        text = "📦 <b>Текущие товары и перепродажи:</b>\n"
        for idx, item in enumerate(items[:6], 1):
            st = item["status"]
            st_str = "⏳ Выкуплен" if st == "bought" else "📋 Готов" if st == "ready_for_sale" else "🏷️ В продаже" if st == "listed" else "🛑 На паузе" if st == "emergency_paused" else "✅ Продан"
            c_id = item.get("category_id", "chatgpt")
            cat_obj = get_category_by_id(c_id)
            c_name = cat_obj.name if cat_obj else c_id
            text += f"\n<b>{idx}.</b> [{c_name}] {item.get('title', '')[:25]}...\n   • Статус: {st_str} | Закупка: {item['buy_price']} ₽ ➔ Продажа: {item['sell_price']} ₽\n"

    kb = inventory_keyboard(items)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("categories"))
async def cmd_categories(message: Message):
    enabled = db.get_enabled_categories()
    medians = flipper_engine.category_medians
    text = format_categories_text(enabled, medians)
    kb = categories_keyboard(enabled)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("boost"))
async def cmd_boost(message: Message):
    """
    /boost — Raises FunPay lots to top, activates 30-minute peak Turbo window,
    and coordinates background auto-raise upon cooldown expiry.
    """
    if not is_admin(message.from_user.id):
        await message.answer("⛔ <b>Доступ запрещен:</b> эта команда доступна только администраторам бота.", parse_mode="HTML")
        return

    await message.answer("🚀 <b>Запуск буста лотов на FunPay...</b>", parse_mode="HTML")
    res = await flipper_engine.execute_boost()
    rem_turbo = (res.get("scheduler") or {}).get("remaining_turbo_seconds") or 0
    kb = boost_keyboard(res.get("turbo_mode") is True, remaining_turbo=rem_turbo)
    await message.answer(format_boost_result(res), reply_markup=kb, parse_mode="HTML")


@router.message(Command("browser"))
async def cmd_browser(message: Message):
    """
    /browser — Inspects FunPay session via golden_key, checks Cloudflare status,
    breaks down available vs 48-hour hold balances, and measures network latency.
    """
    if not is_admin(message.from_user.id):
        await message.answer("⛔ <b>Доступ запрещен:</b> эта команда доступна только администраторам бота.", parse_mode="HTML")
        return

    status_msg = await message.answer("🌐 <b>Проверка сессии FunPay и сетевого подключения...</b>", parse_mode="HTML")
    info = await flipper_engine.client.get_account_info()

    text = format_browser_text(info)
    await edit_text_if_changed(status_msg, text, reply_markup=browser_keyboard(), parse_mode="HTML")


@router.message(Command("goal"))
async def cmd_goal(message: Message):
    """
    /goal [amount] — View or set profit financial target with dynamic ETA & milestone tracking.
    """
    if not is_admin(message.from_user.id):
        await message.answer("⛔ <b>Доступ запрещен:</b> эта команда доступна только администраторам бота.", parse_mode="HTML")
        return

    parts = message.text.split(maxsplit=1) if message.text else []
    if len(parts) == 2:
        try:
            val = parse_goal_amount(parts[1])
            db.set_profit_goal(val, message.from_user.id)
            await message.answer(f"🎯 <b>Новая цель прибыли установлена:</b> <code>{format_goal_amount(val)} ₽</code>", parse_mode="HTML")
        except ValueError:
            await message.answer("⚠️ Введите сумму от 100 до 10 000 000 ₽, максимум две цифры после запятой. Пример: <code>/goal 500 000</code>", parse_mode="HTML")
            return

    goal_data = db.get_goal_progress(message.from_user.id)
    text = format_goal_text(goal_data)
    kb = goal_keyboard(goal_data["goal"])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("emergency_stop"))
async def cmd_emergency_stop(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ <b>Доступ запрещен:</b> эта команда доступна только администраторам бота.", parse_mode="HTML")
        return

    res = await flipper_engine.stop_and_deactivate()
    count = res.get("deactivated_count", 0)
    text = (
        "🛑 <b>ЭКСТРЕННЫЙ СТОП ВЫПОЛНЕН!</b>\n\n"
        "• ⏸️ Авто-выкуп немедленно ПРИОСТАНОВЛЕН.\n"
        f"• 🔒 Подтверждено снятие объявлений (<code>{count}</code> шт.).\n"
        f"• Не подтверждено снятие: {res.get('failed_count', 0)}.\n\n"
        "Для возобновления нажмите кнопку ниже:"
    )
    kb = emergency_stop_keyboard()
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("resume"))
async def cmd_resume(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ <b>Доступ запрещен:</b> эта команда доступна только администраторам бота.", parse_mode="HTML")
        return

    res = flipper_engine.resume_from_emergency()
    count = int(res)
    unresolved_count = res.get("unresolved_intents_count", 0) if hasattr(res, "get") else 0
    status = flipper_engine.get_status_summary()

    warning_text = ""
    if unresolved_count > 0:
        warning_text = (
            f"\n⚠️ <b>ВНИМАНИЕ (СВЕРКА):</b> Обнаружено <b>{unresolved_count}</b> незавершенных намерений покупки (статус UNKNOWN/PENDING)!\n"
            f"Возобновление заблокировано до сверки заказов и списаний.\n"
        )

    text = (
        "▶️ <b>Проверка возобновления</b>\n\n"
        f"• 🔓 Автоматически восстановлено объявлений: <code>{count}</code> шт.\n"
        f"• ⚙️ Режим работы: <code>{status.get('mode', 'OBSERVE')}</code>\n"
        f"• 🟢 Среда: {'🧪 DRY RUN' if status['dry_run'] else '🟢 LIVE'}\n"
        f"• 💰 Бюджет: <code>{int(status['max_budget'])} ₽</code>\n"
        f"{warning_text}"
    )
    kb = main_dashboard_keyboard(status)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("mode"))
async def cmd_mode(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ <b>Доступ запрещен:</b> эта команда доступна только администраторам бота.", parse_mode="HTML")
        return

    current_mode = flipper_engine.mode
    kb = mode_keyboard(current_mode)
    text = (
        "⚙️ <b>Выбор режима работы бота-флипера</b>\n\n"
        "• <b>OBSERVE:</b> Только сбор и карточки кандидатов, без внешних покупок и изменений.\n"
        "• <b>ASSIST:</b> Расчет сделок, приёмка, ручные решения перед покупкой.\n"
        "• <b>LIMITED_AUTO:</b> Автоматический выкуп и перепродажа в рамках лимитов.\n"
        "• <b>PAUSED:</b> Полная приостановка всех торговых операций.\n\n"
        f"Текущий режим: <code>{current_mode}</code>"
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ <b>Доступ запрещен:</b> эта команда доступна только администраторам бота.", parse_mode="HTML")
        return

    await state.clear()
    status = flipper_engine.get_status_summary()
    kb = settings_keyboard(status)
    text = (
        "⚙️ <b>Параметры и настройки бота-флипера</b>\n\n"
        f"• Режим: {'🧪 DRY RUN' if status['dry_run'] else '🟢 LIVE'}\n"
        f"• Авто-выкуп: {'ВКЛ ✅' if status['auto_buy'] else 'ВЫКЛ ⏸️'}\n"
        f"• Доп. лимит покупки (0 = авто): <code>{int(status['max_budget'])} ₽</code>\n"
        f"• Минимальный профит: <code>{int(status['min_profit'])} ₽</code>\n"
        f"• Порог перепродажи: <code>{int(status['target_resale_price'])} ₽</code>\n"
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "<b>Перепродажа: рабочий пилот ASSIST</b>\n\n"
        "/candidates — наблюдения и проверка выкупа\n"
        "/review — точный SKU, заявка покупателя и расходы\n"
        "/seed — один начальный взнос до 1 000 ₽\n"
        "/cash — подтвердить доступные деньги\n"
        "/capital — капитал, открытый риск и просадка\n"
        "/prepare — зарезервировать одну ручную покупку\n"
        "/resolve_purchase — сверить её оплату\n"
        "/asset_intake — подтвердить полученный предмет\n"
        "/manual_exit — записать выполненную передачу\n"
        "/settle /expense /refund /writeoff — денежный результат\n"
        "/deadlines — сроки незавершённого оборота\n"
        "/mode /emergency_stop — режим и остановка\n\n"
        "Подписки исключены. Прибыль считается после сверки денег. "
        "Покупка без свежей проверенной заявки на выкуп блокируется. "
        "Инструкция: auto_flipper/PILOT_RUNBOOK.md"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Главное меню", callback_data="flip_main")]])
    await message.answer(help_text, reply_markup=kb, parse_mode="HTML")


# ─────────────────────────────────────────────────────────────
# Callback Queries
# ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "flip_main")
async def cb_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    status = flipper_engine.get_status_summary()
    text = format_dashboard_text(status, callback.from_user.id)
    kb = main_dashboard_keyboard(status)
    await edit_text_if_changed(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "flip_refresh_dash")
async def cb_refresh_dash(callback: CallbackQuery):
    status = flipper_engine.get_status_summary()
    text = format_dashboard_text(status, callback.from_user.id)
    kb = main_dashboard_keyboard(status)
    changed = await edit_text_if_changed(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("🔄 Данные обновлены" if changed else "Данные без изменений")


@router.callback_query(F.data == "flip_pnl")
async def cb_pnl(callback: CallbackQuery):
    pnl = db.get_pnl_stats()
    goal = db.get_goal_progress(callback.from_user.id)
    text = format_pnl_text(pnl, goal)
    kb = pnl_keyboard()
    await edit_text_if_changed(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "flip_inventory")
async def cb_inventory(callback: CallbackQuery):
    items = db.get_inventory_list(limit=10)
    if not items:
        text = "📦 <b>Склад пуст.</b> Пока не совершено ни одной покупки."
    else:
        text = "📦 <b>Текущие товары и перепродажи:</b>\n"
        for idx, item in enumerate(items[:6], 1):
            st = item["status"]
            st_str = "⏳ Выкуплен" if st == "bought" else "📋 Готов" if st == "ready_for_sale" else "🏷️ В продаже" if st == "listed" else "🛑 На паузе" if st == "emergency_paused" else "✅ Продан"
            c_id = item.get("category_id", "chatgpt")
            cat_obj = get_category_by_id(c_id)
            c_name = cat_obj.name if cat_obj else c_id
            text += f"\n<b>{idx}.</b> [{c_name}] {item.get('title', '')[:25]}...\n   • Статус: {st_str} | {item['buy_price']} ➔ {item['sell_price']} ₽\n"

    kb = inventory_keyboard(items)
    await edit_text_if_changed(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("flip_item_"))
async def cb_item_detail(callback: CallbackQuery):
    item_uuid = callback.data.replace("flip_item_", "")
    item = db.get_inventory_item(item_uuid)
    if not item:
        await callback.answer("Лот не найден", show_alert=True)
        return

    c_id = item.get("category_id", "chatgpt")
    cat_obj = get_category_by_id(c_id)
    cat_name = cat_obj.name if cat_obj else c_id

    text = (
        f"📦 <b>Детали лота {item['item_uuid']}</b>\n\n"
        f"📁 <b>Категория:</b> {cat_name} (Раздел #{item.get('node_id', '—')})\n"
        f"🏷 <b>Название:</b> {item.get('title')}\n"
        f"Статус: <b>{item['status']}</b>\n"
        f"💵 Закупка: <code>{item['buy_price']} ₽</code>\n"
        f"🏷️ Продажа: <code>{item['sell_price']} ₽</code>\n"
        f"💎 Прибыль при исполнении исходной заявки: <code>{item['net_profit_expected']} ₽</code>\n"
        f"💎 Реализованный профит: <code>+{item['net_profit_realized']} ₽</code>\n"
        f"💳 Заказ покупки: <code>{item.get('order_id') or '—'}</code>\n"
        f"🏷️ Лот FunPay: <code>{item.get('resale_lot_id') or '—'}</code>\n"
        f"👤 Покупатель: <code>{item.get('buyer_username') or '—'}</code>\n"
    )
    kb = item_detail_keyboard(item_uuid)
    await edit_text_if_changed(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "flip_boost")
async def cb_boost_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен (только для администраторов)", show_alert=True)
        return

    sched = flipper_engine.boost_scheduler.get_scheduler_info()
    rem_turbo = sched.get("remaining_turbo_seconds", 0)
    rem_raise = sched.get("remaining_raise_seconds")

    if rem_raise and rem_raise > 0:
        if rem_raise >= 3600:
            h = rem_raise // 3600
            m = (rem_raise % 3600) // 60
            raise_str = f"через {h} ч {m} мин" if m > 0 else f"через {h} ч"
        else:
            raise_str = f"через {max(1, rem_raise // 60)} мин"
    else:
        raise_str = "Готово к поднятию 🟢"

    turbo_status = f"ВКЛЮЧЕН ⚡ (осталось {max(1, rem_turbo // 60)} мин)" if flipper_engine.turbo_mode and rem_turbo > 0 else ("ВКЛЮЧЕН ⚡" if flipper_engine.turbo_mode else "ВЫКЛЮЧЕН ⏳")

    kb = boost_keyboard(flipper_engine.turbo_mode, remaining_turbo=rem_turbo)
    text = (
        "🚀 <b>Центр управления бустом лотов (FunPay Boost)</b>\n\n"
        "• <b>Поднятие лотов (Raise):</b> Поднимает ваши активные объявления ChatGPT Plus в топ поисковой выдачи FunPay.\n"
        "• <b>Турбо-режим:</b> Автоматически активируется на 30 мин после буста (интервал 6 сек) для максимального захвата покупателей.\n\n"
        f"• Статус Турбо: <b>{turbo_status}</b>\n"
        f"• Следующее авто-поднятие: <b>{raise_str}</b>"
    )
    await edit_text_if_changed(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "flip_raise_now")
async def cb_raise_now(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен (только для администраторов)", show_alert=True)
        return

    await callback.answer("🚀 Выполняю поднятие лотов...", show_alert=False)
    res = await flipper_engine.execute_boost()
    await callback.message.answer(format_boost_result(res), parse_mode="HTML")


@router.callback_query(F.data == "flip_toggle_turbo")
async def cb_toggle_turbo(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен (только для администраторов)", show_alert=True)
        return

    val = flipper_engine.toggle_turbo_mode()
    state_str = "ВКЛЮЧЕН ⚡ (6 сек)" if val else "ВЫКЛЮЧЕН ⏳ (25 сек)"
    await callback.answer(f"Турбо-режим: {state_str}", show_alert=True)
    status = flipper_engine.get_status_summary()
    await edit_text_if_changed(callback.message, format_dashboard_text(status, callback.from_user.id), reply_markup=main_dashboard_keyboard(status), parse_mode="HTML")


@router.callback_query(F.data == "flip_browser")
async def cb_browser_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен (только для администраторов)", show_alert=True)
        return

    info = await flipper_engine.client.get_account_info()
    text = format_browser_text(info)
    await edit_text_if_changed(callback.message, text, reply_markup=browser_keyboard(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "flip_goal")
async def cb_goal_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен (только для администраторов)", show_alert=True)
        return

    goal_data = db.get_goal_progress(callback.from_user.id)
    text = format_goal_text(goal_data)
    kb = goal_keyboard(goal_data["goal"])
    await edit_text_if_changed(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("flip_set_goal_"))
async def cb_set_goal_preset(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен (только для администраторов)", show_alert=True)
        return

    try:
        amount = parse_goal_amount(callback.data.replace("flip_set_goal_", "", 1))
    except ValueError:
        await callback.answer("⚠️ Некорректная цель", show_alert=True)
        return
    db.set_profit_goal(amount, callback.from_user.id)
    await callback.answer(f"🎯 Цель установлена: {format_goal_amount(amount)} ₽", show_alert=True)
    goal_data = db.get_goal_progress(callback.from_user.id)
    kb = goal_keyboard(goal_data["goal"])
    await edit_text_if_changed(callback.message, format_goal_text(goal_data), reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "flip_custom_goal")
async def cb_custom_goal_prompt(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен (только для администраторов)", show_alert=True)
        return

    await state.set_state(GoalInputState.waiting_for_goal)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Отмена", callback_data="flip_goal")]])
    await edit_text_if_changed(callback.message, "✍️ <b>Введите желаемую цель прибыли в рублях:</b>\nНапример: <code>15000</code>", reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(GoalInputState.waiting_for_goal)
async def handle_custom_goal_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("⛔ Доступ запрещен.", parse_mode="HTML")
        return

    if not message.text:
        return
    try:
        val = parse_goal_amount(message.text)
        db.set_profit_goal(val, message.from_user.id)
        await state.clear()
        await message.answer(f"✅ <b>Цель успешно сохранена:</b> <code>{format_goal_amount(val)} ₽</code>", parse_mode="HTML")
        status = flipper_engine.get_status_summary()
        await message.answer(format_dashboard_text(status, message.from_user.id), reply_markup=main_dashboard_keyboard(status), parse_mode="HTML")
    except ValueError:
        await message.answer("⚠️ Пожалуйста, введите корректное число (например: 7500):")


@router.callback_query(F.data == "flip_emergency_stop")
async def cb_emergency_stop(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен (только для администраторов)", show_alert=True)
        return

    res = await flipper_engine.stop_and_deactivate()
    count = res.get("deactivated_count", 0)
    text = (
        "🛑 <b>ЭКСТРЕННЫЙ СТОП ВЫПОЛНЕН!</b>\n\n"
        "• ⏸️ Авто-выкуп немедленно ПРИОСТАНОВЛЕН.\n"
        f"• 🔒 Деактивировано активных лотов: <code>{count}</code> шт.\n"
        f"• Не подтверждено снятие: {res.get('failed_count', 0)}.\n\n"
        "Для возобновления нажмите кнопку ниже:"
    )
    kb = emergency_stop_keyboard()
    await edit_text_if_changed(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("🛑 Экстренный СТОП выполнен!", show_alert=True)


@router.callback_query(F.data == "flip_resume")
async def cb_resume(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен (только для администраторов)", show_alert=True)
        return

    res = flipper_engine.resume_from_emergency()
    count = int(res)
    unresolved_count = res.get("unresolved_intents_count", 0) if hasattr(res, "get") else 0
    alert_msg = "Пауза сохраняется: нужна сверка" if unresolved_count else "Включено наблюдение; объявления остаются на паузе"
    if unresolved_count > 0:
        alert_msg += f"\n⚠️ Сверка: {unresolved_count} незавершенных платежей UNKNOWN!"
    await callback.answer(alert_msg, show_alert=True)
    status = flipper_engine.get_status_summary()
    await edit_text_if_changed(callback.message, format_dashboard_text(status, callback.from_user.id), reply_markup=main_dashboard_keyboard(status), parse_mode="HTML")


@router.callback_query(F.data == "flip_mode_menu")
async def cb_mode_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен (только для администраторов)", show_alert=True)
        return

    current_mode = flipper_engine.mode
    kb = mode_keyboard(current_mode)
    text = (
        "⚙️ <b>Выбор режима работы бота-флипера</b>\n\n"
        "• <b>OBSERVE:</b> Только сбор и карточки кандидатов, без внешних покупок и изменений.\n"
        "• <b>ASSIST:</b> Расчет сделок, приёмка, ручные решения перед покупкой.\n"
        "• <b>LIMITED_AUTO:</b> Автоматический выкуп и перепродажа в рамках лимитов.\n"
        "• <b>PAUSED:</b> Полная приостановка всех торговых операций.\n\n"
        f"Текущий режим: <code>{current_mode}</code>"
    )
    await edit_text_if_changed(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("flip_set_mode_"))
async def cb_set_mode(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен (только для администраторов)", show_alert=True)
        return

    mode_target = callback.data.replace("flip_set_mode_", "").upper()
    new_mode = flipper_engine.set_mode(mode_target)
    await callback.answer(f"✅ Режим переключен: {new_mode}", show_alert=True)
    kb = mode_keyboard(new_mode)
    text = (
        "⚙️ <b>Выбор режима работы бота-флипера</b>\n\n"
        "• <b>OBSERVE:</b> Только сбор и карточки кандидатов, без внешних покупок и изменений.\n"
        "• <b>ASSIST:</b> Расчет сделок, приёмка, ручные решения перед покупкой.\n"
        "• <b>LIMITED_AUTO:</b> Автоматический выкуп и перепродажа в рамках лимитов.\n"
        "• <b>PAUSED:</b> Полная приостановка всех торговых операций.\n\n"
        f"Текущий режим: <code>{new_mode}</code>"
    )
    await edit_text_if_changed(callback.message, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "flip_settings")
async def cb_settings_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен (только для администраторов)", show_alert=True)
        return

    status = flipper_engine.get_status_summary()
    kb = settings_keyboard(status)
    text = (
        "⚙️ <b>Настройки авто-флипера</b>\n\n"
        f"• <b>Режим:</b> {'🧪 DRY RUN (Безопасный)' if status['dry_run'] else '🟢 LIVE (Реальные деньги)'}\n"
        f"• <b>Авто-выкуп:</b> {'ВКЛ ✅' if status['auto_buy'] else 'ВЫКЛ ⏸️'}\n"
        f"• <b>Доп. лимит покупки (0 = авто):</b> <code>{int(status['max_budget'])} ₽</code>\n"
        f"• <b>Минимальный профит:</b> <code>{int(status['min_profit'])} ₽</code>\n"
    )
    await edit_text_if_changed(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "flip_toggle_dry")
async def cb_toggle_dry(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен (только для администраторов)", show_alert=True)
        return

    val = flipper_engine.toggle_dry_run()
    msg = "🧪 Режим DRY RUN (Симуляция)" if val else "🟢 Режим LIVE (Реальный баланс)"
    await callback.answer(msg, show_alert=True)
    status = flipper_engine.get_status_summary()
    await callback.message.edit_reply_markup(reply_markup=settings_keyboard(status))


@router.callback_query(F.data == "flip_toggle_buy")
async def cb_toggle_buy(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен (только для администраторов)", show_alert=True)
        return

    val = flipper_engine.toggle_auto_buy()
    msg = "🤖 Авто-выкуп ВКЛЮЧЕН!" if val else "⏸️ Авто-выкуп выключен"
    await callback.answer(msg, show_alert=True)
    status = flipper_engine.get_status_summary()
    await callback.message.edit_reply_markup(reply_markup=settings_keyboard(status))


@router.callback_query(F.data == "flip_edit_budget")
async def cb_edit_budget(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен (только для администраторов)", show_alert=True)
        return

    kb = budget_keyboard(flipper_engine.max_budget)
    text = (
        f"💰 <b>Настройка бюджета покупки лота</b>\n\n"
        f"Текущий лимит: <code>{int(flipper_engine.max_budget)} ₽</code>\n"
        "Бот не покупает аккаунты дороже этой суммы.\n\n"
        "Выберите сумму:"
    )
    await edit_text_if_changed(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("flip_preset_budget_"))
async def cb_preset_budget(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен (только для администраторов)", show_alert=True)
        return

    val = float(callback.data.replace("flip_preset_budget_", ""))
    flipper_engine.set_max_budget(val)
    await callback.answer(f"✅ Бюджет: {int(val)} ₽", show_alert=True)
    status = flipper_engine.get_status_summary()
    await edit_text_if_changed(callback.message, format_dashboard_text(status, callback.from_user.id), reply_markup=main_dashboard_keyboard(status), parse_mode="HTML")


@router.callback_query(F.data == "flip_custom_budget")
async def cb_custom_budget_prompt(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен (только для администраторов)", show_alert=True)
        return

    await state.set_state(BudgetInputState.waiting_for_budget)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Отмена", callback_data="flip_settings")]])
    await edit_text_if_changed(callback.message, "✍️ <b>Введите лимит бюджета на выкуп в рублях:</b>\nНапример: <code>350</code>", reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(BudgetInputState.waiting_for_budget)
async def handle_custom_budget_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("⛔ Доступ запрещен.", parse_mode="HTML")
        return

    if not message.text:
        return
    try:
        val = float(message.text.strip().replace(" ", "").replace(",", "."))
        if 0 <= val <= 50000:
            flipper_engine.set_max_budget(val)
            await state.clear()
            await message.answer(f"✅ <b>Бюджет сохранен:</b> <code>{int(val)} ₽</code>", parse_mode="HTML")
            status = flipper_engine.get_status_summary()
            await message.answer(format_dashboard_text(status, message.from_user.id), reply_markup=main_dashboard_keyboard(status), parse_mode="HTML")
        else:
            await message.answer("⚠️ Введите число от 0 до 50 000 ₽ (0 = динамический лимит):")
    except ValueError:
        await message.answer("⚠️ Введите целое число:")


@router.callback_query(F.data == "flip_edit_profit")
async def cb_edit_profit(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен (только для администраторов)", show_alert=True)
        return

    await state.set_state(ProfitInputState.waiting_for_profit)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Отмена", callback_data="flip_settings")]])
    await edit_text_if_changed(callback.message, "✍️ <b>Введите минимальный чистый профит со сделки в рублях:</b>\nНапример: <code>150</code>", reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(ProfitInputState.waiting_for_profit)
async def handle_custom_profit_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("⛔ Доступ запрещен.", parse_mode="HTML")
        return

    if not message.text:
        return
    try:
        val = float(message.text.strip().replace(" ", "").replace(",", "."))
        if 50 <= val <= 20000:
            flipper_engine.set_min_profit(val)
            await state.clear()
            await message.answer(f"✅ <b>Минимальный профит сохранен:</b> <code>{int(val)} ₽</code>", parse_mode="HTML")
            status = flipper_engine.get_status_summary()
            await message.answer(format_dashboard_text(status, message.from_user.id), reply_markup=main_dashboard_keyboard(status), parse_mode="HTML")
        else:
            await message.answer("⚠️ Введите число от 50 до 20 000 ₽:")
    except ValueError:
        await message.answer("⚠️ Введите целое число:")


@router.callback_query(F.data == "flip_edit_key")
async def cb_edit_key_prompt(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен (только для администраторов)", show_alert=True)
        return

    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Назад в настройки", callback_data="flip_settings")]])
    await edit_text_if_changed(callback.message,
        "🔑 <b>Настройка Golden Key для FunPay</b>\n\n"
        "Секрет задаётся исключительно локально через переменную окружения <code>FUNPAY_GOLDEN_KEY</code> "
        "(в файле <code>.env</code> или окружении системы).\n\n"
        "⚠️ <b>В целях безопасности отправка golden_key через Telegram запрещена.</b> "
        "Никогда не отправляйте секретные ключи сессии в чат.",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(KeyInputState.waiting_for_key)
async def handle_key_input(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "⛔ <b>Отправка golden_key через Telegram отключена в целях безопасности.</b>\n\n"
        "Секрет задаётся локально через переменную окружения <code>FUNPAY_GOLDEN_KEY</code> (в файле <code>.env</code>). "
        "Бот не сохраняет секреты из сообщений в базу данных.",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────────────────────
# Multi-Category Management Handlers
# ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "flip_categories")
async def cb_categories_menu(callback: CallbackQuery):
    enabled = db.get_enabled_categories()
    medians = flipper_engine.category_medians
    text = format_categories_text(enabled, medians)
    kb = categories_keyboard(enabled)
    await edit_text_if_changed(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("flip_toggle_cat_"))
async def cb_toggle_category(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен (только для администраторов)", show_alert=True)
        return

    cat_id = callback.data.replace("flip_toggle_cat_", "")
    cat = get_category_by_id(cat_id)
    if not cat:
        await callback.answer("Категория не найдена", show_alert=True)
        return
    is_now_enabled = db.toggle_category_enabled(cat_id)
    state_str = "ВКЛЮЧЕНА ✅" if is_now_enabled else "ВЫКЛЮЧЕНА ❌"
    await callback.answer(f"{cat.name}: {state_str}")

    enabled = db.get_enabled_categories()
    medians = flipper_engine.category_medians
    text = format_categories_text(enabled, medians)
    kb = categories_keyboard(enabled)
    await edit_text_if_changed(callback.message, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "flip_cat_enable_all")
async def cb_enable_all_categories(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен (только для администраторов)", show_alert=True)
        return

    from auto_flipper.safety import EXCLUDED_CATEGORIES
    for cat_id, cat in CATEGORY_REGISTRY.items():
        enabled = not getattr(cat, 'is_deprecated', False) and cat_id not in EXCLUDED_CATEGORIES
        db.set_category_enabled(cat_id, enabled)
    await callback.answer("✅ Все категории успешно включены!", show_alert=True)
    enabled = db.get_enabled_categories()
    medians = flipper_engine.category_medians
    text = format_categories_text(enabled, medians)
    kb = categories_keyboard(enabled)
    await edit_text_if_changed(callback.message, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "flip_pnl_categories")
async def cb_pnl_categories(callback: CallbackQuery):
    pnl = db.get_pnl_stats()
    text = format_pnl_by_categories_text(pnl)
    kb = pnl_categories_keyboard()
    await edit_text_if_changed(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()
