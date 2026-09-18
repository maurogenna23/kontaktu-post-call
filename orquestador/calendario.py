"""Fechas en la zona horaria de la campaña y ventana de llamadas.

Decisión: los plazos en horas o minutos son tiempo absoluto (se suman en UTC); los plazos en
días mantienen la hora de reloj en Madrid. Así «48 horas» son 48 horas aunque haya cambio de
hora por medio, y «dentro de 2 días» cae a la misma hora del día.
"""

from datetime import UTC, date, datetime, time, timedelta

from orquestador.config import Campana

_DIAS_A_EXPLORAR = 8  # una semana completa más el día de partida
_NOMBRES_DIA = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


def en_zona(instante: datetime, campana: Campana) -> datetime:
    return instante.astimezone(campana.zona)


def sumar(instante: datetime, plazo: timedelta, campana: Campana) -> datetime:
    return en_zona(instante.astimezone(UTC) + plazo, campana)


def sumar_dias_naturales(instante: datetime, dias: int, campana: Campana) -> datetime:
    local = en_zona(instante, campana)
    return a_las(local.date() + timedelta(days=dias), local.time(), campana)


def sumar_dias_habiles(instante: datetime, dias: int, campana: Campana) -> datetime:
    """Avanza `dias` días hábiles (dias_habiles de campana.yaml) manteniendo la hora."""
    local = en_zona(instante, campana)
    fecha, contados = local.date(), 0
    while contados < dias:
        fecha += timedelta(days=1)
        if campana.es_dia_habil(fecha.weekday()):
            contados += 1
    return a_las(fecha, local.time(), campana)


def dentro_de_ventana(instante: datetime, campana: Campana) -> bool:
    """La ventana incluye los dos extremos: con ["10:00", "20:00"], las 20:00 es válido."""
    local = en_zona(instante, campana)
    franja = campana.franja(local.weekday())
    return franja is not None and franja[0] <= local.time() <= franja[1]


def primer_instante_valido(desde: datetime, campana: Campana) -> datetime:
    """El propio instante si cae en la ventana; si no, la apertura de la siguiente franja."""
    local = en_zona(desde, campana)
    for dias in range(_DIAS_A_EXPLORAR):
        fecha = local.date() + timedelta(days=dias)
        franja = campana.franja(fecha.weekday())
        if franja is None:
            continue
        inicio, fin = a_las(fecha, franja[0], campana), a_las(fecha, franja[1], campana)
        if dias > 0:
            return inicio
        if local <= fin:
            return max(local, inicio)
    raise ValueError("la ventana de llamadas de campana.yaml no tiene ninguna franja")


def formatear(instante: datetime, campana: Campana) -> str:
    """ISO 8601 con el offset de Madrid, como en el ejemplo resuelto: 2026-09-15T11:31:00+02:00."""
    return en_zona(instante, campana).isoformat(timespec="seconds")


def proxima_vez(hora: time, desde: datetime, campana: Campana) -> datetime:
    """El primer instante posterior a `desde` en que el reloj de Madrid marca `hora`."""
    local = en_zona(desde, campana)
    hoy = a_las(local.date(), hora, campana)
    return hoy if hoy > desde else a_las(local.date() + timedelta(days=1), hora, campana)


def describir_dia(instante: datetime, campana: Campana) -> str:
    """Texto para personas, solo el día: «domingo 20»."""
    local = en_zona(instante, campana)
    return f"{_NOMBRES_DIA[local.weekday()]} {local.day}"


def describir(instante: datetime, campana: Campana) -> str:
    """Texto para personas: «jueves 17 a las 11:00»."""
    return f"{describir_dia(instante, campana)} a las {en_zona(instante, campana):%H:%M}"


def a_las(fecha: date, hora: time, campana: Campana) -> datetime:
    """La fecha y la hora de reloj dadas, en la zona de la campaña."""
    return datetime.combine(fecha, hora.replace(tzinfo=None), tzinfo=campana.zona)
