"""Compara modelos de OpenAI en la única tarea que hace el LLM: clasificar la conversación.

    uv run python scripts/comparar_modelos.py [modelo ...]

Usa las conversaciones de los dos lotes de verificar_lote.py (las que la telefonía no resuelve),
con su etiqueta esperada, y repite cada clasificación varias veces para medir la estabilidad.
Mide la etiqueta final (tras contrastarla con la cita del CRM), la hora del callback, la latencia
y el coste. Es la base de la elección de MODELO que se explica en el README.
"""

import contextvars
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_core.callbacks import get_usage_metadata_callback

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "scripts"))

from verificar_lote import ESPERADO_EJEMPLO, lote_de_ejemplo, lote_sintetico  # noqa: E402

from orquestador.clasificador import ClasificadorLLM, interpretar  # noqa: E402
from orquestador.config import cargar_campana  # noqa: E402
from orquestador.evento import Evento  # noqa: E402
from orquestador.senalizacion import clasificar_por_senalizacion  # noqa: E402

REPETICIONES = 3
# USD por millón de tokens (entrada, salida), developers.openai.com, 18/09/2026.
PRECIOS = {"gpt-4o-mini": (0.15, 0.60), "gpt-5.6-luna": (0.20, 1.20)}
# Hora de callback esperada (fecha y hora de Madrid) en las conversaciones que la piden.
CALLBACK_ESPERADO = {
    "evt_09": "2026-09-16 18:00",
    "sint_05": "2026-09-15 22:00",
    "sint_08": "2026-09-16 18:00",
}


def conversaciones() -> list[tuple[Evento, str]]:
    sinteticos, esperado_sintetico = lote_sintetico()
    esperado = {eid: e.etiqueta for eid, e in {**ESPERADO_EJEMPLO, **esperado_sintetico}.items()}
    casos, vistos = [], set()
    for crudo in lote_de_ejemplo() + sinteticos:
        evento = Evento.model_validate(crudo)
        necesita_llm = (
            evento.telephony
            and clasificar_por_senalizacion(evento.telephony, bool(evento.transcript)) is None
        )
        if necesita_llm and evento.idempotency_key not in vistos:
            vistos.add(evento.idempotency_key)
            casos.append((evento, esperado[evento.event_id]))
    return casos


def evaluar(modelo: str, casos: list[tuple[Evento, str]]) -> dict[str, Any]:
    clasificador = ClasificadorLLM(modelo, cargar_campana(RAIZ / "config" / "campana.yaml"))

    def una(evento: Evento) -> tuple[str, str | None, float]:
        inicio = time.perf_counter()
        clasificacion, datos = interpretar(clasificador(evento), evento)
        hora = (
            f"{datos.callback_fecha} {datos.callback_hora:%H:%M}"
            if datos.callback_fecha and datos.callback_hora
            else None
        )
        return clasificacion.etiqueta, hora, time.perf_counter() - inicio

    trabajos = [evento for evento, _ in casos for _ in range(REPETICIONES)]
    with get_usage_metadata_callback() as uso, ThreadPoolExecutor(max_workers=6) as grupo:
        # El contador de tokens vive en una variable de contexto: cada hilo recibe una copia.
        futuros = [grupo.submit(contextvars.copy_context().run, una, evento) for evento in trabajos]
        resultados = [futuro.result() for futuro in futuros]
    por_evento: dict[str, list[tuple[str, str | None, float]]] = defaultdict(list)
    for evento, resultado in zip(trabajos, resultados, strict=True):
        por_evento[evento.event_id].append(resultado)

    aciertos, estables, fallos = 0, 0, []
    for evento, esperada in casos:
        respuestas = por_evento[evento.event_id]
        aciertos += sum(etiqueta == esperada for etiqueta, _, _ in respuestas)
        estables += len({etiqueta for etiqueta, _, _ in respuestas}) == 1
        for etiqueta, hora, _ in respuestas:
            if etiqueta != esperada:
                fallos.append(f"{evento.event_id}: {etiqueta} (se esperaba {esperada})")
            elif evento.event_id in CALLBACK_ESPERADO and hora != CALLBACK_ESPERADO[evento.event_id]:
                fallos.append(
                    f"{evento.event_id}: callback {hora} (se esperaba {CALLBACK_ESPERADO[evento.event_id]})"
                )
    entrada = sum(u["input_tokens"] for u in uso.usage_metadata.values())
    salida = sum(u["output_tokens"] for u in uso.usage_metadata.values())
    precio_entrada, precio_salida = PRECIOS.get(modelo, (0.0, 0.0))
    return {
        "aciertos": aciertos,
        "total": len(trabajos),
        "estables": estables,
        "casos": len(casos),
        "latencia": sum(r[2] for r in resultados) / len(resultados),
        "coste_1000": (entrada * precio_entrada + salida * precio_salida) / len(trabajos) * 1000 / 1_000_000,
        "fallos": fallos,
    }


def main() -> int:
    load_dotenv(RAIZ / ".env")
    modelos = sys.argv[1:] or list(PRECIOS)
    casos = conversaciones()
    print(f"{len(casos)} conversaciones, {REPETICIONES} repeticiones por modelo\n")
    print(f"{'modelo':<16}{'aciertos':>12}{'estables':>11}{'latencia':>11}{'USD/1000 llamadas':>20}")
    fallos_por_modelo = {}
    for modelo in modelos:
        r = evaluar(modelo, casos)
        fallos_por_modelo[modelo] = r["fallos"]
        print(
            f"{modelo:<16}{r['aciertos']:>7}/{r['total']:<4}{r['estables']:>6}/{r['casos']:<4}"
            f"{r['latencia']:>9.1f} s{r['coste_1000']:>18.2f}"
        )
    for modelo, fallos in fallos_por_modelo.items():
        if fallos:
            print(f"\n{modelo}:")
            for fallo in fallos:
                print(f"  - {fallo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
