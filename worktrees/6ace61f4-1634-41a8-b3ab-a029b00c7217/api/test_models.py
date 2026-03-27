"""Tests for Pydantic v2 models: User, Product, OrderItem, Order."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from models import Order, OrderItem, Product, User


# ── User ──────────────────────────────────────────────────────────────


class TestUser:
    def test_valid_user(self):
        u = User(name="Alice", email="alice@example.com", age=30)
        assert u.name == "Alice"
        assert u.email == "alice@example.com"
        assert u.age == 30

    def test_invalid_email(self):
        with pytest.raises(ValidationError) as exc_info:
            User(name="Bob", email="not-an-email", age=25)
        assert "email" in str(exc_info.value)

    def test_negative_age(self):
        with pytest.raises(ValidationError) as exc_info:
            User(name="Carol", email="carol@example.com", age=-1)
        assert "age" in str(exc_info.value)

    def test_zero_age_is_valid(self):
        u = User(name="Baby", email="baby@example.com", age=0)
        assert u.age == 0


# ── Product ───────────────────────────────────────────────────────────


class TestProduct:
    def test_valid_product(self):
        p = Product(title="Widget", price=Decimal("9.99"))
        assert p.title == "Widget"
        assert p.price == Decimal("9.99")
        assert p.currency == "USD"

    def test_custom_currency(self):
        p = Product(title="Gadget", price=Decimal("100"), currency="EUR")
        assert p.currency == "EUR"

    def test_zero_price_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            Product(title="Free", price=Decimal("0"))
        assert "price" in str(exc_info.value)

    def test_negative_price_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            Product(title="Bad", price=Decimal("-5"))
        assert "price" in str(exc_info.value)


# ── Order ─────────────────────────────────────────────────────────────


def _make_items() -> list[dict]:
    return [
        {"product_title": "A", "price": "10.00", "qty": 2},
        {"product_title": "B", "price": "5.50", "qty": 1},
    ]


class TestOrder:
    def test_valid_order(self):
        items = _make_items()
        # 10*2 + 5.5*1 = 25.50
        order = Order(user_id=1, items=items, total=Decimal("25.50"))
        assert order.total == Decimal("25.50")
        assert len(order.items) == 2

    def test_total_mismatch_rejected(self):
        items = _make_items()
        with pytest.raises(ValidationError) as exc_info:
            Order(user_id=1, items=items, total=Decimal("99.99"))
        assert "total" in str(exc_info.value).lower()

    def test_empty_items_total_zero(self):
        order = Order(user_id=1, items=[], total=Decimal("0"))
        assert order.total == Decimal("0")

    def test_empty_items_nonzero_total_rejected(self):
        with pytest.raises(ValidationError):
            Order(user_id=1, items=[], total=Decimal("10"))

    def test_single_item_order(self):
        order = Order(
            user_id=42,
            items=[{"product_title": "X", "price": "7.00", "qty": 3}],
            total=Decimal("21.00"),
        )
        assert order.total == Decimal("21.00")
