from __future__ import annotations

from ..models import Page, Status, Verdict
from ..parse import Extract, dig, find_dict_with, to_price
from .base import Retailer, sig_elements, sig_jsonld


def sig_next_data(retailer: Retailer, page: Page, ext: Extract) -> Verdict | None:
    """Walmart hydrates the page from a __NEXT_DATA__ JSON blob that carries
    availability, price and the seller. Marketplace resellers list sold-out
    items at a markup, so a non-Walmart seller does not count as a restock."""
    if ext.next_data is None:
        return None
    product = dig(ext.next_data, "props", "pageProps", "initialData", "data", "product")
    if not isinstance(product, dict) or "availabilityStatus" not in product:
        product = find_dict_with(ext.next_data, "availabilityStatus")
    if not isinstance(product, dict):
        return None
    availability = str(product.get("availabilityStatus") or "").upper()
    if not availability:
        return None
    price = to_price(dig(product, "priceInfo", "currentPrice", "price"))
    seller = str(product.get("sellerDisplayName") or product.get("sellerName") or "")
    if availability != "IN_STOCK":
        return Verdict(Status.OUT_OF_STOCK, price, f"walmart data: {availability}")
    if seller and "walmart" not in seller.lower():
        return Verdict(Status.OUT_OF_STOCK, price, f"only third-party seller: {seller}")
    return Verdict(Status.IN_STOCK, price, f"walmart data: IN_STOCK, sold by {seller or 'unknown'}")


RETAILER = Retailer(
    key="walmart",
    display="Walmart",
    default_fetch="browser",
    signals=(sig_next_data, sig_elements, sig_jsonld),
    buy_selectors=(
        '[data-automation-id="atc"]',
        'button[data-automation-id*="add-to-cart" i]',
        '[data-testid*="add-to-cart" i]',
    ),
)
