"""Pydantic v2 models: User, Product, OrderItem, Order."""

from decimal import Decimal
from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


class User(BaseModel):
    model_config = ConfigDict(strict=False)

    name: str
    email: EmailStr
    age: int = Field(ge=0)


class Product(BaseModel):
    title: str
    price: Decimal = Field(gt=0)
    currency: str = "USD"


class OrderItem(BaseModel):
    product_title: str
    price: Decimal = Field(gt=0)
    qty: int = Field(gt=0)


class Order(BaseModel):
    user_id: int
    items: list[OrderItem]
    total: Decimal

    @model_validator(mode="after")
    def check_total_matches_sum(self) -> "Order":
        expected = sum(item.price * item.qty for item in self.items)
        if self.total != expected:
            raise ValueError(
                f"total {self.total} does not match sum of items {expected}"
            )
        return self
