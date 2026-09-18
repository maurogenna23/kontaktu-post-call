"""Configuración de la campaña (config/campana.yaml), validada al cargarla."""

from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

# Posición = datetime.weekday(): 0 es lunes. Las claves de campana.yaml van sin tilde.
DIAS_SEMANA = ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")


class _Seccion(BaseModel):
    model_config = ConfigDict(frozen=True)


class DatosCampana(_Seccion):
    system_key: str
    organization_id: str
    zona_horaria: str


class Reintentos(_Seccion):
    max_intentos: int
    separacion_minima_horas: int
    ocupado_minutos_min: int
    ocupado_minutos_max: int
    cortada_minutos_min: int
    cortada_horas_max: int


class Recordatorios(_Seccion):
    documentacion_lead_horas: int
    seguimiento_comercial_dias_habiles: int


class Tareas(_Seccion):
    confirmar_visita_margen_horas: int
    vencimiento_por_defecto_dias: int


class Campana(_Seccion):
    campana: DatosCampana
    ventana_llamadas: dict[str, list[time]]
    reintentos: Reintentos
    canal_respaldo: str
    dias_habiles: list[str]
    recordatorios: Recordatorios
    tareas: Tareas

    @field_validator("ventana_llamadas")
    @classmethod
    def _franjas_completas(cls, ventana: dict[str, list[time]]) -> dict[str, list[time]]:
        for dia, franja in ventana.items():
            if len(franja) not in (0, 2):
                raise ValueError(f"la franja de {dia} debe ser [] o [inicio, fin]")
        return ventana

    @property
    def zona(self) -> ZoneInfo:
        return ZoneInfo(self.campana.zona_horaria)

    def franja(self, dia_semana: int) -> tuple[time, time] | None:
        """Inicio y fin de la ventana de llamadas de ese día, o None si ese día no se llama."""
        horas = self.ventana_llamadas.get(DIAS_SEMANA[dia_semana], [])
        return (horas[0], horas[1]) if horas else None

    def es_dia_habil(self, dia_semana: int) -> bool:
        return DIAS_SEMANA[dia_semana] in self.dias_habiles


def cargar_campana(ruta: Path) -> Campana:
    return Campana.model_validate(yaml.safe_load(ruta.read_text(encoding="utf-8")))
