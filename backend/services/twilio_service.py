import os
from twilio.rest import Client
from dotenv import load_dotenv

# Cargar el .env desde la carpeta backend/ sin importar desde dónde se ejecute
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

class TwilioService:
    def __init__(self):
        self.account_sid = os.getenv('TWILIO_ACCOUNT_SID')
        self.auth_token = os.getenv('TWILIO_AUTH_TOKEN')
        self.from_whatsapp = os.getenv('TWILIO_WHATSAPP_FROM')

        print(f"[Twilio] SID cargado: {'✅' if self.account_sid else '❌ FALTA'}")
        print(f"[Twilio] Token cargado: {'✅' if self.auth_token else '❌ FALTA'}")
        print(f"[Twilio] FROM: {self.from_whatsapp or '❌ FALTA'}")
        
        if self.account_sid and self.auth_token:
            self.client = Client(self.account_sid, self.auth_token)
            print("[Twilio] Cliente inicializado correctamente ✅")
        else:
            self.client = None
            print("[Twilio] ❌ Cliente NO inicializado — faltan credenciales")

    def enviar_mensaje_whatsapp(self, to_number: str, message: str):
        print(f"[Twilio] Intentando enviar a: {to_number}")
        if not self.client:
            print("[Twilio] ❌ No hay cliente inicializado")
            return {"error": "Credenciales de Twilio no configuradas en el .env"}
        
        # Limpiar espacios y guiones
        to_number = to_number.strip().replace(" ", "").replace("-", "")

        # Quitar prefijo whatsapp: si ya lo tiene para trabajar solo con el número
        if to_number.startswith('whatsapp:'):
            to_number = to_number[len('whatsapp:'):]

        # Agregar código de país +57 si no tiene código internacional
        if not to_number.startswith('+'):
            to_number = f'+57{to_number}'

        to_number = f'whatsapp:{to_number}'
            
        print(f"[Twilio] FROM: {self.from_whatsapp} → TO: {to_number}")
        try:
            result = self.client.messages.create(
                from_=self.from_whatsapp,
                body=message,
                to=to_number
            )
            print(f"[Twilio] ✅ Mensaje enviado — SID: {result.sid}, Status: {result.status}")
            return {"sid": result.sid, "status": result.status}
        except Exception as e:
            print(f"[Twilio] ❌ Error al enviar: {str(e)}")
            return {"error": str(e)}

# Instancia única para ser importada
twilio_service = TwilioService()
