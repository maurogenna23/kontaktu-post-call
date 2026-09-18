from pathlib import Path

import pytest

from orquestador.config import Campana, cargar_campana
from orquestador.evento import Evento, cargar_evento

RAIZ = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def campana() -> Campana:
    return cargar_campana(RAIZ / "config" / "campana.yaml")


def evento(nombre: str) -> Evento:
    return cargar_evento(RAIZ / "eventos" / nombre)
