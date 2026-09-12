"""Opt-in, bounded observation digests to configured administrators only."""
import logging
import time
from html import escape
from auto_flipper.config import ADMIN_IDS
from auto_flipper.economics import kopecks


async def notify_candidates(engine, database, rows):
    bot = getattr(engine, '_bot', None)
    if not bot or engine.is_emergency_stopped or engine.mode not in ('OBSERVE', 'ASSIST'):
        return
    if database.get_setting('candidate_alerts', '0') != '1':
        return
    pending = getattr(engine, '_candidate_alert_queue', {})
    for row in rows:
        pending[row['lot_id']] = row
    engine._candidate_alert_queue = dict(list(pending.items())[-100:])
    pending = engine._candidate_alert_queue
    now = time.time()
    last = float(database.get_setting('candidate_alert_last_at', '0'))
    if now-last < 300:
        return
    selected = []
    for lot_id in list(pending):
        current = database.get_candidate(lot_id)
        if not current or not 0 <= now-current['observed_at'] <= 60:
            pending.pop(lot_id)
            continue
        payload = current['payload']
        try:
            valid_price = kopecks(payload['price']) > 0 and payload.get('currency') == 'RUB'
        except (ValueError, KeyError, TypeError):
            valid_price = False
        if valid_price:
            selected.append(payload)
        else:
            pending.pop(lot_id)
        if len(selected) == 3:
            break
    recipients = sorted({actor for actor in ADMIN_IDS if type(actor) is int and actor > 0})
    if not selected or not recipients:
        return
    lines = ['Симуляция' if engine.dry_run else 'Реальная среда',
             'Новые наблюдения. Спрос и выкуп требуют проверки:']
    for payload in selected:
        lines.append(f"{escape(str(payload['lot_id']))} · {escape(str(payload['title'])[:100])} · {payload['price']} ₽")
    lines.append('/candidates — обновить наблюдения; /alerts off — отключить уведомления.')
    # Bound retries even when Telegram cannot deliver; never retry trades here.
    database.set_setting('candidate_alert_last_at', str(now))
    for payload in selected:
        pending.pop(payload['lot_id'], None)
    for actor in recipients:
        try:
            await bot.send_message(actor, '\n'.join(lines))
        except Exception as error:
            logging.getLogger('FlipperAlerts').warning('Digest delivery failed: %s', type(error).__name__)
