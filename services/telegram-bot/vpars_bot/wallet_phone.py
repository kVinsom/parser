"""Recognize Vinted's observed delivery-phone prompt without reading the number."""

from urllib.parse import urlsplit

# Observed in DE checkout on 2026-10-13. A missing prompt is not proof that the
# phone is valid: the rest of the verified checkout contract remains mandatory.
PHONE_STATE = """() => {
  const visible = e => !!(e.getClientRects().length &&
    getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).visibility !== 'collapse');
  const shown = selector => [...document.querySelectorAll(selector)].some(visible);
  if (shown('[data-testid="shipping-contact-modal"]')) return 'phone_editing';
  if (shown('[data-testid="shipping-contact-details"] [data-testid="shipping-contact-Plus-icon"]'))
    return 'phone_required';
  return null;
}"""


async def phone_requirement(page, origin):
    current = urlsplit(page.url)
    if (f"{current.scheme}://{current.netloc}" != origin
            or current.path.rstrip("/") != "/checkout"):
        return None
    return await page.evaluate(PHONE_STATE)
