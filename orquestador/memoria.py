"""Memoria de un lead entre eventos: lo que R4, R7, N1, N2 y N4 obligan a recordar.

Vive en el Store de LangGraph (un documento por lead) y cada proceso la lee y la reescribe.
"""

from pydantic import AwareDatetime, BaseModel, ConfigDict

from orquestador.catalogo import CanalRecordatorio


class RecordatorioPendiente(BaseModel):
    model_config = ConfigDict(frozen=True)

    reminder_id: str
    canal: CanalRecordatorio
    # Cuándo sale. Pasado ese instante ya se envió y no hay nada que cancelar (R7).
    cuando: AwareDatetime


class MemoriaLead(BaseModel):
    # extra="forbid": un campo mal escrito al actualizarla falla en vez de perderse en silencio.
    model_config = ConfigDict(frozen=True, extra="forbid")

    intentos: int = 0
    llamadas_cortadas: int = 0
    no_contactar: bool = False
    rechaza_whatsapp: bool = False
    # El canal de respaldo ya se resolvió: salió el WhatsApp o se derivó a una persona (tarea).
    respaldo_resuelto: bool = False
    recordatorios_pendientes: tuple[RecordatorioPendiente, ...] = ()
