import uuid
from typing import Callable, Dict, Optional

# async pg
import anyio
import sqlalchemy
from pgvector.sqlalchemy import Vector
from pydantic import Field
from sqlalchemy import Column, String, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from sqlalchemy_utils import create_database, database_exists

import controlflow
from controlflow.memory.async_memory import AsyncMemoryProvider
from controlflow.memory.memory import MemoryProvider

try:
    # For embeddings, we can use langchain_openai or any other library:
    from langchain_openai import OpenAIEmbeddings
except ImportError:
    raise ImportError(
        "To use an embedding function similar to LanceDB's default, "
        "please install lancedb with: pip install lancedb"
    )

# SQLAlchemy base class for declarative models
Base = declarative_base()


class SQLMemoryTable(Base):
    """
    A simple declarative model that represents a memory record.

    We’ll dynamically set the __tablename__ at runtime.
    """

    __abstract__ = True
    id = Column(String, primary_key=True)
    text = Column(String)
    # Use pgvector for storing embeddings in a Postgres Vector column
    # vector = Column(Vector(dim=1536))  # Adjust dimension to match your embedding model


class PostgresMemory(MemoryProvider):
    """
    A ControlFlow MemoryProvider that stores text + embeddings in PostgreSQL
    using SQLAlchemy and pg_vector. Each Memory module gets its own table.
    """

    # Default database URL. You can point this to your actual Postgres instance.
    # Requires the pgvector extension installed and the sqlalchemy-pgvector package.
    database_url: str = Field(
        default="postgresql://user:password@localhost:5432/your_database",
        description="SQLAlchemy-compatible database URL to a Postgres instance with pgvector.",
    )
    table_name: str = Field(
        "memory_{key}",
        description="""
            Name of the table to store this memory partition. "{key}" will be replaced 
            by the memory’s key attribute.
        """,
    )

    embedding_dimension: int = Field(
        default=1536,
        description="Dimension of the embedding vectors. Match your model's output.",
    )

    embedding_fn: Callable = Field(
        default_factory=lambda: OpenAIEmbeddings(
            model="text-embedding-ada-002",
        ),
        description="A function that turns a string into a vector.",
    )

    # Connection pool settings
    pool_size: int = Field(
        5,
        description="Number of connections to keep open in the pool.",
    )

    max_overflow: int = Field(
        10,
        description="Number of connections to allow that can overflow the pool.",
    )

    pool_timeout: int = Field(
        30,
        description="Number of seconds to wait before giving up on getting a connection.",
    )

    pool_recycle: int = Field(
        1800,
        description="Number of seconds a connection can be idle before being recycled.",
    )

    pool_pre_ping: bool = Field(
        True,
        description="Check the connection health upon checkout.",
    )

    # Internal: keep a cached Session maker
    _SessionLocal: Optional[sessionmaker] = None

    # This dict will map "table_name" -> "model class"
    _table_class_cache: Dict[str, Base] = {}

    def configure(self, memory_key: str) -> None:
        """
        Configure a SQLAlchemy session w/connection pooling and ensure the table for this
        memory partition is created if it does not already exist.
        """
        engine = sqlalchemy.create_engine(
            self.database_url,
            pool_size=self.pool_size,
            max_overflow=self.max_overflow,
            pool_timeout=self.pool_timeout,
            pool_recycle=self.pool_recycle,
            pool_pre_ping=self.pool_pre_ping,
        )

        # 2) If DB doesn't exist, create it!
        if not database_exists(engine.url):
            create_database(engine.url)

        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()

        self._SessionLocal = sessionmaker(bind=engine)

        # Dynamically create a specialized table model for this memory_key
        table_name = self.table_name.format(key=memory_key)

        # 1) Check if table already in metadata
        if table_name not in Base.metadata.tables:
            # 2) Create the dynamic class + table
            memory_model = type(
                f"SQLMemoryTable_{memory_key}",
                (SQLMemoryTable,),
                {
                    "__tablename__": table_name,
                    "vector": Column(Vector(dim=self.embedding_dimension)),
                },
            )

            try:
                Base.metadata.create_all(engine, tables=[memory_model.__table__])
                # Store it in the cache
                self._table_class_cache[table_name] = memory_model
            except ProgrammingError as e:
                raise RuntimeError(f"Failed to create table {table_name}: {e}")

    def _get_session(self) -> Session:
        if not self._SessionLocal:
            raise RuntimeError(
                "Session is not initialized. Make sure to call configure() first."
            )
        return self._SessionLocal()

    def _get_table(self, memory_key: str) -> Base:
        """
        Return a dynamically generated declarative model class
        mapped to the memory_{key} table. Each memory partition
        has a separate table.
        """
        table_name = self.table_name.format(key=memory_key)

        # Return the cached class if already built
        if table_name in self._table_class_cache:
            return self._table_class_cache[table_name]

        # If for some reason it's not there, create it now (or raise error):
        memory_model = type(
            f"SQLMemoryTable_{memory_key}",
            (SQLMemoryTable,),
            {
                "__tablename__": table_name,
                "vector": Column(Vector(dim=self.embedding_dimension)),
            },
        )
        self._table_class_cache[table_name] = memory_model
        return memory_model

    def add(self, memory_key: str, content: str) -> str:
        """
        Insert a new memory record into the Postgres table,
        generating an embedding and storing it in a vector column.
        Returns the memory’s ID (uuid).
        """
        memory_id = str(uuid.uuid4())
        model_cls = self._get_table(memory_key)

        # Generate an embedding for the content
        embedding = self.embedding_fn.embed_query(content)

        with self._get_session() as session:
            record = model_cls(id=memory_id, text=content, vector=embedding)
            session.add(record)
            session.commit()

        return memory_id

    def delete(self, memory_key: str, memory_id: str) -> None:
        """
        Delete a memory record by its UUID.
        """
        model_cls = self._get_table(memory_key)

        with self._get_session() as session:
            session.query(model_cls).filter(model_cls.id == memory_id).delete()
            session.commit()

    def search(self, memory_key: str, query: str, n: int = 20) -> Dict[str, str]:
        """
        Uses pgvector’s approximate nearest neighbor search with the `<->` operator to find
        the top N matching records for the embedded query. Returns a dict of {id: text}.
        """
        model_cls = self._get_table(memory_key)
        # Generate embedding for the query
        query_embedding = self.embedding_fn.embed_query(query)
        embedding_col = model_cls.vector

        with self._get_session() as session:
            results = session.execute(
                select(model_cls.id, model_cls.text)
                .order_by(embedding_col.l2_distance(query_embedding))
                .limit(n)
            ).all()

        return {row.id: row.text for row in results}


class AsyncPostgresMemory(AsyncMemoryProvider):
    """
    An async MemoryProvider storing text + embeddings in PostgreSQL
    using SQLAlchemy + pg_vector, but with full async support.
    """

    database_url: str = Field(
        default="postgresql+asyncpg://user:password@localhost:5432/your_database",
        description="Async Postgres URL with the asyncpg driver, e.g. "
        "'postgresql+asyncpg://user:pass@host:5432/dbname'.",
    )

    table_name: str = Field(
        "memory_{key}",
        description="""
            Name of the table for this memory partition. "{key}" gets replaced by the memory key.
        """,
    )

    embedding_dimension: int = Field(
        default=1536,
        description="Dimension of the embedding vectors. Must match your model output size.",
    )

    embedding_fn: Callable = Field(
        default_factory=lambda: OpenAIEmbeddings(model="text-embedding-ada-002"),
        description="Function that turns a string into a numeric vector.",
    )

    # -- Pool / Engine settings (SQLAlchemy will do the pooling)
    pool_size: int = Field(
        5, description="Number of permanent connections in the async pool."
    )
    max_overflow: int = Field(
        10, description="Number of 'overflow' connections if the pool is full."
    )
    pool_timeout: int = Field(
        30, description="Seconds to wait for a connection before raising an error."
    )
    pool_recycle: int = Field(
        1800,
        description="Recycle connections after N seconds to avoid stale connections.",
    )
    pool_pre_ping: bool = Field(
        True, description="Check connection health before using from the pool."
    )

    # We'll store an async engine + session factory:
    _engine: Optional[AsyncEngine] = None
    _SessionLocal: Optional[async_sessionmaker[AsyncSession]] = None

    # Cache for dynamically generated table classes
    _table_class_cache: Dict[str, Base] = {}

    _configured: bool = False

    async def configure(self, memory_key: str) -> None:
        """
        1) Create an async engine.
        2) Optionally create the DB if it doesn't exist (requires sync workaround).
        3) Install pgvector extension.
        4) Generate the memory table if missing.
        5) Initialize the async sessionmaker.
        """
        if self._configured:
            return
        # 1) Create an async engine. Use the asyncpg dialect.
        #    The pool settings are configured in 'create_async_engine' with 'pool_size', etc.
        else:
            self._engine = create_async_engine(
                self.database_url,
                pool_size=self.pool_size,
                max_overflow=self.max_overflow,
                pool_timeout=self.pool_timeout,
                pool_recycle=self.pool_recycle,
                pool_pre_ping=self.pool_pre_ping,
            )

            exists = await anyio.to_thread.run_sync(database_exists, self.database_url)
            if not exists:
                await anyio.to_thread.run_sync(create_database, self.database_url)

            # 3) Run migrations / create extension in an async context:
            async with self._engine.begin() as conn:
                # Create the pgvector extension if not exists
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                # We'll create the table for the memory_key specifically
                # (1) Build the dynamic table class
                table_name = self.table_name.format(key=memory_key)
                if table_name not in Base.metadata.tables:
                    memory_model = type(
                        f"SQLMemoryTable_{memory_key}",
                        (SQLMemoryTable,),
                        {
                            "__tablename__": table_name,
                            "vector": Column(Vector(dim=self.embedding_dimension)),
                        },
                    )
                    self._table_class_cache[table_name] = memory_model

                    # (2) Actually create it (async):
                    def _sync_create(connection):
                        """Helper function to run table creation in sync context."""
                        Base.metadata.create_all(
                            connection, tables=[memory_model.__table__]
                        )

                    try:
                        await conn.run_sync(_sync_create)
                    except ProgrammingError as e:
                        raise RuntimeError(
                            f"Failed to create table '{table_name}': {e}"
                        )

            # 4) Now that the DB and table are ready, create a session factory
            self._SessionLocal = async_sessionmaker(
                self._engine,
                expire_on_commit=False,
            )

            self._configured = True

    def _get_table(self, memory_key: str) -> Base:
        """
        Return or create the dynamic model class for 'memory_{key}' table.
        """
        table_name = self.table_name.format(key=memory_key)
        if table_name in self._table_class_cache:
            return self._table_class_cache[table_name]

        # If not found, define it at runtime (we won't auto-create it here though)
        memory_model = type(
            f"SQLMemoryTable_{memory_key}",
            (SQLMemoryTable,),
            {
                "__tablename__": table_name,
                "vector": Column(Vector(dim=self.embedding_dimension)),
            },
        )
        self._table_class_cache[table_name] = memory_model
        return memory_model

    async def add(self, memory_key: str, content: str) -> str:
        """
        Insert a new record with an embedding vector.
        Returns the inserted record's UUID.
        """
        # lazy config
        if not self._configured:
            await self.configure(memory_key)

        if not self._SessionLocal:
            raise RuntimeError("Call 'configure(...)' before using this provider.")

        memory_id = str(uuid.uuid4())
        model_cls = self._get_table(memory_key)
        embedding = self.embedding_fn.embed_query(content)

        async with self._SessionLocal() as session:
            record = model_cls(
                id=memory_id,
                text=content,
                vector=embedding,
            )
            session.add(record)
            await session.commit()

        return memory_id

    async def delete(self, memory_key: str, memory_id: str) -> None:
        """
        Delete a record by UUID.
        """
        # lazy config
        if not self._configured:
            await self.configure(memory_key)

        if not self._SessionLocal:
            raise RuntimeError("Not configured. Call 'configure(...)' first.")

        model_cls = self._get_table(memory_key)

        async with self._SessionLocal() as session:
            await session.execute(
                sqlalchemy.delete(model_cls).where(model_cls.id == memory_id)
            )
            await session.commit()

    async def search(self, memory_key: str, query: str, n: int = 20) -> Dict[str, str]:
        """
        Async nearest-neighbor search via pgvector <-> operator or .l2_distance(),
        returning up to N results as {id: text}.
        """

        # lazy config
        if not self._configured:
            await self.configure(memory_key)

        if not self._SessionLocal:
            raise RuntimeError("Not configured. Call 'configure(...)' first.")

        model_cls = self._get_table(memory_key)
        embedding = self.embedding_fn.embed_query(query)
        embedding_col = model_cls.vector

        async with self._SessionLocal() as session:
            # Example using l2_distance:
            results = await session.execute(
                select(model_cls.id, model_cls.text)
                .order_by(embedding_col.l2_distance(embedding))
                .limit(n)
            )
            rows = results.all()

        # Convert list of Row objects -> dict
        return {row.id: row.text for row in rows}