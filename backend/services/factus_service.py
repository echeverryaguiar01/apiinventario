"""
Servicio de integración con Factus para facturación electrónica DIAN Colombia.
Documentación: https://factus.com.co/api-docs

Flujo:
1. Autenticación OAuth2 → access_token
2. Enviar factura → Factus firma y envía a DIAN
3. Recibir CUFE + PDF con QR oficial
"""

import os
import httpx
import json
from datetime import datetime, timedelta
from typing import Optional
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# URLs de Factus
FACTUS_URLS = {
    "sandbox": "https://api-sandbox.factus.com.co",
    "production": "https://api.factus.com.co",
}

ENVIRONMENT = os.getenv("FACTUS_ENVIRONMENT", "sandbox")
BASE_URL = FACTUS_URLS.get(ENVIRONMENT, FACTUS_URLS["sandbox"])


class FactusService:
    def __init__(self):
        self.client_id = os.getenv("FACTUS_CLIENT_ID", "")
        self.client_secret = os.getenv("FACTUS_CLIENT_SECRET", "")
        self.username = os.getenv("FACTUS_USERNAME", "")
        self.password = os.getenv("FACTUS_PASSWORD", "")
        self.empresa_nit = os.getenv("EMPRESA_NIT", "")
        self.empresa_razon_social = os.getenv("EMPRESA_RAZON_SOCIAL", "Mi Negocio")
        self.empresa_regimen = os.getenv("EMPRESA_REGIMEN", "49")
        # numbering_range_id debe ser un entero (ej: 389 para sandbox SETP)
        try:
            self.numbering_range_id = int(os.getenv("FACTUS_NUMBERING_RANGE_ID", "389"))
        except (ValueError, TypeError):
            self.numbering_range_id = 389
        self.environment = ENVIRONMENT

        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None

        self.habilitado = bool(self.client_id and self.client_secret and self.username)
        if self.habilitado:
            print(f"[Factus] Servicio inicializado en modo: {self.environment.upper()}")
        else:
            print("[Factus] ⚠️  Credenciales no configuradas — facturación electrónica deshabilitada")

    # ─────────────────────────────────────────────
    # AUTENTICACIÓN
    # ─────────────────────────────────────────────

    async def _obtener_token(self) -> Optional[str]:
        """Obtiene o renueva el access_token de Factus."""
        # Si el token sigue vigente, reutilizarlo
        if self._access_token and self._token_expires_at:
            if datetime.utcnow() < self._token_expires_at - timedelta(minutes=2):
                return self._access_token

        # Si hay refresh_token, intentar renovar
        if self._refresh_token:
            token = await self._renovar_token()
            if token:
                return token

        # Login completo
        return await self._login()

    async def _login(self) -> Optional[str]:
        """Autenticación inicial con usuario y contraseña."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{BASE_URL}/oauth/token",
                    data={
                        "grant_type": "password",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "username": self.username,
                        "password": self.password,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    self._access_token = data.get("access_token")
                    self._refresh_token = data.get("refresh_token")
                    expires_in = data.get("expires_in", 3600)
                    self._token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
                    print(f"[Factus] ✅ Login exitoso. Token válido por {expires_in}s")
                    return self._access_token
                else:
                    print(f"[Factus] ❌ Error en login: {resp.status_code} — {resp.text}")
                    return None
        except Exception as e:
            print(f"[Factus] ❌ Excepción en login: {e}")
            return None

    async def _renovar_token(self) -> Optional[str]:
        """Renueva el access_token usando el refresh_token."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{BASE_URL}/oauth/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "refresh_token": self._refresh_token,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    self._access_token = data.get("access_token")
                    self._refresh_token = data.get("refresh_token")
                    expires_in = data.get("expires_in", 3600)
                    self._token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
                    return self._access_token
                else:
                    self._refresh_token = None
                    return None
        except Exception:
            return None

    # ─────────────────────────────────────────────
    # ENVIAR FACTURA
    # ─────────────────────────────────────────────

    async def emitir_factura(self, factura_data: dict) -> dict:
        """
        Envía una factura a Factus para que la firme y la envíe a la DIAN.

        Args:
            factura_data: Diccionario con los datos de la factura (ver _construir_payload)

        Returns:
            dict con: cufe, qr_code, pdf_url, estado, mensaje
        """
        if not self.habilitado:
            return {
                "exito": False,
                "mensaje": "Factus no está configurado. Agrega las credenciales en el .env",
                "cufe": None,
            }

        token = await self._obtener_token()
        if not token:
            return {"exito": False, "mensaje": "No se pudo autenticar con Factus", "cufe": None}

        payload = self._construir_payload(factura_data)

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{BASE_URL}/v2/bills/validate",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    json=payload,
                )

                if resp.status_code in (200, 201):
                    data = resp.json()
                    bill = data.get("data", {})
                    links = bill.get("links", {})
                    print(f"[Factus] ✅ Factura emitida. CUFE: {bill.get('cufe', 'N/A')}")
                    return {
                        "exito": True,
                        "cufe": bill.get("cufe"),
                        "qr_url": links.get("qr"),           # URL del QR de la DIAN
                        "public_url": links.get("public_url"),  # URL pública de la factura
                        "numero": bill.get("number"),
                        "pdf_url": bill.get("pdf_url"),
                        "xml_url": bill.get("xml_url"),
                        "estado": "EMITIDA" if bill.get("is_validated") else "PENDIENTE",
                        "mensaje": "Factura emitida correctamente",
                        "raw": data,
                    }
                else:
                    error_msg = resp.text
                    error_detail = {}
                    try:
                        error_detail = resp.json()
                        error_msg = error_detail.get("message", error_msg)
                    except Exception:
                        pass
                    # Log completo para depuración
                    print(f"[Factus] ❌ Error al emitir: {resp.status_code}")
                    print(f"[Factus] ❌ Respuesta completa: {json.dumps(error_detail, ensure_ascii=False, indent=2)}")
                    print(f"[Factus] ❌ Payload enviado: {json.dumps(payload, ensure_ascii=False, indent=2)}")
                    # Guardar en archivo para no perder el log con reloads
                    try:
                        log_path = os.path.join(BASE_DIR, "factus_error.log")
                        with open(log_path, "w", encoding="utf-8") as f:
                            f.write(f"STATUS: {resp.status_code}\n")
                            f.write(f"RESPUESTA:\n{json.dumps(error_detail, ensure_ascii=False, indent=2)}\n\n")
                            f.write(f"PAYLOAD:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n")
                    except Exception:
                        pass
                    return {
                        "exito": False,
                        "mensaje": f"Error Factus ({resp.status_code}): {error_msg}",
                        "detalle": error_detail,
                        "cufe": None,
                    }
        except Exception as e:
            print(f"[Factus] ❌ Excepción al emitir factura: {e}")
            return {"exito": False, "mensaje": str(e), "cufe": None}

    # ─────────────────────────────────────────────
    # DESCARGAR PDF
    # ─────────────────────────────────────────────

    async def descargar_pdf(self, numero_factura: str) -> Optional[bytes]:
        """Descarga el PDF oficial de la factura desde Factus."""
        if not self.habilitado:
            return None

        token = await self._obtener_token()
        if not token:
            return None

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{BASE_URL}/v2/bills/download-pdf/{numero_factura}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code == 200:
                    return resp.content
                return None
        except Exception as e:
            print(f"[Factus] ❌ Error al descargar PDF: {e}")
            return None

    # ─────────────────────────────────────────────
    # CONSULTAR ESTADO
    # ─────────────────────────────────────────────

    async def consultar_factura(self, numero_factura: str) -> Optional[dict]:
        """Consulta el estado de una factura en Factus/DIAN."""
        if not self.habilitado:
            return None

        token = await self._obtener_token()
        if not token:
            return None

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{BASE_URL}/v2/bills/{numero_factura}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code == 200:
                    return resp.json()
                return None
        except Exception as e:
            print(f"[Factus] ❌ Error al consultar factura: {e}")
            return None

    # ─────────────────────────────────────────────
    # NOTA CRÉDITO
    # ─────────────────────────────────────────────

    async def emitir_nota_credito(self, nota_data: dict) -> dict:
        """
        Emite una Nota Crédito electrónica ante la DIAN vía Factus.

        nota_data debe contener:
          - numero_nota: int
          - fecha_emision: datetime
          - cufe_factura_original: str  (CUFE de la factura que se corrige)
          - numero_factura_original: str (número DIAN ej: SETP990004213)
          - motivo_codigo: str  (1=devolución, 2=anulación, 3=descuento, 4=ajuste, 5=otro)
          - motivo_descripcion: str
          - cliente_nit: str
          - cliente_nombre: str
          - cliente_tipo_doc: str
          - items: list de {nombre, cantidad, precio_unitario, tarifa_iva}
          - total: float
        """
        if not self.habilitado:
            return {"exito": False, "mensaje": "Factus no configurado", "cude": None}

        token = await self._obtener_token()
        if not token:
            return {"exito": False, "mensaje": "No se pudo autenticar con Factus", "cude": None}

        payload = self._construir_payload_nota_credito(nota_data)

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{BASE_URL}/v2/credit-notes/validate",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    json=payload,
                )
                if resp.status_code in (200, 201):
                    data = resp.json()
                    note = data.get("data", {})
                    links = note.get("links", {})
                    print(f"[Factus] ✅ Nota Crédito emitida. CUDE: {note.get('cude', 'N/A')}")
                    return {
                        "exito": True,
                        "cude": note.get("cude"),
                        "numero": note.get("number"),
                        "qr_url": links.get("qr"),
                        "public_url": links.get("public_url"),
                        "estado": "EMITIDA" if note.get("is_validated") else "PENDIENTE",
                        "mensaje": "Nota Crédito emitida correctamente",
                    }
                else:
                    error_detail = {}
                    try:
                        error_detail = resp.json()
                    except Exception:
                        pass
                    print(f"[Factus] ❌ Error nota crédito {resp.status_code}: {json.dumps(error_detail, ensure_ascii=False)}")
                    return {
                        "exito": False,
                        "mensaje": f"Error Factus ({resp.status_code}): {error_detail.get('message', resp.text)}",
                        "detalle": error_detail,
                        "cude": None,
                    }
        except Exception as e:
            print(f"[Factus] ❌ Excepción nota crédito: {e}")
            return {"exito": False, "mensaje": str(e), "cude": None}

    def _construir_payload_nota_credito(self, data: dict) -> dict:
        """Construye el payload para Factus v2 de una Nota Crédito."""
        fecha = data.get("fecha_emision", datetime.now())
        if isinstance(fecha, str):
            fecha = datetime.fromisoformat(fecha)

        tipo_doc_map = {"CC": "13", "NIT": "31", "CE": "22", "PA": "41"}
        tipo_doc_raw = data.get("cliente_tipo_doc", "CC")
        tipo_doc_cliente = tipo_doc_map.get(tipo_doc_raw, "13")
        es_juridica = tipo_doc_raw == "NIT"

        items = []
        for i, item in enumerate(data.get("items", []), 1):
            precio = float(item.get("precio_unitario", 0))
            tarifa = int(item.get("tarifa_iva", 19))
            if tarifa > 0:
                precio_base = round(precio / (1 + tarifa / 100), 2)
            else:
                precio_base = precio

            # cantidad puede ser decimal (ej: 0.25). Usar float en lugar de int para evitar enviar 0
            cantidad_val = float(item.get("cantidad", 1))
            items.append({
                "code_reference": str(i),
                "name": item.get("nombre", "Producto"),
                "quantity": round(cantidad_val, 6),
                "discount_rate": 0,
                "price": precio_base,
                "unit_measure_code": "94",
                "standard_code": "999",
                "is_excluded": 1 if tarifa == 0 else 0,
                "taxes": [{"code": "01", "rate": f"{tarifa:.2f}", "tribute_id": 22}],
                "withholding_taxes": [],
            })

        # Rango de numeración para Notas Crédito (ID 390 en sandbox)
        nc_range_id = int(os.getenv("FACTUS_NC_RANGE_ID", "390"))

        return {
            "numbering_range_id": nc_range_id,
            "reference_code": f"NC{data.get('numero_nota', 1)}",
            "observation": data.get("motivo_descripcion", "Nota crédito"),
            "discount_type": int(data.get("motivo_codigo", "1")),
            "billing_reference": {
                "number": data.get("numero_factura_original", ""),
                "uuid": data.get("cufe_factura_original", ""),
                "issue_date": fecha.strftime("%Y-%m-%d"),
            },
            "payment_details": [{
                "payment_method_code": "10",
                "payment_form": "1",
                "due_date": fecha.strftime("%Y-%m-%d"),
                "amount": str(round(float(data.get("total", 0)), 2)),
            }],
            "customer": {
                "identification": data.get("cliente_nit", "222222222222"),
                "dv": data.get("cliente_dv", ""),
                "company": data.get("cliente_nombre", "Consumidor Final"),
                "trade_name": data.get("cliente_nombre", "Consumidor Final"),
                "names": data.get("cliente_nombre", "Consumidor Final"),
                "address": data.get("cliente_direccion", "") or "No informado",
                "email": data.get("cliente_email", ""),
                "phone": data.get("cliente_telefono", ""),
                "legal_organization_id": "1" if es_juridica else "2",
                "tribute_id": "48" if es_juridica else "21",
                "identification_document_id": tipo_doc_cliente,
                "identification_document_code": tipo_doc_cliente,
                "municipality_id": data.get("cliente_municipio_id", "980"),
            },
            "items": items,
        }

    # ─────────────────────────────────────────────
    # CONSTRUIR PAYLOAD
    # ─────────────────────────────────────────────

    def _construir_payload(self, data: dict) -> dict:
        """
        Construye el JSON que Factus v2 espera para emitir una factura.

        data debe contener:
          - numero_factura: int
          - fecha_emision: datetime
          - cliente_nombre: str
          - cliente_nit: str
          - cliente_tipo_doc: str (CC, NIT, CE, PA)
          - cliente_direccion: str
          - cliente_telefono: str
          - cliente_email: str
          - items: list de {nombre, cantidad, precio_unitario, total}
          - subtotal: float
          - iva: float
          - total: float
        """
        from datetime import date as date_type

        fecha = data.get("fecha_emision", datetime.now())
        if isinstance(fecha, str):
            fecha = datetime.fromisoformat(fecha)

        fecha_vencimiento_str = data.get("fecha_vencimiento")
        if fecha_vencimiento_str:
            # Crédito: forma de pago 2, fecha real
            payment_form = "2"
            due_date = fecha_vencimiento_str if isinstance(fecha_vencimiento_str, str) else fecha_vencimiento_str.strftime("%Y-%m-%d")
        else:
            # Contado: forma de pago 1, fecha de emisión
            payment_form = "1"
            due_date = fecha.strftime("%Y-%m-%d")

        # Medio de pago DIAN: 10=Efectivo, 48=Tarjeta, 42=Transferencia
        # ZZZ es código interno para crédito — en DIAN se mapea a 1 (instrumento no definido)
        medio_pago_raw = data.get("medio_pago_codigo", "10")
        medio_pago_dian = "1" if medio_pago_raw == "ZZZ" else medio_pago_raw

        # Mapeo tipo documento cliente
        tipo_doc_map = {
            "CC": "13",
            "NIT": "31",
            "CE": "22",
            "PA": "41",
        }
        tipo_doc_raw = data.get("cliente_tipo_doc", "CC")
        tipo_doc_cliente = tipo_doc_map.get(tipo_doc_raw, "13")

        # Determinar si es persona jurídica (NIT) o natural (CC, CE, PA)
        es_juridica = tipo_doc_raw == "NIT"
        legal_organization_id = "1" if es_juridica else "2"
        # Régimen tributario del cliente:
        # "21" = No responsable de IVA (persona natural régimen simple)
        # "49" = No responsable de IVA (simplificado)
        # "48" = Responsable de IVA (régimen común — empresas)
        tribute_id_cliente = "48" if es_juridica else "21"

        # Construir líneas de detalle — formato API v2
        items = []
        total_invoice = 0.0
        for i, item in enumerate(data.get("items", []), 1):
            precio = float(item.get("precio_unitario", 0))
            # aceptar cantidades decimales (ej: 0.25)
            cantidad = float(item.get("cantidad", 1))
            tarifa = int(item.get("tarifa_iva", 19))

            # Si el precio incluye IVA, calcular base gravable
            if tarifa > 0:
                divisor = 1 + tarifa / 100
                precio_base = round(precio / divisor, 2)
            else:
                precio_base = precio

            linea_total = cantidad * precio_base * (1 + tarifa / 100)
            total_invoice += linea_total

            items.append({
                "code_reference": str(i),
                "name": item.get("nombre", "Producto"),
                "quantity": round(cantidad, 6),
                "discount_rate": 0,
                "price": precio_base,          # precio sin IVA para Factus
                "unit_measure_code": "94",
                "standard_code": "999",
                "is_excluded": 1 if tarifa == 0 else 0,
                "taxes": [
                    {
                        "code": "01",                          # 01 = IVA
                        "rate": f"{tarifa:.2f}",               # tarifa real del producto
                        "tribute_id": 22,
                    }
                ],
                "withholding_taxes": [],
            })

        calculado_total = round(total_invoice, 2)
        payload = {
            "numbering_range_id": data.get("rango_numeracion_id", self.numbering_range_id),
            # reference_code debe ser único — usamos prefijo "V" + número para evitar colisiones
            "reference_code": f"V{data.get('numero_factura', 1)}",
            "observation": "Factura de venta",
            # v2: payment_details reemplaza payment_method_code
            "payment_details": [
                {
                    "payment_method_code": medio_pago_dian,
                    "payment_form": payment_form,
                    "due_date": due_date,
                    "amount": f"{calculado_total:.2f}",
                }
            ],
            "customer": {
                "identification": data.get("cliente_nit", "222222222222"),
                "dv": data.get("cliente_dv", ""),
                "company": data.get("cliente_nombre", "Consumidor Final"),
                "trade_name": data.get("cliente_nombre", "Consumidor Final"),
                "names": data.get("cliente_nombre", "Consumidor Final"),
                "address": data.get("cliente_direccion", "") or "No informado",
                "email": data.get("cliente_email", ""),
                "phone": data.get("cliente_telefono", ""),
                "legal_organization_id": legal_organization_id,
                "tribute_id": tribute_id_cliente,
                "identification_document_id": tipo_doc_cliente,
                "identification_document_code": tipo_doc_cliente,
                "municipality_id": data.get("cliente_municipio_id", "980"),
            },
            "items": items,
        }

        return payload


# Instancia única
factus_service = FactusService()
