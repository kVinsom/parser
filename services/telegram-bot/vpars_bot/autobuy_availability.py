"""Autobuy is parked for release. Existing implementation and account data stay intact.

This is deliberately not an environment setting: a stale deployment configuration
must not reactivate purchases. Re-enabling requires a reviewed code change.
"""

AVAILABLE = False
SOON = "Soon"
OLD_SETUP_LABELS = {
    "▶️ Почати підключення", "🔑 Додати ключ сесії", "✅ Зберегти й продовжити",
    "📦 До налаштування доставки", "🔄 Перевірити стан підключення",
    "🔎 Перевірити оформлення", "✅ Активувати Buy", "💰 Змінити ліміт",
    "▶️ Продовжити покупки у Vinted", "⏹ Вимкнути цей регіон", "🌍 Інші регіони",
    "🇩🇪 Німеччина · EUR", "🇫🇷 Франція · EUR", "🇮🇹 Італія · EUR", "🇬🇧 Велика Британія · GBP",
}


def require_available():
    if not AVAILABLE:
        from .domain import UserError

        raise UserError(SOON)


def callback_action(data):
    return (data == "lot:buy" or data.startswith(("wallet:", "ab:"))
            or data in {"faq:connect", "faq:autobuy", "profile:connect",
                        "profile:autobuy", "profile:autobuy_instruction"})


def text_action(text):
    command = text.split("@", 1)[0].split(" ", 1)[0].lower()
    return text in OLD_SETUP_LABELS or text in {SOON, "Autobuy · Soon", "Підключити автобай", "Підключити Autobuy",
                    'Що таке функція "Autobuy" та як її підключити?'} or command in {
                        "/autobuy", "/buy", "/wallet"}
