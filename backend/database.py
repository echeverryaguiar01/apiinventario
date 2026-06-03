import os
from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

# Obtener la ruta del directorio actual (backend/)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(BASE_DIR, ".env")
print(f"Buscando .env en: {env_path}")

# Cargar el .env
load_dotenv(env_path)

SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL")
print(f"URL cargada: {SQLALCHEMY_DATABASE_URL}")

if not SQLALCHEMY_DATABASE_URL:
    raise ValueError("Error: No se encontró DATABASE_URL en el archivo .env")

engine = create_engine(SQLALCHEMY_DATABASE_URL)

@event.listens_for(engine, "connect")
def set_default_schema(dbapi_connection, connection_record):
    # Neon pooler puede iniciar sin schema por defecto; lo fijamos en cada conexión.
    with dbapi_connection.cursor() as cursor:
        cursor.execute("SET search_path TO public")

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# Dependencia para obtener la sesión de DB en las rutas
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
