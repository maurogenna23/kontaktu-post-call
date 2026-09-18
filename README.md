# Orquestador post-llamada

Recibe **un evento** (fin de llamada o WhatsApp entrante), clasifica cómo fue la llamada y emite
**una decisión** y **cero o más órdenes** al CRM. Python 3.11 o superior + LangGraph 1.2.

## Cómo se ejecuta

```bash
uv sync                          # o: python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env             # OPENAI_API_KEY y MODELO (vacío = gpt-5.6-luna)
uv run python run.py eventos/01-call-ended-nuria.json

# El lote completo, desde cero y en orden:
rm -rf salida estado && grep -v '^#' eventos/orden.txt | xargs -I{} uv run python run.py eventos/{}
```

Escribe en `salida/decisiones.jsonl` y `salida/ordenes.jsonl` (se pueden abrir con `visor/index.html`).
Lo que hay que recordar entre procesos va a `estado/orquestador.sqlite`. Código de salida 0 si procesó
el evento; 1 si no pudo (evento ilegible o inválido), sin dejar nada a medias.

## Cómo funciona

```
admitir ─┬─ otra organización o reentrega ─────────────────────────────────► emitir
         ├─ message.received ──► atender_mensaje ──────────────────────────► emitir
         └─ call.ended ──► clasificar_senalizacion ─┬─────────────► decidir ─► emitir
                                                    └─► clasificar_conversacion (LLM) ─┘
```

- **La telefonía decide lo que puede** (486, 603, 408/480, 5xx, buzón, IVR) sin llamar al modelo. El LLM
  solo lee conversaciones con una persona, con salida estructurada estricta
  (`orquestador/clasificador.py`, prompts en `prompts/`). Las fechas y plazos los calcula el código.
- **Las reglas de negocio son funciones puras** (`reglas.py`, `calendario.py`, `senalizacion.py`), sin
  LangGraph, y los nodos solo las llaman. Solo `emitir` tiene efectos.
- **Persistencia de LangGraph, cada pieza para lo suyo:** un **checkpointer** SQLite con
  `thread_id = idempotency_key`, así cada hecho es un thread y una reentrega cae en uno que ya tiene
  decisión (R5); y un **Store** SQLite con un documento por lead: intentos, recordatorios pendientes,
  baja, WhatsApp rechazado y llamadas cortadas (R4, R7, N1, N2, N4).
- **Fallos:** el nodo del LLM tiene `RetryPolicy` solo para errores transitorios de OpenAI y un
  `error_handler` que deja el evento como `otro` (revisión humana, N4) para que se procese igual (R8).
  Como última barrera, `emitir` no reescribe una orden cuya clave ya está en `ordenes.jsonl`.

## Modelo: gpt-5.6-luna

`scripts/comparar_modelos.py` clasifica las 17 conversaciones de los dos lotes de verificación, 3 veces
cada una:

| Modelo | Aciertos | Estables | Latencia | USD / 1000 llamadas |
|---|---|---|---|---|
| gpt-4o-mini | 42/51 | 17/17 | 2,1 s | 0,28 |
| **gpt-5.6-luna** (razonamiento `low`) | **51/51** | 17/17 | 2,3 s | 0,48 |

gpt-4o-mini confunde siempre `cortada` con `visita_sin_confirmar`, uno de los pares que `casos.md`
avisa. Luna acierta todo por 0,2 USD más cada mil llamadas y es el modelo de coste bajo actual de OpenAI.

## Decisiones donde la especificación deja margen

Cada una lleva un comentario `Decisión:` en el código.

- **Ocupado:** a mitad del rango de 30 a 90 minutos (60), como el ejemplo resuelto.
- **Cortada y visita sin confirmar:** a los 30 minutos dentro de la ventana; si ya cerró, la primera
  franja válida aunque pase de 4 horas. La visita acordada de palabra no se reserva (N5).
- **Callback:** a la hora pedida; fuera de ventana, la primera franja válida más `aviso_cambio_hora`.
  Sin hora concreta, la separación general de 2 horas.
- **Intentos:** el máximo vale para cualquier nueva llamada. Agotado, WhatsApp de respaldo una sola vez;
  si el lead rechazó WhatsApp, tarea `revisar_llamada`.
- **Baja:** manda sobre cualquier etiqueta; después, cualquier evento del lead solo cierra la llamada.
- **La cita del CRM manda sobre el modelo:** con cita es `visita_reservada` (salvo baja); sin cita, una
  visita nunca está reservada. Si el modelo falla y hay cita, sigue siendo `visita_reservada`.
- **Descolgó una persona pero no habló:** `otro`. Un `uncertain` del detector se trata como persona.
- **Plazos:** en horas o minutos, tiempo absoluto; en días, la misma hora de Madrid.
- **Confianza:** 0,97 si la decide el código SIP, 0,9 el detector de buzón, 0,7 la heurística del
  saludo; en conversaciones, la que declara el modelo.
- **Ids:** `orden_id` con la fórmula del ejemplo resuelto (SHA-1 de la clave); `reminder_id` estable y
  persistido para cancelarlo desde otro proceso.

## Qué dejé fuera

- **Atomicidad entre la salida y SQLite.** Si el proceso muere después de escribir las órdenes y antes
  de guardar la memoria del lead, la reentrega no duplica órdenes, pero ese intento no quedaría
  contado. Resolverlo del todo pide una transacción común (o un outbox), fuera del alcance.
- **Eventos del mismo lead en paralelo.** El contrato es un proceso por evento, en orden; no hay
  bloqueos por lead.
- **Timeout por nodo** de LangGraph 1.2: exige nodos asíncronos. El timeout va en el cliente de OpenAI.
- **LangSmith:** opcional por variables de entorno (`.env.example`); el sistema no depende de él.

## Cómo lo verifiqué

- `uv run pytest`: 18 tests del dominio (ventana, días hábiles, reglas N1 a N5, recordatorios). Uno
  reproduce el ejemplo resuelto campo por campo.
- `uv run python scripts/verificar_lote.py`: corre, desde cero y un proceso por evento, el lote de
  ejemplo y un lote sintético de 20 eventos con los casos sin ejemplo (603, callback fuera de ventana,
  descartado, IVR, 5xx), bordes de la ventana y memoria entre eventos. Valida decisiones y cuerpos contra
  los esquemas, invariantes (llamadas en ventana, un `cerrar_llamada` por llamada nueva, nada tras
  reentregas, otras organizaciones ni bajas) y el resultado esperado de cada evento; repite el lote
  para R5 y prueba eventos rotos para R8. **36 de 36 correctos.**
- `uv run python scripts/comparar_modelos.py`: la tabla de arriba.
- `uv run ruff check . && uv run mypy orquestador run.py tests scripts`.

`CLAUDE.md` recoge las instrucciones que seguí con el asistente de programación.
