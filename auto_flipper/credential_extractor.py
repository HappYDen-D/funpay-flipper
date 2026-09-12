"""
auto_flipper/credential_extractor.py — Multi-category extraction & parsing of seller credentials
Supports ChatGPT Plus, Steam, Discord Nitro, Cursor AI, ExitLag, and Telegram Premium
"""
import re
from typing import Any, Dict, Optional

from auto_flipper.categories import CATEGORY_REGISTRY, get_category_by_id


class MultiCategoryCredentialExtractor:
    """
    Parses seller order chat messages into structured credentials for multiple digital categories:
    - Discord Nitro: Promo links (discord.com/billing/promotions/..., discord.gift/...)
    - Telegram Premium: Gift links (t.me/giftcode/...)
    - Steam: 4-part (login:pass:mail:mailpass), multi-line or 2-part accounts
    - ExitLag / Software: License keys (AAAA-BBBB-CCCC-DDDD)
    - ChatGPT Plus & Cursor AI: Email, password, mail credentials, session cookies
    """

    # Discord Promo / Gift link patterns
    DISCORD_LINK_PATTERN = re.compile(
        r'(https?://(?:www\.)?(?:discord\.com/billing/promotions/|promotions\.discord\.gg/|discord\.gift/|discordapp\.com/billing/promotions/)[a-zA-Z0-9_\-]+|'
        r'\b(?:discord\.com/billing/promotions/|promotions\.discord\.gg/|discord\.gift/|discordapp\.com/billing/promotions/)[a-zA-Z0-9_\-]+)',
        re.IGNORECASE,
    )

    # Telegram Gift Link patterns
    TELEGRAM_GIFT_PATTERN = re.compile(
        r'(https?://(?:www\.)?(?:t\.me/giftcode/|telegram\.me/giftcode/)[a-zA-Z0-9_\-]+|'
        r'\b(?:t\.me/giftcode/|telegram\.me/giftcode/)[a-zA-Z0-9_\-]+)',
        re.IGNORECASE,
    )

    # License key patterns (ExitLag, Windows, etc.)
    LICENSE_KEY_PATTERN = re.compile(
        r'\b([A-Z0-9]{4,6}-[A-Z0-9]{4,6}-[A-Z0-9]{4,6}-[A-Z0-9]{4,6}(?:-[A-Z0-9]{4,6})?)\b',
        re.IGNORECASE,
    )
    KEY_LABELS = re.compile(
        r'(?:ключ|код|активация|key|license|code|prepaid)\s*[:\-–—=]\s*([A-Za-z0-9_\-]{8,})',
        re.IGNORECASE,
    )

    # Email & Credentials patterns
    EMAIL_PASS_PATTERN = re.compile(
        r"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9.-]+)\s*[:|;/\-–—]\s*([^\s\r\n]{4,})",
        re.IGNORECASE,
    )
    EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9.-]+$")

    LOGIN_LABELS = re.compile(
        r"(?:почта|логин|email|mail|login|аккаунт|данные|u(?:ser)?name)\s*[:\-–—=]\s*([^\s\r\n]+)",
        re.IGNORECASE,
    )
    PASS_LABELS = re.compile(
        r"(?:пароль|пасс|pass|password)(?!\s*от\s*(?:почты|мыла))\s*[:\-–—=]\s*([^\s\r\n]{4,})",
        re.IGNORECASE,
    )
    MAIL_PASS_LABELS = re.compile(
        r"(?:пароль\s+от\s+(?:почты|мыла)|пароль\s+почты|mail\s+pass(?:word)?|native\s+pass|почта\s+пароль)\s*[:\-–—=]\s*([^\s\r\n]{4,})",
        re.IGNORECASE,
    )

    COOKIE_PATTERN = re.compile(
        r"(?:__Secure-next-auth\.session-token|session-token|auth_token)\s*[:=]\s*([a-zA-Z0-9_\-\.]{20,})",
        re.IGNORECASE,
    )

    # ─────────────────────────────────────────────────────────────
    # Main Extraction Entrypoint
    # ─────────────────────────────────────────────────────────────

    @classmethod
    def extract(cls, raw_text: str, category_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Parses raw text into structured credentials dictionary.
        Supports category hints for targeted extraction or auto-detects.
        Returns:
            {
                "category": str,
                "login": str,
                "password": str,
                "mail_password": str,
                "link": str,
                "key": str,
                "cookies": str,
                "raw": str,
                "formatted": str
            } or None.
        """
        if not raw_text or not raw_text.strip():
            return None

        clean_text = raw_text.strip()

        # 1. Target Category Prioritization if category_id given
        if category_id == "discord":
            res = cls.extract_discord_link(clean_text)
            if res:
                return res
        elif category_id == "tg_premium":
            res = cls.extract_telegram_gift(clean_text)
            if res:
                return res
        elif category_id == "exitlag":
            res = cls.extract_license_key(clean_text)
            if res:
                return res
        elif category_id in ("steam", "cs2_prime"):
            res = cls.extract_steam(clean_text, category_id=category_id)
            if res:
                return res
        elif category_id == "valorant_ranked":
            res = cls.extract_game_account(clean_text, category_id="valorant_ranked", account_type="riot_account")
            if res:
                return res
        elif category_id == "minecraft_pc":
            res = cls.extract_game_account(clean_text, category_id="minecraft_pc", account_type="microsoft_account")
            if res:
                return res

        # 2. General Auto-Detection Pipeline
        # A. Discord Promo / Gift Links
        discord_res = cls.extract_discord_link(clean_text)
        if discord_res:
            return discord_res

        # B. Telegram Gift Links
        tg_res = cls.extract_telegram_gift(clean_text)
        if tg_res:
            return tg_res

        # C. License Keys (ExitLag format)
        key_res = cls.extract_license_key(clean_text)
        if key_res:
            return key_res

        # D. Steam Credentials (4-part login:pass:mail:mailpass or non-email login:pass)
        steam_res = cls.extract_steam(clean_text)
        if steam_res and (category_id == "steam" or not cls.EMAIL_REGEX.match(steam_res.get("login", ""))):
            return steam_res

        # E. ChatGPT / Cursor AI / General Account credentials
        acct_res = cls.extract_chatgpt(clean_text, category_id=category_id or "chatgpt")
        if acct_res:
            return acct_res

        # F. Fallback Steam match if 2 parts were found
        if steam_res:
            return steam_res

        return None

    # ─────────────────────────────────────────────────────────────
    # Category-Specific Parsers
    # ─────────────────────────────────────────────────────────────

    @classmethod
    def extract_discord_link(cls, clean_text: str) -> Optional[Dict[str, Any]]:
        """Extracts Discord promotional link or gift link."""
        m = cls.DISCORD_LINK_PATTERN.search(clean_text)
        if m:
            link = m.group(1).strip()
            if not link.startswith("http"):
                link = "https://" + link
            return {
                "type": "link",
                "category": "discord",
                "login": "DISCORD_PROMO_LINK",
                "password": link.split("/")[-1],
                "mail_password": "",
                "mail_login": "",
                "mail": "",
                "link": link,
                "key": "",
                "cookies": "",
                "raw": clean_text,
                "formatted": cls.format_discord_payload(link),
            }
        return None

    @classmethod
    def extract_telegram_gift(cls, clean_text: str) -> Optional[Dict[str, Any]]:
        """Extracts Telegram giftcode link."""
        m = cls.TELEGRAM_GIFT_PATTERN.search(clean_text)
        if m:
            link = m.group(1).strip()
            if not link.startswith("http"):
                link = "https://" + link
            return {
                "type": "link",
                "category": "tg_premium",
                "login": "TELEGRAM_GIFT_LINK",
                "password": link.split("/")[-1],
                "mail_password": "",
                "mail_login": "",
                "mail": "",
                "link": link,
                "key": "",
                "cookies": "",
                "raw": clean_text,
                "formatted": cls.format_telegram_payload(link),
            }
        return None

    @classmethod
    def extract_license_key(cls, clean_text: str) -> Optional[Dict[str, Any]]:
        """Extracts license keys (ExitLag or other software keys)."""
        m = cls.LICENSE_KEY_PATTERN.search(clean_text)
        key_val = m.group(1).strip() if m else None

        if not key_val:
            m_label = cls.KEY_LABELS.search(clean_text)
            if m_label:
                cand = m_label.group(1).strip()
                if len(cand) >= 10 and not cand.startswith("http"):
                    key_val = cand

        if key_val:
            return {
                "type": "license_key",
                "category": "exitlag",
                "login": "EXITLAG_KEY",
                "password": key_val,
                "mail_password": "",
                "mail_login": "",
                "mail": "",
                "link": "",
                "key": key_val,
                "cookies": "",
                "raw": clean_text,
                "formatted": cls.format_license_key_payload(key_val),
            }
        return None

    @classmethod
    def extract_steam(cls, clean_text: str, category_id: str = "steam") -> Optional[Dict[str, Any]]:
        """
        Extracts Steam credentials:
        - 4-part: login:password:mail:mailpass
        - Multi-line: login, password, mail, mailpass
        - Labeled login / pass / mail
        - 2-part: login:password
        """
        lines = [l.strip() for l in clean_text.splitlines() if l.strip()]

        # 1. Check for labeled Steam format
        login_m = cls.LOGIN_LABELS.search(clean_text)
        pass_m = cls.PASS_LABELS.search(clean_text)
        mail_m = re.search(r"(?:почта|mail|email)\s*[:\-–—=]\s*([^\s\r\n]+)", clean_text, re.IGNORECASE)
        mail_pass_m = cls.MAIL_PASS_LABELS.search(clean_text)

        if login_m and pass_m:
            login = login_m.group(1).strip()
            password = pass_m.group(1).strip()
            mail_val = mail_m.group(1).strip() if mail_m and mail_m.group(1).strip() != login else ""
            mail_pass = mail_pass_m.group(1).strip() if mail_pass_m else ""
            mail_info = f"{mail_val} (пароль: {mail_pass})" if (mail_val and mail_pass) else (mail_val or mail_pass)
            return {
                "type": "steam_account",
                "category": category_id,
                "login": login,
                "password": password,
                "mail_password": mail_pass,
                "mail_login": mail_val,
                "mail": mail_val,
                "link": "",
                "key": "",
                "cookies": "",
                "raw": clean_text,
                "formatted": cls.format_category_payload(category_id, login, password, mail_info),
            }

        # 2. Check 4-part single line: login:password:mail:mailpass
        for line in lines:
            if not line.startswith("http"):
                for sep in (":", ";", "|"):
                    if line.count(sep) >= 3:
                        parts = [p.strip() for p in line.split(sep)]
                        if len(parts) >= 4 and len(parts[0]) >= 3 and len(parts[1]) >= 4:
                            login, password = parts[0], parts[1]
                            mail, mail_pass = parts[2], parts[3]
                            mail_info = f"{mail} (пароль: {mail_pass})"
                            return {
                                "type": "steam_account",
                                "category": category_id,
                                "login": login,
                                "password": password,
                                "mail_password": mail_pass,
                                "mail_login": mail,
                                "mail": mail,
                                "link": "",
                                "key": "",
                                "cookies": "",
                                "raw": clean_text,
                                "formatted": cls.format_category_payload(category_id, login, password, mail_info),
                            }

        # 3. Check 4-line format
        if len(lines) >= 4 and not any(l.startswith("http") for l in lines[:4]):
            login, password, mail, mail_pass = lines[0], lines[1], lines[2], lines[3]
            if len(login) >= 3 and len(password) >= 4 and "@" in mail:
                mail_info = f"{mail} (пароль: {mail_pass})"
                return {
                    "type": "steam_account",
                    "category": category_id,
                    "login": login,
                    "password": password,
                    "mail_password": mail_pass,
                    "mail_login": mail,
                    "mail": mail,
                    "link": "",
                    "key": "",
                    "cookies": "",
                    "raw": clean_text,
                    "formatted": cls.format_category_payload(category_id, login, password, mail_info),
                }

        # 4. Check 2-part single line without email: login:password
        for line in lines:
            if not line.startswith("http"):
                for sep in (":", ";", "|"):
                    if sep in line:
                        parts = line.split(sep, 1)
                        p1, p2 = parts[0].strip(), parts[1].strip()
                        if len(p1) >= 3 and len(p2) >= 4 and "@" not in p1 and " " not in p1 and " " not in p2:
                            return {
                                "type": "steam_account",
                                "category": category_id,
                                "login": p1,
                                "password": p2,
                                "mail_password": "",
                                "mail_login": "",
                                "mail": "",
                                "link": "",
                                "key": "",
                                "cookies": "",
                                "raw": clean_text,
                                "formatted": cls.format_category_payload(category_id, p1, p2, ""),
                            }

        return None

    @classmethod
    def extract_game_account(cls, clean_text: str, category_id: str, account_type: str = "account") -> Optional[Dict[str, Any]]:
        """
        Extracts game account credentials for Valorant, Minecraft, CS2, etc.
        Supports 4-part (login:pass:mail:mailpass), 2-part (login:pass or email:pass), multi-line, and labeled formats.
        """
        steam_cand = cls.extract_steam(clean_text, category_id=category_id)
        if steam_cand:
            steam_cand["type"] = account_type
            steam_cand["category"] = category_id
            return steam_cand

        acct_cand = cls.extract_chatgpt(clean_text, category_id=category_id)
        if acct_cand:
            acct_cand["type"] = account_type
            acct_cand["category"] = category_id
            acct_cand["formatted"] = cls.format_category_payload(category_id, acct_cand["login"], acct_cand["password"], acct_cand.get("mail_password", ""))
            return acct_cand

        return None

    @classmethod
    def extract_chatgpt(cls, clean_text: str, category_id: str = "chatgpt") -> Optional[Dict[str, Any]]:
        """
        Parses ChatGPT Plus or Cursor AI credentials (Email, pass, mail access, session cookies).
        """
        cookie_match = cls.COOKIE_PATTERN.search(clean_text)
        cookies_val = cookie_match.group(1).strip() if cookie_match else ""

        # 1. Labeled format
        login_match = cls.LOGIN_LABELS.search(clean_text)
        pass_match = cls.PASS_LABELS.search(clean_text)
        mail_pass_match = cls.MAIL_PASS_LABELS.search(clean_text)

        if login_match and pass_match:
            login = login_match.group(1).strip()
            password = pass_match.group(1).strip()
            mail_pass = mail_pass_match.group(1).strip() if mail_pass_match else ""
            fmt = cls.format_cursor_payload(login, password, mail_pass) if category_id == "cursor" else cls.format_chatgpt_payload(login, password, mail_pass)
            return {
                "type": "cursor_account" if category_id == "cursor" else "account",
                "category": category_id,
                "login": login,
                "password": password,
                "mail_password": mail_pass,
                "mail_login": login,
                "mail": login,
                "link": "",
                "key": "",
                "cookies": cookies_val,
                "raw": clean_text,
                "formatted": fmt,
            }

        # 2. Email:pass combos
        matches = cls.EMAIL_PASS_PATTERN.findall(clean_text)
        if matches:
            login = matches[0][0].strip()
            password = matches[0][1].strip()
            mail_pass = matches[1][1].strip() if len(matches) > 1 else ""
            if not mail_pass and mail_pass_match:
                mail_pass = mail_pass_match.group(1).strip()
            fmt = cls.format_cursor_payload(login, password, mail_pass) if category_id == "cursor" else cls.format_chatgpt_payload(login, password, mail_pass)
            return {
                "type": "cursor_account" if category_id == "cursor" else "account",
                "category": category_id,
                "login": login,
                "password": password,
                "mail_password": mail_pass,
                "mail_login": login,
                "mail": login,
                "link": "",
                "key": "",
                "cookies": cookies_val,
                "raw": clean_text,
                "formatted": fmt,
            }

        # 3. Two-line format: Line 1 = email, Line 2 = password
        lines = [l.strip() for l in clean_text.splitlines() if l.strip()]
        for i in range(len(lines) - 1):
            if cls.EMAIL_REGEX.match(lines[i]):
                candidate_pass = lines[i + 1]
                if len(candidate_pass) >= 4 and not candidate_pass.startswith("http") and " " not in candidate_pass:
                    mail_pass = lines[i + 2] if len(lines) > i + 2 and len(lines[i + 2]) >= 4 and not lines[i + 2].startswith("http") else ""
                    fmt = cls.format_cursor_payload(lines[i], candidate_pass, mail_pass) if category_id == "cursor" else cls.format_chatgpt_payload(lines[i], candidate_pass, mail_pass)
                    return {
                        "type": "cursor_account" if category_id == "cursor" else "account",
                        "category": category_id,
                        "login": lines[i],
                        "password": candidate_pass,
                        "mail_password": mail_pass,
                        "mail_login": lines[i],
                        "mail": lines[i],
                        "link": "",
                        "key": "",
                        "cookies": cookies_val,
                        "raw": clean_text,
                        "formatted": fmt,
                    }

        # 4. Delimiter fallback (any line containing email and separator)
        for line in lines:
            for sep in (":", "|", ";", "/", "-", "–", "—"):
                if sep in line and not line.startswith("http"):
                    parts = line.split(sep, 1)
                    p1, p2 = parts[0].strip(), parts[1].strip()
                    if "@" in p1 and len(p2) >= 4 and not p2.startswith("http"):
                        fmt = cls.format_cursor_payload(p1, p2, "") if category_id == "cursor" else cls.format_chatgpt_payload(p1, p2, "")
                        return {
                            "type": "cursor_account" if category_id == "cursor" else "account",
                            "category": category_id,
                            "login": p1,
                            "password": p2,
                            "mail_password": "",
                            "mail_login": p1,
                            "mail": p1,
                            "link": "",
                            "key": "",
                            "cookies": cookies_val,
                            "raw": clean_text,
                            "formatted": fmt,
                        }

        # 5. Session token / cookies fallback
        if cookies_val:
            return {
                "type": "session_cookies",
                "category": category_id,
                "login": "SESSION_COOKIE_ACCESS",
                "password": cookies_val[:8] + "...",
                "mail_password": "",
                "mail_login": "",
                "mail": "",
                "link": "",
                "key": "",
                "cookies": cookies_val,
                "raw": clean_text,
                "formatted": cls.format_chatgpt_payload("Куки / Session-token", cookies_val, ""),
            }

        return None

    # ─────────────────────────────────────────────────────────────
    # Formatting Helpers
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def mask_password(pwd: str) -> str:
        """Masks a password for safe logging and UI display."""
        if not pwd:
            return "—"
        if len(pwd) <= 3:
            return "***"
        return pwd[:2] + "*" * (len(pwd) - 2)

    @classmethod
    def format_delivery_payload(cls, *args, **kwargs) -> str:
        """
        Formats delivery payload. Supports two calling conventions:
        1. format_delivery_payload(parsed_dict) -> formats based on category/type
        2. format_delivery_payload(login, password, mail_pass="") -> legacy ChatGPT format
        """
        if args and isinstance(args[0], dict):
            d = args[0]
            cat = d.get("category", "")
            t = d.get("type", "")
            if cat == "discord" or t == "link" and "discord" in d.get("link", ""):
                return cls.format_discord_payload(d.get("link", ""))
            elif cat == "tg_premium" or t == "link" and ("t.me" in d.get("link", "") or "telegram" in d.get("link", "")):
                return cls.format_telegram_payload(d.get("link", ""))
            elif cat == "exitlag" or t == "license_key":
                return cls.format_license_key_payload(d.get("key", d.get("password", "")))
            elif cat == "steam" or t == "steam_account":
                m_log = d.get("mail_login") or d.get("mail", "")
                m_pwd = d.get("mail_password", "")
                mail_info = f"{m_log} (пароль: {m_pwd})" if (m_log and m_pwd) else (m_log or m_pwd)
                return cls.format_steam_payload(d.get("login", ""), d.get("password", ""), mail_info)
            elif cat == "cursor" or t == "cursor_account":
                return cls.format_cursor_payload(d.get("login", ""), d.get("password", ""), d.get("mail_password", ""))
            elif d.get("formatted"):
                return d["formatted"]
            else:
                return cls.format_chatgpt_payload(d.get("login", ""), d.get("password", ""), d.get("mail_password", ""))

        login = args[0] if len(args) > 0 else kwargs.get("login", "")
        password = args[1] if len(args) > 1 else kwargs.get("password", "")
        mail_pass = args[2] if len(args) > 2 else kwargs.get("mail_pass", "")
        return cls.format_chatgpt_payload(login, password, mail_pass)

    @staticmethod
    def format_chatgpt_payload(login: str, password: str, mail_pass: str = "") -> str:
        cat = get_category_by_id("chatgpt")
        mail_line = f"📧 Пароль от почты: {mail_pass}\n" if mail_pass else ""
        if cat and cat.template_delivery:
            return cat.template_delivery.format(login=login, password=password, mail_line=mail_line)
        return (
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📦 ДАННЫЕ ВАШЕГО АККАУНТА (ChatGPT Plus):\n"
            f"👤 Логин / Почта: {login}\n"
            f"🔑 Пароль: {password}\n"
            f"{mail_line}"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 ИНСТРУКЦИЯ ПО ЭКСПЛУАТАЦИИ:\n"
            "1. Заходите на сайт https://chatgpt.com/ в режиме Инкогнито.\n"
            "2. Рекомендуется использовать чистый браузер и стабильное подключение.\n"
            "3. Подписка активна 30 дней. Доступны GPT-4o, o1, Canvas, голос.\n"
            "4. Пожалуйста, проверьте данные и подтвердите выполнение заказа вверху чата! ⭐"
        )

    @classmethod
    def format_category_payload(cls, category_id: str, login: str, password: str, mail_info: str = "") -> str:
        cat = get_category_by_id(category_id)
        mail_line = f"📧 Почта / Доступ: {mail_info}\n" if mail_info else ""
        if cat and cat.template_delivery:
            try:
                return cat.template_delivery.format(login=login, password=password, mail_line=mail_line)
            except Exception:
                pass
        if category_id in ("steam", "cs2_prime"):
            return cls.format_steam_payload(login, password, mail_info)
        elif category_id == "cursor":
            return cls.format_cursor_payload(login, password, mail_info)
        elif category_id == "chatgpt":
            return cls.format_chatgpt_payload(login, password, mail_info)
        return (
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 ДАННЫЕ ВАШЕГО АККАУНТА ({category_id}):\n"
            f"👤 Логин: {login}\n"
            f"🔑 Пароль: {password}\n"
            f"{mail_line}"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Пожалуйста, проверьте данные и подтвердите заказ! ⭐"
        )

    @staticmethod
    def format_steam_payload(login: str, password: str, mail_info: str = "") -> str:
        cat = get_category_by_id("steam")
        mail_line = f"📧 Почта / Доступ: {mail_info}\n" if mail_info else ""
        if cat and cat.template_delivery:
            return cat.template_delivery.format(login=login, password=password, mail_line=mail_line)
        return (
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📦 ДАННЫЕ ВАШЕГО АККАУНТА (Steam Авторег):\n"
            f"👤 Логин: {login}\n"
            f"🔑 Пароль: {password}\n"
            f"{mail_line}"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 ИНСТРУКЦИЯ ПО ЭКСПЛУАТАЦИИ:\n"
            "1. Войдите в клиент или на сайт Steam: https://store.steampowered.com/\n"
            "2. При необходимости входа в почту используйте указанные данные.\n"
            "3. Рекомендуется привязать свой номер телефона и сменить пароль.\n"
            "4. Пожалуйста, подтвердите выполнение заказа вверху чата! ⭐"
        )

    @staticmethod
    def format_discord_payload(link: str) -> str:
        cat = get_category_by_id("discord")
        if cat and cat.template_delivery:
            return cat.template_delivery.format(link=link)
        return (
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🎁 ВАША ССЫЛКА АКТИВАЦИИ (Discord Nitro):\n"
            f"🔗 Ссылка: {link}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 ИНСТРУКЦИЯ ПО АКТИВАЦИИ:\n"
            "1. Откройте ссылку в браузере, где вы авторизованы в нужном аккаунте Discord.\n"
            "2. Для активации промо Discord требует привязку зарубежной карты ($0.99 с возвратом).\n"
            "3. Пожалуйста, подтвердите выполнение заказа вверху чата! ⭐"
        )

    @staticmethod
    def format_telegram_payload(link: str) -> str:
        cat = get_category_by_id("tg_premium")
        if cat and cat.template_delivery:
            return cat.template_delivery.format(link=link)
        return (
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🎁 ВАША ПОДАРОЧНАЯ ССЫЛКА TELEGRAM PREMIUM:\n"
            f"🔗 Ссылка: {link}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 ИНСТРУКЦИЯ ПО АКТИВАЦИИ:\n"
            "1. Откройте ссылку прямо в приложении Telegram.\n"
            "2. Нажмите 'Принять подарок'.\n"
            "3. Подписка Telegram Premium активируется моментально на вашем профиле.\n"
            "4. Пожалуйста, подтвердите выполнение заказа вверху чата! ⭐"
        )

    @staticmethod
    def format_license_key_payload(key: str) -> str:
        cat = get_category_by_id("exitlag")
        if cat and cat.template_delivery:
            return cat.template_delivery.format(key=key)
        return (
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔑 ВАШ ЛИЦЕНЗИОННЫЙ КЛЮЧ (ExitLag):\n"
            f"🎫 Код активации: {key}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 ИНСТРУКЦИЯ ПО АКТИВАЦИИ:\n"
            "1. Авторизуйтесь на сайте https://www.exitlag.com/ в своем аккаунте.\n"
            "2. Перейдите в раздел 'My Account' -> 'Prepaid Codes'.\n"
            "3. Вставьте полученный ключ и нажмите Activate.\n"
            "4. Пожалуйста, подтвердите выполнение заказа вверху чата! ⭐"
        )

    @staticmethod
    def format_cursor_payload(login: str, password: str, mail_pass: str = "") -> str:
        cat = get_category_by_id("cursor")
        mail_line = f"📧 Доступ к почте: {mail_pass}\n" if mail_pass else ""
        if cat and cat.template_delivery:
            return cat.template_delivery.format(login=login, password=password, mail_line=mail_line)
        return (
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📦 ДАННЫЕ ВАШЕГО АККАУНТА (Cursor AI Pro):\n"
            f"👤 Логин / Почта: {login}\n"
            f"🔑 Пароль: {password}\n"
            f"{mail_line}"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 ИНСТРУКЦИЯ ПО ВХОДУ:\n"
            "1. Запустите Cursor IDE и войдите через Email & Password.\n"
            "2. Доступно 500 Fast Premium Requests (Claude 3.5 Sonnet & GPT-4o).\n"
            "3. Пожалуйста, подтвердите выполнение заказа вверху чата! ⭐"
        )


# Backward compatibility alias
CredentialExtractor = MultiCategoryCredentialExtractor
