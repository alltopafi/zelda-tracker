from __future__ import annotations

from . import bestbuy, hori, nintendo, target, walmart
from .base import Retailer

REGISTRY: dict[str, Retailer] = {
    module.RETAILER.key: module.RETAILER for module in (nintendo, hori, bestbuy, target, walmart)
}

__all__ = ["REGISTRY", "Retailer"]
