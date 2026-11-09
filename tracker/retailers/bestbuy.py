from .base import Retailer, sig_elements, sig_jsonld

# The rendered buy button is the source of truth; JSON-LD is the fallback.
RETAILER = Retailer(
    key="bestbuy",
    display="Best Buy",
    default_fetch="browser",
    signals=(sig_elements, sig_jsonld),
    buy_selectors=(
        "button[data-button-state]",
        "button.add-to-cart-button",
        '[data-testid*="add-to-cart" i]',
        '[data-test-id*="add-to-cart" i]',
    ),
)
