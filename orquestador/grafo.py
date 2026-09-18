"""El grafo de LangGraph: de un evento a una decisión y sus órdenes.

    START → admitir ─┬─ otra organización o reentrega ──────────────────────────────► emitir → END
                     ├─ message.received ──► atender_mensaje ─────────────────────────► emitir
                     └─ call.ended ──► clasificar_senalizacion ─┬─────────────► decidir ─► emitir
                                                                └─► clasificar_conversacion (LLM) ─┘

Los nodos son finos: leen el estado, llaman al dominio (senalizacion, clasificador, reglas) y
devuelven solo lo que cambia. Persistencia (se arma en aplicacion.py):
- Checkpointer con un thread por hecho, id_del_hecho(evento) = organización + idempotency_key. La
  decisión que queda guardada en ese thread es lo que permite reconocer una reentrega (R5).
- Store con un documento por lead: la memoria que comparten todos sus eventos (R4, R7, N1, N2, N4).
"""

from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.errors import NodeError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore
from langgraph.types import Checkpointer, Command, RetryPolicy
from pydantic import BaseModel

from orquestador.catalogo import Clasificacion, DatosConversacion, Etiqueta
from orquestador.clasificador import (
    Clasificador,
    es_error_de_configuracion,
    es_error_transitorio,
    interpretar,
)
from orquestador.config import Campana
from orquestador.evento import Amd, Campania, Cita, Evento, Lead, Mensaje, NotasAgente, Telefonia, Turno
from orquestador.memoria import MemoriaLead, RecordatorioPendiente
from orquestador.ordenes import Orden
from orquestador.reglas import decidir_llamada, decidir_mensaje
from orquestador.salida import LineaDecision, Salida
from orquestador.senalizacion import clasificar_por_senalizacion


class Estado(TypedDict, total=False):
    evento: Evento
    clasificacion: Clasificacion
    datos: DatosConversacion
    ordenes: list[Orden]
    # La memoria del lead tras este evento; None si el evento no la cambia.
    memoria: MemoriaLead | None
    # Lo último que escribe el grafo. Queda en el checkpoint del hecho y marca que ya se procesó.
    decision: LineaDecision


# Los modelos Pydantic que viajan en el estado y, por tanto, en el checkpoint. Con la lista explícita,
# el serializador bloquea cualquier otro tipo: vuelve como dict y LangGraph registra «Blocked
# deserialization of…». tests/test_grafo.py comprueba que coincide con los modelos que alcanza Estado.
MODELOS_EN_ESTADO: tuple[type[BaseModel], ...] = (
    Evento, Campania, Lead, Amd, Telefonia, Turno, Cita, NotasAgente, Mensaje, Clasificacion,
    DatosConversacion, Orden, MemoriaLead, RecordatorioPendiente, LineaDecision,
)  # fmt: skip


def serializador() -> JsonPlusSerializer:
    """El serializador del checkpoint, con los modelos del estado registrados."""
    return JsonPlusSerializer(
        allowed_msgpack_modules=[(modelo.__module__, modelo.__name__) for modelo in MODELOS_EN_ESTADO]
    )


@dataclass(frozen=True)
class Dependencias:
    """Lo que el grafo recibe de fuera en cada invocación (runtime.context)."""

    campana: Campana
    clasificador: Clasificador
    salida: Salida


def admitir(
    estado: Estado, *, runtime: Runtime[Dependencias]
) -> Command[Literal["emitir", "atender_mensaje", "clasificar_senalizacion"]]:
    evento = estado["evento"]
    organizacion = runtime.context.campana.campana.organization_id
    if evento.organization_id != organizacion:
        # R6: ninguna orden, ni siquiera cerrar_llamada. El evento deja su línea con no_aplica.
        motivo = f"evento de otra organización ({evento.organization_id})"
        return Command(goto="emitir", update=_sin_ordenes("no_aplica", motivo, 1.0))
    anterior = estado.get("decision")
    if anterior is not None:
        # R5: este hecho ya tiene decisión en su thread. Se repite la etiqueta y no sale ninguna orden.
        motivo = f"reentrega de {evento.idempotency_key}, ya procesada: no se emiten órdenes"
        return Command(goto="emitir", update=_sin_ordenes(anterior.etiqueta, motivo, anterior.confianza))
    if evento.type == "message.received":
        return Command(goto="atender_mensaje")
    return Command(goto="clasificar_senalizacion")


def atender_mensaje(estado: Estado, *, runtime: Runtime[Dependencias]) -> dict[str, Any]:
    evento = estado["evento"]
    plan = decidir_mensaje(evento, _leer_memoria(runtime, evento), runtime.context.campana)
    motivo = f"el lead respondió por WhatsApp: se cancelan {len(plan.ordenes)} recordatorios pendientes"
    return {
        "clasificacion": Clasificacion(etiqueta="no_aplica", motivo=motivo, confianza=1.0),
        "ordenes": plan.ordenes,
        "memoria": plan.memoria,
    }


def clasificar_senalizacion(
    estado: Estado, *, runtime: Runtime[Dependencias]
) -> Command[Literal["decidir", "clasificar_conversacion"]]:
    evento = estado["evento"]
    if evento.telephony is None:
        raise ValueError("un call.ended sin telephony no pasa la validación del evento")
    clasificacion = clasificar_por_senalizacion(evento.telephony, bool(evento.transcript))
    if clasificacion is None:
        return Command(goto="clasificar_conversacion")
    # Lo decide la telefonía: no se llama al modelo (ni coste ni latencia).
    return Command(goto="decidir", update={"clasificacion": clasificacion, "datos": DatosConversacion()})


def clasificar_conversacion(estado: Estado, *, runtime: Runtime[Dependencias]) -> dict[str, Any]:
    evento = estado["evento"]
    clasificacion, datos = interpretar(runtime.context.clasificador(evento), evento, runtime.context.campana)
    return {"clasificacion": clasificacion, "datos": datos}


def clasificacion_fallida(estado: Estado, error: NodeError) -> Command[Literal["decidir"]]:
    """R8: si el modelo sigue fallando tras los reintentos, el evento se procesa igual.

    Queda como `otro` (N4: una persona lo revisa). Si había cita creada, es un hecho del CRM y la
    etiqueta es visita_reservada aunque el modelo no haya respondido.

    Un error de configuración (clave, modelo, saldo) no es un fallo de este evento: con él no se
    clasificaría ninguno. Se relanza para que el proceso salga con error sin escribir nada y el
    evento se pueda reprocesar al corregir la configuración, en vez de llenar el CRM de revisiones.
    """
    if es_error_de_configuracion(error.error):
        raise error.error
    hay_cita = estado["evento"].agent_outcome.appointment is not None
    motivo = f"no se pudo clasificar la conversación ({type(error.error).__name__})"
    clasificacion = Clasificacion(
        etiqueta="visita_reservada" if hay_cita else "otro",
        motivo=f"cita creada en el CRM; {motivo}" if hay_cita else motivo,
        confianza=0.5 if hay_cita else 0.0,
    )
    return Command(goto="decidir", update={"clasificacion": clasificacion, "datos": DatosConversacion()})


def decidir(estado: Estado, *, runtime: Runtime[Dependencias]) -> dict[str, Any]:
    evento = estado["evento"]
    plan = decidir_llamada(
        evento,
        estado["clasificacion"],
        estado.get("datos") or DatosConversacion(),
        _leer_memoria(runtime, evento),
        runtime.context.campana,
    )
    return {"ordenes": plan.ordenes, "memoria": plan.memoria}


def emitir(estado: Estado, *, runtime: Runtime[Dependencias]) -> dict[str, Any]:
    """Único nodo con efectos: escribe la salida y guarda la memoria del lead."""
    evento, clasificacion, ordenes = estado["evento"], estado["clasificacion"], estado["ordenes"]
    decision = LineaDecision(
        event_id=evento.event_id,
        call_id=evento.call_id,
        etiqueta=clasificacion.etiqueta,
        motivo=clasificacion.motivo,
        confianza=clasificacion.confianza,
        ordenes=[orden.orden_id for orden in ordenes],
    )
    runtime.context.salida.escribir(ordenes, decision)
    memoria = estado.get("memoria")
    if memoria is not None:
        _store(runtime).put(*_clave_lead(evento), memoria.model_dump(mode="json"))
    return {"decision": decision}


def construir_grafo(
    checkpointer: Checkpointer, store: BaseStore
) -> CompiledStateGraph[Estado, Dependencias, Estado, Estado]:
    grafo = StateGraph(Estado, context_schema=Dependencias)
    grafo.add_node("admitir", admitir)
    grafo.add_node("atender_mensaje", atender_mensaje)
    grafo.add_node("clasificar_senalizacion", clasificar_senalizacion)
    grafo.add_node(
        "clasificar_conversacion",
        clasificar_conversacion,
        retry_policy=RetryPolicy(max_attempts=3, retry_on=es_error_transitorio),
        error_handler=clasificacion_fallida,
    )
    grafo.add_node("decidir", decidir)
    grafo.add_node("emitir", emitir)
    grafo.add_edge(START, "admitir")
    grafo.add_edge("atender_mensaje", "emitir")
    grafo.add_edge("clasificar_conversacion", "decidir")
    grafo.add_edge("decidir", "emitir")
    grafo.add_edge("emitir", END)
    return grafo.compile(checkpointer=checkpointer, store=store)


def _sin_ordenes(etiqueta: Etiqueta, motivo: str, confianza: float) -> dict[str, Any]:
    clasificacion = Clasificacion(etiqueta=etiqueta, motivo=motivo, confianza=confianza)
    return {"clasificacion": clasificacion, "ordenes": [], "memoria": None}


def id_del_hecho(evento: Evento) -> str:
    """El thread del checkpointer para este evento: cada hecho es un thread.

    Decisión: la especificación dice que idempotency_key identifica el hecho; lo acotamos por
    organización, como la memoria del lead. Con la clave sola, un evento de otra organización con la
    misma clave compartiría el thread: si llega antes, el nuestro se toma por reentrega; si llega
    después, pisa la decisión que una reentrega del nuestro tiene que repetir.
    """
    return f"{evento.organization_id}:{evento.idempotency_key}"


def _clave_lead(evento: Evento) -> tuple[tuple[str, ...], str]:
    return ("leads", evento.organization_id), evento.lead.contact_id


def _leer_memoria(runtime: Runtime[Dependencias], evento: Evento) -> MemoriaLead:
    documento = _store(runtime).get(*_clave_lead(evento))
    return MemoriaLead.model_validate(documento.value) if documento else MemoriaLead()


def _store(runtime: Runtime[Dependencias]) -> BaseStore:
    if runtime.store is None:
        raise RuntimeError("el grafo se compila con un store: sin él no hay memoria del lead")
    return runtime.store
