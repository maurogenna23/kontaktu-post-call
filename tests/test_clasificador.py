"""interpretar(): contraste de la salida del modelo con los hechos del evento (sin LLM)."""

import pytest
from conftest import evento

from orquestador.clasificador import EtiquetaConversacion, SalidaModelo, interpretar
from orquestador.config import Campana
from orquestador.evento import NotasAgente


def salida(nota: str | None, etiqueta: EtiquetaConversacion = "cortada") -> SalidaModelo:
    return SalidaModelo(
        etiqueta=etiqueta, motivo="motivo", confianza=0.9, evidencia="frase", callback_fecha=None,
        callback_hora=None, rechaza_whatsapp=False, email="  ", nota_contexto=nota,
    )  # fmt: skip


def test_la_nota_del_modelo_manda_sobre_las_notas_del_agente(campana: Campana) -> None:
    _, datos = interpretar(
        salida("Busca alquiler; presupuesto cortado."), evento("04-call-ended-rosa.json"), campana
    )
    assert datos.nota_contexto == "Busca alquiler; presupuesto cortado."


@pytest.mark.parametrize("nota", [None, "", "   "], ids=["null", "vacía", "espacios"])
def test_sin_nota_del_modelo_se_arrastran_las_notas_del_agente(nota: str | None, campana: Campana) -> None:
    _, datos = interpretar(salida(nota), evento("04-call-ended-rosa.json"), campana)
    assert datos.nota_contexto == "Notas del agente: operacion: alquiler; zonas: Majadahonda, Las Rozas"
    assert datos.email is None  # un email en blanco también cuenta como ausente


def test_un_false_es_informacion_y_se_escribe_como_no(campana: Campana) -> None:
    _, datos = interpretar(
        salida(None, "documentacion_pendiente"), evento("13-call-ended-ivan.json"), campana
    )
    assert datos.nota_contexto == (
        "Notas del agente: operacion: compra; canal consentido: email; "
        "email declarado: ivan.recalde@example.com; docs enviadas: no"
    )


def test_una_fecha_iso_se_escribe_en_texto(campana: Campana) -> None:
    _, datos = interpretar(salida(None, "visita_sin_confirmar"), evento("11-call-ended-carla.json"), campana)
    assert datos.nota_contexto == (
        "Notas del agente: operacion: alquiler; zonas: Majadahonda; "
        "visita acordada verbal: jueves 17 a las 17:00"
    )


def test_una_clave_desconocida_se_conserva(campana: Campana) -> None:
    notas = NotasAgente(slots_snapshot={"mascotas": True, "planta_preferida": 3, "garaje": None})
    ev = evento("04-call-ended-rosa.json").model_copy(update={"agent_outcome": notas})
    _, datos = interpretar(salida(None), ev, campana)
    assert datos.nota_contexto == "Notas del agente: mascotas: sí; planta preferida: 3"


def test_sin_nota_ni_notas_del_agente_no_hay_nota(campana: Campana) -> None:
    _, datos = interpretar(salida(None, "persona_equivocada"), evento("03-call-ended-elena.json"), campana)
    assert datos.nota_contexto is None
