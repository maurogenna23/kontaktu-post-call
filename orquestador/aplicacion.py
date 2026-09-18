"""Un proceso, un evento: carga la entrada, abre la persistencia e invoca el grafo."""

import os
import sqlite3
from contextlib import closing
from functools import cache
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.sqlite import SqliteStore

from orquestador import catalogo, evento, memoria, ordenes, salida
from orquestador.clasificador import Clasificador, ClasificadorLLM, SalidaModelo
from orquestador.config import Campana, cargar_campana
from orquestador.evento import Evento, cargar_evento
from orquestador.grafo import Dependencias, construir_grafo
from orquestador.salida import LineaDecision, Salida

RAIZ = Path(__file__).resolve().parent.parent

# Decisión: gpt-5.6-luna si no se indica MODELO. Ver la comparación con gpt-4o-mini en el README.
MODELO_POR_DEFECTO = "gpt-5.6-luna"

# Modelos Pydantic que viajan en el estado y, por tanto, en el checkpoint. El serializador de
# LangGraph solo reconstruye los tipos registrados; sin esto avisa y en el futuro los bloqueará.
_MODELOS_EN_ESTADO = (
    evento.Evento, evento.Campania, evento.Lead, evento.Amd, evento.Telefonia, evento.Turno,
    evento.Cita, evento.NotasAgente, evento.Mensaje, catalogo.Clasificacion,
    catalogo.DatosConversacion, ordenes.Orden, memoria.MemoriaLead, memoria.RecordatorioPendiente,
    salida.LineaDecision,
)  # fmt: skip


def procesar(ruta_evento: Path, raiz: Path = RAIZ) -> LineaDecision:
    entrada = cargar_evento(ruta_evento)
    campana = cargar_campana(raiz / "config" / "campana.yaml")
    # salida/ y estado/ van en la raíz del repo. ORQUESTADOR_DATOS permite llevarlos a otra carpeta
    # (lo usa scripts/verificar_lote.py para correr cada lote desde cero sin tocar salida/).
    datos = Path(os.getenv("ORQUESTADOR_DATOS") or raiz)
    dependencias = Dependencias(
        campana=campana,
        clasificador=_clasificador_perezoso(os.getenv("MODELO") or MODELO_POR_DEFECTO, campana),
        salida=Salida(datos / "salida"),
    )
    (datos / "estado").mkdir(parents=True, exist_ok=True)
    base = str(datos / "estado" / "orquestador.sqlite")
    serializador = JsonPlusSerializer(
        allowed_msgpack_modules=[(modelo.__module__, modelo.__name__) for modelo in _MODELOS_EN_ESTADO]
    )
    with (
        closing(sqlite3.connect(base, check_same_thread=False)) as conexion,
        SqliteStore.from_conn_string(base) as store,
    ):
        store.setup()
        grafo = construir_grafo(SqliteSaver(conexion, serde=serializador), store)
        final = grafo.invoke(
            {"evento": entrada},
            {"configurable": {"thread_id": entrada.idempotency_key}},
            context=dependencias,
            # Decisión: cada checkpoint se escribe antes de seguir. El proceso vive un evento y no
            # debe terminar con escrituras pendientes en segundo plano.
            durability="sync",
        )
    decision: LineaDecision = final["decision"]
    return decision


def _clasificador_perezoso(modelo: str, campana: Campana) -> Clasificador:
    """El cliente del LLM se crea al usarlo: un evento que resuelve la telefonía no necesita clave."""

    @cache
    def cliente() -> ClasificadorLLM:
        return ClasificadorLLM(modelo, campana)

    def clasificar(entrada: Evento) -> SalidaModelo:
        return cliente()(entrada)

    return clasificar
