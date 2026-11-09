from .base import Retailer, sig_elements, sig_jsonld

RETAILER = Retailer(
    key="target",
    display="Target",
    default_fetch="browser",
    signals=(sig_elements, sig_jsonld),
    buy_selectors=(
        '[data-test="shippingButton"]',
        '[data-test="orderPickupButton"]',
        '[data-test="deliveryButton"]',
        '[data-test="preorderButton"]',
        'button[id^="addToCartButtonOrTextIdFor"]',
    ),
    out_selectors=(
        '[data-test="soldOutBlock"]',
        '[data-test="outOfStockBlock"]',
        '[data-test="NonbuyableSection"]',
    ),
)
