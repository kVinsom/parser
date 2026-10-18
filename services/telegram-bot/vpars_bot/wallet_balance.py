"""One wallet attempt after Buy; retain the same checkout for manual payment."""

import asyncio

from . import autobuy_availability as autobuy
from .db import WalletOrder
from .wallet import WalletError

FALLBACK = {
    "InsufficientBalance": "На балансі Vinted бракує коштів на повну суму. Збережені дані залишилися в оформленні. Вибери спосіб оплати та підтвердь покупку сам.",
    "NotConnected": "Автооплата цього акаунта ще не перевірена або вимкнена. Оформлення відкрите для ручної оплати зі збереженими даними Vinted.",
    "WalletOnly": "Vinted не підтвердив оплату виключно з балансу. Вибери спосіб оплати сам; бот не використовував картку.",
    "ContractMismatch": "Не вдалося надійно прочитати баланс або оформлення. Перевір дані й вибери оплату вручну.",
    "ManualAction": "Vinted потребує уточнення даних. Перевір оформлення й вибери оплату вручну.",
    "CheckoutAddressRequired": "Додай або вибери збережену адресу у Vinted, потім вибери оплату вручну.",
    "CheckoutChallenge": "Пройди CAPTCHA вручну. Автооплата цього сеансу зупинена; після перевірки оплату підтверджуєш сам.",
    "CheckoutLoginRequired": "Віднови вхід у Vinted. Бот не виконував оплату.",
}


async def balance_purchase(service, order, account, identity, browser):
    """Called before streaming any pixels/inputs, after durable balance_checking."""
    autobuy.require_available()
    attempted = False
    try:
        async with asyncio.timeout(45):
            adapter = await browser.balance_adapter(account)
            # All amounts, address, item and owner come from the same visible page.
            adapter.raw = await adapter.snapshot()
            quote = adapter.quote(adapter.raw, require_wallet=False)
            quote.check_details(order, account)
            quote.check(order, account)
            await service.wallet.finish(
                order.id, "paying", quote=quote,
                expected_binding=(account.contract_hash, identity),
            )
            attempted = True
            external_id = await adapter.pay(order, quote)
            await service.wallet.finish(order.id, "succeeded", external_id=external_id)
            return "succeeded"
    except Exception as exc:
        code = str(exc) if isinstance(exc, WalletError) else "ContractMismatch"
        async with service.sessions() as session:
            current = await session.get(WalletOrder, order.id)
        if current.state == "succeeded":
            return "succeeded"
        if attempted or current.state == "paying":
            # Never offer a second payment method after an ambiguous wallet attempt.
            await service.wallet.finish(order.id, "unknown", error=code)
            return "unknown"
        if code in FALLBACK:
            await service.wallet.finish(order.id, "human_waiting", error=code)
            return "human_waiting"
        await service.wallet.finish(order.id, "failed", error=code)
        return "failed"
