"""
Script para migrar datos de PostgreSQL a MySQL.
Ejecutar UNA sola vez después de crear las tablas en MySQL.

Uso:
  python migrate_to_mysql.py

Requiere:
  - DATABASE_URL en .env apuntando a PostgreSQL (origen)
  - MYSQL_URL en .env apuntando a MySQL de Hostinger (destino)
"""
import os
from sqlalchemy import create_engine, text, inspect
from dotenv import load_dotenv

load_dotenv()

PG_URL    = os.getenv("DATABASE_URL")       # PostgreSQL origen
MYSQL_URL = os.getenv("MYSQL_URL")          # MySQL destino

if not PG_URL or not MYSQL_URL:
    print("ERROR: Necesitas DATABASE_URL (PostgreSQL) y MYSQL_URL (MySQL) en el .env")
    exit(1)

# Asegurarse de que MySQL usa pymysql
if "pymysql" not in MYSQL_URL:
    MYSQL_URL = MYSQL_URL.replace("mysql://", "mysql+pymysql://")

pg_engine    = create_engine(PG_URL)
mysql_engine = create_engine(MYSQL_URL, connect_args={"charset": "utf8mb4"})

# Orden respetando FK
TABLES = [
    'roles', 'usuarios', 'categorias', 'unidades_medida',
    'proveedores', 'productos', 'clientes', 'configuraciones',
    'facturas', 'detalle_factura', 'movimientos_inventario',
    'notas_credito', 'detalle_nota_credito',
]

insp = inspect(pg_engine)
existing_tables = insp.get_table_names()

print("Iniciando migración PostgreSQL → MySQL...\n")

with pg_engine.connect() as pg_conn, mysql_engine.begin() as my_conn:
    # Deshabilitar FK checks en MySQL para insertar sin orden
    my_conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))

    for table in TABLES:
        if table not in existing_tables:
            print(f"  ⚠️  Tabla '{table}' no existe en PostgreSQL, saltando...")
            continue

        rows = pg_conn.execute(text(f"SELECT * FROM {table}")).fetchall()
        if not rows:
            print(f"  ✓  {table}: vacía")
            continue

        col_names = [c['name'] for c in insp.get_columns(table)]
        placeholders = ', '.join([f':{c}' for c in col_names])
        insert_sql = f"INSERT IGNORE INTO {table} ({', '.join(col_names)}) VALUES ({placeholders})"

        data = []
        for row in rows:
            row_dict = {}
            for i, col in enumerate(col_names):
                val = row[i]
                # Convertir tipos incompatibles
                if isinstance(val, bool):
                    val = 1 if val else 0
                row_dict[col] = val
            data.append(row_dict)

        my_conn.execute(text(insert_sql), data)

        # Resetear auto_increment si aplica
        pk_constraint = insp.get_pk_constraint(table)
        pk_cols = pk_constraint.get('constrained_columns', [])
        if pk_cols and len(pk_cols) == 1:
            pk = pk_cols[0]
            try:
                max_id = pg_conn.execute(text(f"SELECT MAX({pk}) FROM {table}")).scalar()
                if max_id:
                    my_conn.execute(text(f"ALTER TABLE {table} AUTO_INCREMENT = {int(max_id) + 1}"))
            except Exception:
                pass

        print(f"  ✓  {table}: {len(rows)} filas migradas")

    my_conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

print("\n✅ Migración completada.")
