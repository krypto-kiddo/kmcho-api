from fastapi import FastAPI
from app.routers import users, orders, ledger

app = FastAPI(
    title="KMCho API",
    description="Prepaid meal wallet — customer ledger, payments and order tracking API",
    version="0.1.0",
)

app.include_router(users.router)
app.include_router(orders.router)
app.include_router(ledger.router)

@app.get("/")
async def root():
    return {"message": "KMCho API is running"}