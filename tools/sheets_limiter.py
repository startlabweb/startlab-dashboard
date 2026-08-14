"""Rate limiter para la API de Google Sheets.

El limite de Google es 60 requests/minuto por usuario. El problema real no es el
total del dia sino el PICO por minuto: el 429 se come en la rafaga. Con el diseño
viejo (una escritura por candidato, cada una con ~4 requests) el pico llegaba a
80/min y por eso aparecian los 429.

Token bucket simple con una deque de timestamps. Es por proceso: alcanza porque
solo el worker escribe en los sheets.
"""

import threading
import time
from collections import deque

from tools.logger import get_logger

log = get_logger("sheets_limiter")

_lock = threading.Lock()
_marcas: deque[float] = deque()

LIMITE_DEFAULT = 40      # deja 20/min de margen sobre el limite real de 60
VENTANA_DEFAULT = 60.0


def acquire(cost: int = 1, limit: int = LIMITE_DEFAULT, window: float = VENTANA_DEFAULT) -> None:
    """Bloquea hasta que haya lugar para `cost` requests en la ventana.

    Args:
        cost: cuantos requests va a consumir la operacion que sigue.
        limit: techo de requests por ventana.
        window: tamaño de la ventana en segundos.
    """
    if cost <= 0:
        return

    while True:
        with _lock:
            ahora = time.monotonic()
            corte = ahora - window
            while _marcas and _marcas[0] < corte:
                _marcas.popleft()

            if len(_marcas) + cost <= limit:
                for _ in range(cost):
                    _marcas.append(ahora)
                return

            # Cuanto falta para que la marca mas vieja salga de la ventana
            espera = _marcas[0] + window - ahora

        espera = max(0.05, min(espera, window))
        log.info(f"Rate limit de Sheets: esperando {espera:.1f}s (uso {len(_marcas)}/{limit})")
        time.sleep(espera)


def uso_actual(window: float = VENTANA_DEFAULT) -> int:
    """Requests consumidos en la ventana. Para reportar en /progress."""
    with _lock:
        corte = time.monotonic() - window
        while _marcas and _marcas[0] < corte:
            _marcas.popleft()
        return len(_marcas)


def reset() -> None:
    """Solo para los tests."""
    with _lock:
        _marcas.clear()
