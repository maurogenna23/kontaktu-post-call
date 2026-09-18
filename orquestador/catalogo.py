"""Catálogos cerrados de casos.md y del OpenAPI del CRM, y el resultado de clasificar un evento."""

from datetime import date, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EtiquetaLlamada = Literal[
    "visita_reservada",
    "documentacion_enviada",
    "callback",
    "sin_respuesta",
    "ocupado",
    "buzon",
    "cortada",
    "visita_sin_confirmar",
    "persona_equivocada",
    "no_contactar",
    "rechazada",
    "documentacion_pendiente",
    "descartado",
    "otro",
]
Etiqueta = EtiquetaLlamada | Literal["no_aplica"]

EstadoCola = Literal[
    "successful",
    "completed",
    "callback_requested",
    "no_answer",
    "needs_review",
    "failed",
    "dnc",
    "refused",
    "skipped",
]

# Tabla de casos.md: el estado de cola lo fija la etiqueta, no se decide.
ESTADO_COLA: dict[EtiquetaLlamada, EstadoCola] = {
    "visita_reservada": "successful",
    "documentacion_enviada": "completed",
    "documentacion_pendiente": "completed",
    "callback": "callback_requested",
    "sin_respuesta": "no_answer",
    "ocupado": "no_answer",
    "buzon": "no_answer",
    "cortada": "needs_review",
    "visita_sin_confirmar": "needs_review",
    "otro": "needs_review",
    "persona_equivocada": "failed",
    "no_contactar": "dnc",
    "rechazada": "refused",
    "descartado": "skipped",
}

# Regla N4: una segunda llamada de este tipo con el mismo lead abre una tarea de revisión.
ETIQUETAS_CORTADA: frozenset[EtiquetaLlamada] = frozenset({"cortada", "visita_sin_confirmar"})

Plantilla = Literal["primer_toque_respaldo", "recordatorio_documentacion", "aviso_cambio_hora"]
TipoTarea = Literal[
    "confirmar_visita_direccion",
    "verificar_telefono",
    "enviar_documentacion_email",
    "llamar_a_mano",
    "revisar_llamada",
]
CanalRecordatorio = Literal["whatsapp_lead", "tarea_comercial"]


class Clasificacion(BaseModel):
    """Cómo fue el evento: la etiqueta, una frase de motivo y la confianza (0 a 1)."""

    model_config = ConfigDict(frozen=True)

    etiqueta: Etiqueta
    motivo: str
    confianza: float = Field(ge=0, le=1)


class DatosConversacion(BaseModel):
    """Lo que la conversación aporta a las órdenes, más allá de la etiqueta."""

    model_config = ConfigDict(frozen=True)

    callback_fecha: date | None = None
    callback_hora: time | None = None
    rechaza_whatsapp: bool = False
    email: str | None = None
    nota_contexto: str | None = None
