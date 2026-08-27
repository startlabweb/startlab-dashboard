"""Sincronizacion batcheada de resultados al Google Sheet.

El entregable es el Sheet, pero la fuente de verdad es Postgres. Este modulo es
el puente, y esta hecho para que ese puente se pueda reconstruir en 2 requests
en cualquier momento — incluido el martes a la mañana si algo salio mal.

Por que existe: antes cada candidato escribia al Sheet dos veces (una por fase),
inline, apenas terminaba. Con ~4 requests por llamada eso eran ~2.400 requests
para 300 candidatos, con picos de 80/min contra un limite de 60. Y si el
contenedor moria entre la evaluacion y la escritura, el resultado quedaba en la
base y nunca aparecia en el Sheet, sin que nadie se enterara.

Ahora `sheet_synced_at` es el buffer: sobrevive reinicios, es consultable, y
alimenta el `sheet_dirty` de /progress.
"""

from app import database as db
from app.services import gates
from tools.logger import get_logger
from tools.motivos import resumen_error as _resumen_corto
from tools.sheet_writer import write_column, write_results, write_totals_formula

log = get_logger("sheet_sync")

# Google acepta payloads grandes pero no infinitos, y 300 explicaciones largas
# pueden sumar varios MB. Se cortan las explicaciones y se pagina el flush.
MAX_EXPLICACION = 4000
CHUNK_FILAS = 200


def _recortar(texto: object) -> str:
    s = "" if texto is None else str(texto)
    if len(s) > MAX_EXPLICACION:
        return s[: MAX_EXPLICACION - 3] + "..."
    return s


# El motivo corto para la celda de PUNTAJE (_resumen_corto) ahora vive en
# tools/motivos.py, compartido con la API del dashboard.


def sync_completed_to_sheet(
    monitor: dict,
    force: bool = False,
    email_by_row: dict[int, str] | None = None,
) -> dict:
    """Escribe al Sheet los candidatos terminales que todavia no se sincronizaron.

    Args:
        monitor: fila del monitor.
        force: reescribe TODO ignorando `sheet_synced_at`. Es el boton de panico.
        email_by_row: si viene, se saltea cualquier fila donde el email del sheet
            no coincida con el del candidato. Protege contra que alguien haya
            ordenado o insertado filas: sin esto, escribir por numero de fila le
            pondria el puntaje de un candidato a otro, en silencio.

    Returns:
        {"written": n, "video": n, "skipped": n, "requests": n}
    """
    monitor_id = monitor["id"]
    sheet_id = monitor["sheet_id"]
    worksheet = monitor.get("sheet_name", "Form Responses 1")

    # OJO: nunca cortar la funcion aca con un `return` temprano si `candidatos`
    # viene vacio. El calculo de "Puntaje total" de mas abajo tiene que correr
    # SIEMPRE, incluso cuando no hay nada nuevo que sincronizar por columna:
    # es el caso normal en regimen (todo ya sincronizado individualmente) y es
    # exactamente cuando puede faltar el total de alguien cuyo video termino
    # despues de que las escritas ya se marcaron sincronizadas.
    candidatos = db.list_candidates_for_sheet_sync(monitor_id, force=force)

    filas_escritas: list[dict] = []
    filas_video: list[dict] = []
    filas_iq: list[dict] = []
    ids_written: list[str] = []
    ids_video: list[str] = []
    ids_iq: list[str] = []
    filas_gate1: list[dict] = []
    filas_estado: list[dict] = []
    salteados = 0

    # Las columnas del embudo (IQ, Califica, Estado) se tocan SOLO en un monitor
    # que tiene el embudo configurado. Los otros tres (becas, editor, setter) no
    # tienen esos encabezados en su planilla, y escribirles ahi seria un error de
    # "columna no encontrada" en el log cada 90 segundos.
    con_embudo = gates.gate1_configurado(monitor) or gates.etapa_iq_activa(monitor)

    for c in candidatos:
        fila = c["sheet_row"]

        # Guarda de desalineacion
        if email_by_row is not None:
            esperado = (email_by_row.get(fila) or "").strip().lower()
            actual = (c.get("email") or "").strip().lower()
            if esperado and actual and esperado != actual:
                salteados += 1
                log.error(
                    f"Fila {fila} desalineada: el sheet tiene '{esperado[:40]}' y la "
                    f"base '{actual[:40]}'. NO se escribe."
                )
                continue

        ws_status = c.get("written_status")
        if ws_status in ("completed", "error") and (force or not c.get("sheet_synced_at")):
            if ws_status == "error":
                detalle = c.get("error_message") or "sin detalle"
                puntaje = f"Error: {_resumen_corto(detalle)}"
                explicacion = f"Escritas no evaluadas: {detalle}"
            else:
                puntaje = c.get("written_score")
                explicacion = c.get("written_explanation")
            filas_escritas.append(
                {
                    "row_number": fila,
                    "score": "" if puntaje is None else puntaje,
                    "explanation": _recortar(explicacion),
                }
            )
            ids_written.append(c["id"])

        vs_status = c.get("video_status")
        if vs_status in ("completed", "error") and (force or not c.get("sheet_synced_at")):
            if vs_status == "error":
                detalle = c.get("error_message") or "sin detalle"
                puntaje = f"Error: {_resumen_corto(detalle)}"
                explicacion = f"Video no evaluado: {detalle}"
            else:
                puntaje = c.get("video_score")
                explicacion = c.get("video_explanation")
            filas_video.append(
                {
                    "row_number": fila,
                    "score": "" if puntaje is None else puntaje,
                    "explanation": _recortar(explicacion),
                }
            )
            ids_video.append(c["id"])

        if not con_embudo:
            continue

        iq_status = c.get("iq_status")
        if iq_status in ("completed", "error") and (force or not c.get("sheet_synced_at")):
            if iq_status == "error":
                detalle = c.get("error_message") or "sin detalle"
                puntaje = f"Error: {_resumen_corto(detalle)}"
                explicacion = f"Sesion de IQ no evaluada: {detalle}"
            else:
                puntaje = c.get("iq_score")
                explicacion = c.get("iq_explanation")
            filas_iq.append(
                {
                    "row_number": fila,
                    "score": "" if puntaje is None else puntaje,
                    "explanation": _recortar(explicacion),
                }
            )
            ids_iq.append(c["id"])

        # Estas dos son estado derivado, no una nota: se reescriben cada vez que
        # la fila pasa por aca. Una celda de Estado vieja es peor que una vacia,
        # porque el equipo decide leyendo esa columna.
        if monitor.get("gate1_column"):
            g1 = c.get("gate1_pass")
            filas_gate1.append(
                {
                    "row_number": fila,
                    "value": "" if g1 is None else ("Sí" if g1 else "No"),
                }
            )
        if monitor.get("estado_column"):
            filas_estado.append(
                {"row_number": fila, "value": gates.estado_texto(monitor, c)}
            )

    requests = 0

    # Dos llamadas a write_results con TODAS las filas: 1 update_cells cada una.
    # `write_results` ya soportaba multiples filas; simplemente nunca se usaba asi.
    for i in range(0, len(filas_escritas), CHUNK_FILAS):
        lote = filas_escritas[i : i + CHUNK_FILAS]
        write_results(
            sheet_id=sheet_id,
            results=lote,
            worksheet_name=worksheet,
            score_column=monitor.get("written_score_column") or "Puntaje Preguntas",
            explanation_column=monitor.get("written_explanation_column") or "Explicación",
        )
        requests += 2

    for i in range(0, len(filas_video), CHUNK_FILAS):
        lote = filas_video[i : i + CHUNK_FILAS]
        write_results(
            sheet_id=sheet_id,
            results=lote,
            worksheet_name=worksheet,
            score_column=monitor.get("video_score_column") or "Puntaje Video",
            explanation_column=monitor.get("video_explanation_column") or "Explicación",
        )
        requests += 2

    if filas_iq and monitor.get("iq_score_column"):
        for i in range(0, len(filas_iq), CHUNK_FILAS):
            lote = filas_iq[i : i + CHUNK_FILAS]
            write_results(
                sheet_id=sheet_id,
                results=lote,
                worksheet_name=worksheet,
                score_column=monitor["iq_score_column"],
                explanation_column=monitor.get("iq_explanation_column")
                or "Explicación IQ",
            )
            requests += 2

    for columna, filas in (
        (monitor.get("gate1_column"), filas_gate1),
        (monitor.get("estado_column"), filas_estado),
    ):
        if not columna or not filas:
            continue
        for i in range(0, len(filas), CHUNK_FILAS):
            write_column(
                sheet_id=sheet_id,
                results=filas[i : i + CHUNK_FILAS],
                worksheet_name=worksheet,
                column_name=columna,
            )
            requests += 2

    # Marcar sincronizado DESPUES de escribir. Si el proceso muere en el medio, el
    # proximo flush reescribe las mismas celdas con el mismo contenido: es
    # idempotente porque `update_cells` sobreescribe por coordenada, no hace append.
    sincronizados = sorted(set(ids_written) | set(ids_video) | set(ids_iq))
    if sincronizados:
        db.mark_sheet_synced(sincronizados)

    # "Puntaje total" = escritas + roleplay, solo cuando las DOS fases estan
    # completas. Se recalcula TODOS los ciclos (no solo lo recien sincronizado):
    # si las escritas se sincronizaron en un ciclo y el video recien termino en
    # el siguiente, este candidato no aparece en `candidatos` de arriba (ya
    # tiene sheet_synced_at), pero si en list_completed_for_total. Es 1 sola
    # llamada batcheada, barata incluso con cientos de candidatos completos.
    #
    # Se escribe como FORMULA (=SUM(escritas, roleplay) de la propia fila), no
    # como valor: un valor escrito por numero de fila puede caer en la fila de
    # otra persona si la hoja se reordena (paso el 18-19 ago 2026); una formula
    # siempre suma las celdas de SU fila, este donde este.
    totales = 0
    completos = db.list_completed_for_total(monitor_id)
    if completos:
        # La misma guarda de desalineacion que arriba: aunque la formula sea
        # inocua en cualquier fila, no tiene sentido ponersela a una fila que
        # no corresponde a un candidato completo.
        filas_total = []
        for c in completos:
            fila = c["sheet_row"]
            if email_by_row is not None:
                esperado = (email_by_row.get(fila) or "").strip().lower()
                actual = (c.get("email") or "").strip().lower()
                if esperado and actual and esperado != actual:
                    salteados += 1
                    log.error(
                        f"Fila {fila} desalineada para 'Puntaje total': el sheet tiene "
                        f"'{esperado[:40]}' y la base '{actual[:40]}'. NO se escribe."
                    )
                    continue
            filas_total.append(fila)
        for i in range(0, len(filas_total), CHUNK_FILAS):
            write_totals_formula(
                sheet_id=sheet_id,
                rows=filas_total[i : i + CHUNK_FILAS],
                worksheet_name=worksheet,
                total_column="Puntaje total",
                sum_columns=(
                    monitor.get("written_score_column") or "Puntaje Preguntas",
                    monitor.get("video_score_column") or "Puntaje Video",
                ),
            )
            requests += 1
        totales = len(filas_total)

    resultado = {
        "written": len(filas_escritas),
        "video": len(filas_video),
        "iq": len(filas_iq),
        "estado": len(filas_estado),
        "skipped": salteados,
        "totals": totales,
        "requests": requests,
    }
    if filas_escritas or filas_video or filas_iq or salteados or totales:
        log.info(f"sheet_sync monitor {monitor_id}: {resultado}")
    return resultado
