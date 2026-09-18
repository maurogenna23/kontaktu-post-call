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

**Requiere acceso a `gpt-5.6-luna`.** Sin él, cada conversación sale con 1 y sin salida (se reprocesa al
corregirlo). La alternativa es `MODELO=gpt-4o-mini` en `.env`, a costa de aciertos: 77 de 90 etiquetas
(confunde `cortada` con `visita_sin_confirmar`) y 21 de 27 fechas de callback (ver la tabla del modelo).

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

`scripts/comparar_modelos.py`: 30 conversaciones de los lotes de verificación, 3 veces cada una; en los
callbacks compara la fecha y la hora por separado.

| Modelo | Etiquetas | Fechas | Horas | Estables | Latencia | USD / 1000 llamadas |
|---|---|---|---|---|---|---|
| gpt-4o-mini | 77/90 | 21/27 | 27/27 | 29/30 | 2,4 s | 0,30 |
| **gpt-5.6-luna** (razonamiento `low`) | **90/90** | **27/27** | 27/27 | **30/30** | 2,4 s | 0,51 |

gpt-4o-mini confunde siempre `cortada` con `visita_sin_confirmar`, falla el día de la semana («el
lunes» pedido un viernes le da domingo) y, una de tres veces, da `documentacion_enviada` a quien rechazó
WhatsApp: le programaría uno (N1). Luna acierta todo por 0,21 USD más cada mil llamadas, a igual latencia.

## Decisiones donde la especificación deja margen

Cada una lleva un comentario `Decisión:` en el código.

- **Ocupado** a los 60 minutos (mitad de 30–90), como el ejemplo resuelto; si ahí la ventana está
  cerrada, el primer instante válido desde los 30 (19:10 → 19:40). **Cortada y visita sin confirmar** a
  los 30, o en la primera franja válida si la ventana cerró; la visita no se reserva (N5).
- **Callback** a lo que pidió el lead, que interpreta el modelo porque es lenguaje libre. Día sin hora: la
  apertura de la ventana ese día; franja: su comienzo (tarde, 16:00); hora sin fecha o ya pasada: la
  próxima vez que llega. Si cae en otra hora, u otro día si no dio hora, `aviso_cambio_hora`.
- **Intentos:** el máximo es por lead y N3 no hace excepciones: agotado, tampoco se programa un
  callback (el pedido queda en el CRM: `cerrar_llamada` con `callback_requested`). El respaldo se
  resuelve una vez por lead: WhatsApp o, si lo rechazó, una tarea para decidir cómo seguir.
- **Recordatorios:** el WhatsApp al lead sale a las 48 horas exactas, aunque caiga en domingo: el
  OpenAPI exige la ventana solo a `programar_llamada`. Al responder el lead solo se cancelan los que
  aún no salieron.
- **Baja:** manda sobre cualquier etiqueta y no lleva ninguna otra orden (caso 10), tampoco cancelar
  recordatorios: el contrato solo cancela cuando el lead responde (R7), y siguen guardados para eso.
  Después, cada llamada del lead solo se cierra.
- **La cita del CRM manda sobre el modelo**, salvo una baja; sin cita, una visita nunca está reservada.
- **`nota_contexto`:** la del modelo; si no deja ninguna (o la deja en blanco), las notas del agente con
  el prefijo «Notas del agente:», todas las claves y sin traducir, para no perder las desconocidas.
- **Descolgó y no habló:** `otro`. **Plazos** en horas: tiempo absoluto; en días: misma hora de Madrid.
- **Confianza:** 0,97 SIP, 0,9 detector de buzón, 0,7 heurística; en conversaciones, la del modelo.
- **Ids:** `orden_id` con la fórmula del ejemplo resuelto; `reminder_id` estable y persistido.
- **Idioma del WhatsApp:** el de `lead.language` (`es` si falta o viene vacío). Los parámetros que
  armamos siguen en español, y el recordatorio al lead no lleva idioma en el OpenAPI: lo decide el CRM.

## Qué dejé fuera

- **Atomicidad:** `emitir` escribe los ficheros, después la memoria y al final el checkpoint, sin una
  transacción común. Si el proceso muere entre medio, la reentrega vuelve a pasar por el LLM y puede
  decidir distinto. Tras los ficheros: las órdenes con la misma clave conservan el cuerpo viejo, la
  decisión nueva puede mostrar otra etiqueta y las operaciones nuevas se suman a las viejas; sin
  reentrega, la memoria no registra el evento. Tras la memoria: la reentrega aplica el evento dos veces
  (otro intento, otra cortada), aunque los `reminder_id` no se duplican. Pide un outbox.
- **`nota_contexto` entre llamadas:** no se guarda en la memoria del lead. Si a una cortada le sigue una
  llamada sin respuesta, la siguiente lleva «no se llegó a hablar con el lead» y pierde lo recogido.
- **Eventos del mismo lead en paralelo:** el contrato es un proceso por evento, en orden.
- **Timeout por nodo:** en LangGraph exige nodos asíncronos; el timeout está en el cliente de OpenAI.
- **LangSmith** está apagado por defecto: activarlo (`.env.example`) añade red hacia LangSmith.

## Cómo lo verifiqué

- `uv run pytest` (65): reglas, calendario, el ejemplo resuelto campo por campo y el grafo completo con
  un clasificador falso: nodos recorridos, reentregas y cada camino de fallo del modelo.
- `uv run python scripts/verificar_lote.py`: el lote de ejemplo y 50 eventos sintéticos, desde cero y
  un proceso por evento. Incluye los casos sin ejemplo, otras horas y días (domingo, 19:45, cambio de
  hora) y memoria entre eventos. Valida contra los esquemas, invariantes y el resultado esperado de
  cada evento; repite el lote (R5) y prueba eventos rotos (R8). **66 de 66 correctos.**
- `uv run ruff check . && uv run mypy orquestador run.py tests scripts`.

`CLAUDE.md` recoge las instrucciones que seguí con el asistente de programación, y
`asistente/prompts.md`, los prompts que le envié durante el reto.
