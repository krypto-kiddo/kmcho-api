from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str
    JWT_SECRET: str
    ENVIRONMENT: str = "development"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    SHEET_SYNC_SECRET: str = ""  # shared with the kitchen sheet's Apps Script
    # WhatsApp Cloud API, same values the kitchen sheet uses (login codes)
    WA_TOKEN: str = ""
    PHONE_NUMBER_ID: str = ""
    WA_GRAPH_VERSION: str = "v23.0"
    OTP_TEMPLATE: str = "kmcho_login_otp"
    OTP_DAILY_CAP: int = 150   # all numbers together, per IST day

    class Config:
        env_file = ".env"

settings = Settings()