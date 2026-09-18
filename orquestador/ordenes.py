"""Órdenes al CRM (esquemas/crm-openapi.yaml): cuerpos tipados, claves de idempotencia e ids."""

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from orquestador.catalogo import CanalRecordatorio, EstadoCola, EtiquetaLlamada, Plantilla, TipoTarea

Operacion = Literal[
    "cerrar_llamada",
    "programar_llamada",
    "enviar_plantilla_whatsapp",
    "programar_recordatorio",
    "cancelar_recordatorio",
    "crear_tarea",
    "marcar_no_contactar",
]


class CuerpoOrden(BaseModel):
    """Cuerpo de la petición. Los parámetros de ruta (entry_id, reminder_id) van dentro."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class CerrarLlamada(CuerpoOrden):
    entry_id: str
    status: EstadoCola
    etiqueta: EtiquetaLlamada
    motivo: str
    confianza: float
    duration_seconds: int


class ProgramarLlamada(CuerpoOrden):
    entry_id: str
    telefono: str
    no_antes_de: str
    motivo: str
    nota_contexto: str | None = None


class EnviarPlantillaWhatsapp(CuerpoOrden):
    organization_id: str
    telefono: str
    plantilla: Plantilla
    parametros: dict[str, str]
    idioma: str = "es"


class ProgramarRecordatorio(CuerpoOrden):
    contact_id: str
    canal: CanalRecordatorio
    plantilla: Plantilla | None = None
    tipo_tarea: TipoTarea | None = None
    cuando: str
    cancelar_si: Literal["lead_responde", "ninguna"] = "ninguna"


class CancelarRecordatorio(CuerpoOrden):
    reminder_id: str
    motivo: str


class CrearTarea(CuerpoOrden):
    contact_id: str
    call_id: str | None
    tipo: TipoTarea
    titulo: str
    detalle: str
    vence_el: str
    asignada_a: Literal["comercial_asignado", "cualquiera"] = "comercial_asignado"


class MarcarNoContactar(CuerpoOrden):
    telefono: str
    contact_id: str | None
    canal: Literal["todos", "voz", "whatsapp"]
    motivo: str
    origen: str


class Orden(BaseModel):
    """Una línea de salida/ordenes.jsonl: la petición que se habría hecho al CRM."""

    model_config = ConfigDict(frozen=True)

    orden_id: str
    event_id: str
    operacion: Operacion
    idempotency_key: str
    cuerpo: dict[str, Any]


def huella(texto: str) -> str:
    # Decisión: 8 caracteres del SHA-1, la misma fórmula que produce los orden_id del ejemplo
    # resuelto. Es estable entre ejecuciones: el mismo hecho produce siempre el mismo id.
    return hashlib.sha1(texto.encode("utf-8")).hexdigest()[:8]


def crear_orden(
    event_id: str, clave_hecho: str, operacion: Operacion, cuerpo: CuerpoOrden, distintivo: str | None = None
) -> Orden:
    """La clave es <idempotency_key del evento>:<operacion>[:<distintivo>], como recomienda el OpenAPI."""
    clave = f"{clave_hecho}:{operacion}" + (f":{distintivo}" if distintivo else "")
    return Orden(
        orden_id=f"ord_{huella(clave)}",
        event_id=event_id,
        operacion=operacion,
        idempotency_key=clave,
        cuerpo=cuerpo.model_dump(mode="json", exclude_none=True),
    )
