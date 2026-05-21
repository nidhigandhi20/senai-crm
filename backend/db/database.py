from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv
from db.models import Base

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://nidhigandhi@localhost:5432/senai_crm")

# Create the engine — this is the connection to PostgreSQL
engine = create_engine(DATABASE_URL, echo=False)

# SessionLocal is a factory — call it to get a database session
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """
    FastAPI dependency — use this in every route that needs the database.

    Usage in a route:
        from db.database import get_db
        from sqlalchemy.orm import Session
        from fastapi import Depends

        @app.get("/example")
        def example(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables():
    """
    Creates all tables directly from models.
    Only use this in development/testing.
    In production always use Alembic migrations.
    """
    Base.metadata.create_all(bind=engine) 