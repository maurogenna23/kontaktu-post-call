# Catálogo de casos

15 casos y 13 etiquetas (dos etiquetas cubren dos casos cada una). El enum es cerrado: no
inventes valores. Lo que no encaje en ninguno, incluidos `amd.result` `machine-ivr` o un `5xx`,
es `otro` y aplica la regla N4. Un `uncertain` del detector se trata como persona.

**Ninguna etiqueta viene dada en el evento.** Los casos 4, 5, 6, 11 y 13 salen de
`sip_status_code` y `amd`, y el 1 de que la cita exista. Los demás solo se deciden leyendo la
conversación.

**Los casos marcados ⚠ no tienen evento de ejemplo en `eventos/`.** Están descritos aquí porque
aparecen en el lote con el que evaluamos. Impleméntalos igual.

---

| # | Etiqueta | Cómo se reconoce | Qué se espera |
|---|---|---|---|
| 1 | `visita_reservada` | la llamada termina con cita creada; `agent_outcome.appointment` presente | tarea `confirmar_visita_direccion` al comercial, vence antes de la visita |
| 2 | `documentacion_enviada` | el agente envió el enlace durante la llamada y el lead aceptó recibirlo por WhatsApp | dos `programar_recordatorio`: uno al lead (`whatsapp_lead`, plantilla `recordatorio_documentacion`) y otro al comercial (`tarea_comercial`, tipo `llamar_a_mano`), con los plazos de `campana.yaml`. Los dos se cancelan si el lead responde |
| 3 | `callback` | el lead pide que se le llame en otro momento | llamada programada para ese momento |
| 4 | `sin_respuesta` | `sip_status_code` 408 o 480, sin transcripción | llamada programada, respetando la separación mínima entre reintentos |
| 5 | `ocupado` | `sip_status_code` 486 | llamada programada, reintento corto |
| 6 | `buzon` | `amd.result` es `machine-vm` o `machine-unavailable` | depende de los intentos consumidos: si quedan, otra llamada con la separación mínima; si no, canal de respaldo |
| 7 | `cortada` | la llamada se corta a mitad de la cualificación, sin despedida. La telefonía no lo distingue de un cuelgue normal: se ve en el transcript | llamada programada con los plazos de cortada, lo antes posible dentro de la ventana, arrastrando lo ya recogido en `nota_contexto` |
| 8 | `visita_sin_confirmar` | se acordó visita de palabra y la llamada cayó antes de crearla (`agent_outcome.appointment` ausente) | llamada programada con los plazos de cortada. **No se reserva la visita** (regla N5) |
| 9 | `persona_equivocada` | quien contesta no es el lead y no se sabe cuándo localizarlo | tarea `verificar_telefono`. Sin reintentos |
| 10 | `no_contactar` | el lead pide explícitamente no ser contactado | marcar no contactar en `canal: todos`. Ninguna otra orden (regla N2) |
| 11 ⚠ | `rechazada` | `sip_status_code` 603, rechazo activo antes de descolgar | canal de respaldo. Sin reintento por voz |
| 12 ⚠ | `callback` | el lead pide una hora que cae fuera de la ventana de llamadas | llamada en la primera franja válida, más plantilla `aviso_cambio_hora` |
| 13 | `buzon` | `amd.source` es `heuristic_regex` y el saludo es ambiguo | igual que el 6: manda el recuento de intentos, no la certeza de la detección |
| 14 | `documentacion_pendiente` | el lead pide documentación pero rechaza WhatsApp | tarea `enviar_documentacion_email`. Ningún WhatsApp (regla N1) |
| 15 ⚠ | `descartado` | el lead ya compró, ya alquiló o ya no busca | ninguna orden más allá de cerrar la llamada |

---

## Eventos que no son casos

Los tres dejan línea en `decisiones.jsonl`. Los dos primeros con etiqueta `no_aplica`; la
reentrega repite la etiqueta que ya tenía la llamada.

| Situación | Etiqueta | Órdenes |
|---|---|---|
| El lead responde al WhatsApp de documentación (`message.received`) | `no_aplica` | cancelar los recordatorios pendientes de ese lead |
| Un evento con otra `organization_id` | `no_aplica` | ninguna, ni siquiera `cerrar_llamada` |
| El mismo evento reentregado, con el mismo `idempotency_key` | la de la llamada original | ninguna nueva, tampoco `cerrar_llamada` |

## Catálogos cerrados

**Plantillas de WhatsApp:** `primer_toque_respaldo` · `recordatorio_documentacion` ·
`aviso_cambio_hora`

**Tipos de tarea:** `confirmar_visita_direccion` · `verificar_telefono` ·
`enviar_documentacion_email` · `llamar_a_mano` · `revisar_llamada`

**Estado de cola** que lleva `cerrar_llamada`, fijado por la etiqueta. No hay que decidirlo:

| Etiqueta | `status` |
|---|---|
| `visita_reservada` | `successful` |
| `documentacion_enviada`, `documentacion_pendiente` | `completed` |
| `callback` | `callback_requested` |
| `sin_respuesta`, `ocupado`, `buzon` | `no_answer` |
| `cortada`, `visita_sin_confirmar`, `otro` | `needs_review` |
| `persona_equivocada` | `failed` |
| `no_contactar` | `dnc` |
| `rechazada` | `refused` |
| `descartado` | `skipped` |

## Plazos de las tareas

`vence_el` es a cualquier hora del día. `confirmar_visita_direccion` vence antes de la visita
menos el margen de `campana.yaml`; el resto, a `vencimiento_por_defecto_dias` días naturales.

## Pares que se parecen y no son iguales

Merece la pena mirarlos antes de escribir código.

| | |
|---|---|
| 4 contra 5 | no contesta y comunica no se reintentan con el mismo plazo |
| 5 contra 11 | comunicando es reintentable; un rechazo activo no |
| 6 contra 13 | misma etiqueta, acción opuesta. Lo que decide es cuántos intentos quedan |
| 9 contra 10 | un número equivocado no es una baja: no ensucies la lista de no contactar |
| 7 contra 8 | la diferencia está en si llegó a acordarse una visita, no en cómo se cortó |
| 3 contra 7 | pedir otra llamada es `callback`; que se corte la línea a mitad es `cortada`. «Ahora no puedo» sin pedir otra llamada no es `callback` |
| 10 contra todo | una baja manda sobre cualquier otra etiqueta, la diga cuando la diga y aunque la conversación siga después con normalidad. «Ya encontré piso y no me llaméis más» es `no_contactar`, no `descartado`: las dos cierran el lead, pero solo una impide volver a llamarle |
| 15 contra 7 | que el lead cuelgue seco no lo convierte en `cortada` si ya había dicho que no busca |
