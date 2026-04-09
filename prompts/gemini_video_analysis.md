Analiza este video y devuelve SOLO esto, sin nada más, sin asteriscos, sin markdown:

{
  "text": "<transcripción literal>",
  "duracion_segundos": <número>,
  "pausas": <número de pausas largas detectadas (más de 2 segundos de silencio)>,
  "pausas_detalle": "<descripción breve de cuándo ocurren y qué tan largas son>",
  "muletillas": {
    "conteo": <número total de muletillas detectadas>,
    "lista": {"ehm": <n>, "este": <n>, "o sea": <n>, "bueno": <n>, "como": <n>, "entonces": <n>},
    "detalle": "<observación breve sobre el impacto en la fluidez>"
  },
  "continuidad": {
    "puntaje": <1 al 5, donde 5 es perfectamente fluido>,
    "detalle": "<descripción de si el habla es cortada, titubea, repite palabras o frases>"
  }
}
