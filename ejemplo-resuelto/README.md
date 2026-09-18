# Ejemplo resuelto

El evento más simple del lote, con su salida completa. Sirve para fijar el formato exacto de
`decisiones.jsonl` y `ordenes.jsonl`. **No dice nada sobre cómo construir el sistema.**

## El evento

`evento.json` es una copia de `eventos/02-call-ended-tomas.json`.

Lo que hay que leer para decidirlo:

| Señal | Valor | Qué implica |
|---|---|---|
| `telephony.sip_status_code` | `486` | la línea comunica |
| `telephony.disconnect_reason` | `USER_REJECTED` | así expone LiveKit un 486; no es un rechazo del lead |
| `telephony.answered_at` | `null` | nunca descolgaron |
| `telephony.duration_seconds` | `0` | no hubo llamada |
| `transcript` | `[]` | no hay nada que clasificar por texto |
| `agent_outcome` | sin cita y sin notas | el agente no llegó a hablar con nadie |

La decisión sale entera de la señalización. Llamar al modelo aquí es gastar dinero y latencia
para nada.

## La salida

Dos órdenes. `cerrar_llamada` se espera en todos los `call.ended`. `programar_llamada` sale de
`reintentos.ocupado_minutos_min` y `ocupado_minutos_max` de `config/campana.yaml`: entre 30 y 90
minutos después del evento, y dentro de la ventana de llamadas.

El evento ocurre el martes 15 a las 10:31. La llamada se programa a las 11:31, que cae dentro de
la ventana de ese día (10:00 a 20:00).

## Sobre `idempotency_key`

`lk-out-0306:cerrar_llamada` es `<idempotency_key del evento>:<operacion>`. Si el mismo evento se
reentrega, la clave vuelve a ser la misma y la orden no debe repetirse.
