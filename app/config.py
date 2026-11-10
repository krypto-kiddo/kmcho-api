from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str
    JWT_SECRET: str
    ENVIRONMENT: str = "development"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    SHEET_SYNC_SECRET: str = ""  # shared with the kitchen sheet's Apps Script

    class Config:
        env_file = ".env"

settings = Settings()