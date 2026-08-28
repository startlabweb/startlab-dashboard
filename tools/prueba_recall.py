"""Prueba de admisión: ¿entra un bot INVITADO a un Meet sin que nadie lo admita?

Es la pregunta que decide toda la arquitectura del entrevistador de IA y que nadie
documenta con claridad: Recall dice que un bot no logueado "tipicamente" necesita
que un humano lo admita, y Google no lo aclara en ninguna pagina citable. Se
resuelve empiricamente en 3 minutos.

    python tools/prueba_recall.py "https://meet.google.com/abc-defg-hij"

Lee `RECALL_API_KEY` y `RECALL_REGION` del entorno o del .env del proyecto.

El veredicto sale de los estados que reporta la propia API:

    in_waiting_room  ->  golpeo la puerta: NO sirve como bot invitado
    in_call_*        ->  entro solo: sirve, y es el camino mas barato de todos

El bot se saca de la llamada al terminar, pase lo que pase: las horas de Recall se
cobran por minuto de bot en la reunion (las primeras 5 son gratis).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

MENSAJE_CHAT = (
    "Prueba del asistente de Start Lab. Si ves este mensaje, el asistente puede "
    "escribir en el chat de la reunion."
)

TIMEOUT_TOTAL = 240  # 4 minutos: mas que suficiente para entrar o quedar golpeando
ESPERA_VEREDICTO = 90  # segundos en sala de espera antes de darlo por fallido
NOMBRE_BOT = "Asistente Start Lab"  # sin "bot" ni "IA": Google filtra algunos nombres


def cargar_env():
    """Toma las variables del entorno o, si no estan, del .env del proyecto."""
    clave = os.getenv("RECALL_API_KEY")
    region = os.getenv("RECALL_REGION")
    if clave and region:
        return clave, region

    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ruta = os.path.join(raiz, ".env")
    if os.path.exists(ruta):
        with open(ruta, encoding="utf-8") as f:
            for linea in f:
                if "=" not in linea or linea.strip().startswith("#"):
                    continue
                k, v = linea.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k == "RECALL_API_KEY" and not clave:
                    clave = v
                if k == "RECALL_REGION" and not region:
                    region = v
    return clave, region


class ErrorAPI(Exception):
    pass


def api(metodo: str, url: str, clave: str, cuerpo: dict | None = None) -> dict:
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(url, data=datos, method=metodo)
    req.add_header("Authorization", f"Token {clave}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            texto = r.read().decode()
            return json.loads(texto) if texto.strip() else {}
    except urllib.error.HTTPError as e:
        detalle = e.read().decode()[:500]
        raise ErrorAPI(f"la API respondio {e.code}: {detalle}")


def estados(bot: dict) -> list[dict]:
    return bot.get("status_changes") or []


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    meeting_url = sys.argv[1].strip()

    clave, region = cargar_env()
    if not clave:
        raise SystemExit(
            "Falta RECALL_API_KEY. Ponela en el .env del proyecto o exportala."
        )
    region = region or "us-east-1"
    base = f"https://{region}.recall.ai/api/v1/bot"

    print(f"Region: {region}")
    print(f"Reunion: {meeting_url}")
    print("Lanzando el bot como INVITADO (sin cuenta de Google)...\n")

    bot = api("POST", base + "/", clave, {"meeting_url": meeting_url, "bot_name": NOMBRE_BOT})
    bot_id = bot.get("id")
    if not bot_id:
        raise SystemExit(f"No vino el id del bot: {bot}")
    print(f"Bot creado: {bot_id}\n")

    vistos: set[str] = set()
    chat_ok = False
    chat_detalle = ""
    entro_en = None
    espera_desde = None
    arranque = time.time()
    veredicto = None

    try:
        while time.time() - arranque < TIMEOUT_TOTAL:
            bot = api("GET", f"{base}/{bot_id}/", clave)
            for cambio in estados(bot):
                codigo = cambio.get("code", "?")
                marca = f"{codigo}@{cambio.get('created_at', '')}"
                if marca in vistos:
                    continue
                vistos.add(marca)
                extra = cambio.get("sub_code") or cambio.get("message") or ""
                print(f"  [{int(time.time() - arranque):>3}s] {codigo} {extra}")

                if codigo == "in_waiting_room" and espera_desde is None:
                    espera_desde = time.time()
                if codigo.startswith("in_call"):
                    entro_en = time.time()
                if codigo == "fatal":
                    veredicto = f"FATAL: {extra}"

            if entro_en or veredicto:
                break
            if espera_desde and time.time() - espera_desde > ESPERA_VEREDICTO:
                veredicto = "sala_de_espera"
                break
            time.sleep(5)
        if entro_en:
            print()
            print("Entro. Ahora: puede escribir en el chat siendo invitado?")
            cuerpos = ({"to": "everyone", "message": MENSAJE_CHAT}, {"message": MENSAJE_CHAT})
            for cuerpo in cuerpos:
                try:
                    api("POST", f"{base}/{bot_id}/send_chat_message/", clave, cuerpo)
                    chat_ok = True
                    print("  chat: OK, el mensaje salio")
                    break
                except ErrorAPI as e:
                    chat_detalle = str(e)
            if not chat_ok:
                print(f"  chat: FALLO - {chat_detalle}")

    finally:
        print()
        print("Sacando el bot de la llamada...")
        try:
            api("POST", f"{base}/{bot_id}/leave_call/", clave)
        except ErrorAPI as e:
            print(f"  (no se pudo: {e})")

    print("\n" + "=" * 62)
    if entro_en and chat_ok:
        print("RESULTADO: ENTRO SOLO y ADEMAS puede escribir en el chat.")
        print("  -> Sirve el camino Meet A: sin dominio nuevo, sin SSO, sin costo fijo.")
    elif entro_en:
        print("RESULTADO: ENTRO SOLO, pero NO pudo escribir en el chat.")
        print("  -> La voz sirve igual; el caso se muestra en pantalla en vez de")
        print("     mandarse por chat, o hace falta el bot logueado para el chat.")
    elif veredicto == "sala_de_espera":
        print("RESULTADO: QUEDO GOLPEANDO LA PUERTA (sala de espera).")
        print("  -> Meet exige bot logueado. Hay que elegir entre:")
        print("     B) subdominio + Workspace aparte con SSO (~US$8,40/mes), o")
        print("     C) pasar la sesion a Zoom (~US$17/mes).")
    elif veredicto:
        print(f"RESULTADO: {veredicto}")
        print("  -> Revisar el detalle de arriba antes de concluir.")
    else:
        print("RESULTADO: sin definicion en 4 minutos. Revisar los estados de arriba.")
    print("=" * 62)


if __name__ == "__main__":
    try:
        main()
    except ErrorAPI as e:
        raise SystemExit(f"Se corto la prueba: {e}")
