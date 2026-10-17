"""Broker interface shared by BacktestBroker, PaperBroker and UpbitLiveBroker."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from crypto_trader.core.models import Fill, Mode, Order, OrderRequest


@dataclass
class Balance:
    currency: str
    free: float
    locked: float = 0.0
    avg_buy_price: float = 0.0

    @property
    def total(self) -> float:
        return self.free + self.locked


class OrderRejected(Exception):
    pass


class Broker(ABC):
    mode: Mode

    @abstractmethod
    def submit(self, req: OrderRequest) -> Order:
        """Submit an order. Must never silently resend; an unclear outcome yields state UNKNOWN."""

    @abstractmethod
    def cancel(self, client_order_id: str) -> Order: ...

    @abstractmethod
    def get_order(self, client_order_id: str) -> Order | None: ...

    @abstractmethod
    def open_orders(self) -> list[Order]: ...

    @abstractmethod
    def balances(self) -> dict[str, Balance]: ...

    def drain_fills(self) -> list[Fill]:
        """Fills produced since the last call (for the ledger)."""
        return []
