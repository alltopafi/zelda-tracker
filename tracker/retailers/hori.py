from .base import Retailer, sig_elements, sig_jsonld, sig_meta

# stores.horiusa.com: read JSON-LD first, then availability meta/microdata tags.
RETAILER = Retailer(
    key="hori",
    display="HORI USA",
    default_fetch="http",
    signals=(sig_jsonld, sig_meta, sig_elements),
    buy_selectors=("#form-action-addToCart", 'input[value*="add to cart" i]'),
)
