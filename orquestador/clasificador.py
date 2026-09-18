"""Clasificación de la conversación con el LLM (solo cuando atendió una persona).

El modelo devuelve la etiqueta y los datos en una salida estructurada; las fechas las resuelve a
partir del instante de referencia, pero el cálculo de plazos y ventanas lo hace el código.
"""

import json
from collections.abc import Callable
from datetime import date, time
from pathlib import Path
from typing import Literal, TypeVar

import openai
from langchain.chat_models import init_chat_model
from pydantic import BaseModel, ConfigDict

from orquestador.calendario import describir, en_zona
from orquestador.catalogo import Clasificacion, DatosConversacion
from orquestador.config import Campana
from orquestador.evento import Evento, Turno

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

# Errores transitorios de la API: los reintenta la RetryPolicy del nodo. Una clave inválida o una
# petición mal formada no se reintenta: falla a la primera y va al error_handler.
ERRORES_TRANSITORIOS: tuple[type[Exception], ...] = (
    openai.APIConnectionError,  # incluye APITimeoutError
    openai.RateLimitError,
    openai.InternalServerError,
)

EtiquetaConversacion = Literal[
    "visita_reservada",
    "visita_sin_confirmar",
    "documentacion_enviada",
    "documentacion_pendiente",
    "callback",
    "cortada",
    "persona_equivocada",
    "no_contactar",
    "descartado",
    "buzon",
    "otro",
]


class SalidaModelo(BaseModel):
    """Salida estructurada del LLM. Todos los campos obligatorios: el esquema estricto lo exige."""

    model_config = ConfigDict(frozen=True)

    etiqueta: EtiquetaConversacion
    motivo: str
    confianza: float
    evidencia: str
    callback_fecha: str | None
    callback_hora: str | None
    rechaza_whatsapp: bool
    email: str | None
    nota_contexto: str | None


Clasificador = Callable[[Evento], SalidaModelo]
_T = TypeVar("_T")


class ClasificadorLLM:
    """Llama al modelo de OpenAI configurado en MODELO con los prompts de prompts/."""

    def __init__(self, modelo: str, campana: Campana) -> None:
        # Sin reintentos en el cliente: los reintentos los decide la RetryPolicy del nodo.
        if modelo.startswith("gpt-5"):  # modelos de razonamiento
            llm = init_chat_model(f"openai:{modelo}", timeout=30, max_retries=0, reasoning_effort="low")
        else:
            llm = init_chat_model(f"openai:{modelo}", timeout=30, max_retries=0, temperature=0)
        self._modelo = llm.with_structured_output(SalidaModelo)
        self._campana = campana
        self._sistema = (PROMPTS / "clasificar_llamada.md").read_text(encoding="utf-8")
        self._usuario = (PROMPTS / "clasificar_llamada_usuario.md").read_text(encoding="utf-8")

    def __call__(self, evento: Evento) -> SalidaModelo:
        mensajes = [("system", self._sistema), ("user", self._mensaje_usuario(evento))]
        salida = self._modelo.invoke(mensajes, config={"run_name": "clasificar_conversacion"})
        if not isinstance(salida, SalidaModelo):
            raise TypeError(f"salida inesperada del modelo: {type(salida).__name__}")
        return salida

    def _mensaje_usuario(self, evento: Evento) -> str:
        campana, notas = self._campana, evento.agent_outcome
        referencia = en_zona(evento.occurred_at, campana)
        telefonia = evento.telephony
        cita = f"sí, {describir(notas.appointment.start_time, campana)}" if notas.appointment else "no"
        senalizacion = (
            f"duró {telefonia.duration_seconds} s, colgó {telefonia.hung_up_by or 'no se sabe'}"
            if telefonia
            else "sin telefonía"
        )
        return self._usuario.format(
            referencia=f"{describir(referencia, campana)} ({referencia:%Y-%m-%d}, Europe/Madrid)",
            inmueble=evento.lead.property_address or evento.lead.property_ref or "desconocido",
            cita=cita,
            notas=json.dumps(notas.slots_snapshot, ensure_ascii=False) if notas.slots_snapshot else "ninguna",
            senalizacion=senalizacion,
            transcripcion="\n".join(_linea(turno) for turno in evento.transcript),
        )


def _linea(turno: Turno) -> str:
    quien = "AGENTE" if turno.role == "agent" else "LEAD"
    return f"[{turno.time_in_call_secs}s] {quien}: {turno.message}"


def interpretar(salida: SalidaModelo, evento: Evento) -> tuple[Clasificacion, DatosConversacion]:
    """Contrasta lo que dice el modelo con los hechos del evento y lo pasa al dominio."""
    etiqueta = salida.etiqueta
    hay_cita = evento.agent_outcome.appointment is not None
    # Decisión: la cita creada es un hecho del CRM y manda sobre el modelo (caso 1), salvo una baja,
    # que manda sobre todo. Sin cita, una visita nunca está reservada: es visita_sin_confirmar (N5).
    if hay_cita and etiqueta != "no_contactar":
        etiqueta = "visita_reservada"
    elif not hay_cita and etiqueta == "visita_reservada":
        etiqueta = "visita_sin_confirmar"
    clasificacion = Clasificacion(
        etiqueta=etiqueta, motivo=salida.motivo, confianza=min(max(salida.confianza, 0.0), 1.0)
    )
    datos = DatosConversacion(
        callback_fecha=_o_none(date.fromisoformat, salida.callback_fecha),
        callback_hora=_o_none(time.fromisoformat, salida.callback_hora),
        rechaza_whatsapp=salida.rechaza_whatsapp,
        email=salida.email,
        nota_contexto=salida.nota_contexto,
    )
    return clasificacion, datos


def _o_none(convertir: Callable[[str], _T], texto: str | None) -> _T | None:
    """Un dato mal formado del modelo se descarta (queda None) en vez de tumbar el evento."""
    if not texto:
        return None
    try:
        return convertir(texto)
    except ValueError:
        return None
