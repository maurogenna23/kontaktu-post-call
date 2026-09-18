"""python run.py <evento.json>

Procesa un evento y añade su decisión y sus órdenes a salida/. Un proceso por evento.
Sale con 0 si procesó el evento y con 1 si no pudo (R8: el siguiente evento se procesa igual).
"""

import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from orquestador.aplicacion import RAIZ, procesar


def main(argumentos: list[str]) -> int:
    if len(argumentos) != 1:
        print("uso: python run.py <evento.json>", file=sys.stderr)
        return 2
    load_dotenv(RAIZ / ".env")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    ruta = Path(argumentos[0])
    try:
        decision = procesar(ruta)
    except Exception:
        logging.getLogger("orquestador").exception("no se pudo procesar %s", ruta)
        return 1
    print(f"{decision.event_id}: {decision.etiqueta} · {len(decision.ordenes)} órdenes · {decision.motivo}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
