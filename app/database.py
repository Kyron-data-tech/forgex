from pathlib import Path
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from app.config import settings
from app.models.job import Base

DB_PATH = settings.DATABASE_PATH
DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH.as_posix()}"

engine = create_async_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

async def init_db():
    """Initializes tables on startup and applies safety migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Verify run_id exists in jobs table
        def _check_and_migrate(connection):
            import sqlite3
            cursor = connection.connection.cursor()
            cursor.execute("PRAGMA table_info(jobs)")
            cols = [col[1] for col in cursor.fetchall()]
            if cols and "run_id" not in cols:
                cursor.execute("ALTER TABLE jobs ADD COLUMN run_id INTEGER REFERENCES runs(id)")
                cursor.connection.commit()
        await conn.run_sync(_check_and_migrate)

async def get_db():
    """FastAPI dependency for DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
