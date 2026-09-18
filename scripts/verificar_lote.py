"""Verificación de punta a punta: corre los eventos como lo haría el evaluador y comprueba la salida.

    uv run python scripts/verificar_lote.py

Dos lotes, cada uno desde cero en una carpeta temporal (no toca salida/ ni estado/ del repo), con
un proceso por evento, como pide el enunciado:
1. El lote de ejemplo (eventos/orden.txt).
2. Un lote sintético derivado de esos eventos: los casos ⚠ de casos.md sin ejemplo (603, callback
   fuera de ventana, descartado), IVR, 5xx, 408 y buzón de LiveKit, otras horas y otros días
   (sábado, domingo, 19:45, cambio de hora), memoria entre eventos (segunda cortada, intentos
   agotados, baja con recordatorios, respuesta tras un recordatorio enviado) y reentregas.

Comprueba en cada lote:
- esquemas: cada decisión contra decision.schema.json y cada cuerpo contra su operación del OpenAPI;
- invariantes: una decisión por evento, órdenes referenciadas y con clave única, llamadas dentro
  de la ventana, cerrar_llamada una vez por llamada nueva, nada tras reentregas ni de otras
  organizaciones, la baja sin otras órdenes y nada saliente después;
- el resultado esperado de cada evento (etiqueta, operaciones y campos clave), fijado a mano
  leyendo casos.md;
- R5: repetir el lote de ejemplo entero no añade ninguna orden;
- R8: un evento ilegible o inválido sale con código distinto de 0 y no deja nada a medias.
"""

import copy
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema
import yaml

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from orquestador.calendario import dentro_de_ventana  # noqa: E402
from orquestador.config import cargar_campana  # noqa: E402

CAMPANA = cargar_campana(RAIZ / "config" / "campana.yaml")
ORGANIZACION = CAMPANA.campana.organization_id


@dataclass(frozen=True)
class Esperado:
    etiqueta: str
    operaciones: list[str]
    # "operacion.campo" → valor (o lista de valores si la operación sale varias veces)
    campos: dict[str, str | list[str]] = field(default_factory=dict)


# ─── Lote de ejemplo ───────────────────────────────────────────────────────────────────────────

CERRAR, LLAMAR, TAREA = "cerrar_llamada", "programar_llamada", "crear_tarea"
WHATSAPP, RECORDATORIO = "enviar_plantilla_whatsapp", "programar_recordatorio"
NO_ANTES_DE, TIPO_TAREA = "programar_llamada.no_antes_de", "crear_tarea.tipo"
PLANTILLA, VENCE = "enviar_plantilla_whatsapp.plantilla", "crear_tarea.vence_el"
# Caso 7: la llamada siguiente arrastra lo ya recogido; por lógica, también la visita sin confirmar y
# el callback. Se comprueba que la nota exista, venga del modelo o de las notas del agente.
ARRASTRAN_CONTEXTO = {"cortada", "visita_sin_confirmar", "callback"}

ESPERADO_EJEMPLO = {
    "evt_01": Esperado("sin_respuesta", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-15T12:12:00+02:00"}),
    "evt_02": Esperado("ocupado", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-15T11:31:00+02:00"}),
    "evt_03": Esperado("persona_equivocada", [CERRAR, TAREA], {TIPO_TAREA: "verificar_telefono"}),
    "evt_04": Esperado("cortada", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-15T12:17:00+02:00"}),
    "evt_05": Esperado("sin_respuesta", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-15T14:20:00+02:00"}),
    "evt_06": Esperado(
        "no_contactar", [CERRAR, "marcar_no_contactar"], {"marcar_no_contactar.canal": "todos"}
    ),
    "evt_07": Esperado(
        "visita_reservada",
        [CERRAR, TAREA],
        {TIPO_TAREA: "confirmar_visita_direccion", VENCE: "2026-09-17T09:00:00+02:00"},
    ),
    "evt_08": Esperado(
        "documentacion_enviada",
        [CERRAR, RECORDATORIO, RECORDATORIO],
        {"programar_recordatorio.cuando": ["2026-09-17T16:42:00+02:00", "2026-09-18T16:42:00+02:00"]},
    ),
    "evt_09": Esperado("callback", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-16T18:00:00+02:00"}),
    "evt_10": Esperado("buzon", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-15T19:30:00+02:00"}),
    "evt_11": Esperado("visita_sin_confirmar", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-15T18:20:00+02:00"}),
    "evt_12": Esperado("buzon", [CERRAR, WHATSAPP], {PLANTILLA: "primer_toque_respaldo"}),
    "evt_13": Esperado(
        "documentacion_pendiente", [CERRAR, TAREA], {TIPO_TAREA: "enviar_documentacion_email"}
    ),
    "evt_14": Esperado("no_aplica", ["cancelar_recordatorio", "cancelar_recordatorio"]),
    "evt_15": Esperado("callback", []),
    "evt_16": Esperado("no_aplica", []),
}


def lote_de_ejemplo() -> list[dict[str, Any]]:
    nombres = [linea.strip() for linea in (RAIZ / "eventos" / "orden.txt").read_text().splitlines()]
    return [_leer(nombre) for nombre in nombres if nombre and not nombre.startswith("#")]


# ─── Lote sintético ────────────────────────────────────────────────────────────────────────────


def _leer(nombre: str) -> dict[str, Any]:
    return json.loads((RAIZ / "eventos" / nombre).read_text(encoding="utf-8"))


def _combinar(base: dict[str, Any], cambios: dict[str, Any]) -> dict[str, Any]:
    resultado = copy.deepcopy(base)
    for clave, valor in cambios.items():
        if isinstance(valor, dict) and isinstance(resultado.get(clave), dict):
            resultado[clave] = _combinar(resultado[clave], valor)
        else:
            resultado[clave] = valor
    return resultado


def _turnos(*frases: str) -> list[dict[str, Any]]:
    """Alterna agente y lead empezando por el agente."""
    return [
        {"role": "agent" if i % 2 == 0 else "user", "message": frase, "time_in_call_secs": 3 + 7 * i}
        for i, frase in enumerate(frases)
    ]


def sintetico(
    n: int,
    base: str,
    contacto: str,
    instante: str,
    cambios: dict[str, Any] | None = None,
    hecho: int | None = None,
) -> dict[str, Any]:
    """Un evento derivado de `base`: identidad propia, contacto e instante (hora de Madrid, verano)."""
    evento = _leer(base)
    clave = f"lk-sint-{hecho or n:02d}"
    momento = f"{instante}+02:00"
    identidad: dict[str, Any] = {
        "event_id": f"sint_{n:02d}",
        "idempotency_key": clave if evento["type"] == "call.ended" else f"wa-sint-{hecho or n:02d}",
        "occurred_at": momento,
        "delivery_attempt": 2 if hecho else 1,
        "campaign": {"entry_id": f"ce_sint_{hecho or n:02d}"},
        "lead": {"contact_id": contacto},
    }
    if evento["type"] == "call.ended":
        identidad["telephony"] = {"call_id": clave, "dialed_at": momento, "ended_at": momento}
    return _combinar(_combinar(evento, identidad), cambios or {})


SALUDO = "Hola, muy buenas. Soy Marta, la asistente virtual de Ribera Inmobiliaria. ¿Hablo con Andrés?"


def lote_sintetico() -> tuple[list[dict[str, Any]], dict[str, Esperado]]:
    eventos = [
        # 11 ⚠ rechazada: 603 antes de descolgar → canal de respaldo, sin reintento por voz.
        sintetico(
            1,
            "02-call-ended-tomas.json",
            "c_s01",
            "2026-09-15T11:00:00",
            {"telephony": {"sip_status_code": 603, "sip_status": "Decline D21"}},
        ),
        # Reentrega del hecho anterior: repite la etiqueta, ninguna orden.
        sintetico(
            2,
            "02-call-ended-tomas.json",
            "c_s01",
            "2026-09-15T11:00:00",
            {"telephony": {"sip_status_code": 603, "sip_status": "Decline D21"}},
            hecho=1,
        ),
        # 5xx y machine-ivr no encajan en ningún caso → otro + revisar_llamada (N4).
        sintetico(
            3,
            "01-call-ended-nuria.json",
            "c_s03",
            "2026-09-15T11:10:00",
            {
                "telephony": {
                    "sip_status_code": 503,
                    "sip_status": "Service Unavailable",
                    "disconnect_reason": "SIP_TRUNK_FAILURE",
                }
            },
        ),
        sintetico(
            4,
            "12-call-ended-nuria.json",
            "c_s04",
            "2026-09-15T11:20:00",
            {
                "telephony": {
                    "amd": {
                        "result": "machine-ivr",
                        "source": "livekit_amd",
                        "greeting_transcript": "Bienvenido. Para ventas, pulse uno.",
                    }
                }
            },
        ),
        # 12 ⚠ callback fuera de la ventana → primera franja válida + aviso_cambio_hora.
        sintetico(
            5,
            "09-call-ended-javier.json",
            "c_s05",
            "2026-09-15T16:00:00",
            {
                "agent_outcome": {"slots_snapshot": {"callback_when_raw": "esta noche a las diez"}},
                "transcript": _turnos(
                    SALUDO,
                    "Sí, soy yo, pero estoy en una reunión.",
                    "Sin problema. ¿Cuándo te viene bien que te llamemos?",
                    "Llámame esta noche a las diez, que ya estaré en casa.",
                    "Perfecto, lo dejo anotado. Un saludo.",
                ),
            },
        ),
        # 15 ⚠ descartado: ya alquiló; que cuelgue seco no lo convierte en cortada.
        sintetico(
            6,
            "04-call-ended-rosa.json",
            "c_s06",
            "2026-09-15T12:00:00",
            {
                "agent_outcome": {"slots_snapshot": {}},
                "transcript": _turnos(
                    SALUDO,
                    "Sí, soy yo.",
                    "Te llamo por tu consulta del alquiler en Majadahonda.",
                    "Ah, no, ya alquilé un piso la semana pasada. Ya no busco.",
                ),
            },
        ),
        # Una baja manda aunque venga con un «ya encontré piso».
        sintetico(
            7,
            "04-call-ended-rosa.json",
            "c_s07",
            "2026-09-15T12:10:00",
            {
                "agent_outcome": {"slots_snapshot": {}},
                "transcript": _turnos(
                    SALUDO,
                    "Sí.",
                    "Te llamo por tu consulta del alquiler en Majadahonda.",
                    "Ya encontré piso, y por favor no me llaméis más.",
                    "Entendido, te quito de la lista. Disculpa la molestia.",
                ),
            },
        ),
        # Un uncertain del detector se trata como persona: se lee la conversación.
        sintetico(
            8,
            "09-call-ended-javier.json",
            "c_s08",
            "2026-09-15T17:05:00",
            {"telephony": {"amd": {"result": "uncertain", "source": "livekit_amd"}}},
        ),
        # N4: la segunda llamada cortada con el mismo lead abre una revisión.
        sintetico(9, "04-call-ended-rosa.json", "c_s09", "2026-09-15T11:47:00"),
        sintetico(10, "11-call-ended-carla.json", "c_s09", "2026-09-15T15:00:00"),
        # 6 contra 13: el buzón por heurística con los intentos agotados va al respaldo.
        sintetico(11, "01-call-ended-nuria.json", "c_s11", "2026-09-15T10:00:00"),
        sintetico(12, "02-call-ended-tomas.json", "c_s11", "2026-09-15T12:30:00"),
        sintetico(13, "10-call-ended-sonia.json", "c_s11", "2026-09-15T15:00:00"),
        # Bordes de la ventana: cortada a las 19:50, ocupado el sábado, sin respuesta el viernes.
        sintetico(14, "04-call-ended-rosa.json", "c_s14", "2026-09-15T19:50:00"),
        sintetico(15, "02-call-ended-tomas.json", "c_s15", "2026-09-19T13:30:00"),
        sintetico(16, "01-call-ended-nuria.json", "c_s16", "2026-09-18T19:00:00"),
        # N2: tras una baja, un evento posterior del mismo lead solo cierra la llamada.
        sintetico(17, "06-call-ended-pedro.json", "c_s17", "2026-09-15T12:55:00"),
        sintetico(18, "01-call-ended-nuria.json", "c_s17", "2026-09-16T11:00:00"),
        # R7 sin recordatorios pendientes: nada que cancelar.
        sintetico(19, "14-message-received-marcos.json", "c_s19", "2026-09-16T09:30:00"),
        # Visita reservada sin margen: la tarea vence en el acto.
        sintetico(
            20,
            "07-call-ended-laura.json",
            "c_s20",
            "2026-09-15T16:10:00",
            {
                "lead": {"property_address": "calle de Sorolla 8, Majadahonda"},
                "agent_outcome": {
                    "appointment": {"appointment_id": "apt_s20", "start_time": "2026-09-15T17:00:00+02:00"}
                },
            },
        ),
        # Caso 6: buzón detectado por LiveKit (machine-vm y machine-unavailable) con intentos
        # disponibles, y caso 4 con 408.
        sintetico(21, "12-call-ended-nuria.json", "c_s21", "2026-09-15T11:00:00"),
        sintetico(
            22,
            "12-call-ended-nuria.json",
            "c_s22",
            "2026-09-15T11:30:00",
            {"telephony": {"amd": {"result": "machine-unavailable"}}},
        ),
        sintetico(
            23,
            "01-call-ended-nuria.json",
            "c_s23",
            "2026-09-15T12:00:00",
            {"telephony": {"sip_status_code": 408, "sip_status": "Request Timeout"}},
        ),
        # Otras horas y otros días: ocupado a las 19:45 y un evento en domingo.
        sintetico(24, "02-call-ended-tomas.json", "c_s24", "2026-09-15T19:45:00"),
        sintetico(25, "01-call-ended-nuria.json", "c_s25", "2026-09-20T12:00:00"),
        # Callback con día de la semana, pedido un viernes.
        sintetico(
            26,
            "09-call-ended-javier.json",
            "c_s26",
            "2026-09-18T17:00:00",
            {
                "agent_outcome": {"slots_snapshot": {"callback_when_raw": "el lunes a las once"}},
                "transcript": _turnos(
                    SALUDO,
                    "Sí, soy yo.",
                    "Te llamo por tu consulta sobre la venta en Boadilla. ¿Tienes un minuto?",
                    "Ahora no, estoy saliendo del trabajo. Llámame el lunes a las once, por favor.",
                    "Perfecto, te llamamos el lunes a las once. Un saludo.",
                ),
            },
        ),
        # R7: el lead responde el viernes; el WhatsApp del jueves ya salió y solo queda el del comercial.
        sintetico(28, "08-call-ended-marcos.json", "c_s28", "2026-09-15T16:42:00"),
        sintetico(29, "14-message-received-marcos.json", "c_s28", "2026-09-18T10:00:00"),
        # Caso 10: la baja no cancela los recordatorios pendientes; cuando el lead escribe, los cancela R7.
        sintetico(30, "08-call-ended-marcos.json", "c_s30", "2026-09-15T16:42:00"),
        sintetico(31, "06-call-ended-pedro.json", "c_s30", "2026-09-16T11:00:00"),
        sintetico(32, "14-message-received-marcos.json", "c_s30", "2026-09-16T12:00:00"),
        # Documentación el viernes → el recordatorio al lead, a las 48 horas exactas (domingo 16:42): la
        # ventana es de llamadas; el OpenAPI no se la exige a programar_recordatorio.cuando.
        sintetico(33, "08-call-ended-marcos.json", "c_s33", "2026-09-18T16:42:00"),
        # Reentrega de un WhatsApp: repite no_aplica sin órdenes.
        sintetico(34, "14-message-received-marcos.json", "c_s28", "2026-09-18T10:05:00", hecho=29),
        # Callback en el tercer intento: los intentos de voz están agotados → respaldo (N3).
        sintetico(35, "01-call-ended-nuria.json", "c_s35", "2026-09-15T10:00:00"),
        sintetico(36, "02-call-ended-tomas.json", "c_s35", "2026-09-15T12:30:00"),
        sintetico(37, "09-call-ended-javier.json", "c_s35", "2026-09-15T17:05:00"),
        # Cambio de hora: sábado 24 de octubre a las 12:30 + 2 h → fuera de franja → lunes 26 con +01:00.
        sintetico(38, "01-call-ended-nuria.json", "c_s38", "2026-10-24T12:30:00"),
        # Contesta otra persona pero dice cuándo localizar al lead: callback, no persona_equivocada.
        # A las 20:00, el extremo de la ventana, que es inclusivo.
        sintetico(
            39,
            "03-call-ended-elena.json",
            "c_s39",
            "2026-09-15T17:00:00",
            {
                "transcript": _turnos(
                    "Hola, buenas. ¿Hablo con Elena? Te llamo de parte de Ribera Inmobiliaria.",
                    "No, soy su marido. Elena ahora no está, vuelve a las ocho.",
                    "Vale, ¿le podemos llamar entonces?",
                    "Sí, llamadla a las ocho, que ya estará en casa.",
                    "Perfecto, la llamamos a las ocho. Gracias.",
                ),
            },
        ),
        # Ocupado a las 19:10: +60 cae fuera, pero 19:40 cumple el rango de 30 a 90 min y la ventana.
        sintetico(40, "02-call-ended-tomas.json", "c_s40", "2026-09-15T19:10:00"),
        # Callback con día y sin hora, con día sin franja y con franja horaria (martes 17:05).
        sintetico(
            41,
            "09-call-ended-javier.json",
            "c_s41",
            "2026-09-15T17:05:00",
            {
                "agent_outcome": {"slots_snapshot": {"callback_when_raw": "el jueves"}},
                "transcript": _turnos(
                    SALUDO,
                    "Sí, soy yo, pero ahora no puedo.",
                    "Sin problema. ¿Cuándo te viene bien?",
                    "Llámame el jueves, que estaré libre.",
                    "Perfecto, te llamamos el jueves.",
                ),
            },
        ),
        sintetico(
            42,
            "09-call-ended-javier.json",
            "c_s42",
            "2026-09-15T17:05:00",
            {
                "agent_outcome": {"slots_snapshot": {"callback_when_raw": "el domingo"}},
                "transcript": _turnos(
                    SALUDO,
                    "Sí, soy yo, pero esta semana imposible.",
                    "¿Cuándo te viene bien que te llamemos?",
                    "Llámame el domingo, que es cuando tengo tiempo.",
                    "Hecho, lo anoto.",
                ),
            },
        ),
        sintetico(
            43,
            "09-call-ended-javier.json",
            "c_s43",
            "2026-09-15T17:05:00",
            {
                "agent_outcome": {"slots_snapshot": {"callback_when_raw": "mañana por la tarde"}},
                "transcript": _turnos(
                    SALUDO,
                    "Sí, soy yo, estoy conduciendo.",
                    "Vale, ¿te llamo en otro momento?",
                    "Sí, mejor mañana por la tarde.",
                    "Perfecto, mañana por la tarde te llamamos.",
                ),
            },
        ),
        # R5 y R6 con la misma idempotency_key en dos organizaciones (una copia con otro organization_id).
        # a) ajeno → nuestro: el nuestro se procesa completo.
        sintetico(
            44,
            "02-call-ended-tomas.json",
            "c_s44",
            "2026-09-15T11:00:00",
            {"organization_id": "org_demo_b"},
            hecho=45,
        ),
        sintetico(45, "02-call-ended-tomas.json", "c_s45", "2026-09-15T11:00:00"),
        # b) nuestro → ajeno → reentrega del nuestro: la reentrega repite la etiqueta original.
        sintetico(46, "02-call-ended-tomas.json", "c_s46", "2026-09-15T11:30:00"),
        sintetico(
            47,
            "02-call-ended-tomas.json",
            "c_s46",
            "2026-09-15T11:30:00",
            {"organization_id": "org_demo_b"},
            hecho=46,
        ),
        sintetico(48, "02-call-ended-tomas.json", "c_s46", "2026-09-15T11:30:00", hecho=46),
        # Hueco 19: segunda cortada en el tercer intento de un lead que rechazó WhatsApp. Cada regla
        # deja su tarea: la del respaldo imposible y la de N4.
        sintetico(49, "04-call-ended-rosa.json", "c_s49", "2026-09-15T11:47:00"),
        sintetico(50, "13-call-ended-ivan.json", "c_s49", "2026-09-15T13:00:00"),
        sintetico(51, "11-call-ended-carla.json", "c_s49", "2026-09-15T17:50:00"),
    ]
    esperado = {
        "sint_01": Esperado(
            "rechazada",
            [CERRAR, WHATSAPP],
            {"cerrar_llamada.status": "refused", PLANTILLA: "primer_toque_respaldo"},
        ),
        "sint_02": Esperado("rechazada", []),
        "sint_03": Esperado("otro", [CERRAR, TAREA], {TIPO_TAREA: "revisar_llamada"}),
        "sint_04": Esperado("otro", [CERRAR, TAREA], {TIPO_TAREA: "revisar_llamada"}),
        "sint_05": Esperado(
            "callback",
            [CERRAR, LLAMAR, WHATSAPP],
            {NO_ANTES_DE: "2026-09-16T10:00:00+02:00", PLANTILLA: "aviso_cambio_hora"},
        ),
        "sint_06": Esperado("descartado", [CERRAR], {"cerrar_llamada.status": "skipped"}),
        "sint_07": Esperado("no_contactar", [CERRAR, "marcar_no_contactar"]),
        "sint_08": Esperado("callback", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-16T18:00:00+02:00"}),
        "sint_09": Esperado("cortada", [CERRAR, LLAMAR]),
        "sint_10": Esperado("visita_sin_confirmar", [CERRAR, LLAMAR, TAREA], {TIPO_TAREA: "revisar_llamada"}),
        "sint_11": Esperado("sin_respuesta", [CERRAR, LLAMAR]),
        "sint_12": Esperado("ocupado", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-15T13:30:00+02:00"}),
        "sint_13": Esperado("buzon", [CERRAR, WHATSAPP], {PLANTILLA: "primer_toque_respaldo"}),
        "sint_14": Esperado("cortada", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-16T10:00:00+02:00"}),
        # Sábado 13:30: +60 no cabe, pero +30 son las 14:00, el cierre inclusivo de la franja del sábado.
        "sint_15": Esperado("ocupado", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-19T14:00:00+02:00"}),
        "sint_16": Esperado("sin_respuesta", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-19T10:00:00+02:00"}),
        "sint_17": Esperado("no_contactar", [CERRAR, "marcar_no_contactar"]),
        "sint_18": Esperado("sin_respuesta", [CERRAR]),
        "sint_19": Esperado("no_aplica", []),
        "sint_20": Esperado("visita_reservada", [CERRAR, TAREA], {VENCE: "2026-09-15T16:10:00+02:00"}),
        "sint_21": Esperado("buzon", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-15T13:00:00+02:00"}),
        "sint_22": Esperado("buzon", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-15T13:30:00+02:00"}),
        "sint_23": Esperado("sin_respuesta", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-15T14:00:00+02:00"}),
        "sint_24": Esperado("ocupado", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-16T10:00:00+02:00"}),
        "sint_25": Esperado("sin_respuesta", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-21T10:00:00+02:00"}),
        "sint_26": Esperado("callback", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-21T11:00:00+02:00"}),
        "sint_28": Esperado("documentacion_enviada", [CERRAR, RECORDATORIO, RECORDATORIO]),
        "sint_29": Esperado("no_aplica", ["cancelar_recordatorio"]),
        "sint_30": Esperado("documentacion_enviada", [CERRAR, RECORDATORIO, RECORDATORIO]),
        "sint_31": Esperado("no_contactar", [CERRAR, "marcar_no_contactar"]),
        "sint_32": Esperado("no_aplica", ["cancelar_recordatorio", "cancelar_recordatorio"]),
        "sint_33": Esperado(
            "documentacion_enviada",
            [CERRAR, RECORDATORIO, RECORDATORIO],
            {"programar_recordatorio.cuando": ["2026-09-20T16:42:00+02:00", "2026-09-23T16:42:00+02:00"]},
        ),
        "sint_34": Esperado("no_aplica", []),
        "sint_35": Esperado("sin_respuesta", [CERRAR, LLAMAR]),
        "sint_36": Esperado("ocupado", [CERRAR, LLAMAR]),
        "sint_37": Esperado("callback", [CERRAR, WHATSAPP], {PLANTILLA: "primer_toque_respaldo"}),
        "sint_38": Esperado("sin_respuesta", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-10-26T10:00:00+01:00"}),
        "sint_39": Esperado("callback", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-15T20:00:00+02:00"}),
        "sint_40": Esperado("ocupado", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-15T19:40:00+02:00"}),
        "sint_41": Esperado("callback", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-17T10:00:00+02:00"}),
        "sint_42": Esperado(
            "callback",
            [CERRAR, LLAMAR, WHATSAPP],
            {NO_ANTES_DE: "2026-09-21T10:00:00+02:00", PLANTILLA: "aviso_cambio_hora"},
        ),
        "sint_43": Esperado("callback", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-16T16:00:00+02:00"}),
        # casos.md: otra organización → no_aplica, ninguna orden; reentrega → la etiqueta de la llamada
        # original, ninguna orden nueva; 486 nuestro → cerrar_llamada y reintento corto (+60).
        "sint_44": Esperado("no_aplica", []),
        "sint_45": Esperado("ocupado", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-15T12:00:00+02:00"}),
        "sint_46": Esperado("ocupado", [CERRAR, LLAMAR], {NO_ANTES_DE: "2026-09-15T12:30:00+02:00"}),
        "sint_47": Esperado("no_aplica", []),
        "sint_48": Esperado("ocupado", []),
        "sint_49": Esperado("cortada", [CERRAR, LLAMAR]),
        "sint_50": Esperado(
            "documentacion_pendiente", [CERRAR, TAREA], {TIPO_TAREA: "enviar_documentacion_email"}
        ),
        "sint_51": Esperado(
            "visita_sin_confirmar",
            [CERRAR, TAREA, TAREA],
            {
                "crear_tarea.titulo": [
                    "Decidir cómo seguir: sin reintento por voz ni WhatsApp",
                    "Revisar la llamada: segunda cortada con este lead",
                ]
            },
        ),
    }
    return eventos, esperado


# ─── Ejecución y comprobaciones ────────────────────────────────────────────────────────────────


def correr(eventos: list[dict[str, Any]], datos: Path) -> list[str]:
    """Un proceso por evento. Devuelve los fallos de ejecución."""
    fallos, entorno = [], {**os.environ, "ORQUESTADOR_DATOS": str(datos)}
    for evento in eventos:
        ruta = datos / "entrada" / f"{evento['event_id']}.json"
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(json.dumps(evento, ensure_ascii=False), encoding="utf-8")
        proceso = subprocess.run(
            [sys.executable, str(RAIZ / "run.py"), str(ruta)],
            env=entorno,
            capture_output=True,
            text=True,
            check=False,
        )
        if proceso.returncode != 0:
            fallos.append(f"{evento['event_id']}: salió con {proceso.returncode}: {proceso.stderr[-300:]}")
    return fallos


def leer_salida(datos: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    def jsonl(nombre: str) -> list[dict[str, Any]]:
        ruta = datos / "salida" / nombre
        return (
            [json.loads(linea) for linea in ruta.read_text().splitlines() if linea] if ruta.exists() else []
        )

    return jsonl("decisiones.jsonl"), jsonl("ordenes.jsonl")


def esquemas_de_operaciones() -> dict[str, Any]:
    api = yaml.safe_load((RAIZ / "esquemas" / "crm-openapi.yaml").read_text(encoding="utf-8"))
    return {
        operacion["operationId"]: operacion["requestBody"]["content"]["application/json"]["schema"]
        for metodos in api["paths"].values()
        for operacion in metodos.values()
        if isinstance(operacion, dict) and "operationId" in operacion
    }


def comprobar(eventos: list[dict[str, Any]], datos: Path, esperado: dict[str, Esperado]) -> list[str]:
    fallos: list[str] = []
    decisiones, ordenes = leer_salida(datos)
    esquema_decision = json.loads((RAIZ / "esquemas" / "decision.schema.json").read_text())
    esquemas = esquemas_de_operaciones()

    for decision in decisiones:
        for error in jsonschema.Draft202012Validator(esquema_decision).iter_errors(decision):
            fallos.append(f"{decision['event_id']}: decisión fuera de esquema: {error.message}")
    for orden in ordenes:
        for error in jsonschema.Draft202012Validator(esquemas[orden["operacion"]]).iter_errors(
            orden["cuerpo"]
        ):
            fallos.append(
                f"{orden['event_id']} {orden['operacion']}: cuerpo fuera de esquema: {error.message}"
            )

    por_evento: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for orden in ordenes:
        por_evento[orden["event_id"]].append(orden)
    claves = Counter(orden["idempotency_key"] for orden in ordenes)
    fallos += [f"clave de idempotencia repetida: {clave}" for clave, n in claves.items() if n > 1]
    ids = {orden["orden_id"] for orden in ordenes}
    referenciadas = [oid for decision in decisiones for oid in decision["ordenes"]]
    if sorted(referenciadas) != sorted(ids):
        fallos.append("las órdenes referenciadas en decisiones.jsonl no coinciden con ordenes.jsonl")

    decision_de = {decision["event_id"]: decision for decision in decisiones}
    if len(decisiones) != len(eventos):
        fallos.append(f"{len(decisiones)} decisiones para {len(eventos)} eventos")
    hechos_vistos: set[tuple[str, str]] = set()
    con_baja: set[str] = set()
    for evento in eventos:
        eid, propias = evento["event_id"], por_evento.get(evento["event_id"], [])
        operaciones = [orden["operacion"] for orden in propias]
        hecho = (evento["organization_id"], evento["idempotency_key"])
        nuevo = hecho not in hechos_vistos
        hechos_vistos.add(hecho)
        es_llamada_nueva = (
            evento["type"] == "call.ended" and nuevo and evento["organization_id"] == ORGANIZACION
        )
        if es_llamada_nueva and operaciones.count(CERRAR) != 1:
            fallos.append(f"{eid}: una llamada nueva lleva exactamente un cerrar_llamada")
        if (not nuevo or evento["organization_id"] != ORGANIZACION) and propias:
            fallos.append(f"{eid}: una reentrega o un evento de otra organización no emite órdenes")
        contacto = evento["lead"]["contact_id"]
        # N2: tras la baja, nada saliente; cancelar un recordatorio (R7) no le llega al lead.
        if contacto in con_baja and set(operaciones) - {CERRAR, "cancelar_recordatorio"}:
            fallos.append(f"{eid}: el lead está dado de baja y recibió {operaciones}")
        if "marcar_no_contactar" in operaciones:
            # Caso 10: la baja cierra la llamada y se registra; ninguna otra orden.
            if set(operaciones) - {CERRAR, "marcar_no_contactar"}:
                fallos.append(f"{eid}: la baja lleva otras órdenes: {operaciones}")
            con_baja.add(contacto)
        etiqueta = decision_de.get(eid, {}).get("etiqueta")
        for orden in propias:
            nota = str(orden["cuerpo"].get("nota_contexto") or "").strip()
            if orden["operacion"] == LLAMAR and etiqueta in ARRASTRAN_CONTEXTO and not nota:
                fallos.append(f"{eid}: la llamada de {etiqueta} no arrastra nota_contexto (caso 7)")
            if orden["operacion"] == LLAMAR:
                cuando = datetime.fromisoformat(orden["cuerpo"]["no_antes_de"])
                if not dentro_de_ventana(cuando, CAMPANA):
                    fallos.append(f"{eid}: llamada programada fuera de la ventana ({cuando})")
                if cuando < datetime.fromisoformat(evento["occurred_at"]):
                    fallos.append(f"{eid}: llamada programada antes del evento ({cuando})")

        esperado_evento, obtenida = esperado.get(eid), decision_de.get(eid)
        if esperado_evento is None or obtenida is None:
            fallos.append(f"{eid}: sin decisión o sin resultado esperado")
            continue
        if obtenida["etiqueta"] != esperado_evento.etiqueta:
            fallos.append(f"{eid}: etiqueta {obtenida['etiqueta']}, se esperaba {esperado_evento.etiqueta}")
        if sorted(operaciones) != sorted(esperado_evento.operaciones):
            fallos.append(f"{eid}: operaciones {operaciones}, se esperaban {esperado_evento.operaciones}")
        for ruta, valor in esperado_evento.campos.items():
            operacion, campo = ruta.split(".")
            valores = sorted(str(o["cuerpo"].get(campo)) for o in propias if o["operacion"] == operacion)
            if valores != sorted(valor if isinstance(valor, list) else [valor]):
                fallos.append(f"{eid}: {ruta} = {valores}, se esperaba {valor}")
    return fallos


def resumen(nombre: str, eventos: list[dict[str, Any]], datos: Path, fallos: list[str]) -> None:
    decisiones, ordenes = leer_salida(datos)
    decision_de = {d["event_id"]: d for d in decisiones}
    print(f"\n━━ {nombre}: {len(eventos)} eventos · {len(ordenes)} órdenes")
    for evento in eventos:
        eid = evento["event_id"]
        marca = "✗" if any(f.startswith(f"{eid}:") for f in fallos) else "✓"
        operaciones = [o["operacion"] for o in ordenes if o["event_id"] == eid]
        etiqueta = decision_de.get(eid, {}).get("etiqueta", "—")
        print(f"  {marca} {eid:<8} {etiqueta:<24} {', '.join(operaciones) or '—'}")


def main() -> int:
    fallos_totales: list[str] = []
    with tempfile.TemporaryDirectory(prefix="verificacion-") as tmp:
        ejemplo, datos_ejemplo = lote_de_ejemplo(), Path(tmp) / "ejemplo"
        fallos = correr(ejemplo, datos_ejemplo) + comprobar(ejemplo, datos_ejemplo, ESPERADO_EJEMPLO)
        resumen("Lote de ejemplo", ejemplo, datos_ejemplo, fallos)
        fallos_totales += fallos

        # R5: el mismo lote otra vez sobre el mismo estado no añade ninguna orden.
        _, antes = leer_salida(datos_ejemplo)
        fallos = correr(ejemplo, datos_ejemplo)
        _, despues = leer_salida(datos_ejemplo)
        if len(despues) != len(antes):
            fallos.append(f"R5: repetir el lote añadió {len(despues) - len(antes)} órdenes")
        print(f"\n━━ R5 · lote repetido: {len(despues) - len(antes)} órdenes nuevas (se esperan 0)")
        fallos_totales += fallos

        sinteticos, esperado = lote_sintetico()
        datos_sinteticos = Path(tmp) / "sintetico"
        fallos = correr(sinteticos, datos_sinteticos) + comprobar(sinteticos, datos_sinteticos, esperado)
        resumen("Lote sintético", sinteticos, datos_sinteticos, fallos)
        fallos_totales += fallos

        # R8: un evento ilegible o inválido sale con error y no escribe nada.
        fallos = []
        rotos = Path(tmp) / "rotos"
        rotos.mkdir()
        (rotos / "ilegible.json").write_text("{ esto no es json")
        (rotos / "sin_telefonia.json").write_text(
            json.dumps({**_leer("01-call-ended-nuria.json"), "telephony": None})
        )
        decisiones_antes, _ = leer_salida(datos_sinteticos)
        entorno = {**os.environ, "ORQUESTADOR_DATOS": str(datos_sinteticos)}
        for roto in sorted(rotos.iterdir()):
            proceso = subprocess.run(
                [sys.executable, str(RAIZ / "run.py"), str(roto)],
                env=entorno,
                capture_output=True,
                text=True,
                check=False,
            )
            if proceso.returncode == 0:
                fallos.append(f"R8: {roto.name} debería salir con error")
        decisiones_despues, _ = leer_salida(datos_sinteticos)
        if len(decisiones_despues) != len(decisiones_antes):
            fallos.append("R8: un evento inválido dejó líneas en la salida")
        print(f"\n━━ R8 · eventos inválidos: {'rechazados sin tocar la salida' if not fallos else 'FALLA'}")
        fallos_totales += fallos

    if fallos_totales:
        print(f"\n✗ {len(fallos_totales)} fallos:")
        for fallo in fallos_totales:
            print(f"  - {fallo}")
        return 1
    print("\n✓ Todo en orden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
