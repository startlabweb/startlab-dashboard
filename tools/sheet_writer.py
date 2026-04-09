import gspread

from tools.logger import get_logger
from tools.sheet_reader import get_gspread_client

log = get_logger("sheet_writer")


def _col_letter(col_index: int) -> str:
    result = ""
    while col_index > 0:
        col_index, remainder = divmod(col_index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _find_column(headers: list[str], name: str) -> int | None:
    """Find 1-based column index by header name (case-insensitive partial match)."""
    name_lower = name.lower().strip()
    for i, h in enumerate(headers, 1):
        if h.lower().strip() == name_lower:
            return i
    # Partial match fallback
    for i, h in enumerate(headers, 1):
        if name_lower in h.lower().strip():
            return i
    return None


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

    client = get_gspread_client()
    sheet = client.open_by_key(sheet_id)
    ws = sheet.worksheet(worksheet_name)

    headers = ws.row_values(1)
    score_col = _find_column(headers, score_column)
    explanation_col = _find_column(headers, explanation_column)

    if not score_col:
        log.error(f"Column '{score_column}' not found in headers: {headers}")
        return
    if not explanation_col:
        log.error(f"Column '{explanation_column}' not found in headers: {headers}")
        return

    cells = []
    for r in results:
        row = r["row_number"]
        cells.append(gspread.Cell(row=row, col=score_col, value=str(r.get("score", ""))))
        cells.append(gspread.Cell(row=row, col=explanation_col, value=str(r.get("explanation", ""))))

    ws.update_cells(cells, value_input_option="USER_ENTERED")
    log.info(f"Wrote {len(results)} results to columns '{score_column}' and '{explanation_column}'")
