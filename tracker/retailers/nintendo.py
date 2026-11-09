from .base import Retailer, sig_elements, sig_jsonld, sig_meta

# nintendo.com product pages embed a schema.org Product with offers.availability.
# The selectors are only used if you switch this retailer to fetch = "browser".
RETAILER = Retailer(
    key="nintendo",
    display="Nintendo Store",
    default_fetch="http",
    signals=(sig_jsonld, sig_meta, sig_elements),
    buy_selectors=('button[data-testid*="add-to-cart" i]', 'button[class*="AddToCart" i]'),
)
