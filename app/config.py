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


settings = Settings()
