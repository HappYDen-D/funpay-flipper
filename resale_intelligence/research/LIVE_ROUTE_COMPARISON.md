# Сравнительный анализ торговых маршрутов (Live Route Comparison)

**Дата и время исследования:** 12 сентября 2026 года, 20:50 MSK (UTC+3)  
**Рабочая ветка Git:** `v0.2-route-discovery` (базовый коммит `3b9ba55`)  
**Ограничения пилота:**
- Предполагаемый стартовый капитал: **~500 ₽** (консервативный потолок первого пилота; максимальный лимит до 1 000 ₽ при отдельном подтверждении).
- Пороговые критерии сделки: `minimum_profit = 10.00 ₽`, `minimum_roi = 15%` (оба обязательны).
- Временные лимиты: целевая продажа ≤24 часов, полный цикл до доступных денег ≤72 часов.
- Запреты: подписки, аренда, промокоды подписок, временные аккаунты, товары с торговым удержанием (trade hold) >72 часов.
- Доступные инструменты: строго read-only / публичные данные. Ноль покупок, ноль реальных чекаутов, ноль контактов с контрагентами.

**Базовые валютные курсы (ЦБ РФ на 12.09.2026):**
- USD: `84,2569 ₽`
- EUR: `97,8728 ₽`

---

## 1. Проверенные рынки и категории

В соответствии с заданием исследовано **четыре независимых класса активов**:

1. **Категория A: Стандартизированные предметы Steam / TF2**
   - *Tour of Duty Ticket* (Командировочный билет MvM, SKU `725;6`)
   - *Mann Co. Supply Crate Key* (Ключ от ящика Манн Ко, SKU `5021;6`)
   - *Backpack Expander* (Расширитель рюкзака, SKU `5050;6`)
2. **Категория B: Постоянные цифровые лицензионные ключи (Perpetual Keys)**
   - *Terraria PC Steam Retail Key* (постоянный ключ активации Steam, Global / RU+CIS)
   - Анализ оптовых площадок и механики проверки ключей без активации
3. **Категория C: Игровые аккаунты (Game Accounts)**
   - *Counter-Strike 2 Prime* (Node 1350)
   - *Valorant Ranked Unlocked / 20+ LVL* (Node 612)
   - *Minecraft Java & Bedrock PC Permanent License* (Node 221)
   - *Brawl Stars Supercell ID* (Node 436)
4. **Категория D: Независимый тип (Предметы и валюта других игр)**
   - *Roblox Murder Mystery 2 (MM2)*: Icewing Knife, Iceblaster Gun (Node 925 — обнаружен и подтверждён как действующий каталог)
   - *Roblox Robux* (Chips Node 99)
   - *Durak Online Coins* (Chips Node 180)

---

## 2. Сводная таблица исследованных маршрутов

| № | Товар / exact SKU | Supplier (FunPay ask / debit B) | Buyer / Exit (Bid / N) | B, ₽ | N, ₽ | C, ₽ | Net PnL, ₽ | ROI | Bmax, ₽ | Уровень evidence | Оборот (часы) | ECONOMICALLY_VIABLE | CURRENT_RISK_POLICY_ALLOWED | Вердикт |
|---|---|---|---|---:|---:|---:|---:|---:|---:|:---:|:---:|:---:|:---:|---|
| 1 | **TF2 Tour of Duty Ticket** (`725;6`, craftable) | FunPay #65624843 (pda363): 80.00 ₽ (дебет СБП 80.00 ₽ / баланс 78.56 ₽) | Marketplace.tf Buy Order: $0.76 (5 шт.), комиссия 10% → $0.684 net = 57.63 ₽ | 78.56 | 57.63 | 0.00 | **−20.93** | **−26.6%** | **47.63** | A (bid) / B (вывод) | 2–6ч | **NO** | NO (B > 50 ₽ limit) | **REJECTED** (Отрицательный спред, B > Bmax, санкции на вывод с MP.tf) |
| 2 | **TF2 Mann Co. Key** (`5021;6`, craftable) | FunPay #76841683 (rkrklgrhte): 167.00 ₽ (дебет 167.00 ₽) | tf2key.ru выкуп: 142.00 ₽ (порог СБП от 500 ₽, вывод до 168ч) | 167.00 | 142.00 | 0.00 | **−25.00** | **−15.0%** | **123.47** | A/B | >168ч | **NO** | NO (B > 50 ₽ limit, 33% капитала) | **REJECTED** (Убыток 25 ₽ на единицу, порог вывода 500 ₽ не достижим 1 шт.) |
| 3 | **TF2 Mann Co. Key** (`5021;6`, craftable) | FunPay #76841683: 167.00 ₽ | Marketplace.tf Buy Order: $1.85 (410 шт.) → $1.665 net = 140.29 ₽ | 167.00 | 140.29 | 0.00 | **−26.71** | **−16.0%** | **121.99** | A | 4–12ч | **NO** | NO (B > 50 ₽ limit) | **REJECTED** (Отрицательный спред, блокировка прямых выплат в РФ) |
| 4 | **TF2 Backpack Expander** (`5050;6`, craftable) | FunPay #65626180 (pda363): 85.33 ₽ | Marketplace.tf Buy Order: $0.71 (7 шт.) → $0.639 net = 53.84 ₽ | 85.33 | 53.84 | 0.00 | **−31.49** | **−36.9%** | **43.84** | A | 2–6ч | **NO** | NO (B > 50 ₽ limit) | **REJECTED** (Отрицательный спред, убыток 31.49 ₽) |
| 5 | **Terraria PC Steam Key** (Retail Key, Global) | FunPay #72082408: 824.85 ₽ (дешевле — только подарки-ссылки 197–385 ₽) | Нет моментального выкупа (Level C: розница FunPay ~800 ₽) | 824.85 | ~704.00 (гипотеза) | 0.00 | **−120.85** | **−14.7%** | **603.47** | C (нет бида) | >72ч | **NO** | NO (B > 500 ₽ капитала) | **REJECTED** (Нет покупателя, превышает весь капитал, нет проверки без активации) |
| 6 | **CS2 Prime Account** (RU / Global, родная почта) | FunPay Node 1350: реальный прайм от 1 100.00 ₽ (лоты по 1 ₽ — пустышки) | Нет моментального выкупа (перекупы берут за 400–600 ₽) | 1100.00 | ~500.00 | 0.00 | **−600.00** | **−54.5%** | **426.08** | C | >72ч | **NO** | NO (B > 500 ₽, превышает бюджет) | **REJECTED** (Превышает капитал, риск восстановления 1-й почтой, нет бида) |
| 7 | **Valorant Ranked Ready** (20+ LVL, EU/RU) | FunPay Node 612: реальная продажа от 330.00 ₽ (лоты по 35 ₽ — аренда на 6ч!) | Нет моментального выкупа (Level C: розничные объявления 300–450 ₽) | 330.00 | ~280.00 | 0.00 | **−50.00** | **−15.2%** | **234.78** | C | >72ч | **NO** | NO (B > 50 ₽ limit) | **REJECTED** (Дешёвые лоты — аренда, античит Vanguard HWID ban, нет бида) |
| 8 | **Brawl Stars Account** (Fresh Supercell ID, 2000 силы) | FunPay #72136457 (Fizryk1337): 10.00 ₽ | Нет покупателя (рынок переполнен идентичными авторегами по 10–15 ₽) | 10.00 | ~8.50 | 0.00 | **−1.50** | **−15.0%** | **0.00** | C | Неопределён | **NO** | YES (10 ₽ < 50 ₽, но EV < 0) | **REJECTED** (Нулевая ликвидность, блокировка Supercell при смене IP, нет спроса) |
| 9 | **MM2 Icewing Knife** (Roblox, Trade Item) | FunPay #76836949 (WenzBuyer, Node 925): 117.11 ₽ | mm2.cash (quick-sell требует привязку аккаунта, публичной котировки нет) / FunPay «куплю» | 117.11 | ~90.00 (гипотеза) | 0.00 | **−27.11** | **−23.1%** | **69.56** | B/C | 4–24ч | **NO** | NO (B > 50 ₽ limit) | **REJECTED** (Нет подтверждённого бида, ручной трейд в Roblox, дебет > Bmax) |
| 10 | **Roblox Robux** (Chips Node 99, 500 Robux) | Закупка на FunPay ~0.65–0.70 ₽/R$ | Продажа через Gamepass / Group Payout | ~350.00 | ~350.00 | Налог 30% | Отрицательный | Отрицательный | N/A | C | **120–168ч** (5–7 дней холд Roblox) | **NO** | NO (B > 50 ₽ limit) | **REJECTED** (7-дневный холд Roblox нарушает лимит 72ч; комиссия платформы 30%) |

---

## 3. Детальные профили ключевых кандидатов (Полные строки исследования)

### Профиль 1: TF2 Tour of Duty Ticket (Командировочный билет MvM)
- **Product / exact SKU**: Team Fortress 2 Tour of Duty Ticket (Командировочный билет), стандартный craftable `725;6`.
- **Product type**: `trade_item` (Steam Trade).
- **Supplier**: Продавец `pda363` (219 отзывов, рейтинг 5.0, на сайте 4 года).
- **Supplier URL**: `https://funpay.com/lots/offer?id=65624843`
- **Observed time**: 2026-09-12 20:44:13 MSK.
- **Currency**: RUB.
- **Quantity**: 1 шт. (наличие у продавца — 65 шт.).
- **Minimum quantity**: 1 шт.
- **Listing price**: 80.00 ₽.
- **Expected/final purchase debit**: 78.56 ₽ при оплате с баланса FunPay (price_guard factor `78.5574`); 80.00 ₽ при оплате через СБП (Method 21); 83.57 ₽ при оплате картой RU (Method 7).
- **Buyer / bid**: Публичный стакан Buy Orders на Marketplace.tf (`https://marketplace.tf/items/tf2/725;6`).
- **Buyer evidence URL**: `https://marketplace.tf/items/tf2/725;6`
- **Bid price**: $0.76 USD.
- **Bid capacity**: 5 шт. по $0.76 (следующий уровень $0.75 на 10 шт.).
- **Bid expiry/freshness**: Постоянная действующая рыночная заявка (стакан).
- **Buyer acceptance conditions**: Приёмка ботом Marketplace.tf через автоматический Steam Trade Offer. Товар должен быть `tradable`.
- **Sale fees**: Комиссия Marketplace.tf 10% ($0.076). Чистая сумма: $0.684 USD.
- **Transfer costs**: 0.00 ₽ (комиссия внутри Steam Trade отсутствует).
- **Net payout RUB**: `$0.684 × 84,2569 ₽ = 57.63 ₽` (без учёта дополнительных банковских комиссий и конвертации валюты).
- **Cash availability delay**: 2–6 часов на исполнение заявки ботом; однако вывод средств на карту РФ заблокирован Marketplace.tf с 2022 года.
- **Verification method**: Проверка Steam Inventory API на наличие asset ID и атрибута `tradable=1`.
- **Transfer method**: Внутриплатформенный обмен Steam (`platform_trade`).
- **Account/region limitations**: Требуется Steam Guard на смартфоне, активный более 15 дней. Отсутствие готового аккаунта у оператора накладывает 15-дневный Trade Hold.
- **Defect/recovery/dispute risk**: LOW для предмета TF2 (предмет не отзывается после завершения трейда).
- **Expected sell time**: Мгновенно (принятие офера ботом Marketplace.tf).
- **Expected reusable-cash time**: >168 часов (или невозможность вывода в РФ).
- **Profit**: `57.63 ₽ − 78.56 ₽ = −20.93 ₽` (УБЫТОК).
- **ROI**: `−26.6%`.
- **Bmax**: `floor_to_kopeck(min(57.63 − 10.00, 57.63 / 1.15)) = 47.63 ₽`.
- **ECONOMICALLY_VIABLE**: **NO** (Закупка 78.56 ₽ превышает Bmax 47.63 ₽ на 30.93 ₽; чистый отрицательный спред).
- **CURRENT_RISK_POLICY_ALLOWED**: **NO** (При капитале 500 ₽ лимит риска на лот `0.10 × 500 = 50.00 ₽`; закупка 78.56 ₽ превышает лимит).
- **Confirmed facts**: Реальный стакан на Marketplace.tf существует; цена на FunPay зафиксирована; комиссия сервиса 10%.
- **Unknowns**: Возможность вывода с Marketplace.tf на криптокошелёк без потерь >10%; наличие у пользователя активного Steam Guard без 15-дневного холда.
- **Verdict**: **REJECTED**.

---

### Профиль 2: TF2 Mann Co. Supply Crate Key (Ключ Манн Ко)
- **Product / exact SKU**: Team Fortress 2 Mann Co. Supply Crate Key, `5021;6`, craftable.
- **Product type**: `trade_item` (Steam Trade).
- **Supplier**: Продавец `rkrklgrhte` (362 отзыва, рейтинг 5.0).
- **Supplier URL**: `https://funpay.com/lots/offer?id=76841683`
- **Observed time**: 2026-09-12 20:44:13 MSK.
- **Currency**: RUB.
- **Quantity**: 1 шт. (наличие 68 шт.).
- **Minimum quantity**: 1 шт.
- **Listing price**: 167.00 ₽.
- **Expected/final purchase debit**: 167.00 ₽.
- **Buyer / bid**: Рублёвый обменный сервис `tf2key.ru` (выкуп ключей ботом).
- **Buyer evidence URL**: `https://tf2key.ru` (прямая котировка главной страницы: *«Купить ключи TF2 от 150 ₽ или продать за 142 ₽»*).
- **Bid price**: 142.00 ₽.
- **Bid capacity**: От 1 ключа (но выплата СБП доступна только от 500 ₽).
- **Bid expiry/freshness**: Актуально на момент запроса (2026-09-12 20:45 MSK).
- **Buyer acceptance conditions**: Отправка ключа боту через Steam Trade URL.
- **Sale fees**: Включены в спред сервиса (выплата 142.00 ₽).
- **Transfer costs**: 0.00 ₽.
- **Net payout RUB**: 142.00 ₽ (при накоплении минималки вывода 500 ₽).
- **Cash availability delay**: По регламенту tf2key.ru выплата на СБП занимает до 168 часов (7 суток!).
- **Verification method**: Проверка Steam Inventory API.
- **Transfer method**: `platform_trade`.
- **Account/region limitations**: Steam Guard > 15 дней.
- **Defect/recovery/dispute risk**: LOW для предмета; MEDIUM для задержки выплаты сторонним обменником.
- **Expected sell time**: До 30 минут.
- **Expected reusable-cash time**: До 168 часов (превышает предел 72 часа).
- **Profit**: `142.00 ₽ − 167.00 ₽ = −25.00 ₽` (УБЫТОК).
- **ROI**: `−15.0%`.
- **Bmax**: `floor_to_kopeck(min(142.00 − 10.00, 142.00 / 1.15)) = 123.47 ₽`.
- **ECONOMICALLY_VIABLE**: **NO** (Закупка 167.00 ₽ выше чистой выплаты 142.00 ₽; чистый убыток 25 ₽).
- **CURRENT_RISK_POLICY_ALLOWED**: **NO** (167 ₽ составляет 33.4% капитала 500 ₽ при лимите 10% / 50 ₽).
- **Confirmed facts**: Котировка выкупа 142 ₽ подтверждена ответом сервера tf2key.ru; цена FunPay 167 ₽ проверена.
- **Unknowns**: Срок фактической выплаты tf2key.ru на практике.
- **Verdict**: **REJECTED**.

---

### Профиль 3: Постоянный ключ Terraria PC (Steam Retail Key)
- **Product / exact SKU**: Terraria (PC) Steam CD Key, Global / RU+CIS, постоянная пожизненная лицензия (non-subscription).
- **Product type**: `permanent_key`.
- **Supplier**: Продавец на FunPay (например, лот `72082408`).
- **Supplier URL**: `https://funpay.com/lots/offer?id=72082408`
- **Observed time**: 2026-09-12 20:45:42 MSK.
- **Currency**: RUB.
- **Quantity**: 1 шт.
- **Listing price**: 824.85 ₽ (дешёвые предложения по 197–385 ₽ являются не ключами, а передачей подарка ботом).
- **Expected/final purchase debit**: 824.85 ₽.
- **Buyer / bid**: **ОТСУТСТВУЕТ**. Нет ни одного сервиса мгновенного автоматического выкупа одиночных ключей (Level C).
- **Net payout RUB**: Не определена (чужие объявления о продаже по 800–900 ₽ не гарантируют факт и срок сделки).
- **Verification method**: Невозможно проверить валидность цифрового ключа Steam без его активации на учётную запись.
- **Defect/recovery/dispute risk**: HIGH (риск того, что поставщик продал активированный ключ, либо покупатель активирует его и откроет арбитраж о невалидности).
- **Bmax**: Не применим без доказанного N. При гипотетической рознице 700 ₽ Bmax составил бы ~600 ₽.
- **ECONOMICALLY_VIABLE**: **NO** (B = 825 ₽, покупателя нет).
- **CURRENT_RISK_POLICY_ALLOWED**: **NO** (824.85 ₽ превышает весь капитал 500 ₽ на 65%).
- **Verdict**: **REJECTED**.

---

### Профиль 4: Аккаунты (CS2 Prime / Valorant / Minecraft / Brawl Stars)
- **Product type**: `account`.
- **Supplier URL**:
  - CS2 Prime (Node 1350): реальные лоты с родной почтой от 1 100 ₽ (лоты по 1 ₽ — пустые автореги без игры).
  - Valorant (Node 612): реальные постоянные аккаунты с рангом от 330 ₽ (лоты по 35 ₽ — аренда на 6 часов).
  - Brawl Stars (Node 436): лот `72136457` за 10.00 ₽.
- **Buyer / bid**: **ОТСУТСТВУЕТ**. Ни на один игровой аккаунт нет подтверждённой заявки на мгновенный выкуп (Level A).
- **Risk Penalty**:
  - Восстановление через службу поддержки разработчика по первой почте / чекам / провайдеру.
  - Привязки 2FA / мобильного телефона.
  - Блокировки за передачу аккаунта (Supercell, Riot Games, Valve ToS строго запрещают куплю-продажу учетных записей).
  - `login:password` не гарантирует владение.
- **ECONOMICALLY_VIABLE**: **NO**.
- **CURRENT_RISK_POLICY_ALLOWED**: Для CS2 и Valorant — **NO** (превышают лимиты капитала). Для Brawl Stars — формально проходит лимит 10 ₽ < 50 ₽, но при отсутствии выхода математическое ожидание прибыли отрицательно.
- **Verdict**: **REJECTED**.

---

### Профиль 5: Roblox MM2 — Icewing Knife (Node 925)
- **Product / exact SKU**: Roblox Murder Mystery 2: Icewing (Ancient knife).
- **Product type**: `trade_item` (Roblox in-game trade).
- **Supplier**: Продавец `WenzBuyer` (231 отзыв), лот `76836949`.
- **Supplier URL**: `https://funpay.com/lots/offer?id=76836949`
- **Observed time**: 2026-09-12 20:46:27 MSK.
- **Catalog Discovery**: Node 925 проверен через публичный парсинг каталога FunPay (`https://funpay.com/lots/925/`). Статус **200 OK**, каталог содержит 139 предложений. Ошибка 404 в предыдущих сессиях была ложной (вызвана антибот-проверкой или отсутствием cookies).
- **Listing price**: 117.11 ₽.
- **Buyer / bid**: На площадке `mm2.cash` требуется привязка аккаунта через Turnstile; публичные стаканы выкупа отсутствуют. На FunPay предложения «куплю» не содержат фиксированной цены и объёма (Level B contact).
- **ECONOMICALLY_VIABLE**: **NO** (Закупочная цена 117.11 ₽ не обеспечена гарантированным выходом).
- **CURRENT_RISK_POLICY_ALLOWED**: **NO** (117.11 ₽ > 50.00 ₽ допустимого риска при капитале 500 ₽).
- **Transfer method**: Ручной вход на сервер Roblox в игре для передачи предмета.
- **Verdict**: **REJECTED**.

---

## 4. Анализ ограничений капитала (Capital & Risk Policy Gate)

В проекте действует строгая система разграничения риска:
```text
Капитал K = 500 ₽ (50 000 коп.)
Резерв = max(50 ₽, 40% × K) = 200 ₽ (20 000 коп.)
Свободный денежный бюджет = 300 ₽ (30 000 коп.)
Лимит на один лот (lot_cap = 15%) = 75 ₽ (7 500 коп.)
Лимит на одного поставщика (supplier_cap = 25%) = 125 ₽ (12 500 коп.)
Лимит на категорию (category_cap = 40%) = 200 ₽ (20 000 коп.)
Лимит открытого риска (loss_stop_ratio = 10%) = 50 ₽ (5 000 коп.)
```

Согласно проверке в `auto_flipper/trade_admission.py:check_capital`:
```python
risk_limit = int((Decimal(status['realized_capital']) * Decimal('.10')).to_integral_value(rounding=ROUND_FLOOR))
if status['open_risk'] + kopecks(price) + cost > max(0, risk_limit - state['session_loss']):
    raise ValueError('TOTAL_OPEN_LOSS_LIMIT')
```

Это означает:
- **Любой единичный товар дороже 50.00 ₽** блокируется текущим RiskPolicy при капитале 500 ₽, так как его полная потеря (в худшем сценарии) превышает 10% капитала.
- Товар TF2 Ticket (закупка 78.56 ₽) требует либо повышения капитала до `≥ 786 ₽` (чтобы 10% покрывали 78.56 ₽), либо смягчения `loss_stop_ratio` до `≥ 16%`.
- Товар TF2 Key (закупка 167.00 ₽) требует капитала `≥ 1 670 ₽`, либо смягчения `loss_stop_ratio` до `≥ 34%`.
- Однако, даже если бы RiskPolicy был смягчён, **сделка всё равно отвергается по экономике (`ECONOMICALLY_VIABLE = NO`)**, так как чистый спред отрицателен!

---

## 5. Итоговый рейтинг исследованных направлений

1. **#1 Best candidate:** **ОТСУТСТВУЕТ** (Ни один маршрут не показал одновременно положительный чистый PnL, подтверждённый Level A bid и соответствие лимитам).
2. **#2 Fallback (Watchlist / Наблюдение за спредом):** **TF2 Tour of Duty Ticket**
   - Самый близкий к безубыточности стандартизированный предмет (чистый bid 57.63 ₽ против FunPay ask 78.56 ₽).
   - Условие активации: появление предложения на FunPay по цене `≤ 47.63 ₽` (Bmax) ПРИ одновременном решении вопроса вывода средств с Marketplace.tf или появлении рублёвого покупателя.
3. **#3 Watchlist:** **TF2 Mann Co. Supply Crate Key**
   - Глубокая ликвидность (стакан на сотни штук), но отрицательный спред (−25 ₽ на единицу) и задержка выплат на tf2key.ru до 168 часов.
4. **Rejected:**
   - Постоянные ключи (Terraria и др.) — нет моментального спроса, не проверяются без гашения, цена выше бюджета.
   - Игровые аккаунты (CS2, Valorant, Minecraft, Brawl Stars) — риск отзыва владельцем, баны за смену IP, дешёвые лоты оказались арендой/пустышками.
   - Игровая валюта (Roblox Robux) — 5–7 дней удержания платформой нарушают лимит оборота 72 часа, 30% налог Roblox съедает маржу.
   - MM2 предметы — отсутствие публичного гарантированного выкупа без риска депозита.
