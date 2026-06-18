import sys
# Reconfigurar salida estándar para UTF-8 en Windows para evitar UnicodeEncodeError con emojis
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

from fastapi import FastAPI, HTTPException, Depends, Body, File, UploadFile
from sqlalchemy.orm import Session
from sqlalchemy import func, text
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional
from datetime import datetime, timedelta
import os
import secrets
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import pytz

import models
from database import engine, get_db
from services.twilio_service import twilio_service
from services.factus_service import factus_service
from pydantic import BaseModel, Field, field_validator

# Cargar configuración
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# NOTE: load_dotenv() is intentionally omitted here. database.py (imported above)
# already calls load_dotenv(override=False), which loads a local .env for
# development while never overwriting variables injected by the runtime
# environment (e.g. Railway). Calling load_dotenv() again here with the default
# override=True would let a stale local .env clobber Railway's DATABASE_URL.

# Crear tablas si no existen
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Hanter API")

UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/api/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# Servir facturas generadas como archivos estáticos para que el frontend pueda abrirlas
FACTURAS_DIR = os.path.join(BASE_DIR, "facturas")
os.makedirs(FACTURAS_DIR, exist_ok=True)
app.mount("/facturas", StaticFiles(directory=FACTURAS_DIR), name="facturas")

cors_origins = os.getenv("BACKEND_CORS_ORIGINS", "http://localhost:4321,http://localhost:3000")
allow_all_origins = cors_origins.strip() == "*"
allowed_origins = [] if allow_all_origins else [origin.strip() for origin in cors_origins.split(",") if origin.strip()]

# Si no se configuraron orígenes y no estamos en modo '*' asumimos orígenes de desarrollo locales
if not allow_all_origins and not allowed_origins:
    allowed_origins = ["http://localhost:4321", "http://localhost:3000"]

print(f"CORS config: BACKEND_CORS_ORIGINS={cors_origins!r}, allow_all_origins={allow_all_origins}, allowed_origins={allowed_origins}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if allow_all_origins else allowed_origins,
    allow_credentials=not allow_all_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"]
)


@app.get("/api/health")
def health_check():
    return {
        "status": "ok",
        "service": "hanter-api",
        "time": now_colombia().isoformat()
    }

# --- UTILIDADES ---
TWO_PLACES = Decimal('0.01')
DIVISOR_IVA = Decimal('1.19')  # IVA 19% Colombia

# Zona horaria de Colombia
COLOMBIA_TZ = pytz.timezone('America/Bogota')

def now_colombia():
    """Retorna la fecha y hora actual en zona horaria de Colombia (naive datetime)"""
    # Obtener hora actual en Colombia y convertir a naive datetime
    return datetime.now(COLOMBIA_TZ).replace(tzinfo=None)

def money(value):
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

# --- ESQUEMAS ---
class LoginRequest(BaseModel):
    correo: str
    password: str

class ProductoVenta(BaseModel):
    id_producto: int
    cantidad: float
    descuento: float = Field(default=0.0, ge=0)  # Descuento en pesos colombianos

class ProveedorBase(BaseModel):
    nit: Optional[str] = Field(None, max_length=20)
    nombre_razon_social: str = Field(..., min_length=1, max_length=150)
    contacto_nombre: Optional[str] = Field(None, max_length=100)
    telefono: Optional[str] = Field(None, max_length=30)
    email: Optional[str] = Field(None, max_length=100)
    direccion: Optional[str] = None
    activo: bool = True

class ProductoBase(BaseModel):
    codigo_barra: Optional[str] = Field(None, max_length=50)
    nombre: str = Field(..., min_length=1, max_length=100)
    categoria_id: Optional[int] = None
    proveedor_id: Optional[int] = None
    presentacion: Optional[str] = Field(None, max_length=50)
    precio_compra: float = Field(default=0.0, ge=0)
    precio_venta: float = Field(default=0.0, ge=0)
    stock_actual: int = Field(default=0, ge=0)
    stock_minimo: int = Field(default=5, ge=0)
    unidade_stock: str = Field(default="und", max_length=20)
    activo: bool = True
    imagen: Optional[str] = Field(None, max_length=255)
    # Tarifa IVA: 0 = excluido/exento, 5 = 5%, 19 = 19%
    tarifa_iva: int = Field(default=19)

class ClienteVenta(BaseModel):
    nombre: Optional[str] = Field(default="Cliente contado", max_length=150)
    nit: Optional[str] = Field(None, max_length=20)
    tipo_doc: Optional[str] = Field(default="CC", max_length=5)  # CC, NIT, CE, PA
    telefono: Optional[str] = Field(None, max_length=30)
    direccion: Optional[str] = Field(None, max_length=200)

class VentaRequest(BaseModel):
    productos: List[ProductoVenta]
    tipo_venta: str = "INFORMAL"
    medio_pago_codigo: str = "10"    # 10=Efectivo, 48=Tarjeta, 42=Transferencia, ZZZ=Crédito
    medio_pago_nombre: str = "Efectivo"
    fecha_vencimiento: Optional[str] = None  # Solo para crédito (YYYY-MM-DD)
    cliente: Optional[ClienteVenta] = None
    cuotas: Optional[int] = 1

class RolPayload(BaseModel):
    nombre: str = Field(..., min_length=2, max_length=50)
    descripcion: Optional[str] = Field(None, max_length=150)

class UsuarioPayload(BaseModel):
    nombre: str = Field(..., min_length=2, max_length=120)
    correo: str = Field(..., min_length=5, max_length=120)
    telefono: Optional[str] = Field(None, max_length=30)
    rol_id: int
    activo: bool = True
    password: Optional[str] = Field(None, min_length=6, max_length=120)

# --- SEGURIDAD JWT ---
from jose import JWTError, jwt
from fastapi.security import OAuth2PasswordBearer
from werkzeug.security import generate_password_hash, check_password_hash

SECRET_KEY = os.getenv("JWT_SECRET", "llave-secreta-hanter-2024")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

def crear_token_acceso(data: dict):
    to_encode = data.copy()
    expire = now_colombia() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def obtener_usuario_actual(db: Session = Depends(get_db), token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        correo: str = payload.get("sub")
        if correo is None: raise HTTPException(status_code=401)
    except JWTError: raise HTTPException(status_code=401)
    usuario = db.query(models.Usuario).filter(models.Usuario.correo == correo).first()
    if not usuario: raise HTTPException(status_code=401)
    if not usuario.activo: raise HTTPException(status_code=401, detail="Usuario inactivo")
    return usuario

def _rol_normalizado(usuario: models.Usuario) -> str:
    if not usuario or not usuario.rol or not usuario.rol.nombre:
        return ""
    rol = usuario.rol.nombre.strip().lower()
    # Compatibilidad con registros históricos
    if rol == "muestra":
        return "consulta"
    return rol

def requerir_roles(*roles_permitidos):
    roles_validos = {r.strip().lower() for r in roles_permitidos}

    def dependency(current_user: models.Usuario = Depends(obtener_usuario_actual)):
        rol_actual = _rol_normalizado(current_user)
        if rol_actual not in roles_validos:
            raise HTTPException(status_code=403, detail="No tienes permisos para esta acción")
        return current_user

    return dependency

def requerir_admin(current_user: models.Usuario = Depends(obtener_usuario_actual)):
    if _rol_normalizado(current_user) != "administrador":
        raise HTTPException(status_code=403, detail="Solo administrador")
    return current_user

def asegurar_columnas_factura():
    from database import IS_MYSQL
    with engine.begin() as conn:
        if IS_MYSQL:
            # MySQL: verificar si la columna existe antes de agregarla
            def add_col_if_not_exists(table, col, definition):
                try:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {definition}"))
                except Exception:
                    pass  # La columna ya existe
            add_col_if_not_exists('facturas', 'usuario_id', 'INTEGER')
            add_col_if_not_exists('facturas', 'usuario_nombre_copia', 'VARCHAR(120)')
            add_col_if_not_exists('facturas', 'usuario_rol_copia', 'VARCHAR(50)')
            add_col_if_not_exists('facturas', 'medio_pago_codigo', "VARCHAR(10) DEFAULT '10'")
            add_col_if_not_exists('facturas', 'medio_pago_nombre', "VARCHAR(50) DEFAULT 'Efectivo'")
            add_col_if_not_exists('facturas', 'fecha_vencimiento', 'DATETIME')
            add_col_if_not_exists('facturas', 'retencion_fuente', 'DECIMAL(12,2) DEFAULT 0')
            add_col_if_not_exists('facturas', 'retencion_ica', 'DECIMAL(12,2) DEFAULT 0')
            add_col_if_not_exists('facturas', 'retencion_iva', 'DECIMAL(12,2) DEFAULT 0')
            add_col_if_not_exists('facturas', 'total_retenciones', 'DECIMAL(12,2) DEFAULT 0')
            add_col_if_not_exists('facturas', 'valor_neto_recibido', 'DECIMAL(12,2) DEFAULT 0')
        else:
            # PostgreSQL: soporta IF NOT EXISTS
            conn.execute(text("ALTER TABLE facturas ADD COLUMN IF NOT EXISTS usuario_id INTEGER"))
            conn.execute(text("ALTER TABLE facturas ADD COLUMN IF NOT EXISTS usuario_nombre_copia VARCHAR(120)"))
            conn.execute(text("ALTER TABLE facturas ADD COLUMN IF NOT EXISTS usuario_rol_copia VARCHAR(50)"))
            conn.execute(text("ALTER TABLE facturas ADD COLUMN IF NOT EXISTS medio_pago_codigo VARCHAR(10) DEFAULT '10'"))
            conn.execute(text("ALTER TABLE facturas ADD COLUMN IF NOT EXISTS medio_pago_nombre VARCHAR(50) DEFAULT 'Efectivo'"))
            conn.execute(text("ALTER TABLE facturas ADD COLUMN IF NOT EXISTS fecha_vencimiento TIMESTAMP"))
            conn.execute(text("ALTER TABLE facturas ADD COLUMN IF NOT EXISTS retencion_fuente NUMERIC(12,2) DEFAULT 0"))
            conn.execute(text("ALTER TABLE facturas ADD COLUMN IF NOT EXISTS retencion_ica NUMERIC(12,2) DEFAULT 0"))
            conn.execute(text("ALTER TABLE facturas ADD COLUMN IF NOT EXISTS retencion_iva NUMERIC(12,2) DEFAULT 0"))
            conn.execute(text("ALTER TABLE facturas ADD COLUMN IF NOT EXISTS total_retenciones NUMERIC(12,2) DEFAULT 0"))
            conn.execute(text("ALTER TABLE facturas ADD COLUMN IF NOT EXISTS valor_neto_recibido NUMERIC(12,2) DEFAULT 0"))

def asegurar_columnas_producto():
    from database import IS_MYSQL
    with engine.begin() as conn:
        if IS_MYSQL:
            try:
                conn.execute(text("ALTER TABLE productos ADD COLUMN tarifa_iva INTEGER NOT NULL DEFAULT 19"))
            except Exception:
                pass
        else:
            conn.execute(text("ALTER TABLE productos ADD COLUMN IF NOT EXISTS tarifa_iva INTEGER NOT NULL DEFAULT 19"))
        conn.execute(text("ALTER TABLE productos ADD COLUMN IF NOT EXISTS proveedor_id INTEGER REFERENCES proveedores(id) ON DELETE SET NULL"))

def asegurar_tablas_nota_credito():
    # Crea todas las tablas nuevas (nota crédito, unidades de medida, etc.)
    models.Base.metadata.create_all(bind=engine)

def asegurar_tipos_decimales_y_columnas():
    """Intenta ajustar tipos de columnas críticas para soportar decimales y nuevas columnas."""
    from database import IS_MYSQL
    with engine.begin() as conn:
        try:
            if IS_MYSQL:
                # MySQL: intentar modificar tipo a DECIMAL
                conn.execute(text("ALTER TABLE productos MODIFY COLUMN stock_actual DECIMAL(12,2) DEFAULT 0"))
                conn.execute(text("ALTER TABLE productos MODIFY COLUMN stock_minimo DECIMAL(12,2) DEFAULT 5"))
                conn.execute(text("ALTER TABLE detalle_factura MODIFY COLUMN cantidad DECIMAL(12,2)"))
                conn.execute(text("ALTER TABLE movimientos_inventario MODIFY COLUMN cantidad DECIMAL(12,2)"))
                conn.execute(text("ALTER TABLE detalle_nota_credito MODIFY COLUMN cantidad DECIMAL(12,2)"))
                # Agregar columna descuento si no existe
                try:
                    conn.execute(text("ALTER TABLE detalle_factura ADD COLUMN descuento DECIMAL(12,2) DEFAULT 0"))
                except Exception:
                    pass
                # Agregar columnas cuotas en facturas si no existe
                try:
                    conn.execute(text("ALTER TABLE facturas ADD COLUMN cuotas INTEGER DEFAULT 1"))
                except Exception:
                    pass
            else:
                # Postgres: usar ALTER TABLE ... TYPE
                try:
                    conn.execute(text("ALTER TABLE productos ALTER COLUMN stock_actual TYPE NUMERIC(12,2) USING stock_actual::numeric"))
                    conn.execute(text("ALTER TABLE productos ALTER COLUMN stock_minimo TYPE NUMERIC(12,2) USING stock_minimo::numeric"))
                    conn.execute(text("ALTER TABLE detalle_factura ALTER COLUMN cantidad TYPE NUMERIC(12,2) USING cantidad::numeric"))
                    conn.execute(text("ALTER TABLE movimientos_inventario ALTER COLUMN cantidad TYPE NUMERIC(12,2) USING cantidad::numeric"))
                    conn.execute(text("ALTER TABLE detalle_nota_credito ALTER COLUMN cantidad TYPE NUMERIC(12,2) USING cantidad::numeric"))
                except Exception:
                    pass
                # Agregar columna descuento si no existe
                try:
                    conn.execute(text("ALTER TABLE detalle_factura ADD COLUMN IF NOT EXISTS descuento NUMERIC(12,2) DEFAULT 0"))
                except Exception:
                    pass
                try:
                    conn.execute(text("ALTER TABLE facturas ADD COLUMN IF NOT EXISTS cuotas INTEGER DEFAULT 1"))
                except Exception:
                    pass
        except Exception as e:
            print(f"[migrate-types] Advertencia: no se pudo modificar columnas automáticamente: {e}")

def seed_unidades_medida():
    """Inserta las unidades por defecto si la tabla está vacía."""
    try:
        with engine.begin() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM unidades_medida")).scalar()
            if count == 0:
                unidades_default = [
                    ('und', 'Unidad'), ('kg', 'Kilogramo'), ('g', 'Gramo'),
                    ('litro', 'Litro'), ('ml', 'Mililitro'), ('saco', 'Saco'),
                    ('bolsa', 'Bolsa'), ('caja', 'Caja'), ('par', 'Par'),
                    ('docena', 'Docena'), ('metro', 'Metro'), ('rollo', 'Rollo'),
                ]
                for nombre, desc in unidades_default:
                    if engine.dialect.name == 'mysql':
                        conn.execute(text(
                            "INSERT INTO unidades_medida (nombre, descripcion, activo) "
                            "VALUES (:n, :d, true) ON DUPLICATE KEY UPDATE nombre=nombre"
                        ), {"n": nombre, "d": desc})
                    else:
                        conn.execute(text(
                            "INSERT INTO unidades_medida (nombre, descripcion, activo) "
                            "VALUES (:n, :d, true) ON CONFLICT (nombre) DO NOTHING"
                        ), {"n": nombre, "d": desc})
    except Exception as e:
        print(f"[seed_unidades] {e}")

def seed_roles_y_usuarios():
    """Asegura que existan los roles básicos y al menos un usuario administrador."""
    try:
        with engine.begin() as conn:
            # 1. Asegurar roles
            count_roles = conn.execute(text("SELECT COUNT(*) FROM roles")).scalar()
            if count_roles == 0:
                roles_default = [
                    (1, 'Administrador', 'Acceso total al sistema'),
                    (2, 'Vendedor',      'Gestion comercial y ventas'),
                    (3, 'Inventario',    'Control de stock y movimientos'),
                    (4, 'Consulta',      'Acceso de demostracion')
                ]
                for rid, nombre, desc in roles_default:
                    if engine.dialect.name == 'mysql':
                        conn.execute(text(
                            "INSERT INTO roles (id, nombre, descripcion) "
                            "VALUES (:id, :n, :d) ON DUPLICATE KEY UPDATE nombre=nombre"
                        ), {"id": rid, "n": nombre, "d": desc})
                    else:
                        conn.execute(text(
                            "INSERT INTO roles (id, nombre, descripcion) "
                            "VALUES (:id, :n, :d) ON CONFLICT (nombre) DO NOTHING"
                        ), {"id": rid, "n": nombre, "d": desc})
                if engine.dialect.name == 'postgresql':
                    conn.execute(text("SELECT setval('roles_id_seq', 4, true)"))
                print("[seed_roles_y_usuarios] Roles básicos creados.")

            # 2. Asegurar usuario administrador
            admin_email = os.getenv('DEFAULT_ADMIN_EMAIL', 'admin@inventario.local').strip()
            admin_pwd = os.getenv('DEFAULT_ADMIN_PASSWORD', 'Admin123*')
            
            # Buscar si ya existe el usuario con ese correo o si hay algún administrador
            admin_exists = conn.execute(
                text("SELECT EXISTS(SELECT 1 FROM usuarios WHERE LOWER(correo) = LOWER(:email))"),
                {"email": admin_email}
            ).scalar()
            
            if not admin_exists:
                pwd_hash = generate_password_hash(admin_pwd)
                conn.execute(text(
                    "INSERT INTO usuarios (nombre, correo, telefono, password_hash, rol_id, activo, creado_en) "
                    "VALUES (:nombre, :correo, :telefono, :password_hash, :rol_id, true, :creado_en)"
                ), {
                    "nombre": "Administrador Inicial",
                    "correo": admin_email,
                    "telefono": "70000000",
                    "password_hash": pwd_hash,
                    "rol_id": 1,
                    "creado_en": now_colombia()
                })
                print(f"[seed_roles_y_usuarios] Creado usuario administrador por defecto: {admin_email}")
    except Exception as e:
        print(f"[seed_roles_y_usuarios] Error al sembrar roles y usuarios: {e}")

asegurar_columnas_factura()
asegurar_columnas_producto()
asegurar_tablas_nota_credito()
asegurar_tipos_decimales_y_columnas()
seed_unidades_medida()
seed_roles_y_usuarios()

# --- RUTAS ---

@app.post("/api/auth/login")
def login(data: LoginRequest, db: Session = Depends(get_db)):
    usuario = db.query(models.Usuario).filter(func.lower(models.Usuario.correo) == data.correo.lower()).first()
    if not usuario or not check_password_hash(usuario.password_hash, data.password):
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")
    if not usuario.activo:
        raise HTTPException(status_code=401, detail="Usuario inactivo. Contacta a administración o soporte.")
    
    token = crear_token_acceso(data={"sub": usuario.correo})
    return {
        "access_token": token,
        "token_type": "bearer",
        "usuario": {
            "id": usuario.id,
            "nombre": usuario.nombre,
            "correo": usuario.correo,
            "rol": _rol_normalizado(usuario) or "vendedor"
        }
    }

@app.get("/api/auth/bootstrap")
def auth_bootstrap():
    return {
        "correo_demo": os.getenv('DEFAULT_ADMIN_EMAIL', 'admin@inventario.local'),
        "password_demo": os.getenv('DEFAULT_ADMIN_PASSWORD', 'Admin123*'),
    }

@app.get("/api/productos")
def listar_productos(db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "vendedor", "inventario", "consulta"))):
    return db.query(models.Producto).all()

@app.get("/api/productos/stock-bajo")
def listar_productos_stock_bajo(db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "vendedor", "inventario", "consulta"))):
    return db.query(models.Producto).filter(
        models.Producto.activo == True,
        models.Producto.stock_actual <= models.Producto.stock_minimo
    ).all()

@app.get("/api/productos/public")
def listar_productos_publicos(db: Session = Depends(get_db)):
    return db.query(models.Producto).filter(models.Producto.activo == True).all()

@app.post("/api/productos")
def crear_producto(data: ProductoBase, db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "inventario"))):
    nuevo = models.Producto(
        codigo_barra=data.codigo_barra,
        nombre=data.nombre,
        categoria_id=data.categoria_id,
        proveedor_id=data.proveedor_id,
        presentacion=data.presentacion,
        precio_compra=data.precio_compra,
        precio_venta=data.precio_venta,
        stock_actual=data.stock_actual,
        stock_minimo=data.stock_minimo,
        unidade_stock=data.unidade_stock,
        activo=data.activo,
        imagen=data.imagen,
        tarifa_iva=data.tarifa_iva,
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    return {"mensaje": "Producto creado", "id": nuevo.id}

@app.put("/api/productos/{producto_id}")
def actualizar_producto(producto_id: int, data: ProductoBase, db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "inventario"))):
    producto = db.query(models.Producto).filter(models.Producto.id == producto_id).first()
    if not producto:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    
    producto.codigo_barra = data.codigo_barra
    producto.nombre = data.nombre
    producto.categoria_id = data.categoria_id
    producto.proveedor_id = data.proveedor_id
    producto.presentacion = data.presentacion
    producto.precio_compra = data.precio_compra
    producto.precio_venta = data.precio_venta
    producto.stock_actual = data.stock_actual
    producto.stock_minimo = data.stock_minimo
    producto.unidade_stock = data.unidade_stock
    producto.activo = data.activo
    producto.tarifa_iva = data.tarifa_iva
    if data.imagen:
        producto.imagen = data.imagen
    
    db.commit()
    return {"mensaje": "Producto actualizado"}

@app.delete("/api/productos/{producto_id}")
def eliminar_producto(producto_id: int, db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "inventario"))):
    producto = db.query(models.Producto).filter(models.Producto.id == producto_id).first()
    if not producto:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    
    try:
        db.delete(producto)
        db.commit()
        return {"mensaje": "Producto eliminado"}
    except Exception as e:
        db.rollback()
        # Si hay error de clave foránea, desactivar en lugar de eliminar
        if "foreign key" in str(e).lower() or "violates" in str(e).lower():
            producto.activo = False
            db.commit()
            return {"mensaje": "Producto desactivado (tiene registros asociados)"}
        raise HTTPException(status_code=500, detail=f"Error al eliminar: {str(e)}")

@app.post("/api/vender")
async def procesar_venta(data: VentaRequest, db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "vendedor"))):
    print(f"DEBUG: Iniciando venta para {data.cliente.nombre if data.cliente else 'Sin cliente'}")
    try:
        total_total = Decimal('0.00')
        iva_total = Decimal('0.00')
        productos_db = []

        for item in data.productos:
            p = db.query(models.Producto).filter(models.Producto.id == item.id_producto).first()
            if not p:
                raise HTTPException(status_code=404, detail=f"Producto con ID {item.id_producto} no encontrado")
            cantidad = Decimal(str(item.cantidad))
            if cantidad <= 0:
                raise HTTPException(status_code=400, detail=f"La cantidad para '{p.nombre}' debe ser mayor a 0")
            # p.stock_actual puede ser Decimal o número
            stock_actual = Decimal(str(p.stock_actual or 0))
            if stock_actual < cantidad:
                raise HTTPException(
                    status_code=400,
                    detail=f"Stock insuficiente para '{p.nombre}': disponible {p.stock_actual}, solicitado {item.cantidad}"
                )

            p_venta = money(p.precio_venta)
            total_linea_bruto = money(p_venta * cantidad)
            descuento_item = money(Decimal(str(item.descuento))) if item.descuento > 0 else Decimal('0')
            total_linea = money(max(Decimal('0'), total_linea_bruto - descuento_item))

            # Calcular IVA según tarifa del producto (precio de venta incluye IVA)
            tarifa = int(p.tarifa_iva) if p.tarifa_iva is not None else 19
            if tarifa > 0:
                divisor = Decimal(str(1 + tarifa / 100))
                item_sub = money(total_linea / divisor)
                item_iva = money(total_linea - item_sub)
            else:
                item_sub = total_linea
                item_iva = Decimal('0.00')

            total_total += total_linea
            iva_total += item_iva
            productos_db.append((p, cantidad, p_venta, total_linea, item_sub, item_iva, tarifa, descuento_item))

        subtotal_f = money(total_total - iva_total)

        max_f = db.query(func.max(models.Factura.numero_factura)).scalar() or 0

        nueva_f = models.Factura(
            numero_factura=int(max_f) + 1,
            tipo_documento='ENTRE_LINEAS' if data.tipo_venta == 'INFORMAL' else 'FACTURA',
            tipo_venta=data.tipo_venta,
            fecha_emision=now_colombia(),
            nombre_cliente_copia=data.cliente.nombre if data.cliente else "Cliente Contado",
            nit_cliente_copia=data.cliente.nit if data.cliente and data.cliente.nit else None,
            telefono_cliente_copia=data.cliente.telefono if data.cliente and data.cliente.telefono else None,
            direccion_cliente_copia=data.cliente.direccion if data.cliente and data.cliente.direccion else None,
            subtotal=subtotal_f,
            iva_13=iva_total,
            total_venta=total_total,
            estado='EMITIDA',
            medio_pago_codigo=data.medio_pago_codigo or '10',
            medio_pago_nombre=data.medio_pago_nombre or 'Efectivo',
            fecha_vencimiento=datetime.fromisoformat(data.fecha_vencimiento) if data.fecha_vencimiento else None,
            cuotas=int(data.cuotas) if data.cuotas else 1,
            usuario_id=current_user.id,
            usuario_nombre_copia=current_user.nombre,
            usuario_rol_copia=_rol_normalizado(current_user),
            creado_en=now_colombia()
        )
        db.add(nueva_f)
        db.flush()

        resumen_ws = []
        for p, cant, prec, tot, item_sub, item_iva, tarifa, dcto in productos_db:
            # Ajustar stock con decimal
            p.stock_actual = Decimal(str(p.stock_actual or 0)) - cant
            db.add(models.DetalleFactura(
                factura_id=nueva_f.id,
                producto_id=p.id,
                cantidad=cant,
                precio_unitario=prec,
                subtotal_item=item_sub,
                iva_item=item_iva,
                descuento=dcto,
            ))
            db.add(models.MovimientoInventario(
                producto_id=p.id,
                tipo_movimiento='SALIDA',
                cantidad=cant,
                motivo=f"Venta #{nueva_f.numero_factura}",
                usuario_responsable=current_user.nombre,
            ))
            dcto_str = f" (dcto: -$ {float(dcto):,.2f})".replace(',', '.') if dcto > 0 else ""
            resumen_ws.append(f"- {p.nombre} x{float(cant):,.2f}: $ {float(tot):,.2f} COP{dcto_str}".replace(',', '.'))

        db.commit()

        ws_res = None
        if data.cliente and data.cliente.telefono:
            print(f"DEBUG: Intentando enviar WhatsApp a: {data.cliente.telefono}")
            telefono_con_prefijo = data.cliente.telefono
            if not telefono_con_prefijo.startswith('whatsapp:+'):
                telefono_con_prefijo = f'whatsapp:{telefono_con_prefijo}'

            if not twilio_service.client:
                print("DEBUG: Cliente de Twilio no inicializado.")
                ws_res = {"error": "Cliente Twilio no inicializado."}
            else:
                msg = f"✅ *Venta Confirmada #{nueva_f.numero_factura}*\nTotal: $ {int(total_total):,} COP\n".replace(',', '.') + "\n".join(resumen_ws)
                ws_res = twilio_service.enviar_mensaje_whatsapp(telefono_con_prefijo, msg)
                print(f"DEBUG: Resultado Twilio: {ws_res}")
        else:
            ws_res = {"info": "No se envió WhatsApp: cliente o teléfono no proporcionado."}

        # ── Facturación electrónica DIAN (solo ventas FORMALES con Factus configurado) ──
        factus_res = None
        if factus_service.habilitado and data.tipo_venta == 'FORMAL':
            try:
                detalles_items = []
                for p, cant, prec, tot, item_sub, item_iva, tarifa, dcto in productos_db:
                    detalles_items.append({
                        "nombre": p.nombre,
                        "cantidad": cant,
                        "precio_unitario": float(prec),
                        "total": float(tot),
                        "tarifa_iva": tarifa,
                    })
                factus_data = {
                    "numero_factura": nueva_f.numero_factura,
                    "fecha_emision": nueva_f.fecha_emision,
                    "cliente_nombre": data.cliente.nombre if data.cliente else "Consumidor Final",
                    "cliente_nit": data.cliente.nit if data.cliente and data.cliente.nit else "222222222222",
                    "cliente_tipo_doc": data.cliente.tipo_doc if data.cliente and data.cliente.tipo_doc else "CC",
                    "cliente_direccion": data.cliente.direccion if data.cliente and data.cliente.direccion else "",
                    "cliente_telefono": data.cliente.telefono if data.cliente and data.cliente.telefono else "",
                    "cliente_email": "",
                    "medio_pago_codigo": data.medio_pago_codigo or "10",
                    "fecha_vencimiento": data.fecha_vencimiento,
                    "items": detalles_items,
                    "subtotal": float(subtotal_f),
                    "iva": float(iva_total),
                    "total": float(total_total),
                }
                factus_res = await factus_service.emitir_factura(factus_data)
                if factus_res.get("exito"):
                    nueva_f.cufe = factus_res.get("cufe")
                    nueva_f.qr_code = factus_res.get("qr_url")   # URL del QR DIAN
                    nueva_f.estado_dian = "EMITIDA"
                    nueva_f.numero_dian = factus_res.get("numero")
                    nueva_f.pdf_dian_url = factus_res.get("pdf_url")
                    db.commit()
                else:
                    nueva_f.estado_dian = "PENDIENTE"
                    db.commit()
            except Exception as fe:
                print(f"[Factus] Error al emitir: {fe}")
                factus_res = {"exito": False, "mensaje": str(fe)}

        return {
            "mensaje": "Venta realizada con éxito",
            "numero": nueva_f.numero_factura,
            "factura_id": nueva_f.id,
            "total": float(total_total),
            "whatsapp": ws_res,
            "factus": factus_res,
            "cufe": nueva_f.cufe,
        }

    except Exception as e:
        db.rollback()
        print(f"DEBUG ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/ventas")
def listar_ventas(db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "vendedor", "inventario", "consulta"))):
    ventas = db.query(models.Factura).order_by(models.Factura.fecha_emision.desc()).all()
    return {
        "ventas": [{
            "id": v.id,
            "numero": v.numero_factura,
            "fecha": v.fecha_emision.strftime("%Y-%m-%d %H:%M:%S"),
            "cliente": v.nombre_cliente_copia,
            "total": float(v.total_venta),
            "tipo": v.tipo_venta,
            "estado": v.estado or "EMITIDA",
            "estado_dian": v.estado_dian,
            "cufe": v.cufe,
            "medio_pago": v.medio_pago_nombre or "Efectivo",
            "vendedor_nombre": v.usuario_nombre_copia,
            "vendedor_rol": v.usuario_rol_copia
        } for v in ventas]
    }


@app.post("/api/ventas/{venta_id}/anular")
def anular_venta(venta_id: int, data: dict = Body(default={}), db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "vendedor"))):
    """
    Anula una venta de tipo INFORMAL (mostrador).
    Las ventas FORMAL con CUFE deben anularse mediante Nota Crédito.
    """
    factura = db.query(models.Factura).filter(models.Factura.id == venta_id).first()
    if not factura:
        raise HTTPException(status_code=404, detail="Venta no encontrada")

    if factura.estado == "ANULADA":
        raise HTTPException(status_code=400, detail="Esta venta ya está anulada")

    if factura.tipo_venta == "FORMAL" and factura.cufe:
        raise HTTPException(
            status_code=400,
            detail="Esta factura fue emitida ante la DIAN. Debe anularse mediante una Nota Crédito electrónica."
        )

    # Revertir stock de cada ítem
    detalles = db.query(models.DetalleFactura).filter(models.DetalleFactura.factura_id == venta_id).all()
    for d in detalles:
        producto = db.query(models.Producto).filter(models.Producto.id == d.producto_id).first()
        if producto and d.cantidad:
            producto.stock_actual += d.cantidad
            db.add(models.MovimientoInventario(
                producto_id=d.producto_id,
                tipo_movimiento='ENTRADA',
                cantidad=d.cantidad,
                motivo=f"Anulación venta #{factura.numero_factura} — {data.get('motivo', 'Sin motivo')}",
                usuario_responsable=current_user.nombre,
            ))

    factura.estado = "ANULADA"
    factura.observaciones = f"ANULADA por {current_user.nombre}: {data.get('motivo', 'Sin motivo')}"
    db.commit()

    return {"mensaje": f"Venta #{factura.numero_factura} anulada. Stock revertido."}

@app.get("/api/inventario/movimientos")
def listar_movimientos(db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "inventario"))):
    movs = db.query(models.MovimientoInventario).order_by(models.MovimientoInventario.fecha_movimiento.desc()).limit(50).all()
    return [{
        "id": m.id,
        "producto_nombre": db.query(models.Producto.nombre).filter(models.Producto.id == m.producto_id).scalar() or "Eliminado",
        "tipo_movimiento": m.tipo_movimiento,
        "cantidad": m.cantidad,
        "motivo": m.motivo,
        "fecha_movimiento": m.fecha_movimiento.strftime("%Y-%m-%d %H:%M:%S"),
        "usuario_responsable": m.usuario_responsable
    } for m in movs]

@app.post("/api/inventario/movimientos")
def crear_movimiento(data: dict = Body(...), db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "inventario"))):
    cantidad = Decimal(str(data.get('cantidad', 0)))
    tipo = data.get('tipo_movimiento', 'SALIDA')

    if cantidad <= 0:
        raise HTTPException(status_code=400, detail="La cantidad debe ser mayor a 0")

    p = db.query(models.Producto).filter(models.Producto.id == data['producto_id']).first()
    if not p:
        raise HTTPException(status_code=404, detail="Producto no encontrado")

    if tipo == 'SALIDA':
        stock_actual = Decimal(str(p.stock_actual or 0))
        if stock_actual < cantidad:
            raise HTTPException(
                status_code=400,
                detail=f"No se puede retirar {cantidad} unidades de '{p.nombre}': stock disponible es {p.stock_actual}"
            )
        p.stock_actual = stock_actual - cantidad
    else:
        p.stock_actual = Decimal(str(p.stock_actual or 0)) + cantidad

    nuevo = models.MovimientoInventario(
        producto_id=data['producto_id'],
        tipo_movimiento=tipo,
        cantidad=cantidad,
        motivo=data.get('motivo', 'Ajuste manual'),
        usuario_responsable=current_user.nombre
    )
    db.add(nuevo)
    db.commit()
    return {"mensaje": "Ok", "stock_actual": p.stock_actual}

@app.get("/api/reportes/ventas-hoy")
def ventas_hoy(db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "vendedor", "inventario", "consulta"))):
    hoy = now_colombia().date()
    total = db.query(func.sum(models.Factura.total_venta)).filter(func.date(models.Factura.fecha_emision) == hoy).scalar() or 0
    cant = db.query(func.count(models.Factura.id)).filter(func.date(models.Factura.fecha_emision) == hoy).scalar() or 0
    return {"total_dinero": float(total), "cantidad_ventas": cant}

@app.get("/api/reportes/top-productos")
def top_productos(limit: int = 10, db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "vendedor", "inventario", "consulta"))):
    """Retorna los productos más vendidos ordenados por cantidad total vendida."""
    top = db.query(
        models.Producto.id,
        models.Producto.nombre,
        func.sum(models.DetalleFactura.cantidad).label('total_vendido'),
        func.sum(models.DetalleFactura.subtotal_item + models.DetalleFactura.iva_item).label('total_ingresos')
    ).join(
        models.DetalleFactura, models.Producto.id == models.DetalleFactura.producto_id
    ).group_by(
        models.Producto.id, models.Producto.nombre
    ).order_by(
        func.sum(models.DetalleFactura.cantidad).desc()
    ).limit(limit).all()

    return [{
        "id": p.id,
        "nombre": p.nombre,
        "cantidad_vendida": int(p.total_vendido or 0),
        "ingresos_totales": float(p.total_ingresos or 0)
    } for p in top]


@app.get("/api/reportes/costo-ventas")
def reporte_costo_ventas(
    fecha_inicio: Optional[str] = None,
    fecha_fin: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user = Depends(requerir_roles("administrador", "inventario", "consulta"))
):
    """
    Reporte de costo de ventas y margen bruto por producto.
    Usa precio_compra del producto como costo unitario.
    """
    query = db.query(
        models.Producto.id,
        models.Producto.nombre,
        models.Producto.precio_compra,
        models.Producto.precio_venta,
        models.Producto.tarifa_iva,
        func.sum(models.DetalleFactura.cantidad).label('unidades_vendidas'),
        func.sum(models.DetalleFactura.subtotal_item + models.DetalleFactura.iva_item).label('ingresos_brutos'),
        func.sum(models.DetalleFactura.subtotal_item).label('ingresos_netos'),
    ).join(
        models.DetalleFactura, models.Producto.id == models.DetalleFactura.producto_id
    ).join(
        models.Factura, models.DetalleFactura.factura_id == models.Factura.id
    ).filter(
        models.Factura.estado != 'ANULADA'
    )

    if fecha_inicio:
        try:
            fi = datetime.fromisoformat(fecha_inicio)
            query = query.filter(models.Factura.fecha_emision >= fi)
        except ValueError:
            pass
    if fecha_fin:
        try:
            ff = datetime.fromisoformat(fecha_fin)
            query = query.filter(models.Factura.fecha_emision <= ff)
        except ValueError:
            pass

    query = query.group_by(
        models.Producto.id,
        models.Producto.nombre,
        models.Producto.precio_compra,
        models.Producto.precio_venta,
        models.Producto.tarifa_iva,
    ).order_by(func.sum(models.DetalleFactura.subtotal_item + models.DetalleFactura.iva_item).desc())

    resultados = query.all()

    items = []
    total_ingresos = 0
    total_costo    = 0
    total_margen   = 0

    for r in resultados:
        unidades      = int(r.unidades_vendidas or 0)
        ingresos_brutos = float(r.ingresos_brutos or 0)
        ingresos_netos  = float(r.ingresos_netos or 0)
        precio_compra   = float(r.precio_compra or 0)
        costo_total     = precio_compra * unidades
        margen_bruto    = ingresos_netos - costo_total
        pct_margen      = (margen_bruto / ingresos_netos * 100) if ingresos_netos > 0 else 0

        total_ingresos += ingresos_brutos
        total_costo    += costo_total
        total_margen   += margen_bruto

        items.append({
            "producto_id":      r.id,
            "nombre":           r.nombre,
            "tarifa_iva":       (int(r.tarifa_iva) if r.tarifa_iva is not None else 19),
            "precio_compra":    precio_compra,
            "precio_venta":     float(r.precio_venta or 0),
            "unidades_vendidas": unidades,
            "ingresos_brutos":  ingresos_brutos,
            "ingresos_netos":   ingresos_netos,
            "costo_total":      costo_total,
            "margen_bruto":     margen_bruto,
            "pct_margen":       round(pct_margen, 1),
        })

    return {
        "items": items,
        "totales": {
            "ingresos_brutos": total_ingresos,
            "costo_total":     total_costo,
            "margen_bruto":    total_margen,
            "pct_margen":      round((total_margen / (total_ingresos / 1.19) * 100) if total_ingresos > 0 else 0, 1),
        }
    }

@app.get("/api/categorias")
def listar_categorias(db: Session = Depends(get_db)):
    return db.query(models.Categoria).all()

# --- CLIENTES ---
@app.get("/api/clientes")
def listar_clientes(q: str = None, db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "vendedor"))):
    query = db.query(models.Cliente)
    if q:
        query = query.filter(
            models.Cliente.nombre_razon_social.ilike(f'%{q}%') |
            models.Cliente.numero_doc.ilike(f'%{q}%') |
            models.Cliente.telefono.ilike(f'%{q}%')
        )
    clientes = query.order_by(models.Cliente.nombre_razon_social).all()

    # Contar compras por cliente para ordenar por frecuencia
    frecuencias = {}
    for c in clientes:
        count = db.query(func.count(models.Factura.id)).filter(
            models.Factura.nit_cliente_copia == c.numero_doc,
            models.Factura.estado != 'ANULADA'
        ).scalar() or 0
        frecuencias[c.numero_doc] = count

    # Ordenar: primero los más frecuentes, luego alfabético
    clientes_sorted = sorted(clientes, key=lambda c: (-frecuencias.get(c.numero_doc, 0), c.nombre_razon_social))

    return {
        "total": len(clientes_sorted),
        "clientes": [
            {
                "numero_doc": c.numero_doc,
                "nombre_razon_social": c.nombre_razon_social,
                "tipo_documento": c.tipo_documento,
                "direccion": c.direccion,
                "telefono": c.telefono,
                "email": c.email,
                "compras": frecuencias.get(c.numero_doc, 0),
            }
            for c in clientes_sorted
        ]
    }

@app.post("/api/clientes")
def crear_cliente(data: dict = Body(...), db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "vendedor"))):
    # Verificar que no exista el número de documento
    existente = db.query(models.Cliente).filter(models.Cliente.numero_doc == data.get('numero_doc', '').strip()).first()
    if existente:
        raise HTTPException(status_code=400, detail="Ya existe un cliente con ese número de documento")
    nuevo = models.Cliente(
        numero_doc=data.get('numero_doc', '').strip(),
        nombre_razon_social=data.get('nombre_razon_social', '').strip(),
        tipo_documento=data.get('tipo_documento', 'CC').strip(),
        direccion=data.get('direccion', '').strip() or None,
        telefono=data.get('telefono', '').strip() or None,
        email=data.get('email', '').strip() or None,
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    return nuevo

@app.put("/api/clientes/{numero_doc}")
def actualizar_cliente(numero_doc: str, data: dict = Body(...), db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "vendedor"))):
    cliente = db.query(models.Cliente).filter(models.Cliente.numero_doc == numero_doc).first()
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    cliente.nombre_razon_social = data.get('nombre_razon_social', cliente.nombre_razon_social).strip()
    cliente.tipo_documento = data.get('tipo_documento', cliente.tipo_documento or 'CC').strip()
    cliente.direccion = data.get('direccion', cliente.direccion or '').strip() or None
    cliente.telefono = data.get('telefono', cliente.telefono or '').strip() or None
    cliente.email = data.get('email', cliente.email or '').strip() or None
    db.commit()
    return cliente

@app.delete("/api/clientes/{numero_doc}")
def eliminar_cliente(numero_doc: str, db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "vendedor"))):
    cliente = db.query(models.Cliente).filter(models.Cliente.numero_doc == numero_doc).first()
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    db.delete(cliente)
    db.commit()
    return {"mensaje": "Cliente eliminado"}

# --- PROVEEDORES ---

@app.get("/api/proveedores")
def listar_proveedores(q: Optional[str] = None, db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "vendedor", "inventario", "consulta"))):
    query = db.query(models.Proveedor).filter(models.Proveedor.activo == True)
    if q:
        query = query.filter(
            models.Proveedor.nombre_razon_social.ilike(f'%{q}%') |
            models.Proveedor.nit.ilike(f'%{q}%') |
            models.Proveedor.contacto_nombre.ilike(f'%{q}%')
        )
    return query.order_by(models.Proveedor.nombre_razon_social).all()

@app.get("/api/proveedores/todos")
def listar_proveedores_todos(db: Session = Depends(get_db), current_user = Depends(requerir_admin)):
    return db.query(models.Proveedor).order_by(models.Proveedor.nombre_razon_social).all()

@app.post("/api/proveedores")
def crear_proveedor(data: ProveedorBase, db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "inventario"))):
    nit_limpio = data.nit.strip() if data.nit else None
    if nit_limpio:
        existente = db.query(models.Proveedor).filter(models.Proveedor.nit == nit_limpio).first()
        if existente:
            raise HTTPException(status_code=400, detail="Ya existe un proveedor con ese NIT")

    nuevo = models.Proveedor(
        nit=nit_limpio,
        nombre_razon_social=data.nombre_razon_social.strip(),
        contacto_nombre=data.contacto_nombre.strip() if data.contacto_nombre else None,
        telefono=data.telefono.strip() if data.telefono else None,
        email=data.email.strip() if data.email else None,
        direccion=data.direccion.strip() if data.direccion else None,
        activo=data.activo
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    return nuevo

@app.put("/api/proveedores/{proveedor_id}")
def actualizar_proveedor(proveedor_id: int, data: ProveedorBase, db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "inventario"))):
    proveedor = db.query(models.Proveedor).filter(models.Proveedor.id == proveedor_id).first()
    if not proveedor:
        raise HTTPException(status_code=404, detail="Proveedor no encontrado")

    nit_limpio = data.nit.strip() if data.nit else None
    if nit_limpio:
        existente = db.query(models.Proveedor).filter(
            models.Proveedor.nit == nit_limpio,
            models.Proveedor.id != proveedor_id
        ).first()
        if existente:
            raise HTTPException(status_code=400, detail="Ya existe otro proveedor con ese NIT")
        proveedor.nit = nit_limpio
    else:
        proveedor.nit = None

    proveedor.nombre_razon_social = data.nombre_razon_social.strip()
    proveedor.contacto_nombre = data.contacto_nombre.strip() if data.contacto_nombre else None
    proveedor.telefono = data.telefono.strip() if data.telefono else None
    proveedor.email = data.email.strip() if data.email else None
    proveedor.direccion = data.direccion.strip() if data.direccion else None
    proveedor.activo = data.activo

    db.commit()
    db.refresh(proveedor)
    return proveedor

@app.delete("/api/proveedores/{proveedor_id}")
def eliminar_proveedor(proveedor_id: int, db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "inventario"))):
    proveedor = db.query(models.Proveedor).filter(models.Proveedor.id == proveedor_id).first()
    if not proveedor:
        raise HTTPException(status_code=404, detail="Proveedor no encontrado")

    # Verificar si tiene productos asociados
    productos_asociados = db.query(models.Producto).filter(models.Producto.proveedor_id == proveedor_id).count()
    if productos_asociados > 0:
        proveedor.activo = False
        db.commit()
        return {"mensaje": f"Proveedor desactivado (está asociado a {productos_asociados} producto(s))"}

    db.delete(proveedor)
    db.commit()
    return {"mensaje": "Proveedor eliminado"}



@app.post("/api/categorias")
def crear_categoria(data: dict = Body(...), db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "inventario"))):
    nueva = models.Categoria(
        nombre=data.get('nombre', '').strip(),
        descripcion=data.get('descripcion', '').strip()
    )
    db.add(nueva)
    db.commit()
    db.refresh(nueva)
    return {"id": nueva.id, "nombre": nueva.nombre, "descripcion": nueva.descripcion}

@app.put("/api/categorias/{categoria_id}")
def actualizar_categoria(categoria_id: int, data: dict = Body(...), db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "inventario"))):
    cat = db.query(models.Categoria).filter(models.Categoria.id == categoria_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Categoría no encontrada")
    cat.nombre = data.get('nombre', cat.nombre).strip()
    cat.descripcion = data.get('descripcion', cat.descripcion or '').strip()
    db.commit()
    return {"id": cat.id, "nombre": cat.nombre, "descripcion": cat.descripcion}

@app.delete("/api/categorias/{categoria_id}")
def eliminar_categoria(categoria_id: int, db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "inventario"))):
    cat = db.query(models.Categoria).filter(models.Categoria.id == categoria_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Categoría no encontrada")
    # Verificar si tiene productos asociados
    productos_count = db.query(models.Producto).filter(models.Producto.categoria_id == categoria_id).count()
    if productos_count > 0:
        raise HTTPException(status_code=400, detail=f"No se puede eliminar: tiene {productos_count} producto(s) asociado(s)")
    db.delete(cat)
    db.commit()
    return {"mensaje": "Categoría eliminada"}


# ── Unidades de medida ──

@app.get("/api/unidades")
def listar_unidades(db: Session = Depends(get_db)):
    return db.query(models.UnidadMedida).filter(models.UnidadMedida.activo == True).order_by(models.UnidadMedida.nombre).all()

@app.get("/api/unidades/todas")
def listar_unidades_todas(db: Session = Depends(get_db), current_user = Depends(requerir_admin)):
    return db.query(models.UnidadMedida).order_by(models.UnidadMedida.nombre).all()

@app.post("/api/unidades")
def crear_unidad(data: dict = Body(...), db: Session = Depends(get_db), current_user = Depends(requerir_admin)):
    nombre = data.get('nombre', '').strip().lower()
    if not nombre:
        raise HTTPException(status_code=400, detail="El nombre es obligatorio")
    existe = db.query(models.UnidadMedida).filter(models.UnidadMedida.nombre == nombre).first()
    if existe:
        raise HTTPException(status_code=400, detail="Ya existe una unidad con ese nombre")
    nueva = models.UnidadMedida(
        nombre=nombre,
        descripcion=data.get('descripcion', '').strip() or None,
        activo=True
    )
    db.add(nueva)
    db.commit()
    db.refresh(nueva)
    return {"id": nueva.id, "nombre": nueva.nombre, "descripcion": nueva.descripcion, "activo": nueva.activo}

@app.put("/api/unidades/{unidad_id}")
def actualizar_unidad(unidad_id: int, data: dict = Body(...), db: Session = Depends(get_db), current_user = Depends(requerir_admin)):
    u = db.query(models.UnidadMedida).filter(models.UnidadMedida.id == unidad_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="Unidad no encontrada")
    nombre = data.get('nombre', u.nombre).strip().lower()
    existe = db.query(models.UnidadMedida).filter(
        models.UnidadMedida.nombre == nombre, models.UnidadMedida.id != unidad_id
    ).first()
    if existe:
        raise HTTPException(status_code=400, detail="Ya existe otra unidad con ese nombre")
    u.nombre = nombre
    u.descripcion = data.get('descripcion', u.descripcion or '').strip() or None
    u.activo = bool(data.get('activo', u.activo))
    db.commit()
    return {"id": u.id, "nombre": u.nombre, "descripcion": u.descripcion, "activo": u.activo}

@app.delete("/api/unidades/{unidad_id}")
def eliminar_unidad(unidad_id: int, db: Session = Depends(get_db), current_user = Depends(requerir_admin)):
    u = db.query(models.UnidadMedida).filter(models.UnidadMedida.id == unidad_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="Unidad no encontrada")
    en_uso = db.query(models.Producto).filter(models.Producto.unidade_stock == u.nombre).count()
    if en_uso > 0:
        # Desactivar en lugar de eliminar si está en uso
        u.activo = False
        db.commit()
        return {"mensaje": f"Unidad desactivada (está en uso por {en_uso} producto(s))"}
    db.delete(u)
    db.commit()
    return {"mensaje": "Unidad eliminada"}


@app.get("/api/config")
def get_config(db: Session = Depends(get_db), current_user = Depends(requerir_admin)):
    configs = db.query(models.Configuracion).all()
    return {c.clave: c.valor for c in configs}

@app.get("/api/config/public")
def get_config_public(db: Session = Depends(get_db)):
    """Endpoint público para obtener configuración visible en la página principal"""
    configs = db.query(models.Configuracion).filter(
        models.Configuracion.clave.in_(['banners_carrusel', 'nombre_negocio', 'logo'])
    ).all()
    return {c.clave: c.valor for c in configs}

@app.post("/api/config")
def save_config(data: dict = Body(...), db: Session = Depends(get_db), current_user = Depends(requerir_admin)):
    for clave, valor in data.items():
        config = db.query(models.Configuracion).filter(models.Configuracion.clave == clave).first()
        if config:
            config.valor = valor
        else:
            config = models.Configuracion(clave=clave, valor=valor)
            db.add(config)
    db.commit()
    return {"mensaje": "Configuración guardada correctamente"}

@app.get("/api/roles")
def listar_roles(db: Session = Depends(get_db), current_user = Depends(requerir_admin)):
    roles = db.query(models.Rol).order_by(models.Rol.nombre.asc()).all()
    return [{"id": r.id, "nombre": r.nombre, "descripcion": r.descripcion} for r in roles]

@app.post("/api/roles")
def crear_rol(data: RolPayload, db: Session = Depends(get_db), current_user = Depends(requerir_admin)):
    nombre_normalizado = data.nombre.strip().lower()
    existe = db.query(models.Rol).filter(func.lower(models.Rol.nombre) == nombre_normalizado).first()
    if existe:
        raise HTTPException(status_code=400, detail="Ese rol ya existe")
    rol = models.Rol(nombre=nombre_normalizado, descripcion=(data.descripcion or "").strip() or None)
    db.add(rol)
    db.commit()
    db.refresh(rol)
    return {"id": rol.id, "nombre": rol.nombre, "descripcion": rol.descripcion}

@app.put("/api/roles/{rol_id}")
def actualizar_rol(rol_id: int, data: RolPayload, db: Session = Depends(get_db), current_user = Depends(requerir_admin)):
    rol = db.query(models.Rol).filter(models.Rol.id == rol_id).first()
    if not rol:
        raise HTTPException(status_code=404, detail="Rol no encontrado")

    nombre_normalizado = data.nombre.strip().lower()
    existe = db.query(models.Rol).filter(
        func.lower(models.Rol.nombre) == nombre_normalizado,
        models.Rol.id != rol_id
    ).first()
    if existe:
        raise HTTPException(status_code=400, detail="Ya existe otro rol con ese nombre")

    rol.nombre = nombre_normalizado
    rol.descripcion = (data.descripcion or "").strip() or None
    db.commit()
    return {"id": rol.id, "nombre": rol.nombre, "descripcion": rol.descripcion}

@app.delete("/api/roles/{rol_id}")
def eliminar_rol(rol_id: int, db: Session = Depends(get_db), current_user = Depends(requerir_admin)):
    rol = db.query(models.Rol).filter(models.Rol.id == rol_id).first()
    if not rol:
        raise HTTPException(status_code=404, detail="Rol no encontrado")
    usuarios_asociados = db.query(models.Usuario).filter(models.Usuario.rol_id == rol_id).count()
    if usuarios_asociados > 0:
        raise HTTPException(status_code=400, detail="No se puede eliminar: hay usuarios asociados a este rol")
    db.delete(rol)
    db.commit()
    return {"mensaje": "Rol eliminado"}

@app.get("/api/usuarios")
def listar_usuarios(db: Session = Depends(get_db), current_user = Depends(requerir_admin)):
    usuarios = db.query(models.Usuario).order_by(models.Usuario.nombre.asc()).all()
    return [{
        "id": u.id,
        "nombre": u.nombre,
        "correo": u.correo,
        "telefono": u.telefono,
        "rol_id": u.rol_id,
        "rol_nombre": _rol_normalizado(u) or (u.rol.nombre if u.rol else None),
        "activo": u.activo,
        "creado_en": u.creado_en.isoformat() if u.creado_en else None
    } for u in usuarios]

@app.post("/api/usuarios")
def crear_usuario(data: UsuarioPayload, db: Session = Depends(get_db), current_user = Depends(requerir_admin)):
    if not data.password:
        raise HTTPException(status_code=400, detail="La contraseña es obligatoria")
    rol = db.query(models.Rol).filter(models.Rol.id == data.rol_id).first()
    if not rol:
        raise HTTPException(status_code=400, detail="Rol inválido")
    existente = db.query(models.Usuario).filter(func.lower(models.Usuario.correo) == data.correo.strip().lower()).first()
    if existente:
        raise HTTPException(status_code=400, detail="Ya existe un usuario con ese correo")

    usuario = models.Usuario(
        nombre=data.nombre.strip(),
        correo=data.correo.strip().lower(),
        telefono=(data.telefono or "").strip() or None,
        password_hash=generate_password_hash(data.password),
        rol_id=data.rol_id,
        activo=data.activo
    )
    db.add(usuario)
    db.commit()
    db.refresh(usuario)
    return {"mensaje": "Usuario creado", "id": usuario.id}

@app.put("/api/usuarios/{usuario_id}")
def actualizar_usuario(usuario_id: int, data: UsuarioPayload, db: Session = Depends(get_db), current_user = Depends(requerir_admin)):
    usuario = db.query(models.Usuario).filter(models.Usuario.id == usuario_id).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    rol = db.query(models.Rol).filter(models.Rol.id == data.rol_id).first()
    if not rol:
        raise HTTPException(status_code=400, detail="Rol inválido")
    existente = db.query(models.Usuario).filter(
        func.lower(models.Usuario.correo) == data.correo.strip().lower(),
        models.Usuario.id != usuario_id
    ).first()
    if existente:
        raise HTTPException(status_code=400, detail="Ya existe otro usuario con ese correo")

    usuario.nombre = data.nombre.strip()
    usuario.correo = data.correo.strip().lower()
    usuario.telefono = (data.telefono or "").strip() or None
    usuario.rol_id = data.rol_id
    usuario.activo = data.activo
    if data.password:
        usuario.password_hash = generate_password_hash(data.password)
    db.commit()
    return {"mensaje": "Usuario actualizado"}

@app.post("/api/upload")
async def upload(archivo: UploadFile = File(...), current_user = Depends(requerir_admin)):
    from services.cloudinary_service import subir_imagen, CLOUDINARY_ENABLED
    try:
        file_bytes = await archivo.read()
        result = await subir_imagen(file_bytes, archivo.filename)

        if result["tipo"] == "cloudinary":
            # Devuelve la URL completa de Cloudinary
            return {"ruta": result["url"], "url": result["url"], "public_id": result["public_id"]}
        else:
            # Almacenamiento local (desarrollo)
            return {"ruta": result["url"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al subir imagen: {str(e)}")

@app.get("/api/ventas/{venta_id}")
def obtener_detalle_venta(venta_id: int, db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "vendedor", "consulta"))):
    factura = db.query(models.Factura).filter(models.Factura.id == venta_id).first()
    if not factura:
        raise HTTPException(status_code=404, detail="Venta no encontrada")

    detalles = db.query(models.DetalleFactura).filter(models.DetalleFactura.factura_id == venta_id).all()
    detalles_lista = []
    for d in detalles:
        producto = db.query(models.Producto).filter(models.Producto.id == d.producto_id).first()
        detalles_lista.append({
            "producto_id": d.producto_id,
            "nombre": producto.nombre if producto else "Producto eliminado",
            "cantidad": d.cantidad,
            "precio_unitario": float(d.precio_unitario or 0),
            "subtotal": float(d.subtotal_item or 0),
            "iva": float(d.iva_item or 0),
            "total": float(d.subtotal_item or 0) + float(d.iva_item or 0),
            "tarifa_iva": (int(producto.tarifa_iva) if producto and producto.tarifa_iva is not None else 19),
        })

    return {
        "id": factura.id,
        "numero_factura": factura.numero_factura,
        "tipo_documento": factura.tipo_documento,
        "tipo_venta": factura.tipo_venta,
        "fecha_emision": factura.fecha_emision.isoformat() if factura.fecha_emision else None,
        "nombre_cliente": factura.nombre_cliente_copia,
        "nit": factura.nit_cliente_copia,
        "subtotal": float(factura.subtotal or 0),
        "iva": float(factura.iva_13 or 0),
        "total": float(factura.total_venta or 0),
        "estado": factura.estado,
        "observaciones": factura.observaciones,
        "vendedor_nombre": factura.usuario_nombre_copia,
        "vendedor_rol": factura.usuario_rol_copia,
        "detalle": detalles_lista,
    }


def generar_qr_base64(url: str) -> str:
    """Genera un QR code como imagen base64 a partir de una URL."""
    try:
        import qrcode
        import io
        qr = qrcode.QRCode(version=1, box_size=4, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        import base64
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("utf-8")
    except Exception as e:
        print(f"[QR] Error generando QR: {e}")
        return ""


def generar_html_factura_carta(factura: models.Factura, detalles: list, db: Session) -> str:
    """Genera el HTML tipo factura formal en hoja carta (Letter)."""
    cfg_negocio = db.query(models.Configuracion).filter(models.Configuracion.clave == 'nombre_negocio').first()
    fallback_nombre_negocio = cfg_negocio.valor if cfg_negocio else 'Mi Negocio'
    cfg_tel = db.query(models.Configuracion).filter(models.Configuracion.clave == 'telefono_principal').first()
    telefono = cfg_tel.valor if cfg_tel else ''
    cfg_dir = db.query(models.Configuracion).filter(models.Configuracion.clave == 'direccion_principal').first()
    direccion = cfg_dir.valor if cfg_dir else ''
    cfg_nit = db.query(models.Configuracion).filter(models.Configuracion.clave == 'empresa_nit').first()
    empresa_nit = cfg_nit.valor if cfg_nit else os.getenv('EMPRESA_NIT', '')
    cfg_rs = db.query(models.Configuracion).filter(models.Configuracion.clave == 'empresa_razon_social').first()
    empresa_rs = cfg_rs.valor if cfg_rs else os.getenv('EMPRESA_RAZON_SOCIAL', fallback_nombre_negocio)
    cfg_logo = db.query(models.Configuracion).filter(models.Configuracion.clave == 'logo').first()
    logo_path = cfg_logo.valor if cfg_logo else None

    logo_base64 = None
    if logo_path:
        try:
            import base64
            if not logo_path.startswith('uploads/'):
                logo_path = f'uploads/{logo_path}'
            full_logo_path = os.path.join(BASE_DIR, logo_path)
            if os.path.exists(full_logo_path):
                with open(full_logo_path, 'rb') as img_file:
                    logo_base64 = base64.b64encode(img_file.read()).decode('utf-8')
                    ext = logo_path.split('.')[-1].lower()
                    mime_type = f'image/{ext}' if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp'] else 'image/png'
                    logo_base64 = f'data:{mime_type};base64,{logo_base64}'
        except Exception as e:
            logo_base64 = None

    fecha_str = factura.fecha_emision.strftime("%d/%m/%Y %H:%M") if factura.fecha_emision else ""
    fecha_solo = factura.fecha_emision.strftime("%d/%m/%Y") if factura.fecha_emision else ""

    filas_detalle = ''
    for i, d in enumerate(detalles, 1):
        precio_unit = d.get("precio_unitario", 0)
        total_item  = d.get("total", 0)
        iva_item    = d.get("iva", 0)
        sub_item    = d.get("subtotal", 0)
        tarifa      = d.get("tarifa_iva", 19)
        cantidad_val = d.get("cantidad", 0)
        try:
            cantidad_f = float(cantidad_val)
            cantidad_str = f"{cantidad_f:.2f}".rstrip('0').rstrip('.')
        except Exception:
            cantidad_str = str(cantidad_val)
        precio_sin_iva = sub_item / (d.get("cantidad", 1) or 1) if d.get("cantidad", 1) > 0 else sub_item
        filas_detalle += f'''<tr>
            <td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:center;color:#64748b">{i}</td>
            <td style="padding:6px 8px;border-bottom:1px solid #e2e8f0">{d.get("nombre", "")}
                {f'<div style="font-size:9px;color:#475569;margin-top:4px">Desc: $ {float(d.get("descuento", 0)):.2f}</div>' if d.get("descuento", 0) > 0 else ''}
            </td>
            <td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:center">{cantidad_str}</td>
            <td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:right">$ {float(precio_sin_iva):,.2f}</td>
            <td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:center">{tarifa}%</td>
            <td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:right">$ {float(iva_item):,.2f}</td>
            <td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:right;font-weight:600">$ {float(total_item):,.2f}</td>
        </tr>'''.replace(',', '.')

    total = float(factura.total_venta or 0)
    subtotal = float(factura.subtotal or 0)
    iva = float(factura.iva_13 or 0)

    dian_section = ""
    if factura.cufe and factura.qr_code:
        dian_section = f'''<div style="margin-top:12px;border-top:1px solid #cbd5e1;padding-top:10px;display:flex;align-items:flex-start;gap:16px">
        <div>
            <img src="{generar_qr_base64(factura.qr_code)}" alt="QR DIAN" style="width:80px;height:80px;border:1px solid #e2e8f0" />
        </div>
        <div style="font-size:9px;color:#475569;flex:1">
            <p style="font-weight:700;font-size:10px;color:#1e293b;margin-bottom:4px">✅ Factura Electrónica de Venta — DIAN</p>
            <p><strong>Número DIAN:</strong> {factura.numero_dian or "N/A"}</p>
            <p style="margin-top:3px"><strong>CUFE:</strong></p>
            <p style="word-break:break-all;font-family:monospace;font-size:8px">{factura.cufe}</p>
        </div>
    </div>'''

    html = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Factura {factura.numero_factura}</title>
    <style>
        @page {{
            size: letter;
            margin: 15mm 15mm 20mm 15mm;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: Arial, sans-serif;
            font-size: 11px;
            color: #1e293b;
            background: white;
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            padding-bottom: 12px;
            border-bottom: 2px solid #1e293b;
            margin-bottom: 12px;
        }}
        .logo-area {{
            display: flex;
            align-items: flex-start;
            gap: 16px;
        }}
        .logo-area img {{
            max-height: 72px;
            max-width: 140px;
            object-fit: contain;
        }}
        .empresa-info h1 {{
            font-size: 20px;
            font-weight: 900;
            text-transform: uppercase;
            letter-spacing: 0.4px;
            margin-bottom: 6px;
        }}
        .empresa-info p {{
            font-size: 10px;
            color: #475569;
            line-height: 1.4;
            margin: 2px 0;
        }}
        .doc-box {{
            border: 2px solid #1e293b;
            padding: 10px 16px;
            text-align: center;
            min-width: 170px;
            background: #f8fafc;
            border-radius: 8px;
        }}
        .doc-box .doc-tipo {{
            font-size: 14px;
            letter-spacing: 1px;
        }}
        .doc-box .doc-num {{
            font-size: 14px;
            font-weight: 700;
            margin-top: 4px;
        }}
        .info-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
            margin-bottom: 12px;
        }}
        .info-box {{
            border: 1px solid #cbd5e1;
            padding: 8px 10px;
        }}
        .info-box h4 {{
            font-size: 10px;
            font-weight: 700;
            text-transform: uppercase;
            color: #64748b;
            margin-bottom: 6px;
            border-bottom: 1px solid #e2e8f0;
            padding-bottom: 4px;
        }}
        .info-row {{
            display: flex;
            gap: 6px;
            margin-bottom: 3px;
            font-size: 10px;
        }}
        .info-row .label {{
            font-weight: 600;
            color: #475569;
            min-width: 70px;
        }}
        table.productos {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 12px;
            font-size: 10px;
        }}
        table.productos thead tr {{
            background: #1e293b;
            color: white;
        }}
        table.productos thead th {{
            padding: 7px 8px;
            text-align: left;
            font-weight: 700;
            font-size: 10px;
        }}
        table.productos thead th:not(:first-child):not(:nth-child(2)) {{
            text-align: center;
        }}
        table.productos thead th:last-child,
        table.productos thead th:nth-child(4),
        table.productos thead th:nth-child(6) {{
            text-align: right;
        }}
        .totales-area {{
            display: flex;
            justify-content: space-between;
            gap: 16px;
            margin-bottom: 16px;
        }}
        .notas-box {{
            border: 1px solid #cbd5e1;
            padding: 8px 10px;
            flex: 1;
        }}
        .notas-box h4 {{
            font-size: 10px;
            font-weight: 700;
            color: #64748b;
            margin-bottom: 6px;
        }}
        .totales-box {{
            border: 1px solid #cbd5e1;
            min-width: 220px;
        }}
        .totales-box h4 {{
            font-size: 10px;
            font-weight: 700;
            color: #64748b;
            padding: 6px 10px;
            border-bottom: 1px solid #e2e8f0;
        }}
        .totales-row {{
            display: flex;
            justify-content: space-between;
            padding: 4px 10px;
            font-size: 10px;
            border-bottom: 1px solid #f1f5f9;
        }}
        .totales-row.total-final {{
            background: #1e293b;
            color: white;
            font-weight: 900;
            font-size: 12px;
            padding: 8px 10px;
        }}
        .firmas {{
            display: flex;
            justify-content: space-between;
            margin-top: 24px;
            padding-top: 8px;
        }}
        .firma-box {{
            text-align: center;
            width: 45%;
        }}
        .firma-box .linea {{
            border-top: 1px solid #1e293b;
            margin-bottom: 4px;
        }}
        .firma-box p {{
            font-size: 10px;
            color: #475569;
        }}
        .footer-nota {{
            margin-top: 12px;
            font-size: 9px;
            color: #64748b;
            text-align: center;
        }}
        @media print {{
            body {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
        }}
    </style>
</head>
<body>
    <!-- HEADER -->
    <div class="header">
        <div class="logo-area">
            {f'<img src="{logo_base64}" alt="Logo" />' if logo_base64 else ''}
            <div class="empresa-info">
                <h1>{empresa_rs}</h1>
                {f'<p>NIT: {empresa_nit}</p>' if empresa_nit else ''}
                {f'<p>{direccion}</p>' if direccion else ''}
                {f'<p>Tel: {telefono}</p>' if telefono else ''}
            </div>
        </div>
        <div class="doc-box">
            <div class="doc-tipo">FACTURA</div>
            <div class="doc-num">No. {str(factura.numero_factura).zfill(8)}</div>
        </div>
    </div>

    <!-- INFO CLIENTE + FACTURA -->
    <div class="info-grid">
        <div class="info-box">
            <h4>Cliente</h4>
            <div class="info-row"><span class="label">Nombre:</span><span>{factura.nombre_cliente_copia or "Consumidor Final"}</span></div>
            <div class="info-row"><span class="label">NIT/CC:</span><span>{factura.nit_cliente_copia or "-"}</span></div>
            {f'<div class="info-row"><span class="label">Dirección:</span><span>{factura.direccion_cliente_copia}</span></div>' if factura.direccion_cliente_copia else ''}
            {f'<div class="info-row"><span class="label">Teléfono:</span><span>{factura.telefono_cliente_copia}</span></div>' if factura.telefono_cliente_copia else ''}
        </div>
        <div class="info-box">
            <h4>Información</h4>
            <div class="info-row"><span class="label">Fecha:</span><span>{fecha_solo}</span></div>
            <div class="info-row"><span class="label">Método de pago:</span><span>{factura.medio_pago_nombre or 'Efectivo'}</span></div>
            {(f'<div class="info-row"><span class="label">Cuotas:</span><span>' + str(factura.cuotas) + ' cuota' + ('' if factura.cuotas == 1 else 's') + '</span></div>') if factura.cuotas else ''}
            {f'<div class="info-row"><span class="label">Vence:</span><span>{factura.fecha_vencimiento.strftime("%d/%m/%Y") if factura.fecha_vencimiento else ""}</span></div>' if factura.medio_pago_codigo == 'ZZZ' and factura.fecha_vencimiento else ''}
            <div class="info-row"><span class="label">Vendedor:</span><span>{factura.usuario_nombre_copia or "N/A"}</span></div>
        </div>
    </div>

    <!-- TABLA PRODUCTOS -->
    <table class="productos">
        <thead>
            <tr>
                <th style="width:4%">Nro</th>
                <th>Descripción de Productos</th>
                <th style="width:8%;text-align:center">Cant.</th>
                <th style="width:13%;text-align:right">Vr. Unitario</th>
                <th style="width:8%;text-align:center">% IVA</th>
                <th style="width:13%;text-align:right">IVA $</th>
                <th style="width:13%;text-align:right">Valor Total</th>
            </tr>
        </thead>
        <tbody>
            {filas_detalle}
        </tbody>
    </table>

    <!-- TOTALES + NOTAS -->
    <div class="totales-area">
        <div class="notas-box">
            <h4>Notas</h4>
            <p style="font-size:10px;color:#64748b;margin-top:8px">Quedamos atentos a sus inquietudes y/o requerimientos</p>
        </div>
        <div class="totales-box">
            <h4>Totales</h4>
            <div class="totales-row"><span>Valor bruto:</span><span>$ {f"{total:,.2f}".replace(",", ".")}</span></div>
            <div class="totales-row"><span>Descuentos:</span><span>$ {f"{sum(float(d.get('descuento', 0)) for d in detalles):,.2f}".replace(",", ".")}</span></div>
            <div class="totales-row"><span>Sub Total:</span><span>$ {f"{subtotal:,.2f}".replace(",", ".")}</span></div>
            <div class="totales-row"><span>Valor Impuestos:</span><span>$ {f"{iva:,.2f}".replace(",", ".")}</span></div>
            <div class="totales-row total-final"><span>TOTAL</span><span>$ {f"{total:,.2f}".replace(",", ".")}</span></div>
        </div>
    </div>

    <!-- FIRMAS -->
    <div class="firmas">
        <div class="firma-box">
            <div class="linea"></div>
            <p>Elaborado</p>
        </div>
        <div class="firma-box">
            <div class="linea"></div>
            <p>Firma y cc del Beneficiario</p>
        </div>
    </div>

    {dian_section}

    <div class="footer-nota">
        * NO SOMOS GRANDES CONTRIBUYENTES &nbsp;&nbsp; * NO SOMOS AUTORETENEDORES DE IVA - ICA
    </div>
</body>
</html>'''
    return html


def generar_html_factura(factura: models.Factura, detalles: list, db: Session) -> str:
    """Genera el HTML tipo ticket (58mm) igual que en Flask."""
    cfg_negocio = db.query(models.Configuracion).filter(models.Configuracion.clave == 'nombre_negocio').first()
    fallback_nombre_negocio = cfg_negocio.valor if cfg_negocio else 'Mi Negocio'
    cfg_tel = db.query(models.Configuracion).filter(models.Configuracion.clave == 'telefono_principal').first()
    telefono = cfg_tel.valor if cfg_tel else ''
    cfg_dir = db.query(models.Configuracion).filter(models.Configuracion.clave == 'direccion_principal').first()
    direccion = cfg_dir.valor if cfg_dir else ''
    cfg_nit = db.query(models.Configuracion).filter(models.Configuracion.clave == 'empresa_nit').first()
    empresa_nit = cfg_nit.valor if cfg_nit else os.getenv('EMPRESA_NIT', '')
    cfg_rs = db.query(models.Configuracion).filter(models.Configuracion.clave == 'empresa_razon_social').first()
    empresa_rs = cfg_rs.valor if cfg_rs else os.getenv('EMPRESA_RAZON_SOCIAL', fallback_nombre_negocio)
    cfg_logo = db.query(models.Configuracion).filter(models.Configuracion.clave == 'logo').first()
    logo_path = cfg_logo.valor if cfg_logo else None
    
    # Convertir logo a base64 para embeder en HTML
    logo_base64 = None
    if logo_path:
        try:
            import base64
            # Si la ruta no incluye "uploads/", agregarla
            if not logo_path.startswith('uploads/'):
                logo_path = f'uploads/{logo_path}'
            full_logo_path = os.path.join(BASE_DIR, logo_path)
            print(f"DEBUG: Intentando cargar logo desde: {full_logo_path}")
            if os.path.exists(full_logo_path):
                with open(full_logo_path, 'rb') as img_file:
                    logo_base64 = base64.b64encode(img_file.read()).decode('utf-8')
                    # Detectar tipo de imagen
                    ext = logo_path.split('.')[-1].lower()
                    mime_type = f'image/{ext}' if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp'] else 'image/png'
                    logo_base64 = f'data:{mime_type};base64,{logo_base64}'
                    print(f"DEBUG: Logo cargado exitosamente, tamaño base64: {len(logo_base64)} caracteres")
            else:
                print(f"DEBUG: Logo no encontrado en: {full_logo_path}")
        except Exception as e:
            print(f"DEBUG ERROR al cargar logo: {e}")
            logo_base64 = None

    filas_detalle = ''
    for d in detalles:
        total_item = d.get("total", 0)
        iva_item   = d.get("iva", 0)
        descuento  = d.get("descuento", 0)
        tarifa     = d.get("tarifa_iva", 19)
        iva_str    = f'IVA {tarifa}%: $ {float(iva_item):,.2f}'.replace(',', '.') if iva_item > 0 else ''
        cantidad_val = d.get("cantidad", 0)
        try:
            cantidad_f = float(cantidad_val)
            cantidad_str = f"{cantidad_f:.2f}".rstrip('0').rstrip('.')
        except Exception:
            cantidad_str = str(cantidad_val)
        filas_detalle += f'''<tr class="producto-row">
            <td>{d.get("nombre", "")}
                {f'<br><span style="font-size:7px;color:#555">Desc: $ {float(descuento):,.2f}</span>' if descuento > 0 else ''}
                {f'<br><span style="font-size:7px;color:#555">{iva_str}</span>' if iva_str else ''}
            </td>
            <td style="text-align:center">{cantidad_str}</td>
            <td style="text-align:right">{float(total_item):,.2f}</td>
        </tr>'''.replace(',', '.')

    fecha_str = factura.fecha_emision.strftime("%d/%m/%Y %H:%M") if factura.fecha_emision else ""

    dian_section = ""
    if factura.cufe and factura.qr_code:
        dian_section = f'''<hr>
    <div class="center" style="margin-top:4px">
        <div style="font-size:7px;font-weight:bold;margin-bottom:2px">FACTURA ELECTRÓNICA DIAN</div>
        <img src="{generar_qr_base64(factura.qr_code)}" alt="QR DIAN" style="width:35mm;height:35mm;display:block;margin:0 auto" />
        <div style="font-size:6px;word-break:break-all;margin-top:2px">CUFE: {factura.cufe}</div>
    </div>'''

    html = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Factura {factura.numero_factura}</title>
    <style>
        @page {{
            size: 58mm auto;
            margin: 0;
        }}
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{ 
            font-family: 'Courier New', monospace; 
            font-size: 9px; 
            width: 58mm; 
            margin: 0;
            padding: 3mm;
            background: white;
            line-height: 1.2;
            font-weight: bold;
            color: #000;
            -webkit-print-color-adjust: exact;
            print-color-adjust: exact;
        }}
        h1 {{ 
            font-size: 12px; 
            text-align: center; 
            margin: 0 0 3px 0;
            font-weight: 900;
        }}
        .logo {{
            text-align: center;
            margin: 0 0 5px 0;
        }}
        .logo img {{
            max-width: 40mm;
            max-height: 15mm;
            display: inline-block;
        }}
        .info {{ 
            margin: 2px 0;
            line-height: 1.2;
            font-size: 8px;
        }}
        .center {{ text-align: center; }}
        hr {{
            border: none;
            border-top: 1px dashed #000;
            margin: 3px 0;
        }}
        table {{ 
            width: 100%; 
            border-collapse: collapse; 
            margin: 3px 0;
            font-size: 8px;
        }}
        th {{ 
            padding: 2px 1px;
            border-bottom: 1px dashed #000;
            text-align: left;
            font-weight: bold;
            font-size: 8px;
        }}
        td {{ 
            padding: 1px;
            font-size: 8px;
        }}
        .producto-row {{
            border-bottom: 1px dotted #ccc;
        }}
        .totales {{ 
            margin-top: 3px;
            border-top: 1px dashed #000;
            padding-top: 3px;
            font-size: 9px;
        }}
        .total {{ 
            font-size: 11px; 
            font-weight: 900;
            margin-top: 2px;
        }}
        .footer {{ 
            text-align: center; 
            margin-top: 5px;
            font-size: 8px;
        }}
        @media print {{
            body {{
                padding: 2mm;
            }}
        }}
    </style>
</head>
<body>
    {f'<div class="logo"><img src="{logo_base64}" alt="Logo"></div>' if logo_base64 else ''}
    <h1>{empresa_rs}</h1>
    {f'<div class="info center">NIT: {empresa_nit}</div>' if empresa_nit else ''}
    {f'<div class="info center">Tel: {telefono}</div>' if telefono else ''}
    {f'<div class="info center">{direccion}</div>' if direccion else ''}
    <hr>
    <div class="info">
        <strong>Factura #{factura.numero_factura}</strong><br>
        FACTURA<br>
        {fecha_str}<br>
        Vendedor: {factura.usuario_nombre_copia or "N/A"}<br>
        Pago: {factura.medio_pago_nombre or 'Efectivo'}
        {(f'<br>Cuotas: ' + str(factura.cuotas) + ' cuota' + ('' if factura.cuotas == 1 else 's') + '') if factura.cuotas else ''}
        {f'<br>Vence: {factura.fecha_vencimiento.strftime("%d/%m/%Y")}' if factura.medio_pago_codigo == 'ZZZ' and factura.fecha_vencimiento else ''}<br>
        {factura.nombre_cliente_copia or "Cliente"}<br>
        NIT: {factura.nit_cliente_copia or "-"}
        {f'<br>Tel: {factura.telefono_cliente_copia}' if factura.telefono_cliente_copia else ''}
        {f'<br>Dir: {factura.direccion_cliente_copia}' if factura.direccion_cliente_copia else ''}
    </div>
    <hr>
    <table>
        <thead>
            <tr>
                <th>Producto</th>
                <th style="text-align:center;width:15%">Cant</th>
                <th style="text-align:right;width:25%">Total</th>
            </tr>
        </thead>
        <tbody>
            {filas_detalle}
        </tbody>
    </table>
    <div class="totales">
        <div>Subtotal: $ {f'{int(float(factura.subtotal or 0)):,}'.replace(',', '.')} COP</div>
        <div>IVA: $ {f'{int(float(factura.iva_13 or 0)):,}'.replace(',', '.')} COP</div>
        <div class="total">TOTAL: $ {f'{int(float(factura.total_venta or 0)):,}'.replace(',', '.')} COP</div>
    </div>
    <div class="footer">
        Gracias por su preferencia
    </div>
    {dian_section}
</body>
</html>'''
    return html


@app.get("/api/facturas/{factura_id}/imprimir")
def imprimir_factura(factura_id: int, db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "vendedor", "consulta"))):
    from fastapi.responses import JSONResponse
    import traceback
    try:
        factura = db.query(models.Factura).filter(models.Factura.id == factura_id).first()
        if not factura:
            raise HTTPException(status_code=404, detail="Factura no encontrada")

        detalles = db.query(models.DetalleFactura).filter(models.DetalleFactura.factura_id == factura_id).all()
        detalles_lista = []
        for d in detalles:
            producto = db.query(models.Producto).filter(models.Producto.id == d.producto_id).first()
            total_item = float(d.subtotal_item or 0) + float(d.iva_item or 0)
            iva_item   = float(d.iva_item or 0)
            sub_item   = float(d.subtotal_item or 0)
            descuento  = float(d.descuento or 0)
            # Calcular tarifa real a partir de los montos guardados
            tarifa_iva = (int(producto.tarifa_iva) if producto and producto.tarifa_iva is not None else 19)
            detalles_lista.append({
                "nombre": producto.nombre if producto else "Producto eliminado",
                "cantidad": float(d.cantidad),
                "precio_unitario": float(d.precio_unitario or 0),
                "subtotal": sub_item,
                "iva": iva_item,
                "total": total_item,
                "descuento": descuento,
                "tarifa_iva": tarifa_iva,
            })

        # Elegir formato según configuración
        cfg_tipo = db.query(models.Configuracion).filter(models.Configuracion.clave == 'tipo_impresion').first()
        tipo_impresion = cfg_tipo.valor if cfg_tipo else 'pos'

        if tipo_impresion == 'carta':
            html_factura = generar_html_factura_carta(factura, detalles_lista, db)
        else:
            html_factura = generar_html_factura(factura, detalles_lista, db)

        # Guardar en disco
        ano = factura.fecha_emision.year if factura.fecha_emision else now_colombia().year
        facturas_folder = os.path.join(BASE_DIR, 'facturas', str(ano))
        os.makedirs(facturas_folder, exist_ok=True)
        ruta_archivo = os.path.join(facturas_folder, f'{factura.numero_factura}.html')
        with open(ruta_archivo, 'w', encoding='utf-8') as f:
            f.write(html_factura)

        return {
            "html": html_factura,
            "tipo": tipo_impresion,
            "ruta": f"facturas/{ano}/{factura.numero_factura}.html"
        }
    except Exception as e:
        tb = traceback.format_exc()
        # Asegurar carpeta de logs
        logs_dir = os.path.join(BASE_DIR, 'logs')
        try:
            os.makedirs(logs_dir, exist_ok=True)
            log_file = os.path.join(logs_dir, 'imprimir_error.log')
            with open(log_file, 'a', encoding='utf-8') as lf:
                lf.write(f"\n--- {now_colombia().isoformat()} - factura {factura_id} ---\n")
                lf.write(tb)
        except Exception:
            # Si no puede escribir logs, al menos imprimir en consola
            print('ERROR al escribir log de imprimir_factura:', traceback.format_exc())

        print(tb)
        return JSONResponse(status_code=500, content={"error": "internal_server_error", "detail": str(e)}, headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Credentials": "true"})


@app.get("/health")
def health(): return {"status": "ok"}

# ─────────────────────────────────────────────
# FACTURACIÓN ELECTRÓNICA DIAN / FACTUS
# ─────────────────────────────────────────────

@app.get("/api/factus/estado")
def factus_estado():
    """Verifica si Factus está configurado y en qué ambiente."""
    return {
        "habilitado": factus_service.habilitado,
        "ambiente": factus_service.environment,
        "configurado": bool(factus_service.client_id),
    }

@app.post("/api/factus/recargar")
def factus_recargar(current_user = Depends(obtener_usuario_actual)):
    """
    Recarga las credenciales de Factus desde la BD de configuración sin reiniciar el servidor.
    Llama a este endpoint después de guardar las credenciales en Configuración → DIAN.
    """
    from database import SessionLocal
    db = SessionLocal()
    try:
        claves = ['factus_client_id', 'factus_client_secret', 'factus_username',
                  'factus_password', 'factus_environment', 'empresa_nit',
                  'empresa_razon_social', 'empresa_regimen']
        for clave in claves:
            cfg = db.query(models.Configuracion).filter(models.Configuracion.clave == clave).first()
            if cfg and cfg.valor:
                os.environ[clave.upper()] = cfg.valor

        # Reinicializar el servicio con los nuevos valores
        factus_service.client_id = os.getenv("FACTUS_CLIENT_ID", "")
        factus_service.client_secret = os.getenv("FACTUS_CLIENT_SECRET", "")
        factus_service.username = os.getenv("FACTUS_USERNAME", "")
        factus_service.password = os.getenv("FACTUS_PASSWORD", "")
        factus_service.environment = os.getenv("FACTUS_ENVIRONMENT", "sandbox")
        factus_service.empresa_nit = os.getenv("EMPRESA_NIT", "")
        factus_service.empresa_razon_social = os.getenv("EMPRESA_RAZON_SOCIAL", "Mi Negocio")
        factus_service.empresa_regimen = os.getenv("EMPRESA_REGIMEN", "49")
        factus_service.habilitado = bool(factus_service.client_id and factus_service.client_secret)
        factus_service._access_token = None  # Forzar re-login
        factus_service._refresh_token = None

        return {
            "mensaje": "Credenciales recargadas",
            "habilitado": factus_service.habilitado,
            "ambiente": factus_service.environment,
        }
    finally:
        db.close()

@app.post("/api/facturas/{factura_id}/emitir-dian")
async def emitir_factura_dian(factura_id: int, db: Session = Depends(get_db), current_user = Depends(obtener_usuario_actual)):
    """Reintenta emitir una factura a la DIAN vía Factus (para facturas PENDIENTE o RECHAZADA)."""
    factura = db.query(models.Factura).filter(models.Factura.id == factura_id).first()
    if not factura:
        raise HTTPException(status_code=404, detail="Factura no encontrada")

    detalles = db.query(models.DetalleFactura).filter(models.DetalleFactura.factura_id == factura_id).all()
    items = []
    for d in detalles:
        producto = db.query(models.Producto).filter(models.Producto.id == d.producto_id).first()
        items.append({
            "nombre": producto.nombre if producto else "Producto",
            "cantidad": d.cantidad,
            "precio_unitario": float(d.precio_unitario or 0),
            "total": float((d.subtotal_item or 0) + (d.iva_item or 0)),
        })

    factus_data = {
        "numero_factura": factura.numero_factura,
        "fecha_emision": factura.fecha_emision,
        "cliente_nombre": factura.nombre_cliente_copia or "Consumidor Final",
        "cliente_nit": factura.nit_cliente_copia or "222222222222",
        "cliente_tipo_doc": "CC",
        "cliente_direccion": factura.direccion_cliente_copia or "",
        "cliente_telefono": factura.telefono_cliente_copia or "",
        "cliente_email": "",
        "items": items,
        "subtotal": float(factura.subtotal or 0),
        "iva": float(factura.iva_13 or 0),
        "total": float(factura.total_venta or 0),
    }

    resultado = await factus_service.emitir_factura(factus_data)

    if resultado.get("exito"):
        factura.cufe = resultado.get("cufe")
        factura.qr_code = resultado.get("qr_url")
        factura.estado_dian = "EMITIDA"
        factura.numero_dian = resultado.get("numero")
        factura.pdf_dian_url = resultado.get("pdf_url")
        db.commit()

    return resultado

@app.get("/api/facturas/pendientes-dian")
def facturas_pendientes_dian(db: Session = Depends(get_db), current_user = Depends(obtener_usuario_actual)):
    """Lista facturas que no han sido enviadas a la DIAN."""
    pendientes = db.query(models.Factura).filter(
        (models.Factura.estado_dian == None) |
        (models.Factura.estado_dian == "PENDIENTE") |
        (models.Factura.estado_dian == "RECHAZADA")
    ).order_by(models.Factura.fecha_emision.desc()).limit(50).all()

    return [{
        "id": f.id,
        "numero": f.numero_factura,
        "fecha": f.fecha_emision.strftime("%Y-%m-%d %H:%M") if f.fecha_emision else "",
        "cliente": f.nombre_cliente_copia,
        "total": float(f.total_venta or 0),
        "estado_dian": f.estado_dian or "PENDIENTE",
        "cufe": f.cufe,
    } for f in pendientes]


# ─────────────────────────────────────────────
# NOTAS CRÉDITO
# ─────────────────────────────────────────────

class RetencionRequest(BaseModel):
    retencion_fuente: float = Field(default=0.0, ge=0, description="Retención en la fuente (RteFuente)")
    retencion_ica: float    = Field(default=0.0, ge=0, description="Retención ICA")
    retencion_iva: float    = Field(default=0.0, ge=0, description="Retención IVA (RteIVA)")


class NotaCreditoRequest(BaseModel):
    factura_id: int
    motivo_codigo: str = "1"   # 1=Devolución, 2=Anulación, 3=Descuento, 4=Ajuste precio, 5=Otro
    motivo_descripcion: str = "Devolución de mercancía"
    items: List[dict]          # [{producto_id, cantidad, devolver_stock}]


@app.get("/api/notas-credito")
def listar_notas_credito(db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "vendedor", "consulta"))):
    """Lista todas las notas crédito emitidas."""
    notas = db.query(models.NotaCredito).order_by(models.NotaCredito.fecha_emision.desc()).all()
    result = []
    for n in notas:
        factura = db.query(models.Factura).filter(models.Factura.id == n.factura_id).first()
        result.append({
            "id": n.id,
            "numero_nota": n.numero_nota,
            "fecha": n.fecha_emision.strftime("%Y-%m-%d %H:%M") if n.fecha_emision else "",
            "factura_numero": factura.numero_factura if factura else None,
            "factura_id": n.factura_id,
            "motivo": n.motivo_descripcion,
            "total": float(n.total or 0),
            "estado": n.estado,
            "estado_dian": n.estado_dian,
            "cude": n.cude,
            "numero_dian": n.numero_dian,
            "usuario": n.usuario_nombre_copia,
        })
    return result


@app.get("/api/facturas/{factura_id}/detalle-para-nc")
def detalle_factura_para_nc(factura_id: int, db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "vendedor"))):
    """Retorna el detalle de una factura para crear una nota crédito."""
    factura = db.query(models.Factura).filter(models.Factura.id == factura_id).first()
    if not factura:
        raise HTTPException(status_code=404, detail="Factura no encontrada")

    detalles = db.query(models.DetalleFactura).filter(models.DetalleFactura.factura_id == factura_id).all()
    items = []
    for d in detalles:
        producto = db.query(models.Producto).filter(models.Producto.id == d.producto_id).first()
        items.append({
            "producto_id": d.producto_id,
            "nombre": producto.nombre if producto else "Producto eliminado",
            "cantidad": float(d.cantidad),
            "precio_unitario": float(d.precio_unitario or 0),
            "subtotal": float(d.subtotal_item or 0),
            "iva": float(d.iva_item or 0),
            "total": float((d.subtotal_item or 0) + (d.iva_item or 0)),
            "tarifa_iva": (int(producto.tarifa_iva) if producto and producto.tarifa_iva is not None else 19),
        })

    return {
        "id": factura.id,
        "numero_factura": factura.numero_factura,
        "numero_dian": factura.numero_dian,
        "cufe": factura.cufe,
        "fecha": factura.fecha_emision.strftime("%Y-%m-%d") if factura.fecha_emision else "",
        "cliente_nombre": factura.nombre_cliente_copia,
        "cliente_nit": factura.nit_cliente_copia,
        "total": float(factura.total_venta or 0),
        "estado_dian": factura.estado_dian,
        "items": items,
    }


@app.post("/api/notas-credito")
async def crear_nota_credito(data: NotaCreditoRequest, db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "vendedor"))):
    """Crea y emite una Nota Crédito electrónica ante la DIAN."""
    factura = db.query(models.Factura).filter(models.Factura.id == data.factura_id).first()
    if not factura:
        raise HTTPException(status_code=404, detail="Factura no encontrada")

    if not factura.cufe:
        raise HTTPException(status_code=400, detail="La factura no tiene CUFE — no fue emitida electrónicamente. Use anulación directa.")

    # Calcular totales de la nota crédito
    total_nc = Decimal('0.00')
    iva_nc = Decimal('0.00')
    items_detalle = []

    for item_req in data.items:
        detalle = db.query(models.DetalleFactura).filter(
            models.DetalleFactura.factura_id == data.factura_id,
            models.DetalleFactura.producto_id == item_req.get("producto_id")
        ).first()
        if not detalle:
            continue

        producto = db.query(models.Producto).filter(models.Producto.id == item_req.get("producto_id")).first()
        cant_devolver = Decimal(str(item_req.get("cantidad", detalle.cantidad)))
        cant_devolver = min(cant_devolver, Decimal(str(detalle.cantidad)))  # no puede devolver más de lo comprado

        tarifa = (int(producto.tarifa_iva) if producto and producto.tarifa_iva is not None else 19)
        precio_unit = money(detalle.precio_unitario)
        total_linea = money(precio_unit * cant_devolver)

        if tarifa > 0:
            divisor = Decimal(str(1 + tarifa / 100))
            item_sub = money(total_linea / divisor)
            item_iva = money(total_linea - item_sub)
        else:
            item_sub = total_linea
            item_iva = Decimal('0.00')

        total_nc += total_linea
        iva_nc += item_iva
        items_detalle.append({
            "producto_id": item_req.get("producto_id"),
            "nombre": producto.nombre if producto else "Producto",
            "cantidad": cant_devolver,
            "precio_unitario": float(precio_unit),
            "subtotal_item": float(item_sub),
            "iva_item": float(item_iva),
            "tarifa_iva": tarifa,
            "devolver_stock": bool(item_req.get("devolver_stock", True)),
        })

    if not items_detalle:
        raise HTTPException(status_code=400, detail="No se encontraron ítems válidos para la nota crédito")

    subtotal_nc = money(total_nc - iva_nc)

    # Número consecutivo de nota crédito
    max_nc = db.query(func.max(models.NotaCredito.numero_nota)).scalar() or 0

    nueva_nc = models.NotaCredito(
        factura_id=factura.id,
        numero_nota=int(max_nc) + 1,
        fecha_emision=now_colombia(),
        motivo_codigo=data.motivo_codigo,
        motivo_descripcion=data.motivo_descripcion,
        subtotal=subtotal_nc,
        iva=iva_nc,
        total=total_nc,
        estado='EMITIDA',
        estado_dian='PENDIENTE',
        usuario_id=current_user.id,
        usuario_nombre_copia=current_user.nombre,
    )
    db.add(nueva_nc)
    db.flush()

    # Guardar detalles y revertir stock si aplica
    for item in items_detalle:
        db.add(models.DetalleNotaCredito(
            nota_credito_id=nueva_nc.id,
            producto_id=item["producto_id"],
            nombre_producto=item["nombre"],
            cantidad=item["cantidad"],
            precio_unitario=item["precio_unitario"],
            subtotal_item=item["subtotal_item"],
            iva_item=item["iva_item"],
            devolver_stock=item["devolver_stock"],
        ))
        if item["devolver_stock"] and item["producto_id"]:
            producto = db.query(models.Producto).filter(models.Producto.id == item["producto_id"]).first()
            if producto:
                producto.stock_actual += item["cantidad"]
                db.add(models.MovimientoInventario(
                    producto_id=item["producto_id"],
                    tipo_movimiento='ENTRADA',
                    cantidad=item["cantidad"],
                    motivo=f"Devolución NC#{nueva_nc.numero_nota} — Factura #{factura.numero_factura}",
                    usuario_responsable=current_user.nombre,
                ))

    db.commit()

    # Emitir ante la DIAN si Factus está habilitado
    factus_res = None
    if factus_service.habilitado:
        try:
            nota_data = {
                "numero_nota": nueva_nc.numero_nota,
                "fecha_emision": nueva_nc.fecha_emision,
                "cufe_factura_original": factura.cufe,
                "numero_factura_original": factura.numero_dian or str(factura.numero_factura),
                "motivo_codigo": data.motivo_codigo,
                "motivo_descripcion": data.motivo_descripcion,
                "cliente_nit": factura.nit_cliente_copia or "222222222222",
                "cliente_nombre": factura.nombre_cliente_copia or "Consumidor Final",
                "cliente_tipo_doc": "CC",
                "cliente_direccion": factura.direccion_cliente_copia or "",
                "cliente_telefono": factura.telefono_cliente_copia or "",
                "cliente_email": "",
                "items": items_detalle,
                "total": float(total_nc),
            }
            factus_res = await factus_service.emitir_nota_credito(nota_data)
            if factus_res.get("exito"):
                nueva_nc.cude = factus_res.get("cude")
                nueva_nc.numero_dian = factus_res.get("numero")
                nueva_nc.qr_code = factus_res.get("qr_url")
                nueva_nc.estado_dian = "EMITIDA"
                db.commit()
            else:
                nueva_nc.estado_dian = "PENDIENTE"
                db.commit()
        except Exception as fe:
            print(f"[Factus] Error nota crédito: {fe}")
            factus_res = {"exito": False, "mensaje": str(fe)}

    return {
        "mensaje": "Nota Crédito creada",
        "numero_nota": nueva_nc.numero_nota,
        "nota_id": nueva_nc.id,
        "total": float(total_nc),
        "factus": factus_res,
        "cude": nueva_nc.cude,
    }


# ─────────────────────────────────────────────
# CARTERA — CRÉDITOS Y ABONOS
# ─────────────────────────────────────────────

@app.get("/api/cartera")
def listar_cartera(db: Session = Depends(get_db), current_user = Depends(requerir_roles("administrador", "vendedor", "consulta"))):
    """Lista todas las ventas a crédito con saldo pendiente y estado de vencimiento."""
    creditos = db.query(models.Factura).filter(
        models.Factura.medio_pago_codigo == 'ZZZ',
        models.Factura.estado != 'ANULADA',
    ).order_by(models.Factura.fecha_vencimiento.asc()).all()

    hoy = now_colombia().date()
    resultado = []

    for f in creditos:
        # Calcular total abonado
        total_abonado = db.query(func.sum(models.AbonoCartera.monto)).filter(
            models.AbonoCartera.factura_id == f.id
        ).scalar() or Decimal('0')

        saldo_pendiente = float(f.total_venta or 0) - float(total_abonado)
        if saldo_pendiente <= 0:
            continue  # Ya está pagada

        # Calcular días al vencimiento
        dias_vencimiento = None
        estado_vencimiento = "vigente"
        if f.fecha_vencimiento:
            fecha_venc = f.fecha_vencimiento.date()
            dias_vencimiento = (fecha_venc - hoy).days
            if dias_vencimiento < 0:
                estado_vencimiento = "vencido"
            elif dias_vencimiento <= 5:
                estado_vencimiento = "por_vencer"

        abonos = db.query(models.AbonoCartera).filter(
            models.AbonoCartera.factura_id == f.id
        ).order_by(models.AbonoCartera.fecha_abono.desc()).all()

        resultado.append({
            "factura_id": f.id,
            "numero_factura": f.numero_factura,
            "fecha_emision": f.fecha_emision.strftime("%Y-%m-%d") if f.fecha_emision else "",
            "fecha_vencimiento": f.fecha_vencimiento.strftime("%Y-%m-%d") if f.fecha_vencimiento else "",
            "dias_vencimiento": dias_vencimiento,
            "estado_vencimiento": estado_vencimiento,
            "cliente_nombre": f.nombre_cliente_copia or "Consumidor Final",
            "cliente_nit": f.nit_cliente_copia,
            "cliente_telefono": f.telefono_cliente_copia,
            "cuotas": int(f.cuotas) if getattr(f, 'cuotas', None) is not None else 0,
            "total_factura": float(f.total_venta or 0),
            "total_abonado": float(total_abonado),
            "saldo_pendiente": round(saldo_pendiente, 2),
            "vendedor": f.usuario_nombre_copia,
            "abonos": [{
                "id": a.id,
                "monto": float(a.monto),
                "fecha": a.fecha_abono.strftime("%Y-%m-%d %H:%M") if a.fecha_abono else "",
                "observacion": a.observacion,
                "usuario": a.usuario_nombre,
            } for a in abonos],
        })

    return resultado


class AbonoRequest(BaseModel):
    monto: float = Field(..., gt=0, description="Monto del abono")
    observacion: Optional[str] = Field(None, max_length=200)


@app.post("/api/cartera/{factura_id}/abonar")
def registrar_abono(
    factura_id: int,
    data: AbonoRequest,
    db: Session = Depends(get_db),
    current_user = Depends(requerir_roles("administrador", "vendedor"))
):
    """Registra un abono a una factura de crédito."""
    factura = db.query(models.Factura).filter(models.Factura.id == factura_id).first()
    if not factura:
        raise HTTPException(status_code=404, detail="Factura no encontrada")
    if factura.estado == 'ANULADA':
        raise HTTPException(status_code=400, detail="La factura está anulada")
    if factura.medio_pago_codigo != 'ZZZ':
        raise HTTPException(status_code=400, detail="Esta factura no es una venta a crédito")

    # Verificar que no se abone más del saldo pendiente
    total_abonado = db.query(func.sum(models.AbonoCartera.monto)).filter(
        models.AbonoCartera.factura_id == factura_id
    ).scalar() or Decimal('0')
    saldo_pendiente = float(factura.total_venta or 0) - float(total_abonado)

    if data.monto > saldo_pendiente:
        raise HTTPException(
            status_code=400,
            detail=f"El abono ($ {data.monto:,.0f}) supera el saldo pendiente ($ {saldo_pendiente:,.0f})"
        )

    abono = models.AbonoCartera(
        factura_id=factura_id,
        monto=money(Decimal(str(data.monto))),
        observacion=data.observacion,
        fecha_abono=now_colombia(),
        usuario_id=current_user.id,
        usuario_nombre=current_user.nombre,
        creado_en=now_colombia(),
    )
    db.add(abono)

    # Actualizar vencimiento y cuotas tras el abono
    if factura.medio_pago_codigo == 'ZZZ':
        cuotas_actuales = int(factura.cuotas or 0)
        if cuotas_actuales > 0:
            factura.cuotas = max(0, cuotas_actuales - 1)
            if factura.cuotas > 0:
                factura.fecha_vencimiento = now_colombia() + timedelta(days=30)
            else:
                factura.fecha_vencimiento = None

    db.commit()

    nuevo_saldo = saldo_pendiente - data.monto
    return {
        "mensaje": "Abono registrado correctamente",
        "abono_id": abono.id,
        "monto_abonado": data.monto,
        "saldo_pendiente": round(nuevo_saldo, 2),
        "pagado_completamente": nuevo_saldo <= 0,
        "cuotas_restantes": int(factura.cuotas or 0),
        "fecha_vencimiento": factura.fecha_vencimiento.strftime('%Y-%m-%d') if factura.fecha_vencimiento else None,
    }


@app.post("/api/facturas/{factura_id}/retenciones")
def registrar_retenciones(
    factura_id: int,
    data: RetencionRequest,
    db: Session = Depends(get_db),
    current_user = Depends(requerir_roles("administrador", "vendedor"))
):
    """
    Registra las retenciones practicadas por el cliente en una factura.
    Calcula automáticamente el valor neto a recibir.
    """
    factura = db.query(models.Factura).filter(models.Factura.id == factura_id).first()
    if not factura:
        raise HTTPException(status_code=404, detail="Factura no encontrada")
    if factura.estado == "ANULADA":
        raise HTTPException(status_code=400, detail="No se pueden registrar retenciones en una factura anulada")

    total_ret = money(Decimal(str(data.retencion_fuente)) +
                      Decimal(str(data.retencion_ica)) +
                      Decimal(str(data.retencion_iva)))
    neto = money(Decimal(str(factura.total_venta or 0)) - total_ret)

    factura.retencion_fuente    = money(Decimal(str(data.retencion_fuente)))
    factura.retencion_ica       = money(Decimal(str(data.retencion_ica)))
    factura.retencion_iva       = money(Decimal(str(data.retencion_iva)))
    factura.total_retenciones   = total_ret
    factura.valor_neto_recibido = neto
    db.commit()

    return {
        "mensaje": "Retenciones registradas",
        "factura_id": factura_id,
        "total_venta": float(factura.total_venta or 0),
        "retencion_fuente": float(factura.retencion_fuente),
        "retencion_ica": float(factura.retencion_ica),
        "retencion_iva": float(factura.retencion_iva),
        "total_retenciones": float(total_ret),
        "valor_neto_recibido": float(neto),
    }


@app.get("/api/reportes/retenciones")
def reporte_retenciones(
    fecha_inicio: Optional[str] = None,
    fecha_fin: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user = Depends(requerir_roles("administrador", "consulta"))
):
    """Reporte de retenciones acumuladas por período."""
    query = db.query(models.Factura).filter(
        models.Factura.estado != 'ANULADA',
        models.Factura.total_retenciones > 0
    )
    if fecha_inicio:
        try:
            query = query.filter(models.Factura.fecha_emision >= datetime.fromisoformat(fecha_inicio))
        except ValueError:
            pass
    if fecha_fin:
        try:
            query = query.filter(models.Factura.fecha_emision <= datetime.fromisoformat(fecha_fin))
        except ValueError:
            pass

    facturas = query.order_by(models.Factura.fecha_emision.desc()).all()

    items = [{
        "factura_id": f.id,
        "numero": f.numero_factura,
        "fecha": f.fecha_emision.strftime("%Y-%m-%d") if f.fecha_emision else "",
        "cliente": f.nombre_cliente_copia,
        "total_venta": float(f.total_venta or 0),
        "retencion_fuente": float(f.retencion_fuente or 0),
        "retencion_ica": float(f.retencion_ica or 0),
        "retencion_iva": float(f.retencion_iva or 0),
        "total_retenciones": float(f.total_retenciones or 0),
        "valor_neto_recibido": float(f.valor_neto_recibido or 0),
    } for f in facturas]

    totales = {
        "total_ventas": sum(i["total_venta"] for i in items),
        "total_rte_fuente": sum(i["retencion_fuente"] for i in items),
        "total_rte_ica": sum(i["retencion_ica"] for i in items),
        "total_rte_iva": sum(i["retencion_iva"] for i in items),
        "total_retenciones": sum(i["total_retenciones"] for i in items),
        "total_neto": sum(i["valor_neto_recibido"] for i in items),
    }

    return {"items": items, "totales": totales}


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8001"))
    reload_enabled = os.getenv("DEBUG", "False").strip().lower() == "true"
    uvicorn.run("main_fastapi:app", host=host, port=port, reload=reload_enabled)
