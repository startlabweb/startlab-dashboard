import gspread

from tools.logger import get_logger
from tools.sheet_reader import get_gspread_client

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

    client = get_gspread_client()
    sheet = client.open_by_key(sheet_id)
    ws = sheet.worksheet(worksheet_name)

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

    ws.update_cells(cells, value_input_option="USER_ENTERED")
    log.info(f"Wrote {len(results)} results to cols {score_col} and {explanation_col}")
