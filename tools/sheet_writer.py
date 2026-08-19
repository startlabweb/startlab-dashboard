import gspread

from tools import sheets_limiter as limiter
from tools.logger import get_logger
from tools.sheet_reader import get_worksheet

log = get_logger("sheet_writer")


def _find_column(headers: list[str], name: str) -> int | None:
    """Find 1-based column index by header name (case-insensitive exact match)."""
    name_lower = name.lower().strip()
    for i, h in enumerate(headers, 1):
        if h.lower().strip() == name_lower:
            return i
    # Partial match fallback
    for i, h in enumerate(headers, 1):
        if name_lower in h.lower().strip():
            return i
    return None


def _find_explanation_column(headers: list[str], score_col: int, explanation_name: str) -> int | None:
    """Find the explanation column that comes AFTER the score column.

    When multiple columns share the same name (e.g. two 'Explicación'),
    pick the one immediately following the score column.
    """
    name_lower = explanation_name.lower().strip()
    # First try: the column right after the score column
    if score_col < len(headers):
        next_header = headers[score_col].lower().strip()  # score_col is 1-based, headers is 0-based
        if next_header == name_lower or name_lower in next_header:
            return score_col + 1

    # Fallback: find first match AFTER score_col
    for i, h in enumerate(headers, 1):
        if i > score_col and (h.lower().strip() == name_lower or name_lower in h.lower().strip()):
            return i

    # Last fallback: any match
    return _find_column(headers, explanation_name)


def write_results(
    sheet_id: str,
    results: list[dict],
    worksheet_name: str = "Form Responses 1",
    score_column: str = "Puntaje Roleplay",
    explanation_column: str = "Explicación",
) -> None:
    """Write evaluation results to specific columns in the sheet."""
    if not results:
        return

    # Handle cacheado: antes esto re-autorizaba y volvia a abrir el sheet en cada
    # llamada, y se la llamaba una vez por candidato por fase.
    ws = get_worksheet(sheet_id, worksheet_name)

    limiter.acquire(cost=1)
    headers = ws.row_values(1)
    score_col = _find_column(headers, score_column)

    if not score_col:
        log.error(f"Column '{score_column}' not found in headers: {headers}")
        return

    # Find the explanation column adjacent to the score column
    explanation_col = _find_explanation_column(headers, score_col, explanation_column)

    if not explanation_col:
        log.error(f"Column '{explanation_column}' not found after '{score_column}' in headers")
        return

    log.info(f"Writing to score_col={score_col}, explanation_col={explanation_col}")

    cells = []
    for r in results:
        row = r["row_number"]
        cells.append(gspread.Cell(row=row, col=score_col, value=str(r.get("score", ""))))
        cells.append(gspread.Cell(row=row, col=explanation_col, value=str(r.get("explanation", ""))))

    # Un solo update_cells para TODAS las filas: 600 celdas en 1 request.
    limiter.acquire(cost=1)
    ws.update_cells(cells, value_input_option="USER_ENTERED")
    log.info(f"Wrote {len(results)} results to cols {score_col} and {explanation_col}")


def write_totals_formula(
    sheet_id: str,
    rows: list[int],
    worksheet_name: str = "Form Responses 1",
    total_column: str = "Puntaje total",
    sum_columns: tuple[str, str] = ("Puntaje Preguntas", "Puntaje Roleplay"),
) -> None:
    """Escribe en la columna del total una FORMULA por fila: =SUM(escritas, roleplay).

    Pedido de Jossy (19 ago 2026) tras el incidente de los totales cruzados: el
    valor calculado se escribia por numero de fila y, con la hoja reordenada,
    podia caer en la fila de otra persona. Una formula referencia SU PROPIA
    fila, asi que aunque la base quede desalineada la celda nunca puede mostrar
    el total de otro candidato. SUM ignora texto ("Error: ...") y celdas vacias.
    """
    if not rows:
        return

    ws = get_worksheet(sheet_id, worksheet_name)

    limiter.acquire(cost=1)
    headers = ws.row_values(1)
    col_total = _find_column(headers, total_column)
    col_a = _find_column(headers, sum_columns[0])
    col_b = _find_column(headers, sum_columns[1])
    if not col_total or not col_a or not col_b:
        log.error(
            f"Columnas del total no encontradas (total={col_total}, "
            f"{sum_columns[0]}={col_a}, {sum_columns[1]}={col_b}) en: {headers}"
        )
        return

    cells = []
    for row in rows:
        ref_a = gspread.utils.rowcol_to_a1(row, col_a)
        ref_b = gspread.utils.rowcol_to_a1(row, col_b)
        cells.append(
            gspread.Cell(row=row, col=col_total, value=f"=SUM({ref_a},{ref_b})")
        )

    limiter.acquire(cost=1)
    ws.update_cells(cells, value_input_option="USER_ENTERED")
    log.info(f"Wrote {len(rows)} total formulas to col {col_total}")


def write_column(
    sheet_id: str,
    results: list[dict],
    worksheet_name: str = "Form Responses 1",
    column_name: str = "Puntaje total",
) -> None:
    """Escribe UNA sola columna (sin explicacion al lado). Usado para 'Puntaje
    total': una columna del equipo que el sistema calcula (escritas + roleplay)
    una vez que las dos fases estan completas.

    `results` es una lista de {"row_number": int, "value": ...}.
    """
    if not results:
        return

    ws = get_worksheet(sheet_id, worksheet_name)

    limiter.acquire(cost=1)
    headers = ws.row_values(1)
    col = _find_column(headers, column_name)
    if not col:
        log.error(f"Column '{column_name}' not found in headers: {headers}")
        return

    cells = [
        gspread.Cell(row=r["row_number"], col=col, value=str(r.get("value", "")))
        for r in results
    ]

    limiter.acquire(cost=1)
    ws.update_cells(cells, value_input_option="USER_ENTERED")
    log.info(f"Wrote {len(results)} totals to col {col}")
