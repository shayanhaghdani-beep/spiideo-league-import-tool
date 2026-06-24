"""Product classification by pricebook-family membership (ported from engine).

Used by the per-row Opp Product emitter. A product is classified by its Family
in the pricebook (subscription vs camera), not by name regex.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config
from .load_pricebook import PricebookIndex
from .schema import Product


@dataclass(frozen=True)
class ProductClassification:
    """All derived fields a single OLI row needs from a product name."""
    product: Product
    family: str
    charge_type: str
    unit_of_measure: str
    sales_price: float
    billing_period: str
    price_period: str
    camera_order_type: str
    height: str
    distance_from_sideline: str
    opp_currency: str


def classify(product_name: str, pricebook: PricebookIndex) -> ProductClassification:
    """Classify a single product. Raises KeyError if not in the pricebook."""
    product = pricebook.require(product_name)

    if product.family == "subscription":
        return ProductClassification(
            product=product,
            family="subscription",
            charge_type=config.SUBSCRIPTION_CHARGE_TYPE,
            unit_of_measure=config.SUBSCRIPTION_UOM,
            sales_price=product.list_price,
            billing_period=config.DEFAULT_BILLING_PERIOD,
            price_period=config.DEFAULT_PRICE_PERIOD,
            camera_order_type="",
            height="",
            distance_from_sideline="",
            opp_currency=product.currency,
        )

    if product.family == "camera":
        return ProductClassification(
            product=product,
            family="camera",
            charge_type=config.CAMERA_CHARGE_TYPE,
            unit_of_measure=config.CAMERA_UOM,
            sales_price=0.0,
            billing_period="",
            price_period=config.DEFAULT_PRICE_PERIOD,
            camera_order_type=config.CAMERA_ORDER_TYPE,
            height=str(config.CAMERA_DEFAULT_HEIGHT),
            distance_from_sideline=str(config.CAMERA_DEFAULT_DISTANCE),
            opp_currency=product.currency,
        )

    raise ValueError(f"Unknown product family {product.family!r} for {product_name!r}")
