"""
auto_flipper/categories.py — Central registry and definitions for multi-category arbitrage
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CategoryDefinition:
    """
    Defines configuration, scraping, pricing, and copywriting rules for a target liquid category on FunPay.
    """
    id: str                                      # Unique internal ID: steam, discord, cursor, exitlag, tg_premium, chatgpt
    name: str                                    # Display name in Russian
    node_id: int                                 # FunPay section node ID (e.g. 89, 923, 3734, etc.)
    game_id: int                                 # FunPay game/service ID (e.g. 45, 283, 805, etc.)
    item_type: str                               # account, promo_link, license_key, gift_link
    min_buy_price: float                         # Minimum sanity purchase price (RUB)
    max_buy_price: float                         # Maximum purchase budget for this category (RUB)
    market_benchmark: float                      # Initial/fallback market median price (RUB)
    price_floor: float                           # Resale price floor (RUB)
    markup_discount: float                       # Discount multiplier against median for undercut listing (e.g. 0.82-0.85)
    min_profit: float                            # Minimum required net profit in RUB
    min_margin_pct: float                        # Minimum required profit margin in percent
    min_seller_rating: float                     # Minimum seller rating (1.0 to 5.0)
    min_seller_reviews: int                      # Minimum seller reviews count
    enabled_default: bool                        # Enabled out-of-the-box
    fee_rate: float = 0.12                       # FunPay commission rate for this category (e.g. 0.15 for accounts, 0.12 for digital)
    allowed_keywords: List[str] = field(default_factory=list)      # Keywords qualifying candidate deals
    blacklisted_keywords: List[str] = field(default_factory=list)  # Keywords strictly rejecting deals
    custom_fields: Dict[str, str] = field(default_factory=dict)    # FunPay POST fields for saveOffer
    template_title: str = ""                     # Eye-catching title for FunPay listing
    template_desc: str = ""                      # High-converting description for FunPay listing
    template_delivery: str = ""                  # Buyer order fulfillment delivery template
    is_deprecated: bool = False                  # Flag for legacy or unverified sections
    deprecated_reason: str = ""                  # Reason why category is deprecated


CATEGORY_REGISTRY: Dict[str, CategoryDefinition] = {
    "steam": CategoryDefinition(
        id="steam",
        name="Steam Автореги",
        node_id=89,
        game_id=45,
        item_type="steam_account",
        min_buy_price=5.0,
        max_buy_price=35.0,
        market_benchmark=99.0,
        price_floor=69.0,
        markup_discount=0.85,
        min_profit=35.0,
        min_margin_pct=50.0,
        min_seller_rating=4.5,
        min_seller_reviews=2,
        enabled_default=True,
        fee_rate=0.15,
        allowed_keywords=[
            "авторег", "autoreg", "чистый", "родная почта", "родная", "native",
            "первая почта", "first mail", "steam", "аккаунт", "без лимита",
            "nolimit", "no limit", "чистый аккаунт", "с почтой"
        ],
        blacklisted_keywords=[
            "vac", "кт", "community ban", "бан", "общий", "shared", "аренда",
            "куплю", "восстановление", "привязка", "пополнение", "баланс", "часы",
            "накрутка", "инвентарь", "соседи"
        ],
        custom_fields={"fields[type]": "autoreg"},
        template_title="⭐ Steam Авторег (Чистый + Родная почта) 🔑 Полный доступ | Без привязок",
        template_desc=(
            "Чистый авторег аккаунт Steam с родной первоначальной почтой.\n"
            "• Без игровых и VAC блокировок, телефон не привязывался.\n"
            "• Вы получаете: логин:пароль от Steam и логин:пароль от почты.\n"
            "• Полная смена всех данных на ваши! Моментальная автовыдача 24/7."
        ),
        template_delivery=(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📦 ДАННЫЕ ВАШЕГО АККАУНТА (Steam Авторег):\n"
            "👤 Логин: {login}\n"
            "🔑 Пароль: {password}\n"
            "{mail_line}"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 ИНСТРУКЦИЯ ПО ЭКСПЛУАТАЦИИ:\n"
            "1. Войдите в клиент или на сайт Steam: https://store.steampowered.com/\n"
            "2. При необходимости входа в почту используйте указанные данные.\n"
            "3. Рекомендуется привязать свой номер телефона и сменить пароль.\n"
            "4. Пожалуйста, подтвердите выполнение заказа вверху чата! ⭐"
        ),
    ),
    "discord": CategoryDefinition(
        id="discord",
        name="Discord Nitro",
        node_id=923,
        game_id=283,
        item_type="discord_link",
        min_buy_price=10.0,
        max_buy_price=50.0,
        market_benchmark=169.0,
        price_floor=119.0,
        markup_discount=0.85,
        min_profit=60.0,
        min_margin_pct=70.0,
        min_seller_rating=4.5,
        min_seller_reviews=3,
        enabled_default=True,
        allowed_keywords=[
            "nitro", "нитро", "3 месяца", "1 месяц", "full", "qr", "ссылка",
            "промо", "gift", "гифт", "boost", "буст", "активация"
        ],
        blacklisted_keywords=[
            "общий", "shared", "вход в аккаунт", "логин и пароль", "куплю",
            "автопродление с вашей карты", "аренда"
        ],
        custom_fields={"fields[type]": "nitro"},
        template_title="⚡ Discord Nitro 3 Месяца + 2 Буста 🎁 Ссылка-активация (Промо / Gift)",
        template_desc=(
            "Официальная промо-ссылка для активации Discord Nitro на 3 месяца.\n"
            "• Включает 2 бесплатных серверных буста, HD стриминг, эмодзи и значок.\n"
            "• Подходит для аккаунтов, где не было активной подписки последние 12 месяцев.\n"
            "• Моментальная выдача ссылки в чат заказа 24/7!"
        ),
        template_delivery=(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🎁 ВАША ССЫЛКА АКТИВАЦИИ (Discord Nitro):\n"
            "🔗 Ссылка: {link}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 ИНСТРУКЦИЯ ПО АКТИВАЦИИ:\n"
            "1. Откройте ссылку в браузере, где вы авторизованы в нужном аккаунте Discord.\n"
            "2. Для завершения активации промо Discord требует привязку зарубежной карты (авторизация $0.99 с возвратом).\n"
            "3. Если у вас нет карты, вы можете использовать виртуальную карту для авторизации.\n"
            "4. Пожалуйста, подтвердите выполнение заказа вверху чата! ⭐"
        ),
    ),
    "cursor": CategoryDefinition(
        id="cursor",
        name="Cursor AI Pro",
        node_id=3734,
        game_id=805,
        item_type="cursor_account",
        min_buy_price=15.0,
        max_buy_price=70.0,
        market_benchmark=199.0,
        price_floor=149.0,
        markup_discount=0.85,
        min_profit=70.0,
        min_margin_pct=60.0,
        min_seller_rating=4.5,
        min_seller_reviews=2,
        enabled_default=True,
        allowed_keywords=[
            "cursor", "курсор", "pro", "ai", "аккаунт", "подписка", "trial",
            "500 fast", "fast requests", "claude", "sonnet"
        ],
        blacklisted_keywords=[
            "общий", "shared", "слот", "аренда", "куплю", "инвайт", "тима"
        ],
        custom_fields={"fields[type]": "pro"},
        template_title="🤖 Cursor AI Pro 500 Fast Requests 🔑 Личный аккаунт + Родная почта",
        template_desc=(
            "Личный чистый аккаунт Cursor AI (IDE редактор) с активной Pro подпиской.\n"
            "• 500 быстрых запросов Claude 3.5 Sonnet / GPT-4o / Cursor Small.\n"
            "• Полный доступ: логин, пароль и доступ к родной почте.\n"
            "• Моментальная автовыдача данных в чат 24/7!"
        ),
        template_delivery=(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📦 ДАННЫЕ ВАШЕГО АККАУНТА (Cursor AI Pro):\n"
            "👤 Логин / Почта: {login}\n"
            "🔑 Пароль: {password}\n"
            "{mail_line}"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 ИНСТРУКЦИЯ ПО ВХОДУ:\n"
            "1. Запустите Cursor IDE и нажмите 'Sign In' (войти через Email & Password).\n"
            "2. Доступно 500 Fast Premium Requests (Claude 3.5 Sonnet & GPT-4o).\n"
            "3. Пожалуйста, подтвердите выполнение заказа вверху чата! ⭐"
        ),
    ),
    "exitlag": CategoryDefinition(
        id="exitlag",
        name="ExitLag Ключи",
        node_id=1568,
        game_id=385,
        item_type="license_key",
        min_buy_price=20.0,
        max_buy_price=90.0,
        market_benchmark=219.0,
        price_floor=159.0,
        markup_discount=0.85,
        min_profit=70.0,
        min_margin_pct=50.0,
        min_seller_rating=4.5,
        min_seller_reviews=2,
        enabled_default=True,
        allowed_keywords=[
            "exitlag", "экситлаг", "ключ", "key", "код", "активация",
            "30 дней", "1 месяц", "60 дней", "prepaid", "лицензия"
        ],
        blacklisted_keywords=[
            "общий", "shared", "аренда", "куплю", "вход в аккаунт", "чужой аккаунт"
        ],
        custom_fields={"fields[type]": "key"},
        template_title="🚀 ExitLag Ключ активации (Лицензия) 🔑 Global / Регион Free",
        template_desc=(
            "Официальный предоплаченный лицензионный ключ для ExitLag.\n"
            "• Снижает сетевой пинг и устраняет packet loss во всех онлайн играх.\n"
            "• Активируется в личном кабинете на официальном сайте exitlag.com.\n"
            "• Моментальная автовыдача ключа сразу после оплаты 24/7!"
        ),
        template_delivery=(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔑 ВАШ ЛИЦЕНЗИОННЫЙ КЛЮЧ (ExitLag):\n"
            "🎫 Код активации: {key}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 ИНСТРУКЦИЯ ПО АКТИВАЦИИ:\n"
            "1. Авторизуйтесь на сайте https://www.exitlag.com/ в своем аккаунте.\n"
            "2. Перейдите в раздел 'My Account' -> 'Prepaid Codes' (Предоплаченные коды).\n"
            "3. Вставьте полученный ключ и нажмите Activate.\n"
            "4. Пожалуйста, подтвердите выполнение заказа вверху чата! ⭐"
        ),
    ),
    "tg_premium": CategoryDefinition(
        id="tg_premium",
        name="Telegram Premium",
        node_id=1391,
        game_id=637,
        item_type="gift_link",
        min_buy_price=150.0,
        max_buy_price=320.0,
        market_benchmark=389.0,
        price_floor=330.0,
        markup_discount=0.90,
        min_profit=40.0,
        min_margin_pct=12.0,
        min_seller_rating=4.7,
        min_seller_reviews=5,
        enabled_default=True,
        allowed_keywords=[
            "premium", "премиум", "gift", "гифт", "ссылка", "t.me/giftcode",
            "3 месяца", "1 месяц", "подарок", "без входа", "подарочная"
        ],
        blacklisted_keywords=[
            "с входом", "вход по qr", "вход по номеру", "сессия", "куплю",
            "общий", "на ваш номер с входом", "tdata"
        ],
        custom_fields={"fields[type]": "gift"},
        template_title="⭐ Telegram Premium 3 Месяца 🎁 Gift Link (Подарочная ссылка БЕЗ входа)",
        template_desc=(
            "Официальная подарочная ссылка активации Telegram Premium.\n"
            "• Без передачи аккаунта, без паролей и без QR-кодов.\n"
            "• Мгновенная активация на любой ваш личный Telegram аккаунт.\n"
            "• Моментальная автовыдача подарочной ссылки в чат 24/7!"
        ),
        template_delivery=(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🎁 ВАША ПОДАРОЧНАЯ ССЫЛКА TELEGRAM PREMIUM:\n"
            "🔗 Ссылка: {link}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 ИНСТРУКЦИЯ ПО АКТИВАЦИИ:\n"
            "1. Откройте ссылку прямо в приложении Telegram.\n"
            "2. Нажмите 'Принять подарок'.\n"
            "3. Подписка Telegram Premium активируется моментально на вашем профиле.\n"
            "4. Пожалуйста, подтвердите выполнение заказа вверху чата! ⭐"
        ),
    ),
    "chatgpt": CategoryDefinition(
        id="chatgpt",
        name="ChatGPT Plus",
        node_id=1355,
        game_id=593,
        item_type="chatgpt_account",
        min_buy_price=100.0,
        max_buy_price=350.0,
        market_benchmark=1099.0,
        price_floor=450.0,
        markup_discount=0.82,
        min_profit=150.0,
        min_margin_pct=35.0,
        min_seller_rating=4.8,
        min_seller_reviews=5,
        enabled_default=True,
        allowed_keywords=[
            "plus", "плюс", "gpt 4", "gpt-4", "gpt4", "gpt-5", "gpt5",
            "4o", "личный", "персональный", "родная почта", "с почтой"
        ],
        blacklisted_keywords=[
            "общий", "shared", "слот", "slot", "инвайт", "invite", "тима", "team",
            "workspace", "семейный", "аренда", "прокат", "карпулинг", "соседи", "пул",
            "1 час", "2 часа", "3 часа", "на час", "на сутки", "1 день", "2 дня", "3 дня",
            "на 1 день", "на 2 дня", "на 3 дня", "на 7 дней", "вход по коду", "без смены",
            "без почты", "не личный", "продление на ваш", "активация на ваш", "подключение на ваш",
            "на вашу почту", "на ваш аккаунт", "куплю"
        ],
        custom_fields={},
        template_title="⭐ ChatGPT Plus 1 месяц (30 дней) 🔑 Личный аккаунт 📩 Родная почта",
        template_desc=(
            "Личный чистый аккаунт ChatGPT Plus на 1 месяц.\n"
            "• Вы получаете: логин, пароль и доступ к родной почте.\n"
            "• Доступны GPT-4o, o1, Canvas, анализ данных, генерация изображений.\n"
            "• Моментальная автовыдача данных сразу после оплаты 24/7!"
        ),
        template_delivery=(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📦 ДАННЫЕ ВАШЕГО АККАУНТА (ChatGPT Plus):\n"
            "👤 Логин / Почта: {login}\n"
            "🔑 Пароль: {password}\n"
            "{mail_line}"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 ИНСТРУКЦИЯ ПО ЭКСПЛУАТАЦИИ:\n"
            "1. Заходите на сайт https://chatgpt.com/ в режиме Инкогнито.\n"
            "2. Рекомендуется использовать чистый браузер и стабильное подключение.\n"
            "3. Подписка активна 30 дней. Доступны GPT-4o, o1, Canvas, голос.\n"
            "4. Пожалуйста, проверьте данные и подтвердите выполнение заказа вверху чата! ⭐"
        ),
    ),
    "cs2_prime": CategoryDefinition(
        id="cs2_prime",
        name="CS2 Prime (Без наценки)",
        node_id=1350,
        game_id=117,
        item_type="steam_account",
        min_buy_price=500.0,
        max_buy_price=1400.0,
        market_benchmark=1850.0,
        price_floor=1450.0,
        markup_discount=0.85,
        min_profit=200.0,
        min_margin_pct=15.0,
        min_seller_rating=4.8,
        min_seller_reviews=10,
        enabled_default=True,
        fee_rate=0.15,
        allowed_keywords=[
            "prime", "прайм", "кс2", "cs2", "кс 2", "cs 2", "родная почта",
            "first mail", "с почтой", "без блокировок", "чистый", "full access"
        ],
        blacklisted_keywords=[
            "vac", "кт", "community ban", "бан", "общий", "shared", "аренда",
            "с читами", "faceit ban", "no prime", "без прайма", "нон прайм", "non-prime", "куплю"
        ],
        custom_fields={"fields[type]": "prime"},
        template_title="⭐ CS2 Prime (Чистый + Родная почта) 🔑 Без блокировок | Полная передача",
        template_desc=(
            "Аккаунт CS2 с подтвержденным статусом Prime без наценки за редкие скины.\n"
            "• Без VAC, игровых и трейд-блокировок. Телефон не привязан.\n"
            "• В комплекте: логин:пароль от Steam и логин:пароль от родной почты.\n"
            "• Полная передача доступа в одни руки. Автовыдача 24/7."
        ),
        template_delivery=(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📦 ДАННЫЕ ВАШЕГО АККАУНТА (CS2 Prime):\n"
            "👤 Логин: {login}\n"
            "🔑 Пароль: {password}\n"
            "{mail_line}"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 ИНСТРУКЦИЯ ПО ВХОДУ И СМЕНЕ ДАННЫХ:\n"
            "1. Войдите в клиент Steam: https://store.steampowered.com/\n"
            "2. Войдите в родную почту и подтвердите смену адреса на ваш личный.\n"
            "3. Привяжите Steam Guard и свой номер телефона.\n"
            "4. Пожалуйста, подтвердите выполнение заказа вверху чата! ⭐"
        ),
    ),
    "valorant_ranked": CategoryDefinition(
        id="valorant_ranked",
        name="Valorant (Рейтинг доступен)",
        node_id=612,
        game_id=144,
        item_type="riot_account",
        min_buy_price=250.0,
        max_buy_price=900.0,
        market_benchmark=1250.0,
        price_floor=950.0,
        markup_discount=0.85,
        min_profit=150.0,
        min_margin_pct=20.0,
        min_seller_rating=4.8,
        min_seller_reviews=10,
        enabled_default=True,
        fee_rate=0.15,
        allowed_keywords=[
            "ранг", "рейтинг", "ranked", "ready for ranked", "20 lvl", "20 лвл",
            "открыт рейтинг", "доступен рейтинг", "eu", "ru", "полный доступ", "родная почта"
        ],
        blacklisted_keywords=[
            "бан", "ban", "общий", "shared", "аренда", "с читами", "hwid",
            "без смены", "без доступа к почте", "куплю"
        ],
        custom_fields={"fields[type]": "ranked"},
        template_title="⭐ Valorant (Доступ к рейтингу / 20+ LVL) 🔑 Родная почта | Без привязок",
        template_desc=(
            "Аккаунт Valorant с открытым доступом к соревновательному (ранговому) режиму.\n"
            "• Без дорогих скинов, без блокировок, без ограничений по чату/матчмейкингу.\n"
            "• Полная смена почты и пароля на ваши данные.\n"
            "• Моментальная передача данных в одни руки 24/7."
        ),
        template_delivery=(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📦 ДАННЫЕ ВАШЕГО АККАУНТА (Valorant):\n"
            "👤 Riot ID / Логин: {login}\n"
            "🔑 Пароль: {password}\n"
            "{mail_line}"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 ИНСТРУКЦИЯ ПО ВХОДУ:\n"
            "1. Авторизуйтесь на сайте https://account.riotgames.com/ или в Riot Client.\n"
            "2. Смените адрес электронной почты и пароль на свои личные данные.\n"
            "3. Пожалуйста, подтвердите выполнение заказа вверху чата! ⭐"
        ),
    ),
    "minecraft_pc": CategoryDefinition(
        id="minecraft_pc",
        name="Minecraft Java & Bedrock (PC)",
        node_id=221,
        game_id=36,
        item_type="microsoft_account",
        min_buy_price=400.0,
        max_buy_price=1200.0,
        market_benchmark=1650.0,
        price_floor=1290.0,
        markup_discount=0.85,
        min_profit=200.0,
        min_margin_pct=20.0,
        min_seller_rating=4.8,
        min_seller_reviews=10,
        enabled_default=True,
        fee_rate=0.15,
        allowed_keywords=[
            "java", "bedrock", "pc", "лицензия", "полный доступ", "смена почты",
            "смена ника", "microsoft", "чистый", "вечная лицензия"
        ],
        blacklisted_keywords=[
            "game pass", "gamepass", "подписка", "аренда", "общий", "shared",
            "бан на hypixel", "hypixel ban", "с секретками", "куплю"
        ],
        custom_fields={"fields[type]": "license"},
        template_title="⭐ Minecraft Java & Bedrock PC (Лицензия навсегда) 🔑 Смена почты и ника",
        template_desc=(
            "Лицензионный Microsoft-аккаунт с купленной игрой Minecraft (Java + Bedrock для ПК).\n"
            "• Не временная подписка Xbox Game Pass, а постоянная лицензия навсегда.\n"
            "• Без банов на серверах (Hypixel чистый).\n"
            "• Полная смена почты, пароля, скина и никнейма."
        ),
        template_delivery=(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📦 ДАННЫЕ ВАШЕГО АККАУНТА (Minecraft PC):\n"
            "👤 Microsoft Email: {login}\n"
            "🔑 Пароль: {password}\n"
            "{mail_line}"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 ИНСТРУКЦИЯ ПО ВХОДУ:\n"
            "1. Войдите на https://account.microsoft.com/ и https://www.minecraft.net/\n"
            "2. Смените основной псевдоним почты и пароль безопасности на свои.\n"
            "3. Пожалуйста, подтвердите выполнение заказа вверху чата! ⭐"
        ),
    ),
}


# Only permanent goods are in the current pilot. Old categories remain readable
# for reconciliation of historical inventory, but cannot authorize new purchases.
for _excluded in ('chatgpt', 'discord', 'cursor', 'exitlag', 'tg_premium'):
    CATEGORY_REGISTRY[_excluded].enabled_default = False
for _cid, _name, _node, _keywords, _dep, _reason, _enabled in (
    ('tf2_items', 'TF2: предметы с проверенным выкупом', 1808, ['key', 'ключ', 'ticket', 'билет', 'expander', 'расширитель'], False, '', True),
    ('mm2_items', 'MM2: предметы, наблюдение спроса (устаревший node 925 / 404)', 925, ['icewing', 'iceblaster', 'godly'], True, 'FunPay node 925 returned 404; unverified catalog section disabled for live purchases', False),
):
    CATEGORY_REGISTRY[_cid] = CategoryDefinition(
        id=_cid, name=_name, node_id=_node, game_id=0, item_type='trade_item',
        min_buy_price=.01, max_buy_price=1_000_000_000, market_benchmark=0,
        price_floor=0, markup_discount=1, min_profit=10, min_margin_pct=15,
        min_seller_rating=4.5, min_seller_reviews=10, enabled_default=_enabled,
        is_deprecated=_dep, deprecated_reason=_reason,
        fee_rate=0, allowed_keywords=_keywords,
        blacklisted_keywords=['аренда', 'подписка', 'subscription', 'куплю'])


def get_category_by_id(cat_id: str) -> Optional[CategoryDefinition]:
    """Retrieves CategoryDefinition by ID."""
    return CATEGORY_REGISTRY.get(cat_id)


def get_category_by_node(node_id: int) -> Optional[CategoryDefinition]:
    """Retrieves CategoryDefinition by FunPay node ID."""
    for cat in CATEGORY_REGISTRY.values():
        if cat.node_id == node_id:
            return cat
    # Node 3559 is ChatGPT Subscriptions fallback
    if node_id == 3559:
        return CATEGORY_REGISTRY.get("chatgpt")
    return None


def get_all_target_node_ids(include_deprecated: bool = False) -> List[int]:
    """Returns all active, non-deprecated FunPay node IDs handled by the multi-category flipper."""
    nodes = [
        cat.node_id for cat in CATEGORY_REGISTRY.values()
        if include_deprecated or (not getattr(cat, 'is_deprecated', False) and cat.node_id not in (0, 925))
    ]
    if 3559 not in nodes:
        nodes.append(3559)
    return nodes


def detect_category_for_lot(node_id: int, title: str = "") -> Optional[CategoryDefinition]:
    """
    Detects which category a lot belongs to based on node_id and title keywords.
    Returns None if category cannot be confidently matched (never blindly falls back).
    """
    by_node = get_category_by_node(node_id)
    if by_node:
        return by_node

    t = title.lower()
    # Check new target game account categories first
    if (any(k in t for k in ["cs2", "cs 2", "кс2", "кс 2", "counter-strike 2"])
            and any(p in t for p in ["prime", "прайм"])):
        return CATEGORY_REGISTRY.get("cs2_prime")
    if any(k in t for k in ["valorant", "валорант"]):
        return CATEGORY_REGISTRY.get("valorant_ranked")
    if any(k in t for k in ["minecraft", "майнкрафт"]):
        return CATEGORY_REGISTRY.get("minecraft_pc")

    # Standard categories
    if any(k in t for k in ["steam", "стим"]):
        return CATEGORY_REGISTRY.get("steam")
    if any(k in t for k in ["discord", "дискорд", "nitro"]):
        return CATEGORY_REGISTRY.get("discord")
    if any(k in t for k in ["cursor", "курсор"]):
        return CATEGORY_REGISTRY.get("cursor")
    if any(k in t for k in ["exitlag", "экситлаг"]):
        return CATEGORY_REGISTRY.get("exitlag")
    if any(k in t for k in ["telegram", "телеграм", "tg premium"]):
        return CATEGORY_REGISTRY.get("tg_premium")
    if any(k in t for k in ["chatgpt", "chat gpt", "openai", "gpt-4", "gpt 4"]):
        return CATEGORY_REGISTRY.get("chatgpt")

    return None
