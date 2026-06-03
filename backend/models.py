from sqlalchemy import Column, Integer, String, Numeric, Boolean, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
import pytz
from database import Base

# Zona horaria de Colombia
COLOMBIA_TZ = pytz.timezone('America/Bogota')

def now_colombia():
    """Retorna la fecha y hora actual en zona horaria de Colombia (naive datetime)"""
    return datetime.now(COLOMBIA_TZ).replace(tzinfo=None)

class Proveedor(Base):
    __tablename__ = 'proveedores'
    id = Column(Integer, primary_key=True, index=True)
    nit = Column(String(20), unique=True, index=True)
    nombre_razon_social = Column(String(150), nullable=False)
    contacto_nombre = Column(String(100))
    telefono = Column(String(30))
    email = Column(String(100))
    direccion = Column(Text)
    activo = Column(Boolean, default=True)
    creado_en = Column(DateTime, default=now_colombia)

class Categoria(Base):
    __tablename__ = 'categorias'
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(50), nullable=False)
    descripcion = Column(Text)

class Rol(Base):
    __tablename__ = 'roles'
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(50), unique=True, nullable=False)
    descripcion = Column(String(150))

class Usuario(Base):
    __tablename__ = 'usuarios'
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(120), nullable=False)
    correo = Column(String(120), unique=True, nullable=False)
    telefono = Column(String(30))
    password_hash = Column(String(255), nullable=False)
    rol_id = Column(Integer, ForeignKey('roles.id'), nullable=False)
    activo = Column(Boolean, default=True)
    creado_en = Column(DateTime, default=now_colombia)
    
    rol = relationship("Rol")

class Producto(Base):
    __tablename__ = 'productos'
    id = Column(Integer, primary_key=True, index=True)
    codigo_barra = Column(String(50), unique=True)
    nombre = Column(String(100), nullable=False)
    categoria_id = Column(Integer, ForeignKey('categorias.id'))
    proveedor_id = Column(Integer, ForeignKey('proveedores.id'))
    presentacion = Column(String(50))
    precio_compra = Column(Numeric(10, 2))
    precio_venta = Column(Numeric(10, 2))
    stock_actual = Column(Integer, default=0)
    stock_minimo = Column(Integer, default=5)
    activo = Column(Boolean, default=True)
    imagen = Column(String(255))
    unidade_stock = Column(String(20), default='und')
    # Tarifa IVA Colombia: 0 = excluido/exento, 5 = 5%, 19 = 19% (default)
    tarifa_iva = Column(Integer, default=19, nullable=False)
    creado_en = Column(DateTime, default=now_colombia)
    
    proveedor = relationship("Proveedor")

class Factura(Base):
    __tablename__ = 'facturas'
    id = Column(Integer, primary_key=True, index=True)
    numero_factura = Column(Integer, unique=True)
    tipo_documento = Column(String(20))
    tipo_venta = Column(String(20))
    fecha_emision = Column(DateTime, default=now_colombia)
    cliente_id = Column(Integer)
    nombre_cliente_copia = Column(String(150))
    nit_cliente_copia = Column(String(20))
    telefono_cliente_copia = Column(String(30))
    direccion_cliente_copia = Column(String(200))
    subtotal = Column(Numeric(12, 2))
    iva_13 = Column(Numeric(12, 2))
    total_venta = Column(Numeric(12, 2))
    estado = Column(String(20))
    codigo_control = Column(String(50))
    observaciones = Column(Text)
    usuario_id = Column(Integer, ForeignKey('usuarios.id'))
    usuario_nombre_copia = Column(String(120))
    usuario_rol_copia = Column(String(50))
    creado_en = Column(DateTime, default=now_colombia)
    # Facturación electrónica DIAN
    cufe = Column(String(200))
    qr_code = Column(Text)
    estado_dian = Column(String(30))    # PENDIENTE, EMITIDA, RECHAZADA
    numero_dian = Column(String(50))    # Número asignado por Factus/DIAN
    pdf_dian_url = Column(String(300))  # URL del PDF oficial
    # Medio de pago
    medio_pago_codigo = Column(String(10), default='10')   # 10=Efectivo, 48=Tarjeta, 42=Transferencia, ZZZ=Crédito
    medio_pago_nombre = Column(String(50), default='Efectivo')
    fecha_vencimiento = Column(DateTime)                   # Solo para ventas a crédito
    # Retenciones practicadas por el cliente (grandes contribuyentes / agentes de retención)
    retencion_fuente    = Column(Numeric(12, 2), default=0)  # RteFuente (3.5% general o tarifa especial)
    retencion_ica       = Column(Numeric(12, 2), default=0)  # RteICA (según municipio)
    retencion_iva       = Column(Numeric(12, 2), default=0)  # RteIVA (15% del IVA cobrado)
    total_retenciones   = Column(Numeric(12, 2), default=0)  # Suma de las anteriores
    valor_neto_recibido = Column(Numeric(12, 2), default=0)  # total_venta - total_retenciones

class DetalleFactura(Base):
    __tablename__ = 'detalle_factura'
    id = Column(Integer, primary_key=True, index=True)
    factura_id = Column(Integer, ForeignKey('facturas.id'))
    producto_id = Column(Integer, ForeignKey('productos.id'))
    cantidad = Column(Integer)
    precio_unitario = Column(Numeric(10, 2))
    subtotal_item = Column(Numeric(12, 2))
    iva_item = Column(Numeric(12, 2))

class MovimientoInventario(Base):
    __tablename__ = 'movimientos_inventario'
    id = Column(Integer, primary_key=True, index=True)
    producto_id = Column(Integer, ForeignKey('productos.id'))
    tipo_movimiento = Column(String(20))
    cantidad = Column(Integer)
    motivo = Column(Text)
    fecha_movimiento = Column(DateTime, default=now_colombia)
    usuario_responsable = Column(String(50))

class Configuracion(Base):
    __tablename__ = 'configuraciones'
    id = Column(Integer, primary_key=True, index=True)
    clave = Column(String(50), unique=True, nullable=False)
    valor = Column(Text)

class Cliente(Base):
    __tablename__ = 'clientes'
    numero_doc = Column(String(20), primary_key=True, index=True)
    nombre_razon_social = Column(String(150), nullable=False)
    tipo_documento = Column(String(10))
    direccion = Column(Text)
    telefono = Column(String(20))
    email = Column(String(100))
    creado_en = Column(DateTime, default=now_colombia)


class UnidadMedida(Base):
    __tablename__ = 'unidades_medida'
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(30), unique=True, nullable=False)   # "und", "kg", "litro"...
    descripcion = Column(String(100))
    activo = Column(Boolean, default=True)
    creado_en = Column(DateTime, default=now_colombia)


class NotaCredito(Base):
    __tablename__ = 'notas_credito'
    id = Column(Integer, primary_key=True, index=True)
    # Referencia a la factura original
    factura_id = Column(Integer, ForeignKey('facturas.id'), nullable=False)
    numero_nota = Column(Integer, unique=True, nullable=False)
    fecha_emision = Column(DateTime, default=now_colombia)
    # Motivo DIAN: 1=Devolución parcial, 2=Anulación, 3=Descuento, 4=Ajuste precio, 5=Otro
    motivo_codigo = Column(String(2), default='1')
    motivo_descripcion = Column(String(200))
    subtotal = Column(Numeric(12, 2), default=0)
    iva = Column(Numeric(12, 2), default=0)
    total = Column(Numeric(12, 2), default=0)
    estado = Column(String(20), default='EMITIDA')  # EMITIDA, ANULADA
    # Facturación electrónica DIAN
    cude = Column(String(200))           # Código único nota crédito
    numero_dian = Column(String(50))     # Número asignado por Factus
    qr_code = Column(Text)
    estado_dian = Column(String(30), default='PENDIENTE')
    # Auditoría
    usuario_id = Column(Integer, ForeignKey('usuarios.id'))
    usuario_nombre_copia = Column(String(120))
    creado_en = Column(DateTime, default=now_colombia)

    factura = relationship("Factura")


class DetalleNotaCredito(Base):
    __tablename__ = 'detalle_nota_credito'
    id = Column(Integer, primary_key=True, index=True)
    nota_credito_id = Column(Integer, ForeignKey('notas_credito.id'), nullable=False)
    producto_id = Column(Integer, ForeignKey('productos.id'))
    nombre_producto = Column(String(100))   # copia por si el producto se elimina
    cantidad = Column(Integer, default=1)
    precio_unitario = Column(Numeric(10, 2))
    subtotal_item = Column(Numeric(12, 2))
    iva_item = Column(Numeric(12, 2))
    devolver_stock = Column(Boolean, default=True)
