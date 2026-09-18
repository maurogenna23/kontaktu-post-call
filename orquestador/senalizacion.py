"""Clasificación a partir de la telefonía: los casos que no necesitan leer la conversación.

casos.md: los casos 4, 5, 6, 11 y 13 salen de `sip_status_code` y `amd`. Un `5xx` o un
`machine-ivr` no encaja en ninguno y es `otro`. Un `uncertain` del detector se trata como persona.
"""

from orquestador.catalogo import Clasificacion
from orquestador.evento import Telefonia

_SIN_RESPUESTA = frozenset({408, 480})
_BUZON = frozenset({"machine-vm", "machine-unavailable"})

# Decisión: confianza fija por origen de la señal. El código SIP es un dato del operador; el
# detector de buzón no da confianza numérica, y la heurística sobre el saludo es la más débil.
_CONFIANZA_SIP = 0.97
_CONFIANZA_DETECTOR = 0.9
_CONFIANZA_HEURISTICA = 0.7


def clasificar_por_senalizacion(telefonia: Telefonia, hay_conversacion: bool) -> Clasificacion | None:
    """La clasificación si la telefonía la decide; None si atendió una persona y hay que leer."""
    codigo, texto = telefonia.sip_status_code, telefonia.sip_status
    if codigo == 486:
        return Clasificacion(
            etiqueta="ocupado", motivo=f"486 {texto}: la línea comunica", confianza=_CONFIANZA_SIP
        )
    if codigo == 603:
        return Clasificacion(
            etiqueta="rechazada",
            motivo=f"603 {texto}: rechazo activo antes de descolgar",
            confianza=_CONFIANZA_SIP,
        )
    if codigo in _SIN_RESPUESTA:
        return Clasificacion(
            etiqueta="sin_respuesta", motivo=f"{codigo} {texto}: nadie descolgó", confianza=_CONFIANZA_SIP
        )
    if 500 <= codigo <= 599:
        return Clasificacion(
            etiqueta="otro",
            motivo=f"{codigo} {texto}: fallo del trunk antes de conectar",
            confianza=_CONFIANZA_SIP,
        )
    if codigo != 200:
        return Clasificacion(
            etiqueta="otro", motivo=f"código SIP {codigo} sin caso en el catálogo", confianza=_CONFIANZA_SIP
        )

    amd = telefonia.amd
    if amd.result in _BUZON:
        heuristica = amd.source == "heuristic_regex"
        return Clasificacion(
            etiqueta="buzon",
            motivo=f"contestó un buzón de voz ({amd.result}, {amd.source})",
            confianza=_CONFIANZA_HEURISTICA if heuristica else _CONFIANZA_DETECTOR,
        )
    if amd.result == "machine-ivr":
        return Clasificacion(
            etiqueta="otro",
            motivo="contestó una centralita automática (machine-ivr)",
            confianza=_CONFIANZA_DETECTOR,
        )
    if not hay_conversacion:
        # Decisión: descolgó una persona pero no se dijo nada; no hay nada que leer ni caso que
        # encaje, así que es otro y una persona lo revisa (N4).
        return Clasificacion(
            etiqueta="otro", motivo="descolgaron pero no hubo conversación", confianza=_CONFIANZA_DETECTOR
        )
    return None
