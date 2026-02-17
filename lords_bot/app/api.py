from fastapi import FastAPI, HTTPException

from app.account_service import AccountService
from app.auth import AuthService
from app.fyers_client import FyersAPIError, FyersClient
from app.order_service import OrderService
from app.schemas import OrderRequest

app = FastAPI(title="Lords FYERS Bot", version="1.0.0")
auth_service = AuthService()
client = FyersClient(auth_service)
account_service = AccountService(client)
order_service = OrderService(client)


@app.post("/auth/validate")
async def validate(auth_code: str) -> dict:
    tokens = await auth_service.validate_auth_code(auth_code)
    return {"status": "ok", "access_token": tokens.access_token[:8] + "..."}


@app.get("/account/profile")
async def profile() -> dict:
    try:
        return await account_service.get_profile()
    except FyersAPIError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "error": str(exc)}) from exc


@app.post("/orders")
async def place_order(order: OrderRequest) -> dict:
    try:
        return await order_service.place_order(order)
    except (FyersAPIError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
