import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    GOOGLE_SERVICE_ACCOUNT_JSON: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    MAX_VIDEO_SIZE_MB: int = int(os.getenv("MAX_VIDEO_SIZE_MB", "500"))
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")
    SECRET_KEY: str = os.getenv("SECRET_KEY", "change-me-in-production")
    POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

    # --- Cola ---
    # ROLE: "all" (web + worker, como hoy), "web" (solo dashboard) o "worker".
    # Permite separar el worker en un servicio aparte de Railway con solo cambiar
    # esta variable, sin tocar codigo.
    ROLE: str = os.getenv("ROLE", "all")

    # Cuantos candidatos se procesan a la vez. Es un dial: si a la medianoche se
    # va atrasado, se sube y se reinicia. El claim atomico lo hace seguro.
    # Con 2: 300 candidatos en ~2,1 h (medido). Pico de disco ~14 MB de 5 GB.
    WORKER_CONCURRENCY: int = int(os.getenv("WORKER_CONCURRENCY", "2"))

    # Techo del peor caso, no un parametro de tuning: yt-dlp 240s + subida a
    # Gemini hasta 300s + transcripcion + GPT, con margen. Un lease corto hace que
    # se le robe el trabajo a un worker que esta trabajando bien.
    LEASE_SECONDS: int = int(os.getenv("LEASE_SECONDS", "1800"))

    # Tope de intentos por candidato. Tambien es el techo de gasto:
    # 300 candidatos x 3 x $0.17 = $153 en el peor caso absoluto.
    MAX_ATTEMPTS: int = int(os.getenv("MAX_ATTEMPTS", "3"))

    # Cada cuanto se vuelcan los resultados al Sheet.
    SHEET_FLUSH_SECONDS: int = int(os.getenv("SHEET_FLUSH_SECONDS", "90"))

    # Escape del plan B nivel 2: si Sheets tira 429 igual, se apaga el volcado en
    # vivo y se sincroniza todo de una vez al final.
    SHEETS_SYNC_ENABLED: bool = os.getenv("SHEETS_SYNC_ENABLED", "true").lower() != "false"

    # Motor de transcripcion: "assembly" (titular) o "gemini" (respaldo).
    #
    # Decision del 14 ago 2026: AssemblyAI transcribe (timestamps reales, 2x mas
    # rapido, $50 de credito que cubren 330 h) y GPT-4o juzga la continuidad y
    # evalua — cero dependencia de Gemini, cuyo billing no estaba confirmado.
    # Calibrado contra Gemini sobre 4 roleplays reales: misma nota promedio
    # (11.5/20 ambos), diferencia media 1 punto.
    # Si AssemblyAI falla el lunes: TRANSCRIBER=gemini en Railway y reiniciar.
    TRANSCRIBER: str = os.getenv("TRANSCRIBER", "assembly")

    # --- Etapa IQ y avisos ---

    # Webhook entrante de Slack para el aviso "estos candidatos pasaron el corte
    # y esperan la aprobacion de Paula". Vacio = no se avisa, y el resto del
    # sistema funciona igual (el estado sigue estando en la planilla).
    SLACK_WEBHOOK_URL: str = os.getenv("SLACK_WEBHOOK_URL", "")

    # Alternativa al webhook: un bot token del workspace + el canal. Se agrego
    # porque no existia ningun webhook de Start Lab y crear uno requiere
    # autorizar una app dentro de Slack, que es una accion de persona; el bot
    # token ya existia. El webhook tiene prioridad si algun dia se crea.
    #
    # El bot TIENE que estar invitado al canal: con `chat:write` solo puede
    # escribir donde esta, y sin `chat:write.public` no puede entrar solo.
    # `SLACK_CANAL` acepta el ID (recomendado, ej. C01ABC2DEF3) o el nombre.
    SLACK_BOT_TOKEN: str = os.getenv("SLACK_BOT_TOKEN", "")
    SLACK_CANAL: str = os.getenv("SLACK_CANAL", "")

    # URL publica del dashboard. Solo se usa para poner el link en el aviso.
    DASHBOARD_URL: str = os.getenv("DASHBOARD_URL", "")

    # Tope de tamaño para la GRABACION de una sesion de IQ, aparte de
    # MAX_VIDEO_SIZE_MB (500, pensado para el video de 5 min del candidato): una
    # sesion de Meet de 40 minutos pesa entre 150 y 600 MB, y de esa grabacion se
    # baja solo el audio, asi que el disco no es la restriccion.
    IQ_MAX_RECORDING_MB: int = int(os.getenv("IQ_MAX_RECORDING_MB", "3000"))

    # --- La sala del IQ Test (la IA que conduce la sesion) ---

    # Puerta de la sala. Crear una clave efimera gasta plata y la URL del
    # dashboard es publica, asi que sin token cualquiera puede quemar credito de
    # OpenAI. Vacio = sala abierta, comodo para probar en local; en Railway TIENE
    # que estar seteada.
    IQ_SALA_TOKEN: str = os.getenv("IQ_SALA_TOKEN", "")

    # Que modelo de voz conduce la sesion. El grande entiende mejor cuando hay
    # que repreguntar; el mini cuesta como un tercio. Se arranca con el grande
    # para ver el techo de calidad y se baja despues sin tocar codigo.
    IQ_MODELO_VOZ: str = os.getenv("IQ_MODELO_VOZ", "gpt-realtime-2.1")
    IQ_VOZ: str = os.getenv("IQ_VOZ", "marin")

    # --- El correo con el formulario ---

    # APAGADO por defecto y a proposito. Es lo unico del sistema que le escribe a
    # una persona de afuera, y un correo mandado no se puede deshacer. En
    # simulacion se registra exactamente lo que se habria mandado, sin mandarlo.
    IQ_CORREO_ACTIVO: bool = os.getenv("IQ_CORREO_ACTIVO", "false").lower() == "true"

    # Interruptor aparte para la invitacion al IQ Test. Existe porque los dos
    # correos maduran a distinto ritmo: el del formulario se probo primero y
    # puede salir en vivo, mientras el del IQ todavia manda a un flujo (agendar
    # turno -> entrar a la sesion) que no se recorrio de punta a punta. Sin esta
    # separacion, prender uno obliga a prender el otro y un candidato real
    # recibiria un camino sin probar.
    IQ_INVITACION_ACTIVA: bool = (
        os.getenv("IQ_INVITACION_ACTIVA", "false").lower() == "true"
    )

    # Desde que buzon de Startlab sale el correo. La cuenta de servicio se hace
    # pasar por el, asi que un admin de Workspace tiene que autorizarle el scope
    # `gmail.send` una vez.
    IQ_CORREO_REMITENTE: str = os.getenv("IQ_CORREO_REMITENTE", "")

    # La planilla donde Paula deja nombre y mail de a quien invitar, y la hoja.
    # Se comparte con la cuenta de servicio como EDITOR: el sistema escribe la
    # fecha de envio ahi, y esa fecha es lo que evita mandar el correo dos veces.
    IQ_HOJA_ASIGNADOS: str = os.getenv("IQ_HOJA_ASIGNADOS", "")
    IQ_HOJA_ASIGNADOS_TAB: str = os.getenv("IQ_HOJA_ASIGNADOS_TAB", "")  # vacio = la primera

    # El link del formulario que va en el correo.
    IQ_LINK_FORM: str = os.getenv("IQ_LINK_FORM", "")


settings = Settings()
