# bot/keyboards.py
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Анализ", callback_data="menu:analyze"),
            InlineKeyboardButton("💰 Фандинг", callback_data="menu:funding"),
        ],
        [
            InlineKeyboardButton("🧠 Оракул", callback_data="menu:oracle"),
            InlineKeyboardButton("📈 Утро", callback_data="menu:morning"),
        ],
        [
            InlineKeyboardButton("📒 Журнал", callback_data="menu:journal"),
            InlineKeyboardButton("⚙️ Статус", callback_data="menu:status"),
        ],
    ])


def signal_actions_kb(symbol: str, ex_a: str, ex_b: str, diff: float = 0.0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🧠 Спросить Оракула",
                                 callback_data=f"oracle:{symbol}:{ex_a}:{ex_b}"),
            InlineKeyboardButton("🔍 Deep Analysis",
                                 callback_data=f"deep_analyze:{symbol}:{ex_a}:{ex_b}:{diff}"),
        ],
        [
            InlineKeyboardButton("✅ Войти", callback_data=f"enter:{symbol}:{ex_a}:{ex_b}"),
            InlineKeyboardButton("❌ Игнор", callback_data="ignore"),
        ]
    ])


def confirm_kb(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да", callback_data=f"confirm:{action}"),
        InlineKeyboardButton("❌ Отмена", callback_data="cancel"),
    ]])


def get_analyze_keyboard(symbol: str) -> InlineKeyboardMarkup:
    """Для алертов: Анализ и Guardian."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 Анализ", callback_data=f"analyze:{symbol}"),
            InlineKeyboardButton("🛡 Watch", callback_data=f"watch_ui_start:{symbol}"),
        ]
    ])
