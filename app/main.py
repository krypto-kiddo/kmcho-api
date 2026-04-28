from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from app.routers import users, orders, ledger
from app.routers import auth

app = FastAPI(
    title="KMCho API",
    description="Prepaid meal wallet — customer ledger, payments and order tracking API",
    version="0.1.0",
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(orders.router)
app.include_router(ledger.router)

@app.get("/")
async def root():
    return {"message": "KMCho API is running"}

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    schema["components"]["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }
    }
    for path in schema["paths"].values():
        for method in path.values():
            method["security"] = [{"BearerAuth": []}]
    app.openapi_schema = schema
    return schema

app.openapi = custom_openapi