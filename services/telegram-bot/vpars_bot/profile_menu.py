"""Profile/FAQ labels and user-supplied help copy; no billing or Autobuy actions."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import autobuy_availability as autobuy
from .wallet import CONNECT_TEXT

PROFILE = "👤 Профіль"
FAQ = "FAQ❓"
FAQ_TEXT = "<b>FAQ❓</b>\n\nОбери питання."
FAQ_BACK = "↩️ Назад"
CONNECT_AUTOBUY = "Підключити автобай"
INSTRUCTION_CALLBACK = "profile:autobuy_instruction"

PAGES = {
    "about": (
        "Для чого створений наш бот?",
        "<b><i>Наш бот створений для того, щоб дати вам зручний інструмент для покупки речей.\n\n"
        "Бот був написаний під керівництвом реселера, та тут були виконані всі його правки "
        "для вашої зручності.\n\n"
        "Наш інструмент має такі переваги:\n"
        "• Найнижча затримка на ринку за співвідношенням ціни.\n"
        '• Виключення слів. Autobuy — Soon.\n'
        "• Велика гнучкість та зручність бота, які дають можливість забути про все "
        "і зосередитись на власному прибутку.</i></b>",
    ),
    "support": (
        "Підтримка",
        "<b><i>Потрібна допомога чи щось не зрозуміло?\n@BladeBotSupport</i></b>",
    ),
    "payment": (
        "Які варіанти оплати доступні?",
        "<b><i>Доступна оплата на карту Райффайзен Банку.\n\n"
        "Якщо потрібна оплата криптовалютою або на інший банк, зверніться в підтримку, "
        "і ми підберемо для вас зручний спосіб оплати.\n@BladeBotSupport</i></b>",
    ),
    "exclude_words": (
        'Як працює функція "Виключення слів"?',
        "<b><i>Виключення слів — це унікальна для українського ринку функція.</i></b>\n\n"
        "<b>Функція дозволяє прибрати небажані оголошення з результатів пошуку "
        "за допомогою стоп-слів.</b>\n\n"
        "<b><i>Ви вказуєте слова, які не хочете бачити в оголошеннях. Якщо бот знаходить "
        "хоча б одне з них у назві або описі товару — таке оголошення автоматично "
        "пропускається і не надсилається вам.\n"
        "Наприклад: Fake, Hole, Junior, Damaged, kids.\n"
        "Доступно на PRO та ULTRA. Додай виключення під час створення фільтра.</i></b>",
    ),
    "autobuy": (
        'Що таке функція "Autobuy" та як її підключити?',
        "<b><i>Знайоме відчуття, коли хтось раніше купив річ, яку ти побачив?\n\n"
        "Autobuy — це найважливіша функція цього бота, створена для найшвидшої покупки "
        "речі за вашим бажанням.\n\n"
        "Відкрийте «Підключити автобай» у FAQ: оберіть країну, введіть ліміт покупки "
        "й додайте ключ refresh_token_web зі свого акаунту Vinted.\n\n"
        "DE, FR, IT та UK підключаються окремо. Бот сам перевірить сесію й оформлення. "
        "Доставка береться зі збереженого вибору у Vinted; якщо його немає, "
        "виберіть адресу або пункт видачі на сайті. Адресу додайте один раз "
        "й натисніть «Зберегти адресу» у Vinted; повторно надсилати її боту не потрібно.\n\n"
        "У режимі особистого вікна Buy відкриває оформлення Vinted: CAPTCHA, "
        "перевірку повної суми й остаточну оплату виконуєте ви. "
        "Бот не натискає кнопку оплати. Якщо збережених даних бракує, "
        "Vinted попросить їх додати. Пароль і коди SMS у Telegram не потрібні.</i></b>",
    ),
}

if not autobuy.AVAILABLE:
    # The detailed setup copy above is retained for the future release.
    PAGES["autobuy"] = (autobuy.SOON, autobuy.SOON)
    CONNECT_AUTOBUY = autobuy.SOON

AUTOBUY_TEXT = CONNECT_TEXT if autobuy.AVAILABLE else autobuy.SOON
INSTRUCTION_TEXT = (
    "<b>Підключення Autobuy</b>\n\n"
    "Відкрий FAQ → «Підключити автобай» й пройди покрокове налаштування. "
    "Додай ключ сесії тільки на кроці «Ключ сесії Vinted».\n\n"
    "Пароль і коди підтвердження входу залишаються у Vinted."
)
if not autobuy.AVAILABLE:
    INSTRUCTION_TEXT = autobuy.SOON


def faq_rows():
    return [
        *[[(label, f"faq:{key}")] for key, (label, _) in PAGES.items()],
        *([[(CONNECT_AUTOBUY, "faq:connect")]] if autobuy.AVAILABLE else []),
        [(FAQ_BACK, "home")],
    ]


def instruction_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📖 Інструкція" if autobuy.AVAILABLE else autobuy.SOON,
                                     callback_data=INSTRUCTION_CALLBACK),
            ]
        ]
    )
