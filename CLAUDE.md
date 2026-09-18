# Kontaktu — Orquestador post-llamada

Reto técnico: un programa que recibe **un evento** de fin de llamada (o un WhatsApp entrante) y emite
**una decisión** y **cero o más órdenes** al CRM. Python + LangGraph. La especificación manda:
`enunciado.md`, `casos.md`, `esquemas/` y `config/campana.yaml`. Lo evalúan con eventos que no tenemos,
así que se implementa lo que dice la especificación, no lo que pasa con los 16 eventos de ejemplo.

## Contrato
- `python run.py <evento.json>`: un proceso por evento. Añade líneas a `salida/decisiones.jsonl` y
  `salida/ordenes.jsonl`. Sale con 0 si procesó el evento y con otro código si no pudo.
- Nada queda en memoria entre eventos: lo que haga falta recordar se persiste en `estado/` (SQLite).
- No se modifican `eventos/`, `config/` ni `esquemas/`.

## Arquitectura
- `orquestador/` es el paquete; `run.py` solo lee el argumento, arma las dependencias e invoca el grafo.
- Las reglas de negocio (calendario, ventana, órdenes por etiqueta, señalización) son **funciones
  puras** sin importar LangGraph. Los nodos del grafo son finos: leen el estado, llaman al dominio y
  devuelven solo lo que cambia.
- Estado del grafo: `TypedDict`. Pydantic solo en los bordes: evento de entrada, salida del LLM,
  órdenes y memoria del lead.
- Persistencia de LangGraph, cada pieza para lo suyo:
  - **Checkpointer** (`SqliteSaver`) con `thread_id = idempotency_key`: cada hecho es un thread. Una
    reentrega cae en el mismo thread, ya procesado, y repite la etiqueta sin órdenes.
  - **Store** (`SqliteStore`) por lead: intentos, recordatorios pendientes, baja, WhatsApp rechazado,
    llamadas cortadas. Es la memoria que comparten todos los eventos de un lead.
- Los modelos Pydantic que se guardan en el checkpoint se registran en el serializador
  (`allowed_msgpack_modules`); si aparece el aviso "Deserializing unregistered type", falta uno.
- El LLM solo clasifica conversaciones con una persona. Lo que resuelve la señalización (SIP, `amd`)
  se decide en código. El modelo solo interpreta la hora que pide el lead (callback); plazos, ventana,
  días hábiles y zona horaria los calcula el código.
- Los prompts viven solo en `prompts/`, como archivos. El modelo sale de `MODELO` en `.env`.
- `orden_id` = `ord_` + 8 primeros caracteres del SHA-1 de su `idempotency_key` (así lo hace el
  ejemplo resuelto).

## Reglas de trabajo
- Nunca leer ni mostrar `.env`.
- Nombres en castellano, como los esquemas. Sin valores mágicos: umbrales y plazos salen de
  `campana.yaml`; catálogos cerrados como `Literal`.
- Cada decisión donde la especificación deja margen lleva un comentario `Decisión:` y va al README.
- Ningún `except` que se trague errores: todo fallo va a un camino definido (reintento,
  `error_handler` que etiqueta `otro`, o código de salida distinto de 0).
- LangSmith es opcional (variables `LANGSMITH_*` en `.env`, región EU). El sistema funciona sin él.
- Antes de tocar APIs de LangGraph o LangChain, comprobar la firma en la versión instalada.

## Comandos
- `uv sync` · `uv run python run.py eventos/01-call-ended-nuria.json`
- `uv run python scripts/verificar_lote.py`: corre desde cero el lote de ejemplo y el sintético y
  comprueba esquemas, invariantes, resultados esperados, R5 y R8. Si cambia una regla, su resultado
  esperado se actualiza ahí leyendo `casos.md`, no copiando lo que salga.
- `uv run python scripts/comparar_modelos.py`: aciertos, estabilidad y coste de cada modelo.
- `uv run pytest` · `uv run ruff check . && uv run ruff format --check .` · `uv run mypy orquestador run.py tests scripts`

## Antes de dar algo por terminado
`uv run ruff check . && uv run ruff format --check . && uv run mypy orquestador run.py tests scripts && uv run pytest && uv run python scripts/verificar_lote.py`
