"""Reglas de negocio contra los eventos de ejemplo, con la clasificación que les corresponde.

La clasificación de las conversaciones se fija a mano: aquí se prueban las órdenes, no el LLM.
"""

import json
from datetime import date, datetime, time
from typing import Any

import pytest
from conftest import RAIZ, evento

from orquestador.catalogo import Clasificacion, DatosConversacion, EtiquetaLlamada
from orquestador.config import Campana
from orquestador.evento import Evento
from orquestador.memoria import MemoriaLead, RecordatorioPendiente
from orquestador.reglas import Plan, _Planificador, decidir_llamada, decidir_mensaje
from orquestador.senalizacion import clasificar_por_senalizacion

SIN_DATOS = DatosConversacion()


def llamada(
    nombre: str,
    campana: Campana,
    memoria: MemoriaLead | None = None,
    etiqueta: EtiquetaLlamada | None = None,
    datos: DatosConversacion = SIN_DATOS,
) -> Plan:
    ev = evento(nombre)
    assert ev.telephony is not None
    clasificacion = clasificar_por_senalizacion(ev.telephony, bool(ev.transcript))
    if clasificacion is None:
        assert etiqueta is not None, f"{nombre} necesita la etiqueta de la conversación"
        clasificacion = Clasificacion(etiqueta=etiqueta, motivo="motivo de prueba", confianza=0.9)
    return decidir_llamada(ev, clasificacion, datos, memoria or MemoriaLead(), campana)


def operaciones(plan: Plan) -> list[str]:
    return [orden.operacion for orden in plan.ordenes]


def cuerpo(plan: Plan, operacion: str) -> dict[str, Any]:
    return next(orden.cuerpo for orden in plan.ordenes if orden.operacion == operacion)


def test_ocupado_reproduce_exactamente_el_ejemplo_resuelto(campana: Campana) -> None:
    esperado = [json.loads(linea) for linea in (RAIZ / "ejemplo-resuelto/salida/ordenes.jsonl").open()]
    plan = llamada("02-call-ended-tomas.json", campana)
    assert [orden.model_dump() for orden in plan.ordenes] == esperado


def test_sin_respuesta_respeta_la_separacion_y_al_tercer_intento_va_al_respaldo(campana: Campana) -> None:
    primero = llamada("01-call-ended-nuria.json", campana)
    assert cuerpo(primero, "programar_llamada")["no_antes_de"] == "2026-09-15T12:12:00+02:00"
    segundo = llamada("05-call-ended-nuria.json", campana, primero.memoria)
    assert cuerpo(segundo, "programar_llamada")["no_antes_de"] == "2026-09-15T14:20:00+02:00"
    tercero = llamada("12-call-ended-nuria.json", campana, segundo.memoria)  # buzón, 3 de 3
    assert operaciones(tercero) == ["cerrar_llamada", "enviar_plantilla_whatsapp"]
    assert cuerpo(tercero, "enviar_plantilla_whatsapp")["plantilla"] == "primer_toque_respaldo"


def test_buzon_con_intentos_disponibles_vuelve_a_llamar(campana: Campana) -> None:
    plan = llamada("10-call-ended-sonia.json", campana)
    assert cuerpo(plan, "programar_llamada")["no_antes_de"] == "2026-09-15T19:30:00+02:00"


def test_cortada_y_visita_sin_confirmar_se_recuperan_a_los_30_minutos(campana: Campana) -> None:
    rosa = llamada("04-call-ended-rosa.json", campana, etiqueta="cortada")
    assert cuerpo(rosa, "programar_llamada")["no_antes_de"] == "2026-09-15T12:17:00+02:00"
    carla = llamada("11-call-ended-carla.json", campana, etiqueta="visita_sin_confirmar")
    assert operaciones(carla) == ["cerrar_llamada", "programar_llamada"]  # N5: no se reserva
    assert cuerpo(carla, "programar_llamada")["no_antes_de"] == "2026-09-15T18:20:00+02:00"


def test_la_segunda_llamada_cortada_abre_una_revision(campana: Campana) -> None:
    primera = llamada("04-call-ended-rosa.json", campana, etiqueta="cortada")
    segunda = llamada("04-call-ended-rosa.json", campana, primera.memoria, etiqueta="visita_sin_confirmar")
    assert cuerpo(segunda, "crear_tarea")["tipo"] == "revisar_llamada"


def test_visita_reservada_vence_dos_horas_antes_y_avisa_si_falta_la_direccion(campana: Campana) -> None:
    plan = llamada("07-call-ended-laura.json", campana, etiqueta="visita_reservada")
    tarea = cuerpo(plan, "crear_tarea")
    assert tarea["tipo"] == "confirmar_visita_direccion"
    assert tarea["vence_el"] == "2026-09-17T09:00:00+02:00"
    assert "no tiene dirección" in tarea["detalle"]


def test_documentacion_enviada_y_la_respuesta_del_lead_cancelan_los_dos_recordatorios(
    campana: Campana,
) -> None:
    plan = llamada("08-call-ended-marcos.json", campana, etiqueta="documentacion_enviada")
    cuandos = [o.cuerpo["cuando"] for o in plan.ordenes if o.operacion == "programar_recordatorio"]
    assert cuandos == ["2026-09-17T16:42:00+02:00", "2026-09-18T16:42:00+02:00"]
    respuesta = decidir_mensaje(evento("14-message-received-marcos.json"), plan.memoria, campana)
    cancelados = {o.cuerpo["reminder_id"] for o in respuesta.ordenes}
    assert cancelados == {p.reminder_id for p in plan.memoria.recordatorios_pendientes}
    assert len(cancelados) == 2 and respuesta.memoria.recordatorios_pendientes == ()


def test_callback_a_la_hora_pedida(campana: Campana) -> None:
    datos = DatosConversacion(callback_fecha=date(2026, 9, 16), callback_hora=time(18, 0))
    plan = llamada("09-call-ended-javier.json", campana, etiqueta="callback", datos=datos)
    assert cuerpo(plan, "programar_llamada")["no_antes_de"] == "2026-09-16T18:00:00+02:00"


def test_callback_fuera_de_ventana_va_a_la_primera_franja_y_avisa_del_cambio(campana: Campana) -> None:
    datos = DatosConversacion(callback_fecha=date(2026, 9, 16), callback_hora=time(21, 30))
    plan = llamada("09-call-ended-javier.json", campana, etiqueta="callback", datos=datos)
    assert cuerpo(plan, "programar_llamada")["no_antes_de"] == "2026-09-17T10:00:00+02:00"
    assert cuerpo(plan, "enviar_plantilla_whatsapp")["plantilla"] == "aviso_cambio_hora"


def test_la_baja_solo_cierra_y_registra_y_despues_no_sale_nada(campana: Campana) -> None:
    baja = llamada("06-call-ended-pedro.json", campana, etiqueta="no_contactar")
    assert operaciones(baja) == ["cerrar_llamada", "marcar_no_contactar"]
    assert cuerpo(baja, "marcar_no_contactar")["canal"] == "todos"
    despues = llamada("06-call-ended-pedro.json", campana, baja.memoria, etiqueta="callback")
    assert operaciones(despues) == ["cerrar_llamada"]


def test_quien_rechaza_whatsapp_no_recibe_whatsapp_ni_de_respaldo(campana: Campana) -> None:
    ivan = llamada("13-call-ended-ivan.json", campana, etiqueta="documentacion_pendiente")
    assert cuerpo(ivan, "crear_tarea")["tipo"] == "enviar_documentacion_email"
    assert ivan.memoria.rechaza_whatsapp
    agotado = ivan.memoria.model_copy(update={"intentos": 2})
    plan = llamada("01-call-ended-nuria.json", campana, agotado)  # tercer intento sin respuesta
    assert "enviar_plantilla_whatsapp" not in operaciones(plan)
    assert cuerpo(plan, "crear_tarea")["tipo"] == "revisar_llamada"


def test_persona_equivocada_no_reintenta(campana: Campana) -> None:
    plan = llamada("03-call-ended-elena.json", campana, etiqueta="persona_equivocada")
    assert operaciones(plan) == ["cerrar_llamada", "crear_tarea"]
    assert cuerpo(plan, "crear_tarea")["tipo"] == "verificar_telefono"


def en(nombre: str, instante: str) -> Evento:
    """El evento de ejemplo, movido a otro instante (hora de Madrid)."""
    return evento(nombre).model_copy(update={"occurred_at": datetime.fromisoformat(instante)})


def documentacion(instante: str, campana: Campana) -> Plan:
    clasificacion = Clasificacion(etiqueta="documentacion_enviada", motivo="enlace enviado", confianza=0.9)
    return decidir_llamada(
        en("08-call-ended-marcos.json", instante), clasificacion, SIN_DATOS, MemoriaLead(), campana
    )


def test_solo_se_cancelan_los_recordatorios_que_aun_no_salieron(campana: Campana) -> None:
    plan = documentacion("2026-09-15T16:42:00+02:00", campana)  # lead: jueves 16:42 · comercial: viernes
    respuesta = decidir_mensaje(
        en("14-message-received-marcos.json", "2026-09-18T10:00:00+02:00"), plan.memoria, campana
    )  # viernes: el WhatsApp del jueves ya salió
    comercial = next(p for p in plan.memoria.recordatorios_pendientes if p.canal == "tarea_comercial")
    assert [o.cuerpo["reminder_id"] for o in respuesta.ordenes] == [comercial.reminder_id]
    assert respuesta.memoria.recordatorios_pendientes == ()


def test_el_recordatorio_al_lead_sale_a_las_48_horas_aunque_caiga_fuera_de_la_ventana(
    campana: Campana,
) -> None:
    # viernes 16:42 + 48 h = domingo 16:42: la ventana es de llamadas (el OpenAPI solo la exige a
    # programar_llamada.no_antes_de). El del comercial, a 3 días hábiles: el miércoles.
    plan = documentacion("2026-09-18T16:42:00+02:00", campana)
    cuandos = {
        o.cuerpo["canal"]: o.cuerpo["cuando"] for o in plan.ordenes if o.operacion == "programar_recordatorio"
    }
    assert cuandos == {
        "whatsapp_lead": "2026-09-20T16:42:00+02:00",
        "tarea_comercial": "2026-09-23T16:42:00+02:00",
    }


def test_la_baja_cancela_los_recordatorios_pendientes(campana: Campana) -> None:
    plan = documentacion("2026-09-15T16:42:00+02:00", campana)
    baja = decidir_llamada(
        en("06-call-ended-pedro.json", "2026-09-16T11:00:00+02:00"),
        Clasificacion(etiqueta="no_contactar", motivo="pidió la baja", confianza=0.95),
        SIN_DATOS,
        plan.memoria,
        campana,
    )
    assert operaciones(baja) == [
        "cerrar_llamada",
        "marcar_no_contactar",
        "cancelar_recordatorio",
        "cancelar_recordatorio",
    ]
    assert baja.memoria.recordatorios_pendientes == ()


def test_el_detalle_no_duplica_el_punto_del_motivo_del_modelo(campana: Campana) -> None:
    clasificacion = Clasificacion(
        etiqueta="persona_equivocada", motivo="Contestó otra persona.", confianza=0.9
    )
    plan = decidir_llamada(
        evento("03-call-ended-elena.json"), clasificacion, SIN_DATOS, MemoriaLead(), campana
    )
    assert cuerpo(plan, "crear_tarea")["detalle"] == "Contestó otra persona. Sin reintentos por voz."


@pytest.mark.parametrize(
    ("instante", "esperado"),
    [
        (
            "2026-09-15T19:10:00+02:00",
            "2026-09-15T19:40:00+02:00",
        ),  # +60 son las 20:10: primer hueco desde +30
        ("2026-09-19T13:15:00+02:00", "2026-09-19T13:45:00+02:00"),  # sábado: la ventana cierra a las 14:00
        ("2026-09-15T19:45:00+02:00", "2026-09-16T10:00:00+02:00"),  # nada del rango cabe: próxima apertura
    ],
)
def test_ocupado_se_queda_entre_30_y_90_minutos_si_la_ventana_lo_permite(
    instante: str, esperado: str, campana: Campana
) -> None:
    clasificacion = Clasificacion(etiqueta="ocupado", motivo="comunica", confianza=0.97)
    plan = decidir_llamada(
        en("02-call-ended-tomas.json", instante), clasificacion, SIN_DATOS, MemoriaLead(), campana
    )
    assert cuerpo(plan, "programar_llamada")["no_antes_de"] == esperado


@pytest.mark.parametrize(
    ("fecha", "hora", "esperado", "aviso"),
    [
        # Martes 15 a las 17:05 (instante de referencia del evento 09).
        (None, time(19, 0), "2026-09-15T19:00:00+02:00", None),  # hora sin fecha, aún por llegar hoy
        (None, time(10, 0), "2026-09-16T10:00:00+02:00", None),  # hora sin fecha, ya pasó: mañana
        (date(2026, 9, 14), time(18, 0), "2026-09-15T18:00:00+02:00", None),  # fecha pasada: próxima vez
        (date(2026, 9, 17), None, "2026-09-17T10:00:00+02:00", None),  # día sin hora: apertura ese día
        (date(2026, 9, 20), None, "2026-09-21T10:00:00+02:00", "domingo 20"),  # domingo sin franja: aviso
        (date(2026, 9, 15), None, "2026-09-15T19:05:00+02:00", None),  # «hoy» sin hora: separación general
        (None, None, "2026-09-15T19:05:00+02:00", None),  # ni día ni hora: separación general
    ],
)
def test_callback_usa_lo_que_dijo_el_lead_y_completa_lo_que_falta(
    fecha: date | None, hora: time | None, esperado: str, aviso: str | None, campana: Campana
) -> None:
    datos = DatosConversacion(callback_fecha=fecha, callback_hora=hora)
    plan = llamada("09-call-ended-javier.json", campana, etiqueta="callback", datos=datos)
    assert cuerpo(plan, "programar_llamada")["no_antes_de"] == esperado
    avisos = [o.cuerpo["parametros"] for o in plan.ordenes if o.operacion == "enviar_plantilla_whatsapp"]
    assert [a["hora_pedida"] for a in avisos] == ([aviso] if aviso else [])


@pytest.mark.parametrize(
    ("memoria", "datos"),
    [
        (MemoriaLead(intentos=2, llamadas_cortadas=1, rechaza_whatsapp=True), SIN_DATOS),
        (MemoriaLead(intentos=2, llamadas_cortadas=1), DatosConversacion(rechaza_whatsapp=True)),
    ],
    ids=["rechazó WhatsApp antes", "rechaza WhatsApp en esta llamada"],
)
def test_segunda_cortada_sin_intentos_ni_whatsapp_crea_las_dos_tareas(
    memoria: MemoriaLead, datos: DatosConversacion, campana: Campana
) -> None:
    plan = llamada("11-call-ended-carla.json", campana, memoria, etiqueta="visita_sin_confirmar", datos=datos)
    tareas = [o for o in plan.ordenes if o.operacion == "crear_tarea"]
    assert [t.idempotency_key for t in tareas] == [
        "lk-out-0315:crear_tarea:respaldo",
        "lk-out-0315:crear_tarea",
    ]
    assert [t.cuerpo["titulo"] for t in tareas] == [
        "Decidir cómo seguir: sin reintento por voz ni WhatsApp",
        "Revisar la llamada: segunda cortada con este lead",
    ]
    assert plan.memoria.respaldo_resuelto


def test_el_respaldo_se_resuelve_una_vez_por_lead_tambien_con_la_tarea(campana: Campana) -> None:
    agotado = MemoriaLead(intentos=3, rechaza_whatsapp=True)
    primero = llamada("01-call-ended-nuria.json", campana, agotado)
    assert operaciones(primero) == ["cerrar_llamada", "crear_tarea"]
    despues = llamada("05-call-ended-nuria.json", campana, primero.memoria)
    assert operaciones(despues) == ["cerrar_llamada"]


def test_dos_ordenes_distintas_con_la_misma_clave_son_un_defecto(campana: Campana) -> None:
    plan = _Planificador(evento("02-call-ended-tomas.json"), campana, MemoriaLead())
    plan.tarea("revisar_llamada", "Uno", "detalle uno")
    with pytest.raises(ValueError, match="misma clave"):
        plan.tarea("revisar_llamada", "Dos", "detalle dos")


def test_repetir_la_misma_orden_es_idempotente(campana: Campana) -> None:
    plan = _Planificador(evento("02-call-ended-tomas.json"), campana, MemoriaLead())
    plan.tarea("revisar_llamada", "Uno", "detalle")
    plan.tarea("revisar_llamada", "Uno", "detalle")
    assert len(plan.resultado().ordenes) == 1


def test_un_reminder_id_repetido_en_la_memoria_se_cancela_una_vez(campana: Campana) -> None:
    pendiente = RecordatorioPendiente(
        reminder_id="rem_x", canal="whatsapp_lead", cuando=datetime.fromisoformat("2026-09-20T10:00:00+02:00")
    )
    memoria = MemoriaLead(recordatorios_pendientes=(pendiente, pendiente))
    plan = decidir_mensaje(evento("14-message-received-marcos.json"), memoria, campana)
    assert [o.cuerpo["reminder_id"] for o in plan.ordenes] == ["rem_x"]


def test_reprocesar_el_mismo_hecho_no_duplica_los_recordatorios_en_la_memoria(campana: Campana) -> None:
    # Caída entre el store y el checkpoint: la reentrega vuelve a decidir con la memoria ya escrita.
    primera = documentacion("2026-09-15T16:42:00+02:00", campana)
    clasificacion = Clasificacion(etiqueta="documentacion_enviada", motivo="enlace enviado", confianza=0.9)
    otra_vez = decidir_llamada(
        en("08-call-ended-marcos.json", "2026-09-15T16:42:00+02:00"),
        clasificacion,
        SIN_DATOS,
        primera.memoria,
        campana,
    )
    assert otra_vez.memoria.recordatorios_pendientes == primera.memoria.recordatorios_pendientes


@pytest.mark.parametrize(("idioma", "esperado"), [("ca", "ca"), ("", "es"), (None, "es")])
def test_la_plantilla_sale_en_el_idioma_del_lead(idioma: str | None, esperado: str, campana: Campana) -> None:
    ev = evento("12-call-ended-nuria.json")
    ev = ev.model_copy(update={"lead": ev.lead.model_copy(update={"language": idioma})})
    assert ev.telephony is not None
    clasificacion = clasificar_por_senalizacion(ev.telephony, bool(ev.transcript))
    assert clasificacion is not None
    plan = decidir_llamada(ev, clasificacion, SIN_DATOS, MemoriaLead(intentos=2), campana)  # buzón, 3 de 3
    assert cuerpo(plan, "enviar_plantilla_whatsapp")["idioma"] == esperado
