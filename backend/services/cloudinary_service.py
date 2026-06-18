"""
Servicio de almacenamiento de imágenes con Cloudinary.
Si Cloudinary no está configurado, usa almacenamiento local (desarrollo).
"""
import os
import cloudinary
import cloudinary.uploader
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

CLOUD_NAME  = os.getenv("CLOUDINARY_CLOUD_NAME", "")
API_KEY     = os.getenv("CLOUDINARY_API_KEY", "")
API_SECRET  = os.getenv("CLOUDINARY_API_SECRET", "")

CLOUDINARY_ENABLED = bool(CLOUD_NAME and API_KEY and API_SECRET)

if CLOUDINARY_ENABLED:
    cloudinary.config(
        cloud_name=CLOUD_NAME,
        api_key=API_KEY,
        api_secret=API_SECRET,
        secure=True,
    )
    print(f"[Cloudinary] Configurado — cloud: {CLOUD_NAME}")
else:
    print("[Cloudinary] No configurado — usando almacenamiento local")


async def subir_imagen(file_bytes: bytes, filename: str, carpeta: str = "hanter") -> dict:
    """
    Sube una imagen a Cloudinary o la guarda localmente.
    Retorna: {"url": str, "public_id": str, "tipo": "cloudinary"|"local"}
    """
    if CLOUDINARY_ENABLED:
        try:
            result = cloudinary.uploader.upload(
                file_bytes,
                folder=carpeta,
                use_filename=True,
                unique_filename=True,
                overwrite=False,
                resource_type="image",
            )
            return {
                "url": result.get("secure_url"),
                "public_id": result.get("public_id"),
                "tipo": "cloudinary",
            }
        except Exception as e:
            print(f"[Cloudinary] Error al subir: {e}")
            raise

    # Fallback: almacenamiento local
    import secrets
    ext = filename.rsplit(".", 1)[-1] if "." in filename else "png"
    name = f"{secrets.token_hex(8)}.{ext}"
    upload_dir = os.path.join(BASE_DIR, "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    path = os.path.join(upload_dir, name)
    with open(path, "wb") as f:
        f.write(file_bytes)
    return {
        "url": f"uploads/{name}",
        "public_id": name,
        "tipo": "local",
    }


def eliminar_imagen(public_id: str) -> bool:
    """Elimina una imagen de Cloudinary."""
    if CLOUDINARY_ENABLED and public_id and "/" in public_id:
        try:
            cloudinary.uploader.destroy(public_id)
            return True
        except Exception as e:
            print(f"[Cloudinary] Error al eliminar {public_id}: {e}")
    return False
