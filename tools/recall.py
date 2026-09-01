"""Cliente de Recall.ai: mete la sala del IQ Test adentro de una reunion.

Como funciona, que es menos obvio de lo que parece: el bot **abre nuestra pagina
web** y transmite su audio y su video a la reunion como si fuera un participante
mas. Y en el otro sentido, Recall le mete el audio mezclado de la reunion al
microfono de esa pagina, con el permiso ya otorgado. O sea que
`getUserMedia({audio:true})` adentro del bot devuelve la voz del candidato.

Por eso `iq_sala.html` no necesita saber nada de Recall: es la misma pagina que
se prueba en un navegador. El bot es transporte, no logica.

Prueba de punta a punta contra una reunion de Zoom:

    python tools/recall.py "https://us02web.zoom.us/j/..." "https://<host>/iq/sala?t=<token>&auto=1&limpio=1"

El bot se saca de la llamada al terminar, pase lo que pase: Recall cobra por
minuto de bot en la reunion.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

TIMEOUT_ENTRADA = 180  # segundos esperando que entre antes de darlo por fallido
NOMBRE_BOT = "Asistente Start Lab"  # sin "bot" ni "IA": algunas salas los filtran


class ErrorRecall(Exception):
    pass


def _credenciales() -> tuple[str, str]:
    clave = os.getenv("RECALL_API_KEY")
    region = os.getenv("RECALL_REGION")
    if not (clave and region):
        raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ruta = os.path.join(raiz, ".env")
        if os.path.exists(ruta):
            for linea in open(ruta, encoding="utf-8"):
                if "=" not in linea or linea.strip().startswith("#"):
                    continue
                k, v = linea.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k == "RECALL_API_KEY" and not clave:
                    clave = v
                if k == "RECALL_REGION" and not region:
                    region = v
    if not clave:
        raise ErrorRecall("Falta RECALL_API_KEY")
    return clave, (region or "us-east-1")


def _base() -> tuple[str, str]:
    clave, region = _credenciales()
    return f"https://{region}.recall.ai/api/v1/bot", clave


def _api(metodo: str, url: str, clave: str, cuerpo: dict | None = None) -> dict:
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(url, data=datos, method=metodo)
    req.add_header("Authorization", f"Token {clave}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            texto = r.read().decode()
            return json.loads(texto) if texto.strip() else {}
    except urllib.error.HTTPError as e:
        raise ErrorRecall(f"la API respondio {e.code}: {e.read().decode()[:600]}")


def crear_bot(
    meeting_url: str,
    pagina_url: str,
    nombre: str = NOMBRE_BOT,
    join_at: str | None = None,
) -> dict:
    """Crea el bot que va a abrir `pagina_url` y transmitirla a la reunion.

    `join_at` en ISO-8601 hace que Recall lo mande solo a esa hora; sin eso entra
    ya mismo. Es lo que despues va a usar el agendamiento, para no tener un
    proceso nuestro esperando.
    """
    base, clave = _base()
    cuerpo: dict = {
        "meeting_url": meeting_url,
        "bot_name": nombre,
        "output_media": {
            "camera": {"kind": "webpage", "config": {"url": pagina_url}}
        },
    }
    if join_at:
        cuerpo["join_at"] = join_at
    return _api("POST", base + "/", clave, cuerpo)


def estado(bot_id: str) -> dict:
    base, clave = _base()
    return _api("GET", f"{base}/{bot_id}/", clave)


def mandar_chat(bot_id: str, mensaje: str) -> bool:
    """El chat es el respaldo de la pantalla: lo que muestra la camara del bot NO
    queda en la grabacion, y el texto del chat si."""
    base, clave = _base()
    for cuerpo in ({"to": "everyone", "message": mensaje}, {"message": mensaje}):
        try:
            _api("POST", f"{base}/{bot_id}/send_chat_message/", clave, cuerpo)
            return True
        except ErrorRecall:
            continue
    return False


def sacar(bot_id: str) -> None:
    base, clave = _base()
    try:
        _api("POST", f"{base}/{bot_id}/leave_call/", clave)
    except ErrorRecall:
        pass


def _seguir(bot_id: str, segundos: int) -> str | None:
    """Imprime los cambios de estado. Devuelve el codigo con el que entro, o None."""
    vistos: set[str] = set()
    arranque = time.time()
    while time.time() - arranque < segundos:
        bot = estado(bot_id)
        for cambio in bot.get("status_changes") or []:
            codigo = cambio.get("code", "?")
            marca = f"{codigo}@{cambio.get('created_at','')}"
            if marca in vistos:
                continue
            vistos.add(marca)
            extra = cambio.get("sub_code") or cambio.get("message") or ""
            print(f"  [{int(time.time()-arranque):>3}s] {codigo} {extra}", flush=True)
            if codigo.startswith("in_call"):
                return codigo
            if codigo in ("fatal", "call_ended"):
                return None
        time.sleep(5)
    return None


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    meeting_url, pagina_url = sys.argv[1].strip(), sys.argv[2].strip()

    if pagina_url.startswith("http://localhost") or "127.0.0.1" in pagina_url:
        raise SystemExit(
            "La pagina tiene que ser publica: el bot la abre desde su propio\n"
            "navegador, en la nube. localhost no existe para el."
        )

    print(f"Reunion: {meeting_url}")
    print(f"Pagina : {pagina_url}\n")

    bot = crear_bot(meeting_url, pagina_url)
    bot_id = bot.get("id")
    if not bot_id:
        raise SystemExit(f"No vino el id del bot: {bot}")
    print(f"Bot creado: {bot_id}\nEsperando que entre...\n")

    entro = None
    try:
        entro = _seguir(bot_id, TIMEOUT_ENTRADA)
        if not entro:
            print("\nNo entro. Revisar los estados de arriba.")
            return
        print(f"\nENTRO ({entro}).")
        print("Mira la reunion: tendrias que ver la sala en su camara y escuchar")
        print("la presentacion. Hablale como si fueras el candidato.\n")
        print("Enter para sacar el bot y terminar la prueba...")
        try:
            input()
        except EOFError:
            time.sleep(600)
    finally:
        print("\nSacando el bot de la llamada...")
        sacar(bot_id)
        print(f"Listo. Bot {bot_id} fuera.")


if __name__ == "__main__":
    try:
        main()
    except ErrorRecall as e:
        raise SystemExit(f"Se corto: {e}")
