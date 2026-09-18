# Orquestador post-llamada

Recibe **un evento** (fin de llamada o WhatsApp entrante), clasifica cómo fue y emite **una decisión**
y **cero o más órdenes** al CRM. Python 3.11+ y LangGraph 1.2.

## Cómo se ejecuta

```bash
uv sync               # o: python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env  # OPENAI_API_KEY; MODELO vacío = gpt-5.6-luna
uv run python run.py eventos/01-call-ended-nuria.json
rm -rf salida estado && grep -v '^#' eventos/orden.txt | xargs -I{} uv run python run.py eventos/{}  # lote completo
```

Escribe en `salida/` (se abre con `visor/index.html`) y guarda lo que debe recordar entre procesos en
`estado/orquestador.sqlite`. Sale con 0 si procesó el evento y con 1 si no pudo, sin escribir nada.

## Cómo funciona

```
admitir ─┬─ otra organización o reentrega ──────────────────────────► emitir
         ├─ message.received ──► atender_mensaje ───────────────────► emitir
         └─ call.ended ──► clasificar_senalizacion ─┬──────► decidir ─► emitir
                                                    └─► clasificar_conversacion (LLM) ─┘
```

- **La telefonía decide lo que puede** (486, 603, 408/480, 5xx, buzón, IVR) sin el modelo. El LLM solo
  lee conversaciones con una persona, con salida estructurada estricta; los prompts están en `prompts/`.
  El modelo solo interpreta la hora que pide el lead («mañana a las seis» → 16/09 18:00); plazos,
  ventana, días hábiles y zona horaria los calcula el código.
- **Las reglas son funciones puras** (`reglas.py`, `calendario.py`, `senalizacion.py`); los nodos solo
  las llaman y solo `emitir` tiene efectos.
- **Persistencia:** un **checkpointer** SQLite con un thread por hecho (organización +
  `idempotency_key`), donde una reentrega cae en un thread que ya tiene decisión (R5), y un **Store**
  con la memoria de cada lead: intentos, recordatorios, baja, WhatsApp rechazado, llamadas cortadas
  (R4, R7, N1, N2, N4).
- **Fallos del modelo:** `RetryPolicy` solo para errores transitorios (3 intentos) y un `error_handler`
  que deja el evento como `otro` para revisión humana (R8, N4). Un error de configuración (clave,
  modelo, saldo) no es del evento: el proceso sale con 1 y se reprocesa al corregirlo.

## Modelo: gpt-5.6-luna

`scripts/comparar_modelos.py`: 27 conversaciones de los lotes de verificación, 3 veces cada una; en los
callbacks compara la fecha y la hora por separado.

| Modelo | Etiquetas | Fechas | Horas | Estables | Latencia | USD / 1000 llamadas |
|---|---|---|---|---|---|---|
| gpt-4o-mini | 72/81 | 21/27 | 27/27 | 27/27 | 2,2 s | 0,30 |
| **gpt-5.6-luna** (razonamiento `low`) | **81/81** | **27/27** | 27/27 | 27/27 | 2,3 s | 0,51 |

gpt-4o-mini confunde siempre `cortada` con `visita_sin_confirmar` y falla el día de la semana («el
lunes» pedido un viernes le da domingo). Luna acierta todo por 0,2 USD más cada mil llamadas.

## Decisiones donde la especificación deja margen

Cada una lleva un comentario `Decisión:` en el código.

- **Ocupado** a los 60 minutos (mitad de 30–90), como el ejemplo resuelto. **Cortada y visita sin
  confirmar** a los 30, o en la primera franja válida si la ventana cerró; la visita no se reserva (N5).
- **Callback** a lo que pidió el lead, que interpreta el modelo porque es lenguaje libre. Día sin hora: la
  apertura de la ventana ese día; franja: su comienzo (tarde, 16:00); hora sin fecha o ya pasada: la
  próxima vez que llega. Si cae en otra hora, u otro día si no dio hora, `aviso_cambio_hora`.
- **Intentos:** el máximo vale para toda nueva llamada, callbacks incluidos. Agotado, el respaldo se
  resuelve una vez por lead: WhatsApp o, si lo rechazó, una tarea para decidir cómo seguir.
- **Recordatorios:** el WhatsApp al lead sale tras 48 horas como mínimo y dentro de la ventana. Al
  responder el lead solo se cancelan los que aún no salieron.
- **Baja:** manda sobre cualquier etiqueta y cancela los recordatorios pendientes; después, cada
  evento del lead solo cierra la llamada.
- **La cita del CRM manda sobre el modelo**, salvo una baja; sin cita, una visita nunca está reservada.
- **`nota_contexto`:** la del modelo; si no deja ninguna (o la deja en blanco), las notas del agente con
  el prefijo «Notas del agente:», todas las claves y sin traducir, para no perder las desconocidas.
- **Descolgó y no habló:** `otro`. **Plazos** en horas: tiempo absoluto; en días: misma hora de Madrid.
- **Confianza:** 0,97 SIP, 0,9 detector de buzón, 0,7 heurística; en conversaciones, la del modelo.
- **Ids:** `orden_id` con la fórmula del ejemplo resuelto; `reminder_id` estable y persistido.
- **Idioma del WhatsApp:** el de `lead.language` (`es` si falta o viene vacío). Los parámetros que
  armamos siguen en español, y el recordatorio al lead no lleva idioma en el OpenAPI: lo decide el CRM.

## Qué dejé fuera

- **Atomicidad entre la salida y SQLite:** si el proceso muere entre escribir las órdenes y guardar la
  memoria, la reentrega no duplica órdenes pero ese intento no queda contado. Pide un outbox.
- **Eventos del mismo lead en paralelo:** el contrato es un proceso por evento, en orden.
- **Timeout por nodo:** en LangGraph exige nodos asíncronos; el timeout está en el cliente de OpenAI.
- **LangSmith** está apagado por defecto: activarlo (`.env.example`) añade red hacia LangSmith.

## Cómo lo verifiqué

- `uv run pytest` (63): reglas, calendario, el ejemplo resuelto campo por campo y el grafo completo con
  un clasificador falso: nodos recorridos, reentregas y cada camino de fallo del modelo.
- `uv run python scripts/verificar_lote.py`: el lote de ejemplo y 50 eventos sintéticos, desde cero y
  un proceso por evento. Incluye los casos sin ejemplo, otras horas y días (domingo, 19:45, cambio de
  hora) y memoria entre eventos. Valida contra los esquemas, invariantes y el resultado esperado de
  cada evento; repite el lote (R5) y prueba eventos rotos (R8). **66 de 66 correctos.**
- `uv run ruff check . && uv run mypy orquestador run.py tests scripts`.

`CLAUDE.md` recoge las instrucciones que seguí con el asistente de programación.
