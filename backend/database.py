import os
from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

# Load .env for local development. override=False ensures that variables
# already injected by the runtime environment (e.g. Railway) are never
# overwritten. If no .env file is found, load_dotenv() silently does nothing.
load_dotenv(override=False)

SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL")
print(f"URL cargada: {SQLALCHEMY_DATABASE_URL}")

if not SQLALCHEMY_DATABASE_URL:
    raise ValueError("Error: No se encontró DATABASE_URL en el archivo .env")

# Detectar tipo de base de datos
IS_MYSQL = SQLALCHEMY_DATABASE_URL.startswith("mysql")
IS_POSTGRES = SQLALCHEMY_DATABASE_URL.startswith("postgresql") or SQLALCHEMY_DATABASE_URL.startswith("postgres")

# Configurar el engine según el tipo de BD
if IS_MYSQL:
    # MySQL: usar pymysql, charset utf8mb4
    if "pymysql" not in SQLALCHEMY_DATABASE_URL:
        SQLALCHEMY_DATABASE_URL = SQLALCHEMY_DATABASE_URL.replace("mysql://", "mysql+pymysql://")
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        connect_args={
            "charset": "utf8mb4",
            "ssl_disabled": True
        },
        pool_pre_ping=True,
        pool_recycle=3600,
    )
else:
    # PostgreSQL (Neon o local)
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        pool_pre_ping=True,
    )
    @event.listens_for(engine, "connect")
    def set_default_schema(dbapi_connection, connection_record):
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET search_path TO public")

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
