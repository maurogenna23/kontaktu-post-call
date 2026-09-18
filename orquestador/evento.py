"""Evento de entrada (esquemas/evento.schema.json), validado en el borde del sistema."""

import json
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class _Entrada(BaseModel):
    # Decisión: los campos que no conocemos se ignoran. Un campo nuevo en el evento no debe
    # impedir procesarlo; los que sí usamos se validan con su tipo.
    model_config = ConfigDict(extra="ignore", frozen=True)


class Campania(_Entrada):
    system_key: str
    entry_id: str


class Lead(_Entrada):
    contact_id: str
    phone: str
    full_name: str | None = None
    property_ref: str | None = None
    property_address: str | None = None


class Amd(_Entrada):
    result: Literal["human", "machine-vm", "machine-ivr", "machine-unavailable", "uncertain", "not_run"] = (
        "not_run"
    )
    greeting_transcript: str | None = None
    source: Literal["livekit_amd", "heuristic_regex", "none"] = "none"


class Telefonia(_Entrada):
    call_id: str
    answered_at: AwareDatetime | None = None
    ended_at: AwareDatetime
    sip_status_code: int
    sip_status: str = ""
    disconnect_reason: Literal[
        "CLIENT_INITIATED", "USER_REJECTED", "USER_UNAVAILABLE", "SIP_TRUNK_FAILURE", "ROOM_DELETED"
    ]
    hung_up_by: Literal["callee", "agent"] | None = None
    duration_seconds: int
    amd: Amd = Field(default_factory=Amd)


class Turno(_Entrada):
    role: Literal["agent", "user"]
    message: str
    time_in_call_secs: int


class Cita(_Entrada):
    appointment_id: str
    start_time: AwareDatetime


class NotasAgente(_Entrada):
    appointment: Cita | None = None
    slots_snapshot: dict[str, Any] = Field(default_factory=dict)


class Mensaje(_Entrada):
    channel: Literal["whatsapp"]
    text: str


class Evento(_Entrada):
    event_id: str
    type: Literal["call.ended", "message.received"]
    occurred_at: AwareDatetime
    organization_id: str
    idempotency_key: str
    delivery_attempt: int = 1
    campaign: Campania
    lead: Lead
    telephony: Telefonia | None = None
    transcript: list[Turno] = Field(default_factory=list)
    agent_outcome: NotasAgente = Field(default_factory=NotasAgente)
    message: Mensaje | None = None

    @model_validator(mode="after")
    def _bloque_segun_tipo(self) -> Self:
        if self.type == "call.ended" and self.telephony is None:
            raise ValueError("un call.ended tiene que traer el bloque telephony")
        if self.type == "message.received" and self.message is None:
            raise ValueError("un message.received tiene que traer el bloque message")
        return self

    @property
    def call_id(self) -> str | None:
        return self.telephony.call_id if self.telephony else None


def cargar_evento(ruta: Path) -> Evento:
    return Evento.model_validate(json.loads(ruta.read_text(encoding="utf-8")))
