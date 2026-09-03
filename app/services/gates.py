"""Los dos cortes del proceso de Consultor de Negocios.

**Gate 1** (automatico + humano): el candidato pasa si saco `gate1_written_min`
en las preguntas Y `gate1_video_min` en el video. Pasar el corte de puntaje NO lo
habilita: despues Paula tiene que mirar el video y aprobarlo a mano, porque
presencia y tono no se puntuan con una rubrica. El sistema avisa a Slack y espera
la decision escrita en la planilla.

**Gate 2** (automatico): el candidato pasa si en la sesion del Business IQ Test
identifico las dos palancas correctas (asistencia en el Caso 1, publicidad en el
Caso 2). Ahi termina el trabajo del sistema: la entrevista con el lider comercial
es humana y no se registra aca.

Todo lo de este modulo es barato y sin LLM: corre en el ciclo de ingesta (cada 60
s), no en el de procesamiento.
"""

from datetime import datetime, timezone

import secrets

from app import database as db
from app.config import settings
from tools import slack
from tools import correo
from tools.logger import get_logger
from tools.meet_recordings import listar_sesiones, matchear, normalizar
from tools.motivos import resumen_error

log = get_logger("gates")

# Problemas de matcheo ya avisados, por monitor. En memoria a proposito: es un
# antirruido para no escribir la misma linea de actividad cada 60 segundos. Que
# se pierda en un reinicio no tiene costo (vuelve a avisar una vez).
_problemas_avisados: dict[str, set[str]] = {}

# Tope de candidatos por aviso de Slack. Con el backfill de una convocatoria
# entera (74 candidatos ya evaluados) un mensaje por candidato seria spam, y uno
# solo con 74 lineas no se lee: se manda de a tandas.
MAX_POR_AVISO = 15


def _ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def etapa_iq_activa(monitor: dict) -> bool:
    """La etapa IQ existe solo si el monitor tiene carpeta de grabaciones.

    Es lo que mantiene a los otros monitores (becas, editor, setter) exactamente
    como estaban: sin carpeta configurada, nada de este modulo los toca.
    """
    return bool(monitor.get("iq_recordings_folder_id"))


def gate1_configurado(monitor: dict) -> bool:
    return (
        monitor.get("gate1_written_min") is not None
        or monitor.get("gate1_video_min") is not None
    )


def calcular_gate1(monitor: dict, c: dict) -> bool | None:
    """True/False si ya se puede decidir; None si todavia no.

    'no_video' cuenta como NO califica y no como "todavia no": un candidato que
    no mando video no puede evaluarse en presencia ni en tono, que es justo lo
    que este corte protege.
    """
    if c.get("written_status") != "completed":
        return None
    vs = c.get("video_status")
    if vs not in ("completed", "no_video"):
        return None

    min_w = monitor.get("gate1_written_min")
    min_v = monitor.get("gate1_video_min")

    if min_w is not None and float(c.get("written_score") or 0) < float(min_w):
        return False
    if min_v is not None:
        if vs == "no_video":
            return False
        if float(c.get("video_score") or 0) < float(min_v):
            return False
    return True


def calcular_gate2(c: dict) -> bool | None:
    """El IQ se pasa con las DOS palancas correctas. None si no hay nota todavia."""
    if c.get("iq_status") != "completed":
        return None
    desglose = c.get("iq_breakdown") or {}
    return bool(desglose.get("caso_1_correcto")) and bool(desglose.get("caso_2_correcto"))


def estado_texto(monitor: dict, c: dict) -> str:
    """La frase que se escribe en la columna `Estado` de la planilla.

    Es la unica lectura del embudo que ve el equipo sin abrir el dashboard, asi
    que dice siempre de quien es el proximo paso.
    """
    ws, vs, iq = c.get("written_status"), c.get("video_status"), c.get("iq_status")

    if ws == "error" or vs == "error":
        return f"Revisar a mano: {resumen_error(c.get('error_message'))}"
    if calcular_gate1(monitor, c) is None:
        return "En evaluacion"

    decision = c.get("gate1_decision") or "pendiente"
    if decision == "rechazado":
        return "Rechazado en la revision de Paula"

    # La decision de Paula gana sobre el corte de puntaje, en los dos sentidos.
    # No es un caso raro: en la convocatoria de agosto entro a entrevista un
    # candidato con 69/80 sobre un corte de 70. El sistema marca el corte; quien
    # decide es ella, y la excepcion queda escrita en la planilla en vez de
    # quedar solo en su cabeza.
    excepcion = "" if c.get("gate1_pass") else " (excepcion al corte)"

    if not c.get("gate1_pass") and decision != "aprobado":
        if vs == "no_video":
            return "No califica: sin video evaluable"
        return "No califica"

    if decision == "pendiente":
        return "Califica: esperando aprobacion de Paula"

    if not etapa_iq_activa(monitor):
        return f"Aprobado por Paula{excepcion}"
    if iq == "waiting":
        return f"Aprobado{excepcion}: esperando la sesion de IQ"
    if iq in ("pending", "processing"):
        return "Sesion de IQ en evaluacion"
    if iq == "error":
        return f"Sesion de IQ con error: {resumen_error(c.get('error_message'))}"
    if iq == "no_session":
        return "Sin sesion de IQ"
    if iq == "completed":
        return "IQ aprobado: pasa a entrevista" if c.get("gate2_pass") else "No pasa el IQ"
    return "En evaluacion"



def _invitar_al_iq(monitor: dict, c: dict) -> None:
    """Le manda al candidato aprobado el link para agendar su sesion de IQ.

    Es el ultimo eslabon del embudo: Paula escribe "Si" en la planilla, y de ahi
    sale solo el correo con el link. Nadie mas toca nada.

    Tres decisiones que importan:

    - **El token se genera una sola vez.** Si ya tiene uno, se reusa: el link que
      el candidato guardo en su bandeja tiene que seguir funcionando para
      siempre. Regenerarlo le rompe el correo que ya recibio.
    - **`iq_invited_at` se escribe DESPUES de que Gmail confirma.** Es lo que
      evita mandarle el correo dos veces. Al reves -- marcar primero -- un fallo
      dejaria a un candidato aprobado sin recibir nada, y nadie se enteraria.
    - **No levanta excepcion nunca.** Esto corre dentro del ciclo de gates: un
      correo que falla no puede dejar sin procesar a los demas candidatos. Se
      loguea y el proximo ciclo reintenta, porque sin `iq_invited_at` la
      condicion sigue dando verdadera.
    """
    if c.get("iq_invited_at"):
        return
    if not settings.IQ_INVITACION_ACTIVA:
        # A proposito NO se marca `iq_invited_at`: cuando se prenda el
        # interruptor, los aprobados de mientras reciben su invitacion sin que
        # nadie tenga que ir a buscarlos.
        log.info(
            f"{c.get('name')}: aprobado, pero la invitacion al IQ esta apagada "
            "(IQ_INVITACION_ACTIVA). Queda pendiente."
        )
        return
    email = (c.get("email") or "").strip()
    nombre = (c.get("name") or "").strip()
    if not email:
        log.warning(f"{nombre or 'sin nombre'} aprobado pero sin mail: no se puede invitar")
        return

    base = (settings.DASHBOARD_URL or "").strip().rstrip("/")
    if not base:
        log.error("Falta DASHBOARD_URL: no se puede armar el link de la sesion")
        return

    token = c.get("iq_session_token")
    if not token:
        token = secrets.token_urlsafe(32)
        try:
            db.update_candidate(c["id"], {"iq_session_token": token})
            c["iq_session_token"] = token
        except Exception as e:
            log.error(f"No se pudo guardar el token de {nombre}: {str(e)[:200]}")
            return

    try:
        asunto, cuerpo = correo.cargar_plantilla(
            "correo_iq_test.md",
            nombre=nombre.split(" ")[0] or nombre,
            link=f"{base}/iq/entrar/{token}",
        )
    except Exception as e:
        log.error(f"La plantilla del IQ no se pudo armar: {e}")
        return

    r = correo.enviar(email, asunto, cuerpo)
    if not r["enviado"]:
        log.info(f"{nombre}: invitacion al IQ no enviada ({r['motivo']})")
        return

    db.update_candidate(c["id"], {"iq_invited_at": _ahora_iso()})
    c["iq_invited_at"] = _ahora_iso()
    db.log_activity(
        monitor["id"], "iq_invitacion",
        f"{nombre}: se le mando el link para agendar su sesion de IQ",
    )
    log.info(f"{nombre}: invitado al IQ Test")

# --- Gate 1: calculo, decision de Paula y aviso ----------------------------

_SI = {"si", "s", "yes", "y", "x", "ok", "true", "verdadero", "aprobado", "aprobada", "1"}
_NO = {"no", "n", "false", "falso", "rechazado", "rechazada", "0"}


def _leer_decision(valor: object) -> str | None:
    """Traduce la celda de Paula a 'aprobado' / 'rechazado' / None (sin decidir).

    Acepta lo que una persona realmente escribe en una planilla (si, sí, x, ok) y
    lo que escribe una casilla de verificacion de Sheets (TRUE/FALSE). Cualquier
    otra cosa se ignora: una celda con un comentario no es una decision.
    """
    texto = normalizar(valor)
    if not texto:
        return None
    if texto in _SI:
        return "aprobado"
    if texto in _NO:
        return "rechazado"
    return None


def _indice_columna(headers: list[str], nombre: str | None) -> int | None:
    if not nombre:
        return None
    objetivo = (nombre or "").strip().lower()
    for i, h in enumerate(headers):
        if (h or "").strip().lower() == objetivo:
            return i
    return None


def _sincronizar_aprobaciones(
    monitor: dict, headers: list[str], data_rows: list[list[str]], estados: list[dict]
) -> int:
    """Trae de la planilla la decision manual de Paula. Devuelve cuantas cambiaron.

    Por que se lee aca y no en el ingest de filas nuevas: el ingest saltea las
    filas que ya existen (`if sheet_row in ya_existen: continue`), y la decision
    de Paula aparece SIEMPRE en una fila que ya existe. Es la unica celda de la
    planilla que el sistema vuelve a leer despues de ingerir la fila.
    """
    idx = _indice_columna(headers, monitor.get("approval_column"))
    if idx is None:
        return 0

    por_fila = {c["sheet_row"]: c for c in estados}
    cambios = 0

    for row_idx, fila_datos in enumerate(data_rows):
        sheet_row = row_idx + 2
        c = por_fila.get(sheet_row)
        if not c:
            continue
        if idx >= len(fila_datos):
            continue

        nueva = _leer_decision(fila_datos[idx])
        actual = c.get("gate1_decision") or "pendiente"
        if nueva is None or nueva == actual:
            continue

        update = {
            "gate1_decision": nueva,
            "gate1_decided_at": _ahora_iso(),
            # La decision cambia la columna Estado: hay que volver a escribir la fila.
            "sheet_synced_at": None,
        }
        if nueva == "rechazado":
            # Cierra la etapa IQ: no se busca su sesion ni se le paga una evaluacion.
            update["iq_status"] = "no_session"
        elif c.get("iq_status") == "no_session":
            # Paula cambio de "No" a "Si": se vuelve a habilitar la busqueda.
            update["iq_status"] = "waiting"

        db.update_candidate(c["id"], update)
        c.update(update)
        cambios += 1
        db.log_activity(
            monitor["id"],
            "gate1_decision",
            f"{c.get('name') or 'sin nombre'} (fila {sheet_row}): Paula marco {nueva}",
        )

        if nueva == "aprobado":
            _invitar_al_iq(monitor, c)

    if cambios:
        log.info(f"Monitor {monitor['id']}: {cambios} decisiones de Paula sincronizadas")
    return cambios


def _persistir_gates(monitor: dict, estados: list[dict]) -> int:
    """Calcula y guarda gate1_pass / gate2_pass de los que ya se pueden decidir.

    Se recalcula sobre todos los candidatos en cada ciclo (una query, sin LLM):
    asi entran tambien los que quedaron evaluados ANTES de que este corte
    existiera, que es el caso de la convocatoria que ya estaba corriendo.
    """
    cambios = 0
    for c in estados:
        update = {}

        g1 = calcular_gate1(monitor, c)
        if g1 is not None and g1 != c.get("gate1_pass"):
            update["gate1_pass"] = g1

        g2 = calcular_gate2(c)
        if g2 is not None and g2 != c.get("gate2_pass"):
            update["gate2_pass"] = g2

        if not update:
            continue
        update["sheet_synced_at"] = None  # cambia la columna Estado
        db.update_candidate(c["id"], update)
        c.update(update)
        cambios += 1

    return cambios


def _linea_candidato(monitor: dict, c: dict) -> str:
    w = c.get("written_score")
    v = c.get("video_score")
    min_w = monitor.get("gate1_written_min")
    min_v = monitor.get("gate1_video_min")
    partes = [f"*{c.get('name') or 'sin nombre'}*"]
    if w is not None:
        partes.append(f"preguntas {w:g}" + (f" (corte {float(min_w):g})" if min_w else ""))
    if v is not None:
        partes.append(f"video {v:g}" + (f" (corte {float(min_v):g})" if min_v else ""))
    partes.append(f"fila {c.get('sheet_row')}")
    linea = " · ".join(partes)
    if c.get("email"):
        linea += f"\n   {c['email']}"
    if c.get("video_url"):
        linea += f"\n   Video: {c['video_url']}"
    return linea


def _texto_aviso(monitor: dict, tanda: list[dict], total: int, errores: int) -> str:
    titulo = monitor.get("sheet_title") or "Proceso de seleccion"
    col = monitor.get("approval_column") or "Aprobacion Paula"

    lineas = [
        f"*{titulo}* — {len(tanda)} candidato(s) pasaron el corte del formulario",
        f"Mira el video (presencia y tono) y escribi *Si* o *No* en la columna "
        f"*{col}* de la planilla. Hasta que no este esa decision, el candidato no "
        f"avanza a la sesion de IQ.",
        "",
    ]
    if total > len(tanda):
        lineas.insert(
            1, f"_Quedan {total - len(tanda)} mas en cola: se avisan en los proximos minutos._"
        )

    for i, c in enumerate(tanda, 1):
        lineas.append(f"{i}. {_linea_candidato(monitor, c)}")

    if errores:
        lineas += [
            "",
            f":warning: {errores} candidato(s) quedaron *sin evaluar* (video sin "
            f"acceso, link roto o error de transcripcion). NO estan descartados: "
            f"hay que revisarlos a mano en el dashboard.",
        ]

    if monitor.get("sheet_url"):
        lineas += ["", f"Planilla: {monitor['sheet_url']}"]
    if settings.DASHBOARD_URL:
        base = settings.DASHBOARD_URL.rstrip("/")
        lineas.append(f"Dashboard: {base}/monitors/{monitor['id']}")

    return "\n".join(lineas)


def _avisar_gate1(monitor: dict, estados: list[dict]) -> int:
    """Avisa a Slack los que pasaron el corte y todavia no se avisaron.

    Un mensaje por tanda, no por candidato: cuando esto se prende sobre una
    convocatoria que ya venia corriendo hay decenas de candidatos evaluados, y 74
    mensajes seguidos no los lee nadie.

    Si Slack falla o no esta configurado NO se marca como avisado: el proximo
    ciclo lo vuelve a intentar. Perder el aviso es perder el paso del proceso.
    """
    pendientes = [
        c
        for c in estados
        if c.get("gate1_pass")
        and not c.get("gate1_notified_at")
        and (c.get("gate1_decision") or "pendiente") == "pendiente"
    ]
    if not pendientes:
        return 0

    tanda = pendientes[:MAX_POR_AVISO]

    # Los errores que se mencionan al pie son solo los de candidatos que todavia
    # esperan atencion: sin avisar y sin decision de Paula. Antes se contaban
    # TODOS, asi que un aviso sobre dos candidatos nuevos arrastraba "16
    # quedaron sin evaluar" de una convocatoria vieja que Paula ya estaba
    # resolviendo a mano. Un numero que no cambia nunca deja de ser informacion.
    errores = sum(
        1
        for c in estados
        if (c.get("written_status") == "error" or c.get("video_status") == "error")
        and not c.get("gate1_notified_at")
        and (c.get("gate1_decision") or "pendiente") == "pendiente"
    )

    if not slack.enviar(_texto_aviso(monitor, tanda, len(pendientes), errores)):
        return 0

    ids = [c["id"] for c in tanda]
    db.mark_gate1_notified(ids)
    ahora = _ahora_iso()
    for c in tanda:
        c["gate1_notified_at"] = ahora

    db.log_activity(
        monitor["id"],
        "gate1_aviso",
        f"Aviso a Slack: {len(tanda)} candidato(s) esperando la aprobacion de Paula",
    )
    return len(tanda)


# --- Etapa IQ: encontrar la sesion de cada aprobado ------------------------


def _matchear_sesiones(monitor: dict, estados: list[dict], emit_event=None) -> int:
    """Busca en Drive la sesion de los aprobados y los pone en la cola del IQ.

    Solo se buscan los candidatos que Paula aprobo: nunca se evalua (ni se paga)
    la sesion de alguien que no paso por su revision.
    """
    if not etapa_iq_activa(monitor):
        return 0

    elegibles = [
        c
        for c in estados
        if c.get("gate1_decision") == "aprobado"
        and c.get("iq_status") == "waiting"
        and not c.get("iq_source_file_id")
    ]
    if not elegibles:
        # Sin nadie esperando sesion no se toca Drive: el ciclo corre cada 60 s.
        return 0

    # La rubrica tiene que estar confirmada ANTES de encolar la sesion de nadie.
    # Si se encolara sin rubrica, cada candidato gastaria sus 3 intentos contra
    # una evaluacion que no puede correr y quedaria varado: exactamente lo que
    # paso en la convocatoria pasada con la rubrica de video.
    rubrica = db.get_criteria_for_monitor(monitor["id"], "iq")
    if not rubrica or not rubrica.get("confirmed"):
        avisados = _problemas_avisados.setdefault(monitor["id"], set())
        if "sin_rubrica" not in avisados:
            avisados.add("sin_rubrica")
            mensaje = (
                f"{len(elegibles)} candidato(s) aprobados esperan su sesion de IQ, "
                f"pero la rubrica 'iq' no esta cargada o no esta confirmada: no se "
                f"encola ninguna sesion hasta que lo este."
            )
            log.warning(f"Monitor {monitor['id']}: {mensaje}")
            db.log_activity(monitor["id"], "iq_sin_rubrica", mensaje)
        return 0

    sesiones = listar_sesiones(
        monitor["iq_recordings_folder_id"], monitor.get("iq_session_title")
    )
    if not sesiones:
        return 0

    resultado = matchear(sesiones, elegibles)
    encontrados = 0

    for m in resultado["matches"]:
        c, s = m["candidate"], m["sesion"]
        update = {
            "iq_source_file_id": s["file_id"],
            "iq_source_kind": s["kind"],
            "iq_status": "pending",
            # Trabajo nuevo = intentos nuevos. Si el candidato venia con intentos
            # gastados de las etapas anteriores, sin esto su sesion no se
            # reclamaria nunca (la cola pide attempts < MAX_ATTEMPTS).
            "attempts": 0,
            "worker_id": None,
            "lease_expires_at": db.EPOCH,
            "sheet_synced_at": None,
        }
        try:
            db.update_candidate(c["id"], update)
        except Exception as e:
            # El indice unico (monitor_id, iq_source_file_id) rechaza asignar el
            # mismo archivo a dos candidatos. Que falle uno no puede dejar sin
            # asignar a los demas de esta tanda.
            log.error(
                f"No se pudo asignar la sesion '{s['name']}' a {c.get('name')}: {e}"
            )
            continue
        c.update(update)
        encontrados += 1
        db.log_activity(
            monitor["id"],
            "iq_sesion_encontrada",
            f"{c.get('name')} (fila {c.get('sheet_row')}): {s['kind']} de la sesion "
            f"encontrado en Drive",
        )
        if emit_event:
            emit_event({"type": "iq_found", "name": c.get("name"), "kind": s["kind"]})

    # Los problemas se avisan UNA vez cada uno: sin esto, una grabacion que no
    # matchea con nadie escribiria una linea de actividad cada 60 segundos.
    avisados = _problemas_avisados.setdefault(monitor["id"], set())
    for p in resultado["problemas"]:
        clave = f"{p['motivo']}:{p['sesion']}"
        if clave in avisados:
            continue
        avisados.add(clave)
        mensaje = (
            f"Sesion sin candidato: '{p['sesion']}' ({p['detalle']})"
            if p["motivo"] == "sin_match"
            else f"Sesion ambigua: '{p['sesion']}' matchea con {p['detalle']}"
        )
        log.warning(f"Monitor {monitor['id']}: {mensaje}")
        db.log_activity(monitor["id"], "iq_sesion_sin_match", mensaje)

    return encontrados


def ciclo(monitor: dict, headers: list[str], data_rows: list[list[str]], emit_event=None) -> dict:
    """Todo el trabajo de gates de un ciclo de ingesta. Barato y sin LLM.

    Corre en este orden a proposito:

    1. **calcular los gates** — asi los candidatos evaluados antes de que este
       corte existiera entran igual;
    2. **leer la decision de Paula** de la planilla;
    3. **avisar a Slack** a los nuevos que pasaron y todavia no se avisaron;
    4. **buscar en Drive** la sesion de los aprobados.

    Si algo de esto falla (Drive caido, webhook vencido) NO puede romper la
    ingesta: el llamador lo envuelve en try/except y el estado real vive en la
    base, asi que el proximo ciclo lo retoma.
    """
    if not gate1_configurado(monitor) and not etapa_iq_activa(monitor):
        return {}

    estados = db.list_candidates_gate_state(monitor["id"])
    if not estados:
        return {}

    return {
        "gates": _persistir_gates(monitor, estados),
        "decisiones": _sincronizar_aprobaciones(monitor, headers, data_rows, estados),
        "avisados": _avisar_gate1(monitor, estados),
        "sesiones": _matchear_sesiones(monitor, estados, emit_event),
    }
