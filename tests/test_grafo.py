"""El grafo completo, con persistencia en memoria y un clasificador falso en lugar del LLM.

Prueba lo que las reglas puras no ven: por qué nodos pasa cada evento, que una reentrega no emita
nada, y que un fallo del modelo termine en el error_handler y el evento se procese igual (R8).
"""

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, cast, get_args, get_type_hints

import httpx2
import openai
import pytest
from conftest import evento
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import BaseModel

from orquestador.clasificador import EtiquetaConversacion, SalidaModelo
from orquestador.config import Campana
from orquestador.evento import Evento
from orquestador.grafo import (
    MODELOS_EN_ESTADO,
    Dependencias,
    Estado,
    construir_grafo,
    id_del_hecho,
    serializador,
)
from orquestador.salida import Salida


class ClasificadorFalso:
    """Devuelve siempre la misma respuesta, o lanza la excepción indicada, y cuenta las llamadas."""

    def __init__(self, respuesta: SalidaModelo | Exception) -> None:
        self._respuesta = respuesta
        self.llamadas = 0

    def __call__(self, _evento: Evento) -> SalidaModelo:
        self.llamadas += 1
        if isinstance(self._respuesta, Exception):
            raise self._respuesta
        return self._respuesta


def respuesta(etiqueta: EtiquetaConversacion, callback: date | None = None) -> SalidaModelo:
    return SalidaModelo(
        etiqueta=etiqueta, motivo="motivo del modelo", confianza=0.9, evidencia="frase",
        callback_fecha=callback.isoformat() if callback else None,
        callback_hora="18:00" if callback else None,
        rechaza_whatsapp=False, email=None, nota_contexto=None,
    )  # fmt: skip


class Sistema:
    """El grafo compilado con un checkpointer y un store que duran todo el test, como el SQLite.

    El checkpointer usa el serializador de producción.
    """

    def __init__(self, campana: Campana, clasificador: ClasificadorFalso, directorio: Path) -> None:
        self._grafo = construir_grafo(InMemorySaver(serde=serializador()), InMemoryStore())
        self._dependencias = Dependencias(
            campana=campana, clasificador=clasificador, salida=Salida(directorio)
        )
        self._directorio = directorio

    def nuevo_proceso(self, clasificador: ClasificadorFalso) -> None:
        """Otro run.py sobre la misma persistencia, por ejemplo tras corregir la configuración."""
        self._dependencias = replace(self._dependencias, clasificador=clasificador)

    def procesar(self, nombre: str) -> list[str]:
        """Procesa un evento de ejemplo y devuelve los nodos por los que pasó, en orden."""
        return self.procesar_evento(evento(nombre))

    def procesar_evento(self, ev: Evento) -> list[str]:
        partes = self._grafo.stream(
            {"evento": ev},
            {"configurable": {"thread_id": id_del_hecho(ev)}},
            context=self._dependencias,
            stream_mode="updates",
            version="v2",
        )
        # Con stream_mode="updates", cada parte es {nodo: lo que devolvió}.
        return [nodo for parte in partes for nodo in cast(dict[str, Any], parte["data"])]

    def decision(self, event_id: str) -> dict[str, Any]:
        return next(linea for linea in self._leer("decisiones.jsonl") if linea["event_id"] == event_id)

    def operaciones(self, event_id: str) -> list[str]:
        return [o["operacion"] for o in self._leer("ordenes.jsonl") if o["event_id"] == event_id]

    def cuerpo(self, event_id: str, operacion: str) -> dict[str, Any]:
        ordenes = self._leer("ordenes.jsonl")
        return next(o["cuerpo"] for o in ordenes if o["event_id"] == event_id and o["operacion"] == operacion)

    def _leer(self, nombre: str) -> list[dict[str, Any]]:
        ruta = self._directorio / nombre
        return [json.loads(linea) for linea in ruta.read_text().splitlines()] if ruta.exists() else []


def error_de_conexion() -> openai.APIConnectionError:
    return openai.APIConnectionError(
        request=httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
    )


def test_lo_que_decide_la_telefonia_no_llama_al_modelo(campana: Campana, tmp_path: Path) -> None:
    modelo = ClasificadorFalso(respuesta("otro"))
    sistema = Sistema(campana, modelo, tmp_path)
    assert sistema.procesar("02-call-ended-tomas.json") == [
        "admitir",
        "clasificar_senalizacion",
        "decidir",
        "emitir",
    ]
    assert modelo.llamadas == 0


def test_una_conversacion_pasa_por_el_modelo(campana: Campana, tmp_path: Path) -> None:
    modelo = ClasificadorFalso(respuesta("persona_equivocada"))
    sistema = Sistema(campana, modelo, tmp_path)
    nodos = sistema.procesar("03-call-ended-elena.json")
    assert nodos == ["admitir", "clasificar_senalizacion", "clasificar_conversacion", "decidir", "emitir"]
    assert sistema.decision("evt_03")["etiqueta"] == "persona_equivocada"


def test_un_error_no_transitorio_no_se_reintenta_y_el_evento_queda_como_otro(
    campana: Campana, tmp_path: Path
) -> None:
    modelo = ClasificadorFalso(ValueError("respuesta imposible de interpretar"))
    sistema = Sistema(campana, modelo, tmp_path)
    nodos = sistema.procesar("03-call-ended-elena.json")
    assert modelo.llamadas == 1
    assert nodos.count("decidir") == 1 and nodos[-1] == "emitir"
    assert "__error_handler__clasificar_conversacion" in nodos
    decision = sistema.decision("evt_03")
    assert (decision["etiqueta"], decision["confianza"]) == ("otro", 0.0)
    assert sistema.operaciones("evt_03") == ["cerrar_llamada", "crear_tarea"]
    assert sistema.cuerpo("evt_03", "crear_tarea")["tipo"] == "revisar_llamada"  # N4


def test_un_error_transitorio_se_reintenta_tres_veces_antes_del_error_handler(
    campana: Campana, tmp_path: Path
) -> None:
    modelo = ClasificadorFalso(error_de_conexion())
    sistema = Sistema(campana, modelo, tmp_path)
    sistema.procesar("03-call-ended-elena.json")
    assert modelo.llamadas == 3
    assert sistema.decision("evt_03")["etiqueta"] == "otro"


def test_con_cita_creada_y_el_modelo_caido_la_visita_sigue_reservada(
    campana: Campana, tmp_path: Path
) -> None:
    sistema = Sistema(campana, ClasificadorFalso(ValueError("caído")), tmp_path)
    sistema.procesar("07-call-ended-laura.json")
    assert sistema.decision("evt_07")["etiqueta"] == "visita_reservada"
    assert sistema.cuerpo("evt_07", "crear_tarea")["tipo"] == "confirmar_visita_direccion"


def test_una_reentrega_repite_la_etiqueta_sin_ordenes_ni_modelo(campana: Campana, tmp_path: Path) -> None:
    modelo = ClasificadorFalso(respuesta("callback", callback=date(2026, 9, 16)))
    sistema = Sistema(campana, modelo, tmp_path)
    sistema.procesar("09-call-ended-javier.json")
    nodos = sistema.procesar("15-call-ended-javier-reentrega.json")
    assert nodos == ["admitir", "emitir"]
    assert modelo.llamadas == 1
    assert sistema.decision("evt_15")["etiqueta"] == "callback"
    assert sistema.operaciones("evt_15") == []


def test_un_evento_de_otra_organizacion_no_emite_nada(campana: Campana, tmp_path: Path) -> None:
    sistema = Sistema(campana, ClasificadorFalso(respuesta("otro")), tmp_path)
    assert sistema.procesar("16-call-ended-alberto.json") == ["admitir", "emitir"]
    assert sistema.decision("evt_16")["etiqueta"] == "no_aplica"
    assert sistema.operaciones("evt_16") == []


def error_de_la_api(clase: type[openai.APIStatusError], estado: int, codigo: str) -> openai.APIStatusError:
    peticion = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
    return clase(codigo, response=httpx2.Response(estado, request=peticion), body={"code": codigo})


@pytest.mark.parametrize(
    "error",
    [
        error_de_la_api(openai.AuthenticationError, 401, "invalid_api_key"),
        error_de_la_api(openai.NotFoundError, 404, "model_not_found"),
        error_de_la_api(openai.RateLimitError, 429, "insufficient_quota"),
        openai.OpenAIError("The api_key client option must be set"),
    ],
    ids=["clave inválida", "modelo inexistente", "sin saldo", "sin clave"],
)
def test_un_error_de_configuracion_detiene_el_proceso_sin_escribir_nada(
    error: Exception, campana: Campana, tmp_path: Path
) -> None:
    modelo = ClasificadorFalso(error)
    sistema = Sistema(campana, modelo, tmp_path)
    with pytest.raises(type(error)):
        sistema.procesar("03-call-ended-elena.json")
    assert modelo.llamadas == 1  # sin reintentos: no se arregla solo
    assert not (tmp_path / "decisiones.jsonl").exists() and not (tmp_path / "ordenes.jsonl").exists()


def test_tras_un_error_de_configuracion_el_evento_se_reprocesa_al_corregirla(
    campana: Campana, tmp_path: Path
) -> None:
    """El hecho no queda como procesado: al volver a correrlo, no cae como reentrega."""
    clave_invalida = error_de_la_api(openai.AuthenticationError, 401, "invalid_api_key")
    sistema = Sistema(campana, ClasificadorFalso(clave_invalida), tmp_path)
    with pytest.raises(openai.AuthenticationError):
        sistema.procesar("03-call-ended-elena.json")
    assert not (tmp_path / "decisiones.jsonl").exists() and not (tmp_path / "ordenes.jsonl").exists()

    sistema.nuevo_proceso(ClasificadorFalso(respuesta("persona_equivocada")))
    nodos = sistema.procesar("03-call-ended-elena.json")
    assert nodos == ["admitir", "clasificar_senalizacion", "clasificar_conversacion", "decidir", "emitir"]
    assert sistema.decision("evt_03")["etiqueta"] == "persona_equivocada"
    assert sistema.operaciones("evt_03") == ["cerrar_llamada", "crear_tarea"]


def copia(nombre: str, event_id: str, organizacion: str | None = None) -> Evento:
    """Otra entrega del mismo hecho (misma idempotency_key), opcionalmente de otra organización."""
    original = evento(nombre)
    return original.model_copy(
        update={"event_id": event_id, "organization_id": organizacion or original.organization_id}
    )


def test_un_evento_ajeno_con_la_misma_clave_no_hace_pasar_el_nuestro_por_reentrega(
    campana: Campana, tmp_path: Path
) -> None:
    sistema = Sistema(campana, ClasificadorFalso(respuesta("otro")), tmp_path)
    sistema.procesar_evento(copia("02-call-ended-tomas.json", "evt_ajeno", organizacion="org_demo_b"))
    nodos = sistema.procesar("02-call-ended-tomas.json")
    assert nodos == ["admitir", "clasificar_senalizacion", "decidir", "emitir"]
    assert sistema.decision("evt_02")["etiqueta"] == "ocupado"
    assert sistema.operaciones("evt_02") == ["cerrar_llamada", "programar_llamada"]
    assert sistema.operaciones("evt_ajeno") == []


def test_un_evento_ajeno_no_pisa_la_etiqueta_que_repite_la_reentrega_del_nuestro(
    campana: Campana, tmp_path: Path
) -> None:
    sistema = Sistema(campana, ClasificadorFalso(respuesta("otro")), tmp_path)
    sistema.procesar("02-call-ended-tomas.json")
    sistema.procesar_evento(copia("02-call-ended-tomas.json", "evt_ajeno", organizacion="org_demo_b"))
    nodos = sistema.procesar_evento(copia("02-call-ended-tomas.json", "evt_reentrega"))
    assert nodos == ["admitir", "emitir"]
    assert sistema.decision("evt_ajeno")["etiqueta"] == "no_aplica"
    assert sistema.decision("evt_reentrega")["etiqueta"] == "ocupado"
    assert sistema.operaciones("evt_reentrega") == []


def modelos_alcanzables(tipo: object, vistos: set[type[BaseModel]]) -> set[type[BaseModel]]:
    """Los modelos Pydantic que puede contener un valor de este tipo, recorriendo campo a campo."""
    if isinstance(tipo, type) and issubclass(tipo, BaseModel):
        if tipo not in vistos:
            vistos.add(tipo)
            for campo in tipo.model_fields.values():
                modelos_alcanzables(campo.annotation, vistos)
    else:
        for argumento in get_args(tipo):  # uniones, listas, tuplas, Annotated…
            modelos_alcanzables(argumento, vistos)
    return vistos


def test_el_serializador_registra_cada_modelo_que_viaja_en_el_estado() -> None:
    """Un modelo sin registrar vuelve del checkpoint como dict, y solo falla si algo lo usa como
    objeto: los tests de arriba no lo detectan en todos los caminos. Este compara los tipos."""
    en_el_estado: set[type[BaseModel]] = set()
    for tipo in get_type_hints(Estado).values():
        modelos_alcanzables(tipo, en_el_estado)
    assert en_el_estado == set(MODELOS_EN_ESTADO)
