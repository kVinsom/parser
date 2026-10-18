"""Use Vinted's saved address in the same session; never store or invent an address.

Only observed checkout controls are recognized. An absent warning is not proof of
delivery readiness: the checkout contract still verifies the actual delivery key.
"""

from urllib.parse import urlsplit

EMPTY_ADDRESS = '[data-testid="empty-shipping-address-details"]'
ADDRESS_DIALOG = '[data-testid="shipping-address-dialog"]'

# Shared by the manual view and the atomic pre-payment snapshot. Return only a
# fixed state, never names, addresses, form values or account information.
ADDRESS_STATE = """() => {
  const visible = e => !!(e.getClientRects().length &&
    getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).visibility !== 'collapse');
  const shown = selector => [...document.querySelectorAll(selector)].some(visible);
  if (shown('[data-testid="shipping-address-dialog"]')) return 'address_editing';
  if (shown('[data-testid="empty-shipping-address-details"]')) return 'address_required';
  return null;
}"""


async def address_requirement(page, origin):
    current = urlsplit(page.url)
    if (f"{current.scheme}://{current.netloc}" != origin
            or current.path.rstrip("/") != "/checkout"):
        return None
    return await page.evaluate(ADDRESS_STATE)


async def open_address_form(page, origin):
    """An explicit Continue opens Vinted's form. Saving remains a user action."""
    if await address_requirement(page, origin) != "address_required":
        return
    candidates = [node for node in await page.locator(EMPTY_ADDRESS).all()
                  if await node.is_visible()]
    if len(candidates) != 1:
        return
    node = candidates[0]
    if await node.get_attribute("role") != "button":
        return
    await node.click(timeout=5000)
