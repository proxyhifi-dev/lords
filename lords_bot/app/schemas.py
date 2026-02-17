from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    qty: int = Field(..., gt=0)
    type: int
    side: Literal[1, -1]
    productType: str
    limitPrice: float = Field(default=0, ge=0)
    stopPrice: float = Field(default=0, ge=0)
    validity: str = "DAY"
    disclosedQty: int = Field(default=0, ge=0)
    offlineOrder: bool = False
    stopLoss: float = Field(default=0, ge=0)
    takeProfit: float = Field(default=0, ge=0)
    orderTag: str | None = None

    @field_validator("orderTag")
    @classmethod
    def validate_order_tag(cls, value: str | None) -> str | None:
        if value and len(value) > 20:
            raise ValueError("orderTag should be <= 20 characters")
        return value

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        if ":" not in value:
            raise ValueError("symbol must include exchange prefix e.g. NSE:SBIN-EQ")
        return value


class MultiOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orders: list[OrderRequest] = Field(min_length=1, max_length=50)


class MultiLegOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orderType: Literal["3L", "2L"]
    legs: list[OrderRequest] = Field(min_length=2, max_length=3)


class AutoSliceOrderRequest(OrderRequest):
    sliceQuantity: int = Field(..., gt=0)
