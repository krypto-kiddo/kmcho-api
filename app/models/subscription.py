from sqlalchemy import Column, Integer, Boolean, Date, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base

class Subscription(Base):
    __tablename__ = "subscriptions"

    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    active        = Column(Boolean, default=True, nullable=False)
    paused_until  = Column(Date, nullable=True)
    bowl          = Column(JSONB, nullable=True)
    broth         = Column(JSONB, nullable=True)
    protein_index = Column(Integer, default=0, nullable=False)
    base_index    = Column(Integer, default=0, nullable=False)
    created_at    = Column(DateTime, server_default=func.now())
    updated_at    = Column(DateTime, server_default=func.now(), onupdate=func.now())
    user = relationship("User", lazy="joined")