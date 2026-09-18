"""Memoria de un lead entre eventos: lo que R4, R7, N1, N2 y N4 obligan a recordar.

Vive en el Store de LangGraph (un documento por lead) y cada proceso la lee y la reescribe.
"""

from pydantic import BaseModel, ConfigDict

from orquestador.catalogo import CanalRecordatorio


class RecordatorioPendiente(BaseModel):
    model_config = ConfigDict(frozen=True)

    reminder_id: str
    canal: CanalRecordatorio


class MemoriaLead(BaseModel):
    model_config = ConfigDict(frozen=True)

    intentos: int = 0
    llamadas_cortadas: int = 0
    no_contactar: bool = False
    rechaza_whatsapp: bool = False
    respaldo_enviado: bool = False
    recordatorios_pendientes: tuple[RecordatorioPendiente, ...] = ()
