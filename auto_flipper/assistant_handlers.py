"""Admin tools for inspecting evidence and explicitly confirming one operation."""
import contextlib
import json
import sqlite3
from html import escape
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from auto_flipper.access import AdminAccess
from auto_flipper.database import db
from auto_flipper.flipper_engine import flipper_engine

router = Router()
router.message.outer_middleware(AdminAccess())
router.callback_query.outer_middleware(AdminAccess())


@router.message(Command('alerts'))
async def alerts(message: Message):
    parts = message.text.split()
    if len(parts) != 2 or parts[1].lower() not in ('on', 'off'):
        await message.answer('/alerts on или /alerts off — уведомления о новых наблюдениях, не чаще раза в 5 минут.')
        return
    enabled = parts[1].lower() == 'on'
    db.set_setting('candidate_alerts', '1' if enabled else '0')
    await message.answer('Уведомления включены. Объявление не является доказательством спроса.' if enabled else 'Уведомления выключены.')


def get_liquidity_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔝 TOP-10 Ликвидных", callback_data="liquidity:top"),
                InlineKeyboardButton(text="🔻 BOTTOM Неликвид", callback_data="liquidity:bottom"),
            ],
            [
                InlineKeyboardButton(text="🔑 Mann Co. Key", callback_data="liquidity:key"),
                InlineKeyboardButton(text="🎫 Duty Ticket", callback_data="liquidity:ticket"),
            ],
            [
                InlineKeyboardButton(text="🔄 Обновить сводку", callback_data="liquidity:summary"),
            ],
        ]
    )


def get_liquidity_sub_keyboard(current: str) -> InlineKeyboardMarkup:
    buttons = []
    row1 = []
    if current != "top":
        row1.append(InlineKeyboardButton(text="🔝 TOP-10", callback_data="liquidity:top"))
    if current != "bottom":
        row1.append(InlineKeyboardButton(text="🔻 BOTTOM", callback_data="liquidity:bottom"))
    if row1:
        buttons.append(row1)

    row2 = []
    if current != "key":
        row2.append(InlineKeyboardButton(text="🔑 Key", callback_data="liquidity:key"))
    if current != "ticket":
        row2.append(InlineKeyboardButton(text="🎫 Ticket", callback_data="liquidity:ticket"))
    row2.append(InlineKeyboardButton(text="📊 Меню", callback_data="liquidity:summary"))
    buttons.append(row2)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_liquidity_summary(db_inst) -> tuple[str, InlineKeyboardMarkup]:
    from auto_flipper.liquidity import evaluate_sku_liquidity
    from auto_flipper.sku_matcher import BENCHMARK_SKUS, BENCHMARK_SKU_NAMES

    blocks = []
    for sku in BENCHMARK_SKUS:
        assessment = evaluate_sku_liquidity(db_inst, sku)
        display_name = BENCHMARK_SKU_NAMES.get(sku, sku)
        if not assessment:
            blocks.append(
                f"<b>{escape(display_name)}</b>\n"
                f"Score: N/A · Confidence: LOW\n"
                f"<i>История пока не накоплена. Включите OBSERVE.</i>"
            )
            continue

        m = assessment.metrics
        hours = int(m.observation_duration_hours)
        minutes = int((m.observation_duration_hours - hours) * 60)
        dur_str = f"{hours}h {minutes:02d}m" if hours > 0 else f"{minutes}m"

        icon = "🔑" if "5021" in sku else "🎫"
        blocks.append(
            f"{icon} <b>{escape(assessment.sku_name)}</b>\n"
            f"Score: <b>{assessment.score:.0f}/100</b>\n"
            f"Confidence: <b>{escape(assessment.confidence)}</b>\n"
            f"Active lots: {m.active_listings} · Sellers: {m.unique_sellers}"
            + (f" · Stock: {m.observed_stock}" if m.observed_stock > 0 else "") + "\n"
            f"P25: {m.p25_price:.2f} ₽ · Median: {m.p50_price:.2f} ₽\n"
            f"Turnover proxy: {m.turnover_proxy:.1f}/h · Reappearance: {m.reappearance_rate:.1f}/h\n"
            f"Observed: {dur_str} ({m.sample_count} samples)"
        )

    response = "📊 <b>Ликвидность рынка (Benchmark SKUs)</b>\n\n" + "\n\n".join(blocks)
    response += (
        "\n\n<b>Быстрые команды (кликабельные):</b>\n"
        "• /liquidity_top — TOP ликвидных товаров\n"
        "• /liquidity_bottom — BOTTOM неликвидных товаров\n"
        "• /liquidity_key — детальный профиль Mann Co. Key\n"
        "• /liquidity_ticket — детальный профиль Tour of Duty Ticket\n\n"
        "<i>Или нажимайте интерактивные кнопки ниже:</i>"
    )
    return response, get_liquidity_menu_keyboard()


def build_liquidity_top(db_inst, limit: int = 10) -> tuple[str, InlineKeyboardMarkup]:
    from auto_flipper.discovery import rank_market_liquidity

    ranking = rank_market_liquidity(db_inst)
    top_items = ranking["top_candidates"][:limit]
    if not top_items:
        text = (
            "📊 <b>Кандидаты для рейтинга пока не найдены.</b>\n"
            "<i>Накопите историю наблюдений через OBSERVE (минимум 3 лота, 3 продавца).</i>"
        )
        return text, get_liquidity_sub_keyboard("top")

    lines = [
        f"🏆 <b>TOP-{len(top_items)} Ликвидных товаров (TF2)</b>\n"
        f"<i>Групп найдено: {ranking['total_discovery_groups_found']} · Прошло фильтры: {ranking['qualified_groups_count']}</i>\n"
    ]
    for idx, c in enumerate(top_items, start=1):
        ref = " [REF]" if c.is_benchmark else ""
        icon = "🔑" if "5021" in c.canonical_sku else ("🎫" if "725" in c.canonical_sku else "📦")
        hours = int(c.observation_duration_hours)
        mins = int((c.observation_duration_hours - hours) * 60)
        dur = f"{hours}h {mins:02d}m" if hours > 0 else f"{mins}m"
        lines.append(
            f"{idx}. {icon} <b>{escape(c.display_name)}</b>{ref}\n"
            f"   Score: <b>{c.liquidity_score:.1f}/100</b> · Conf: <b>{escape(c.confidence)}</b>\n"
            f"   Лотов: {c.active_lots} · Продавцов: {c.unique_sellers} · P25/P50: {c.p25_price:.1f}/{c.p50_price:.1f} ₽ · Depth: {c.competition_depth_bottom}\n"
            f"   Turnover: {c.turnover_proxy:.1f}/h · Reappear: {c.reappearance_rate:.1f}/h · Vol: {c.price_volatility:.1%} · Набл: {dur}"
        )
    lines.append("\n<i>[REF] — benchmark эталон (только контроль, без бонусов).</i>")
    return "\n\n".join(lines), get_liquidity_sub_keyboard("top")


def build_liquidity_bottom(db_inst, limit: int = 10) -> tuple[str, InlineKeyboardMarkup]:
    from auto_flipper.discovery import rank_market_liquidity

    ranking = rank_market_liquidity(db_inst)
    bottom_items = ranking["bottom_candidates"][:limit]
    if not bottom_items:
        text = (
            "📊 <b>Кандидаты для рейтинга пока не найдены.</b>\n"
            "<i>Накопите историю наблюдений через OBSERVE.</i>"
        )
        return text, get_liquidity_sub_keyboard("bottom")

    lines = [
        f"📉 <b>BOTTOM-{len(bottom_items)} Наименее ликвидных товаров (TF2)</b>\n"
        f"<i>Групп найдено: {ranking['total_discovery_groups_found']} · Прошло фильтры: {ranking['qualified_groups_count']}</i>\n"
    ]
    for idx, c in enumerate(bottom_items, start=1):
        ref = " [REF]" if c.is_benchmark else ""
        icon = "🔑" if "5021" in c.canonical_sku else ("🎫" if "725" in c.canonical_sku else "📦")
        hours = int(c.observation_duration_hours)
        mins = int((c.observation_duration_hours - hours) * 60)
        dur = f"{hours}h {mins:02d}m" if hours > 0 else f"{mins}m"
        lines.append(
            f"{idx}. {icon} <b>{escape(c.display_name)}</b>{ref}\n"
            f"   Score: <b>{c.liquidity_score:.1f}/100</b> · Conf: <b>{escape(c.confidence)}</b>\n"
            f"   Лотов: {c.active_lots} · Продавцов: {c.unique_sellers} · P25/P50: {c.p25_price:.1f}/{c.p50_price:.1f} ₽ · Depth: {c.competition_depth_bottom}\n"
            f"   Turnover: {c.turnover_proxy:.1f}/h · Reappear: {c.reappearance_rate:.1f}/h · Vol: {c.price_volatility:.1%} · Набл: {dur}"
        )
    lines.append("\n<i>[REF] — benchmark эталон (только контроль, без бонусов).</i>")
    return "\n\n".join(lines), get_liquidity_sub_keyboard("bottom")


def build_liquidity_sku(db_inst, target_arg: str) -> tuple[str, InlineKeyboardMarkup]:
    from auto_flipper.liquidity import evaluate_sku_liquidity
    from auto_flipper.sku_matcher import SKU_TF2_KEY, SKU_TF2_TICKET

    sku = None
    cur = "key"
    if target_arg in (
        SKU_TF2_KEY, "key", "keys", "5021", "5021;6", "mann co", "mann",
        "ключ", "ключи", "манн", "манн ко", "манко"
    ):
        sku = SKU_TF2_KEY
        cur = "key"
    elif target_arg in (
        SKU_TF2_TICKET, "ticket", "tickets", "725", "725;6", "tour",
        "билет", "билеты", "тикет", "тикеты", "мвм", "mvm"
    ):
        sku = SKU_TF2_TICKET
        cur = "ticket"
    else:
        sku = target_arg
        cur = "custom"

    assessment = evaluate_sku_liquidity(db_inst, sku)
    if not assessment:
        text = f"По товару <code>{escape(sku)}</code> история рынка пока не накоплена."
        return text, get_liquidity_sub_keyboard(cur)

    m = assessment.metrics
    hours = int(m.observation_duration_hours)
    minutes = int((m.observation_duration_hours - hours) * 60)
    dur_str = f"{hours}h {minutes:02d}m" if hours > 0 else f"{minutes}m"

    text = (
        f"📊 <b>Детальный профиль ликвидности: {escape(assessment.sku_name)}</b>\n"
        f"SKU: <code>{escape(m.canonical_sku)}</code>\n\n"
        f"🏆 <b>Liquidity Score: {assessment.score:.1f}/100</b>\n"
        f"🛡️ <b>Confidence: {escape(assessment.confidence)}</b>\n"
        f"<i>{escape('; '.join(assessment.confidence_reasons))}</i>\n\n"
        f"<b>Метрики стакана:</b>\n"
        f"• Активных лотов: {m.active_listings}\n"
        f"• Уникальных продавцов: {m.unique_sellers}\n"
        f"• Наблюдаемый остаток (stock): {m.observed_stock}\n"
        f"• Мин / P10 / P25: {m.min_price:.2f} / {m.p10_price:.2f} / {m.p25_price:.2f} ₽\n"
        f"• Медиана (P50) / P90 / Макс: {m.p50_price:.2f} / {m.p90_price:.2f} / {m.max_price:.2f} ₽\n"
        f"• Дисперсия цен (P90-P10)/P50: {m.price_dispersion:.2%}\n"
        f"• Волатильность P50: {m.price_volatility:.2%}\n\n"
        f"<b>Динамика и оборачиваемость:</b>\n"
        f"• Turnover proxy: {m.turnover_proxy:.1f}/h (net disappearances)\n"
        f"• Исчезновений (disappeared): {m.disappearance_count} ({m.disappearance_rate:.1f}/h)\n"
        f"• Возвращений (reappeared): {m.reappearance_count} ({m.reappearance_rate:.1f}/h)\n"
        f"• Новых лотов (new): {m.new_listings_count} ({m.new_listings_rate:.1f}/h)\n"
        f"• Средний возраст лота: {m.median_listing_age_hours:.1f}h\n"
        f"• Глубина у дна рынка (до +5%): {m.competition_depth_bottom} лотов ({m.competition_depth_sellers} продавцов)\n\n"
        f"<b>Суб-оценки (0..100):</b>\n"
        f"• Turnover: {assessment.sub_scores['turnover']}\n"
        f"• Стабильность: {assessment.sub_scores['stability']}\n"
        f"• Узость спреда: {assessment.sub_scores['dispersion']}\n"
        f"• Чистота уходов: {assessment.sub_scores['reappearance']}\n"
        f"• Конкуренция: {assessment.sub_scores['competition']}\n\n"
        f"⏱ История: {dur_str} ({m.sample_count} сэмплов)"
    )
    return text, get_liquidity_sub_keyboard(cur)


@router.message(Command('liquidity', 'liquidity_top', 'liquidity_bottom', 'liquidity_key', 'liquidity_ticket'))
async def cmd_liquidity(message: Message):
    if not message.text:
        return
    raw_text = message.text.strip()
    cmd_token = raw_text.split()[0].lower() if raw_text else ""
    clean_cmd = cmd_token.lstrip("/").split("@")[0]

    parts = raw_text.split(maxsplit=2)
    subcmd = parts[1].strip().lower() if len(parts) > 1 else ""

    # 1. /liquidity_top или /liquidity top [N]
    if clean_cmd == "liquidity_top" or subcmd == "top":
        limit = 10
        if len(parts) > 2:
            try:
                limit = int(parts[2].strip())
            except ValueError:
                limit = 10
        limit = max(1, min(25, limit))
        text, kb = build_liquidity_top(db, limit=limit)
        await message.answer(text, reply_markup=kb)
        return

    # 2. /liquidity_bottom или /liquidity bottom [N]
    if clean_cmd == "liquidity_bottom" or subcmd == "bottom":
        limit = 10
        if len(parts) > 2:
            try:
                limit = int(parts[2].strip())
            except ValueError:
                limit = 10
        limit = max(1, min(25, limit))
        text, kb = build_liquidity_bottom(db, limit=limit)
        await message.answer(text, reply_markup=kb)
        return

    # 3. /liquidity_key
    if clean_cmd == "liquidity_key":
        text, kb = build_liquidity_sku(db, "key")
        await message.answer(text, reply_markup=kb)
        return

    # 4. /liquidity_ticket
    if clean_cmd == "liquidity_ticket":
        text, kb = build_liquidity_sku(db, "ticket")
        await message.answer(text, reply_markup=kb)
        return

    # 5. /liquidity <аргумент>
    if subcmd:
        text, kb = build_liquidity_sku(db, subcmd)
        await message.answer(text, reply_markup=kb)
        return

    # 6. /liquidity — общая сводка и меню
    text, kb = build_liquidity_summary(db)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("liquidity:"))
async def on_liquidity_callback(query: CallbackQuery):
    action = query.data.split(":", 1)[1]
    if action == "top":
        text, kb = build_liquidity_top(db, limit=10)
    elif action == "bottom":
        text, kb = build_liquidity_bottom(db, limit=10)
    elif action == "key":
        text, kb = build_liquidity_sku(db, "key")
    elif action == "ticket":
        text, kb = build_liquidity_sku(db, "ticket")
    else:
        text, kb = build_liquidity_summary(db)

    with contextlib.suppress(Exception):
        await query.answer()

    if query.message:
        try:
            await query.message.edit_text(text, reply_markup=kb)
        except Exception:
            await query.message.answer(text, reply_markup=kb)


@router.message(Command('candidates'))
async def candidates(message: Message):
    rows = db.recent_candidates()
    if not rows:
        await message.answer('Кандидатов пока нет. Включите OBSERVE и нужную категорию.')
        return
    for row in rows:
        p = row['payload']
        text = f"{escape(p['lot_id'])} · {escape(p['title'])}\nЦена объявления: {p['price']} {escape(p.get('currency','UNKNOWN'))}\n"
        keyboard = None
        try:
            result = flipper_engine.evaluate_reviewed_candidate(row)
            text += f"Прибыль при исполнении заявки: {result.expected_profit:.2f} ₽ · предел цены: {result.b_max:.2f} ₽\n"
            text += 'Данные проверены; финансовые лимиты повторно проверятся перед покупкой.'
            if flipper_engine.mode == 'ASSIST':
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                    text='Подтвердить одну покупку',callback_data='assist_buy:'+p['lot_id'])]])
        except (ValueError, KeyError, TypeError) as error:
            text += escape(str(error))
        await message.answer(text,reply_markup=keyboard)


@router.message(Command('review'))
async def review(message: Message):
    try:
        _,lot_id,body = message.text.split(maxsplit=2)
        db.review_candidate(lot_id,json.loads(body),message.from_user.id)
        await message.answer('Проверка сохранена на 60 секунд. /candidates — расчёт и подтверждение.')
    except (ValueError, KeyError, TypeError, PermissionError) as error:
        await message.answer('Не сохранено: '+escape(str(error)))


@router.callback_query(F.data.startswith('assist_buy:'))
async def approve(callback: CallbackQuery):
    # Acknowledge immediately: checkout and postpurchase may take longer than Telegram allows.
    await callback.answer('Проверяю ограничения')
    try:
        item_id = await flipper_engine.approve_candidate(callback.data.split(':',1)[1],callback.from_user.id)
        text = f'Покупка записана: {item_id}. Проверьте приёмку.' if item_id else 'Покупка не подтверждена. Проверьте /reconciliation.'
    except (ValueError, KeyError, TypeError, PermissionError) as error:
        text = 'Покупка не выполнена: '+str(error)
    await callback.message.answer(escape(text))


@router.message(Command('intake'))
async def intake(message: Message):
    try:
        _,item_id,source = message.text.split(maxsplit=2)
        db.record_intake(item_id,source,message.from_user.id)
        await message.answer('Приёмка записана. Товар готов к отдельной публикации через /publish.')
    except (ValueError, PermissionError) as error:
        await message.answer(escape(str(error)))


@router.message(Command('capital', 'seed', 'cash'))
async def capital(message: Message):
    try:
        command = message.text.split()[0].split('@')[0]
        if command != '/capital':
            _, amount, source = message.text.split(maxsplit=2)
            method = db.seed_capital if command == '/seed' else db.confirm_cash
            method(amount, source, message.from_user.id, flipper_engine.dry_run)
        state = db.capital_status(flipper_engine.dry_run)
        await message.answer(
            ('Симуляция' if flipper_engine.dry_run else 'Реальные операции') + '\n'
            f"Начальный капитал: {state['initial_capital']/100:.2f} ₽\n"
            f"Капитал по результатам: {state['realized_capital']/100:.2f} ₽\n"
            f"Доступность денег подтверждена: {'да' if state['balance_verified'] else 'нет'}\n"
            f"Доступные деньги: {state['available_cash']/100:.2f} ₽\n"
            f"Открытый риск: {state['open_risk']/100:.2f} ₽\n"
            f"Просадка от максимума: {state['session_loss']/100:.2f} ₽\n"
            'Старт до 1 000 ₽; далее капитал меняется по результатам. Резерв 40%, риск открытых покупок до 10%.\n'
            + escape(', '.join(state['errors'])))
    except (ValueError, KeyError, TypeError, PermissionError) as error:
        await message.answer(escape(str(error)))


@router.message(Command('prepare'))
async def prepare(message: Message):
    try:
        _, lot_id = message.text.split()
        intent = flipper_engine.prepare_manual_purchase(lot_id, message.from_user.id)
        await message.answer(f'Резерв создан: {intent}. Оплата НЕ отправлена. '
            'Перед ручной оплатой повторно проверьте выкуп и цену. '
            'Затем /resolve_purchase с фактическим результатом. Неопределённость блокирует следующие покупки.')
    except (ValueError, KeyError, TypeError, PermissionError) as error:
        await message.answer(escape(str(error)))


@router.message(Command('asset_intake', 'manual_exit'))
async def asset_operation(message: Message):
    try:
        command, item, identity, sku_or_buyer, source = message.text.split(maxsplit=4)
        if command.split('@')[0] == '/asset_intake':
            result = db.record_asset_intake(item, identity, sku_or_buyer, source, message.from_user.id)
        else:
            result = db.record_manual_exit(item, identity, sku_or_buyer, source, message.from_user.id)
        await message.answer(('Подтверждение записано.' if result else 'Уже учтено.') +
            ' Денежное поступление учитывается отдельно через /settle. Если товар был выставлен, снимите объявление.')
    except (ValueError, KeyError, TypeError, PermissionError) as error:
        await message.answer(escape(str(error)))


@router.message(Command('deadlines'))
async def deadlines(message: Message):
    rows = db.inventory_deadlines(flipper_engine.dry_run)
    await message.answer(escape(json.dumps(rows, ensure_ascii=False, indent=2)[:3800]) if rows else 'Незавершённого оборота нет.')


@router.message(Command('writeoff'))
async def writeoff(message: Message):
    try:
        _, item, event, source = message.text.split(maxsplit=3)
        result = db.record_writeoff(item, event, source, message.from_user.id)
        await message.answer('Убыток учтён без повторного списания закупки.' if result else 'Уже учтено.')
    except (ValueError, KeyError, TypeError, PermissionError) as error:
        await message.answer(escape(str(error)))


@router.message(Command('publish'))
async def publish(message: Message):
    try:
        _,item_id,body = message.text.split(maxsplit=2)
        copy = json.loads(body)
        if not copy.get('title') or not copy.get('description'):
            raise ValueError('Нужны title и description с проверенными свойствами товара')
        result = await flipper_engine.publish_checked_item(item_id,message.from_user.id,copy['title'],copy['description'])
        await message.answer('Результат публикации: '+escape(json.dumps(result,ensure_ascii=False)))
    except (ValueError, KeyError, TypeError, PermissionError) as error:
        await message.answer(escape(str(error)))


@router.message(Command('settle','refund','expense'))
async def settle(message: Message):
    try:
        command,item_id,event_id,amount,source = message.text.split(maxsplit=4)
        if command.split('@')[0] == '/expense':
            recorded=db.record_expense(item_id,event_id,amount,source,message.from_user.id)
        else:
            recorded = db.record_receipt(item_id,event_id,amount,source,message.from_user.id,
                                         refund=command.split('@')[0] == '/refund')
        await message.answer('Денежное событие записано.' if recorded else 'Это событие уже было учтено.')
    except (ValueError, KeyError, TypeError, PermissionError) as error:
        await message.answer(escape(str(error)))


@router.message(Command('reconciliation'))
async def reconciliation(message: Message):
    rows = db.get_unresolved_purchase_intents()
    text = '\n'.join(f"{r['intent_id']} · {r['lot_id']} · {r['status']} · {r.get('order_id') or 'заказ неизвестен'}" for r in rows)
    await message.answer(escape(text[:3500]) if text else 'Незавершённых платежей нет. Это не подтверждает состояние внешнего баланса.')


@router.message(Command('resolve_purchase'))
async def resolve_purchase(message: Message):
    try:
        _,intent_id,body=message.text.split(maxsplit=2)
        item=db.resolve_purchase(intent_id,json.loads(body),message.from_user.id)
        await message.answer('Сверка сохранена. '+('Товар: '+item if item else 'Подтверждено отсутствие оплаты.'))
    except (ValueError, KeyError, TypeError, PermissionError) as error:
        await message.answer(escape(str(error)))


@router.message(Command('bind_sale'))
async def bind_sale(message: Message):
    try:
        _,order_id,offer_id,source=message.text.split(maxsplit=3)
        db.bind_sale(order_id,offer_id,source,message.from_user.id)
        await message.answer('Заказ связан с точным объявлением. Выдача возможна после подтверждения оплаты.')
    except (ValueError, PermissionError, sqlite3.IntegrityError) as error:
        await message.answer(escape(str(error)))
