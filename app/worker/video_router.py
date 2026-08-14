"""Ruteo de la fuente del video.

Antes habia aca una segunda implementacion de la deteccion, con logica distinta a la
de `tools/sheet_reader.detect_video_url` — y esa divergencia era un bug: el processor
usa `detect_video_url` para clasificar y persistir `video_source`, y esta funcion solo
para re-extraer el file_id de Drive. Dos reglas distintas para la misma URL.

Ahora es un alias: una sola fuente de verdad, sin tocar los call sites.
"""

from tools.sheet_reader import detect_video_url as detect_video_source

__all__ = ["detect_video_source"]
