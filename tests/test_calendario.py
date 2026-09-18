from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from orquestador.calendario import (
    dentro_de_ventana,
    formatear,
    primer_instante_valido,
    sumar,
    sumar_dias_habiles,
)
from orquestador.config import Campana

MADRID = ZoneInfo("Europe/Madrid")


def madrid(texto: str) -> datetime:
    return datetime.fromisoformat(texto).replace(tzinfo=MADRID)


def test_la_ventana_incluye_los_dos_extremos(campana: Campana) -> None:
    assert dentro_de_ventana(madrid("2026-09-15T10:00"), campana)  # martes
    assert dentro_de_ventana(madrid("2026-09-15T20:00"), campana)
    assert not dentro_de_ventana(madrid("2026-09-15T20:01"), campana)


def test_el_sabado_solo_se_llama_por_la_manana_y_el_domingo_no_se_llama(campana: Campana) -> None:
    assert dentro_de_ventana(madrid("2026-09-19T14:00"), campana)
    assert not dentro_de_ventana(madrid("2026-09-19T14:30"), campana)
    assert not dentro_de_ventana(madrid("2026-09-20T12:00"), campana)


def test_fuera_de_ventana_pasa_a_la_siguiente_franja(campana: Campana) -> None:
    assert primer_instante_valido(madrid("2026-09-15T08:00"), campana) == madrid("2026-09-15T10:00")
    assert primer_instante_valido(madrid("2026-09-15T20:30"), campana) == madrid("2026-09-16T10:00")
    # sábado a las 14:30 → el domingo no hay franja → lunes a las 10:00
    assert primer_instante_valido(madrid("2026-09-19T14:30"), campana) == madrid("2026-09-21T10:00")


def test_dentro_de_ventana_se_respeta_el_instante(campana: Campana) -> None:
    instante = madrid("2026-09-15T12:12")
    assert primer_instante_valido(instante, campana) == instante


def test_el_sabado_no_es_dia_habil(campana: Campana) -> None:
    # martes + 3 días hábiles = viernes; viernes + 1 = lunes
    assert sumar_dias_habiles(madrid("2026-09-15T16:42"), 3, campana) == madrid("2026-09-18T16:42")
    assert sumar_dias_habiles(madrid("2026-09-18T16:42"), 1, campana) == madrid("2026-09-21T16:42")


def test_las_horas_son_tiempo_absoluto_aunque_cambie_la_hora(campana: Campana) -> None:
    # El 25 de octubre de 2026 se atrasa el reloj: 24 horas después de las 12:00 son las 11:00.
    despues = sumar(madrid("2026-10-24T12:00"), timedelta(hours=24), campana)
    assert formatear(despues, campana) == "2026-10-25T11:00:00+01:00"
