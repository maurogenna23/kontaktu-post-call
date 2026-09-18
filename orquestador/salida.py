"""Escritura de salida/decisiones.jsonl y salida/ordenes.jsonl (ficheros de append)."""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from orquestador.catalogo import Etiqueta
from orquestador.ordenes import Orden


class LineaDecision(BaseModel):
    """Una línea de salida/decisiones.jsonl (esquemas/decision.schema.json)."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    call_id: str | None
    etiqueta: Etiqueta
    motivo: str
    confianza: float
    ordenes: list[str]


class Salida:
    """Los dos ficheros de salida del contrato, en el directorio indicado."""

    def __init__(self, directorio: Path) -> None:
        self._decisiones = directorio / "decisiones.jsonl"
        self._ordenes = directorio / "ordenes.jsonl"

    def escribir(self, ordenes: list[Orden], decision: LineaDecision) -> None:
        """Añade las órdenes que aún no se habían emitido y, después, la línea de decisión.

        R5: una orden cuya idempotency_key ya está en ordenes.jsonl no se vuelve a escribir. Cubre
        el caso de un proceso que se cayó después de escribir órdenes y antes de marcar el hecho
        como procesado: al reentregarse, no duplica nada.
        """
        self._ordenes.parent.mkdir(parents=True, exist_ok=True)
        emitidas = self._claves_emitidas()
        nuevas = [orden for orden in ordenes if orden.idempotency_key not in emitidas]
        self._anadir(self._ordenes, [orden.model_dump(mode="json") for orden in nuevas])
        self._anadir(self._decisiones, [decision.model_dump(mode="json")])

    def _claves_emitidas(self) -> set[str]:
        if not self._ordenes.exists():
            return set()
        with self._ordenes.open(encoding="utf-8") as fichero:
            return {json.loads(linea)["idempotency_key"] for linea in fichero if linea.strip()}

    @staticmethod
    def _anadir(ruta: Path, filas: list[dict[str, object]]) -> None:
        if not filas:
            return
        with ruta.open("a", encoding="utf-8") as fichero:
            for fila in filas:
                fichero.write(json.dumps(fila, ensure_ascii=False) + "\n")
