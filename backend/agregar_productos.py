import os
from dotenv import load_dotenv

os.chdir('C:/laragon/inventario/backend')
load_dotenv()

import psycopg2

db_url = os.getenv('DATABASE_URL')
conn = psycopg2.connect(db_url)
cur = conn.cursor()

# Crear/actualizar categoría única
categorias = [
    (1, 'Avicultura', 'Productos y concentrados para aves'),
]

for id_, nom, desc in categorias:
    cur.execute('INSERT INTO categorias (id, nombre, descripcion) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE SET nombre = EXCLUDED.nombre, descripcion = EXCLUDED.descripcion', (id_, nom, desc))

# Agregar columnas a productos si no existen
cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='productos'")
cols = [c[0] for c in cur.fetchall()]

if 'imagen' not in cols:
    cur.execute('ALTER TABLE productos ADD COLUMN imagen VARCHAR(255)')
    print('Columna imagen agregada')
if 'unidade_stock' not in cols:
    cur.execute('ALTER TABLE productos ADD COLUMN unidade_stock VARCHAR(20)')
    print('Columna unidade_stock agregada')

conn.commit()

# Eliminar todos los productos existentes
cur.execute('DELETE FROM productos')
print('Productos existentes eliminados')
conn.commit()

# Insertar productos nuevos (precios directos desde la imagen)
# Todos los productos en categoría 1 (Avicultura)
# Formato: (codigo, nombre, categoria_id, presentacion, precio_compra, precio_venta, stock, stock_min)
productos = [
    # Concentrados Pollitos Iniciación
    ('POLL-INIC-C', 'C. POLLITOS INICIACION C.', 1, 'Bulto', 94811, 105712, 50, 10),
    ('POLL-INIC-1K', 'C. POLLITOS INICIACION C.1K*20', 1, 'Kilo x20', 2495, 2781, 100, 20),
    ('BROL-PIG-C', 'C. BROILER PIGMENTADO C.', 1, 'Bulto', 98306, 109596, 50, 10),
    ('BROL-PIG-P', 'C. BROILER PIGMENTADO P.', 1, 'Bulto', 98306, 109596, 50, 10),
    
    # Concentrados Pollos
    ('POLL-INIC-C2', 'C. POLLO INICIACION C.', 1, 'Bulto', 86770, 96778, 50, 10),
    ('POLL-INIC-1K2', 'C. POLLO INICIACION C.1K*20', 1, 'Kilo x20', 165, 193, 100, 20),
    ('POLL-ENG-P', 'C. POLLO ENGORDE P.', 1, 'Bulto', 87471, 97557, 50, 10),
    ('POLL-ENG-1K', 'C. POLLO ENGORDE P.1K*20', 1, 'Kilo x20', 165, 193, 100, 20),
    ('POLL-ENG-C', 'C. POLLO ENGORDE C.', 1, 'Bulto', 86258, 96209, 50, 10),
    ('POLL-CAMP-P', 'C. POLLO CAMPESINO P.', 1, 'Bulto', 77213, 86159, 50, 10),
    ('POLL-CAMP-1K-P', 'C. POLLO CAMPESINO P. 1K', 1, 'Kilo x20', 2055, 2293, 100, 20),
    ('POLL-CAMP-C', 'C. POLLO CAMPESINO C.', 1, 'Bulto', 77213, 86159, 50, 10),
    ('POLL-CAMP-1K-C', 'C. POLLO CAMPESINO C. 1K', 1, 'Kilo x20', 2055, 2293, 100, 20),
    ('PREINIC-POLL', 'C. PREINICIACION POLLITOS', 1, 'Bulto', 107798, 120143, 50, 10),
    
    # Concentrados Maxi
    ('MAXI-POLL-C', 'C. MAXI-POLLITOS C.', 1, 'Bulto', 102443, 114192, 50, 10),
    ('MAXI-POLL-1K', 'C. MAXI-POLLITOS C.1K*20', 1, 'Kilo x20', 2686, 2993, 100, 20),
    ('MAXI-BROL-P', 'C. MAXI-BROILER P.', 1, 'Bulto', 102443, 114192, 50, 10),
    ('MAXI-BROL-1K', 'C. MAXI-BROILER P.1K*20', 1, 'Kilo x20', 165, 193, 100, 20),
    ('MAXI-BROL-PIG-P', 'C. MAXI-BROILER PIGMENTO P.', 1, 'Bulto', 108309, 120710, 50, 10),
    ('MAXI-BROL-C', 'C. MAXI-BROILER C.', 1, 'Bulto', 6600, 7700, 50, 10),
    ('MAXI-BROL-PIG-C', 'C. MAXI-BROILER PIGMENTO C.', 1, 'Bulto', 108309, 120710, 50, 10),
]

for cbar, nombre, cat, pres, pc, pv, stock, smin in productos:
    cur.execute('''INSERT INTO productos (codigo_barra, nombre, categoria_id, presentacion, precio_compra, precio_venta, stock_actual, stock_minimo, activo) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true)''', (cbar, nombre, cat, pres, pc, pv, stock, smin))

conn.commit()
print('1 categoria actualizada: Avicultura')
print(f'{len(productos)} productos insertados')
cur.close()
conn.close()