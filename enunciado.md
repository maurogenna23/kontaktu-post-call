# Reto técnico · Orquestador post-llamada

**Tope de tiempo: 4 horas.**
**Stack obligatorio: Python + LangGraph.**

---

## 1. Contexto

Kontaktu opera agentes de voz que llaman a los leads de las inmobiliarias. Cada llamada saliente termina
produciendo un evento con la transcripción y la señalización de telefonía.

Buscamos construir un sistema: que clasifique cómo fue
la llamada y decida qué hacer después, emitiendo órdenes contra el CRM.

## 2. Qué construyes

Un programa que recibe **un evento** y emite **una decisión** y **cero o más órdenes**.

```
python run.py eventos/01-call-ended-nuria.json
```

**Contrato de invocación:**

| | |
|---|---|
| Entrada | ruta de un fichero de evento, como único argumento |
| Salida | dos ficheros de append en `salida/`: `decisiones.jsonl` y `ordenes.jsonl` |
| Código de salida | `0` si procesó el evento, distinto de `0` si no pudo |

**Los eventos se entregan de uno en uno, en el orden de `eventos/orden.txt`, y cada invocación es
un proceso nuevo.** Nada permanece en memoria entre eventos. Si tu sistema necesita recordar algo
de un evento anterior, tiene que haberlo persistido.

Para este reto no hay servidor ni red, dispones de todos los eventos en local.

## 3. Entrada

Dos tipos de evento, ambos validados por `esquemas/evento.schema.json`.

| `type` | Cuándo llega | Contiene |
|---|---|---|
| `call.ended` | al terminar una llamada saliente | señalización, transcripción, notas del agente |
| `message.received` | cuando un lead escribe por WhatsApp | teléfono y texto |

Un evento `call.ended` **no lleva el número de intento, ni la etiqueta, ni ningún campo de
desenlace**. El máximo de intentos está en `config/campana.yaml`. Cómo acabó la llamada solo
está en dos sitios: la señalización de telefonía y lo que se dijo. Si nadie descolgó, lo dice
la telefonía. Si descolgaron, hay que leer la transcripción.

**Definiciones que fijan el resultado:**

| | |
|---|---|
| Intento | todo `call.ended`, conteste alguien o no. Una reentrega no cuenta |
| Identidad del lead | `lead.contact_id` |
| Instante de referencia | `occurred_at`. Coincide con `telephony.ended_at` |
| Dos plazos aplicables | gana el específico (ocupado, cortada) sobre el general |
| Ventana `["10:00", "20:00"]` | ambos extremos inclusive |
| Días en `campana.yaml` | naturales salvo que diga `dias_habiles` |

### 3.1 Qué trae `agent_outcome`

Solo lo que el agente de voz dejó anotado mientras hablaba. No es un resultado.

| Campo | Qué es |
|---|---|
| `appointment` | la cita, **solo si el agente llegó a crearla** durante la llamada. Si viene, existe ya en el CRM |
| `slots_snapshot` | notas sueltas de la conversación: operación, zonas, canal consentido, la hora de callback tal cual la dijo el lead… |

Las notas son parciales y opcionales: pueden faltar, venir a medias o quedarse obsoletas si la
conversación siguió después de anotarlas. Ante una discrepancia, manda la transcripción.

### 3.2 Bloque de telefonía

| Campo | Valores |
|---|---|
| `sip_status_code` | `200` contestada · `486` comunica · `603` rechazo activo · `408`/`480` sin respuesta · `5xx` fallo de trunk antes de conectar |
| `sip_status` | texto del operador. Cuando la respuesta la genera Telnyx, añade su propio código al final (`D52`, `D21`…) |
| `disconnect_reason` | `CLIENT_INITIATED` colgó alguien tras conectar · `USER_REJECTED` 486 o 603 · `USER_UNAVAILABLE` 408 o 480 · `SIP_TRUNK_FAILURE` 5xx · `ROOM_DELETED` colgó el agente. Ojo: LiveKit mete el 486 en `USER_REJECTED` aunque no sea un rechazo del lead |
| `hung_up_by` | `callee` · `agent` · `null` cuando no se sabe (una caída de línea a mitad de llamada llega como `CLIENT_INITIATED` sin más pista) |
| `amd.result` | `human` · `machine-vm` · `machine-ivr` · `machine-unavailable` · `uncertain` · `not_run` |
| `amd.source` | `livekit_amd` (clasificador) · `heuristic_regex` (heurística sobre el transcript) · `none` |
| `amd.greeting_transcript` | lo que se oyó al descolgar. La detección no devuelve confianza numérica |

Un buzón de voz **contesta con `200 OK`**. No es un fallo a nivel SIP. La única señal de que es
una máquina está en `amd`.

## 4. Salida

### 4.1 `salida/decisiones.jsonl`

Una línea por evento **recibido**, incluidos los que se rechazan: un evento de otra organización
o una reentrega también dejan su línea, con cero órdenes. Esquema en `esquemas/decision.schema.json`.

```json
{"event_id":"evt_09","call_id":"lk-out-0311","etiqueta":"callback","motivo":"el lead pide que se le llame mañana a las 18:00","confianza":0.92,"ordenes":["ord_a1b2"]}
```

`etiqueta` sale del catálogo cerrado de `casos.md`. `motivo` es texto libre, una frase.
`confianza` entre 0 y 1.

### 4.2 `salida/ordenes.jsonl`

Una línea por orden emitida. Cada orden es la petición HTTP que se habría hecho al CRM.

```json
{"orden_id":"ord_a1b2","event_id":"evt_09","operacion":"programar_llamada","idempotency_key":"lk-out-0311:programar_llamada","cuerpo":{"entry_id":"ce_0304","telefono":"+34600000104","no_antes_de":"2026-09-16T18:00:00+02:00","motivo":"callback solicitado","nota_contexto":"pidió que se le llame mañana a las seis"}}
```

`orden_id` es tuyo: único entre ejecuciones (un hash de la clave de idempotencia vale). Los
identificadores que devuelven las respuestas de ejemplo del OpenAPI son plantillas: genera los
tuyos y persístelos, porque un `cancelar_recordatorio` del miércoles necesita el `reminder_id`
que creaste el martes en otro proceso.

Las siete operaciones, sus cuerpos y sus respuestas están en `esquemas/crm-openapi.yaml`.

**Cómo se «ejecuta» una orden:** tu código escribe la línea en `ordenes.jsonl` y sigue como si
el CRM hubiera respondido lo que el OpenAPI declara para esa operación. Los parámetros de ruta
(`entry_id`, `reminder_id`) van dentro de `cuerpo`.

**Cerrar la llamada es una orden más.** `cerrar_llamada` lleva la etiqueta y el estado de cola
(la tabla de correspondencia está en `casos.md`). Se espera en todo `call.ended` que se procese
por primera vez; no en una reentrega ni en un evento de otra organización.

## 5. Requisitos

| # | Requisito |
|---|---|
| R1 | Clasificar cada `call.ended` con una etiqueta del catálogo, cruzando transcripción y señalización. La etiqueta no viene en el evento: se deduce |
| R2 | Emitir las órdenes que procedan, con sus parámetros correctos |
| R3 | Las fechas se calculan en `Europe/Madrid` y respetan la ventana de llamadas de `campana.yaml` |
| R4 | Contar los intentos por lead. El máximo está en la configuración, no en el evento. Igual que los intentos, sobreviven entre ejecuciones: los recordatorios creados, las bajas registradas y las llamadas ya cortadas |
| R5 | Un evento reentregado con el mismo `idempotency_key` no puede duplicar ninguna orden |
| R6 | Un evento de otra `organization_id` no genera ninguna orden |
| R7 | Al llegar el `message.received` de un lead, tu sistema emite `cancelar_recordatorio` por cada recordatorio pendiente que le hubiera programado. El campo `cancelar_si` de la orden documenta la intención; no cancela nada por sí solo |
| R8 | Un fallo procesando un evento no puede impedir procesar los siguientes |

## 6. Reglas de negocio

| # | Regla |
|---|---|
| N1 | Ni documentación ni respuestas por WhatsApp a un lead que ha rechazado ese canal |
| N2 | Un lead que pide no ser contactado no recibe ninguna orden saliente |
| N3 | Agotados los intentos de voz, el canal de respaldo es el declarado en la configuración |
| N4 | Etiqueta `otro`, o una segunda llamada cortada con el mismo lead (`cortada` o `visita_sin_confirmar`) → tarea `revisar_llamada` con el motivo, además de lo que toque por la etiqueta |
| N5 | Una visita acordada de palabra pero no reservada en el CRM no se reserva después: se vuelve a llamar |

## 7. Restricciones

- Python y LangGraph.
- Modelo de OpenAI a tu elección; declara cuál y por qué.
- Sin red salvo el modelo. Sin base de datos externa: un fichero SQLite local sí, un Postgres o
  un Redis no.
- No modifiques `eventos/`, `config/` ni `esquemas/`.

## 8. Entrega

Repositorio con:

1. El código.
2. **Historial de git intacto**, empezando por un primer commit con el contenido de este zip
   descomprimido y sin tocar.
3. **Los prompts que use tu código, versionados en el repo.** Si además quieres incluir los que
   le diste a tu asistente de programación, mejor.
4. `README.md` de una página: cómo se ejecuta, qué decidiste dejar fuera y por qué, y cómo
   verificaste que hace lo que crees que hace.

Los puntos 2 y 3 son obligatorios.

No pedimos tests ni cobertura. Sí nos interesa cómo compruebas tu propio trabajo; cuéntalo en el
README. Es muy importante la comprensión a bajo nivel del proyecto, la evaluación será revisión exhaustiva de código para entender el manejo de LangGraph del candidato.

## 9. Cómo se evalúa

Ejecutamos tu sistema contra **un segundo lote de eventos que no tienes**, desde cero (sin
estado ni salida del lote de ejemplo). Incluye tipos de caso descritos en `casos.md` sin ejemplo
en `eventos/`, y variantes de los que sí tienes con otros datos, otras horas y otros días.

Después hay una videollamada de 45 minutos para que nos lo enseñes.

Dudas sobre el enunciado: pregúntanos por correo.

## 10. Ficheros

```
enunciado.md              este documento
casos.md                  catálogo de etiquetas y qué se espera en cada caso
esquemas/
  evento.schema.json      entrada
  decision.schema.json    salida
  crm-openapi.yaml        las 7 operaciones del CRM
config/campana.yaml       ventana, zona horaria, intentos, plazos
eventos/                  16 eventos + orden.txt
ejemplo-resuelto/         un caso con su entrada y su salida completa
visor/index.html          abre en el navegador y carga tu carpeta salida/
```
