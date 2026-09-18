# Prompts al asistente de programación

Los mensajes que le envié a Claude Code durante el reto, en orden y sin editar, desde que descargué
el zip (12:09) hasta el cierre. Las instrucciones permanentes están en `CLAUDE.md`.

A partir del hueco 17, las propuestas de Claude Code pasaron por una segunda sesión que hizo de
revisor: comprobaba cada diagnóstico contra el código y la especificación, y el feedback que
ves aquí es el resultado de esa revisión.

## 12:09

> @"/Users/maurogenna/Downloads/reto-kontaktu-v2.zip"

## 12:16

> Perfecto, continua

## 12:51

> Excelente, comparaste los modelos con LangSmith?

## 12:53

> Primero vayamos a lo importante, necesito que dividamos en etapas el proceso del proyecto, así como hicimos con el anterior. En que etapa estamos ahora y con que podemos continuar

## 12:54

> Claro, la etapa 7 son varias cosas y es la mas importante, dividamos en pasos para ver como procedemos

## 12:55

> si

## 12:59

> la propuesta del 7.3 arregla los huecos anteriores?

## 13:01

> Exacto, en vez de documentar los huecos veamos de corregirlo, cada hueco que encuentres anotalo y vemos de corregirlo luego, a menos de que ese hueco nos trabe para continuar

## 13:07

> Perfecto, y ademas utilicemos el modelo de GPT-5.6 Luna que nos dió buenos resultados

## 13:16

> sí, seguí con el 7.5

## 13:23

> Antes de seguir al cierre, me gustaría dar una ultima revisada a todo lo que tenemos del proyecto. Vayamos evaluando el paso a paso de los requerimientos para corroborrar que esté todo en orden. Necesito que seas minusioso durante este proceso ya que es el analisis para poder dar el cierre final

## 13:29

> Excelente, buena revisión los dos huecos encontrados. Esperemos a que termine el proceso de revisión y cuando tengamos todos los huecos los revisamos para poder corregirlos

## 13:32

> Perfecto

## 13:52

> Vayamos bloque por bloque. Empecemos por el A y vamos desglosando cada uno. Del 14 al 21 me dices de corregir todos, necesito entender mejor el criterio que has tomado en cada uno y vemos el porque. Empecemos por el 14 y vamos avanzando al siguiente cuando terminemos, te parece bien?

## 13:54

> Dale perfecto

## 13:56

> Dale, aplicalo y pasamos al 16

## 14:00

> Dale, aplicalo y pasamos al 17

## 14:11

> Adelante con el hueco 17, con estos ajustes: (1) cubrí la hora sin fecha y la fecha pasada con la regla "próxima vez que llegue esa hora"; (2) con día sin hora, mandá el aviso si la llamada cae en otro día, y en hora_pedida describí solo el día; (3) la franja horaria se traduce siempre al comienzo de la franja, con horas fijas en el prompt; (4) agregá al prompt "nunca un momento anterior al instante de referencia" y cambiá "si no dijo un momento concreto" por "si no dijo ni día ni hora"; (5) adaptá comparar_modelos.py para comparar fecha y hora por separado y corré la comparación completa y verificar_lote.py; (6) actualizá el Decisión: y el README.
>
> Te parecen bien estos ajustes?

## 14:23

> Dale con el hueco 18, pero el diagnóstico está incompleto: el orden inverso también falla.
>
> Lo probé con el grafo real y el evento 02 (486, sin LLM), más una copia con organization_id = org_demo_b:
> - Ajeno primero y después el nuestro: el nuestro sale no_aplica, sin órdenes y sin cerrar_llamada. Es lo que describiste.
> - Nuestro, ajeno y después una reentrega del nuestro: el ajeno pasa el control de organización, pero emitir igual guarda su decisión en el thread compartido y pisa la nuestra. La reentrega repite no_aplica en lugar de "ocupado". casos.md dice que la reentrega repite la etiqueta que ya tenía la llamada.
>
> Tampoco creo que sea tan improbable: para armar un evento de otra organización, lo más fácil es copiar uno nuestro y cambiarle solo organization_id, y así se conserva la idempotency_key.
>
> El arreglo que proponés está bien y cubre los dos órdenes. Ajustes:
>
> 1. id_del_hecho(evento) va en grafo.py, al lado de _clave_lead, para que las dos claves de persistencia estén juntas. La usan aplicacion.py y test_grafo.py. Poné un comentario Decisión: la especificación dice que idempotency_key identifica el hecho, y nosotros lo acotamos por organización.
>
> 2. Tests del grafo con las dos secuencias:
>    a) ajeno(X) → nuestro(X): el nuestro se procesa completo, con cerrar_llamada.
>    b) nuestro(X) → ajeno(X) → reentrega del nuestro(X): la reentrega repite la etiqueta original, sin órdenes.
>
> 3. Lote sintético: las dos secuencias, con eventos que no pasen por el LLM. Antes arreglá comprobar() en verificar_lote.py: hechos_vistos usa solo idempotency_key, así que en la secuencia (a) tomaría nuestro evento como reentrega y daría fallo con el código ya arreglado. La clave tiene que ser (organization_id, idempotency_key). Los resultados esperados salen de leer casos.md, no de lo que dé la corrida.
>
> 4. Documentación: la docstring de grafo.py, CLAUDE.md y el README dicen thread_id = idempotency_key. Actualizalos.
>
> 5. Después del cambio, borrá salida/ y estado/ antes de correr nada localmente: con los nombres de thread nuevos, el estado viejo haría pasar las reentregas por hechos nuevos.
>
> Aparte, no lo mezcles con este cambio: test_grafo.py compila el grafo con InMemorySaver() sin el serializador de producción (allowed_msgpack_modules solo se arma en aplicacion.py) y sale el aviso "Deserializing unregistered type". Es el mismo problema de una sola fuente de verdad. Anotalo como hueco siguiente.
>
> Cuando termines, pasame el diff y el resultado de ruff, mypy, pytest y verificar_lote.py.

## 14:28

> Dale, actualizá las cifras, commiteá y pasamos al 19

## 14:31

> Hueco 19: el diagnóstico está bien, lo reproduje con el evento 11 y la tarea de N4 se pierde. Un detalle: pasa igual si el lead rechaza WhatsApp en esa misma llamada, no solo antes, porque la memoria se actualiza antes de decidir.
>
> Estoy de acuerdo con dejar de descartar en silencio. Pero "no existe ningún duplicado legítimo" vale para el código, no para los datos persistidos. El README reconoce que no hay atomicidad entre la salida y SQLite: si el proceso muere entre el store.put y el checkpoint, la reentrega vuelve a pasar por decidir y agrega a la memoria los mismos reminder_id otra vez. Hoy eso se absorbe sin que se note. Con el error, cada WhatsApp o baja de ese lead fallaría para siempre, porque la memoria nunca se llega a limpiar.
>
> Tampoco veo en el plan un test del error en sí, y la propuesta dice que tiene que verse en los tests.
>
> Dos tareas me parece bien, pero no por el motivo que das: fundirlas no exige modificar una orden ya emitida, alcanza con decidir antes de emitir. El argumento bueno es que cada tarea corresponde a una regla. El costo es que el comercial vería dos "Revisar la llamada" iguales, de la misma llamada y con el mismo vencimiento, y no sabría cuál es cuál.
>
> Detalles: respaldo_enviado solo se marca cuando sale el WhatsApp, no cuando se crea la tarea. Lo que evita el duplicado dentro de un evento es que respaldo() se ejecuta una sola vez por evento. Y con el error, el None de _emitir y el if de recordatorio() quedan muertos.

## 14:33

> Dale, aplicá el plan con el punto 5 incluido

## 14:40

> Hueco 20: de acuerdo con el resguardo, pero hay dos cosas que no cubre.
>
> La primera: el modelo puede devolver cadena vacía en lugar de null. Lo probé con el evento 04: con nota "" la orden sale con "nota_contexto": "", y con "   " sale con los espacios. Si el respaldo solo mira None, eso se escapa.
>
> La segunda: la verificación no prueba lo que dice. verificar_lote.py no mira nota_contexto en ningún evento, así que "la nota del modelo sigue ganando" daría verde igual aunque se perdiera.
>
> Además, falta una regla explícita para pasar las notas a texto. slots_snapshot es libre y en los ejemplos trae booleanos (docs_enviadas: false), fechas ISO (visita_acordada_verbal), emails y callback_when_raw. Si traducís claves conocidas, como sugiere "operación alquiler", las notas que no conozcamos del lote de evaluación se pierden. Si volcás todo, salen claves crudas. Y hay que definir si un false cuenta como vacío.
>
> Sobre dónde va: interpretar() ya es el lugar donde se contrasta lo que dice el modelo con los hechos del evento (la cita pisa la etiqueta). Completar ahí la nota que falta la resuelve una sola vez, en vez de en tres ramas de reglas.py. No digo que sea obligatorio, pero quiero que lo decidas a propósito.
>
> El prefijo "Notas del agente:" me parece bien: las notas pueden estar obsoletas y el agente siguiente tiene que saber que no están contrastadas con la transcripción.

## 14:41

> Dale, aplicalo y pasamos al 21

## 14:48

> Hueco 21: sí, aplicalo. Tres detalles.
>
> Un language vacío ("") tiene que caer en "es" igual que si no viniera. Es lo mismo que pasaba con la nota vacía.
>
> El comentario sobre los textos en español tiene que decir la consecuencia: a un lead en "ca" le llega la plantilla en catalán con "jueves 17 a las 10:00" adentro. Se puede aceptar porque los parámetros no se comparan, pero que quede dicho. Si preferís evitarlo sin traducir, un formato neutro en esas fechas alcanza.
>
> Y el título promete más de lo que cubre: el recordatorio al lead (programar_recordatorio con recordatorio_documentacion) también es un WhatsApp y su cuerpo no tiene idioma en el OpenAPI, así que ese sigue saliendo en lo que decida el CRM. Dejalo escrito.
>
> La especificación no conecta language con idioma, así que va con su Decisión: y su línea en el README.

## 14:54

> Hueco 22: de acuerdo con revertir, pero ajustá el argumento, porque es la segunda vez que cambiamos esta decisión y el Decisión: tiene que aguantar que alguien lea el historial.
>
> Te falta el argumento más fuerte: el OpenAPI escribe la restricción de la ventana solo para programar_llamada.no_antes_de ("Tiene que caer dentro de la ventana de llamadas"), y programar_recordatorio.cuando es un date-time sin ninguna restricción. Eso pesa más que el comentario de campana.yaml.
>
> El punto 1 exagera: "horas naturales, no hábiles" dice cómo contar las 48 horas, no dónde tienen que caer. Sirve de apoyo, pero no demuestra que el autor anticipó la confusión.
>
> Y falta responder el contraargumento: casos.md exime explícitamente a las tareas ("vence_el es a cualquier hora del día") y no dice nada de los recordatorios, así que alguien puede leer que R3 aplica al resto. La respuesta es tu punto 2 más el OpenAPI, pero tiene que estar en el Decisión:. No es un caso cerrado, es el más sólido.
>
> El argumento del evaluador funciona para los dos lados: uno que aplique la ventana marcaría mal la versión revertida igual.
>
> Detalles: el test test_el_recordatorio_al_lead_sale_dentro_de_la_ventana y el comentario de sint_33 ("cae el lunes, no el domingo") quedarían diciendo lo contrario de lo que prueban.

## 15:02

> Hueco 23: no estoy de acuerdo, prefiero revertir.
>
> Usás "el mismo rigor del 22", pero en el 22 ganó el texto más específico: la restricción escrita para no_antes_de le ganó a la R3 genérica. Acá lo más específico sobre una baja es el caso 10: "Ninguna otra orden". El enunciado desempata igual: gana el específico sobre el general.
>
> La cita del OpenAPI está cortada. Sigue así: "cuando llegue el evento (regla R7)". Habla del mecanismo de R7, no de las bajas. R7 nombra un solo disparador (el message.received), y el enum de cancelar_si solo prevé lead_responde. El contrato ata la cancelación a que el lead responda.
>
> La premisa de que el recordatorio le llegaría igual no está demostrada. Tampoco nada dice que el CRM ignore su propia lista de no contactar con canal "todos": para eso existe.
>
> Y "cancelar no es una orden saliente" responde a N2, no al caso 10, que dice "ninguna otra orden", sin "saliente".
>
> Al revertir, ojo: hoy la baja vacía los recordatorios de la memoria. Tienen que quedar guardados para que R7 los cancele si el lead escribe después. Eso cambia sint_31, sint_32 y el test de la baja con recordatorios.

## 15:12

> Hueco 24: sí, aplicalo. Dos ajustes al Decisión:.
>
> "Conteste alguien o no" define qué suma al contador, no a qué llamadas les aplica el tope. Hay otra lectura que no deja sobrando la frase: toda llamada cuenta, pero el tope solo frena reintentos sin contacto, y la frase igual importaría para el buzón del caso 6 después de un callback. Lo que cierra el tema es N3 sin excepciones más el "por lead" de campana.yaml. Apoyá el comentario ahí.
>
> Y al contraargumento le falta un dato a favor: el pedido no se pierde. cerrar_llamada sale con status callback_requested y el motivo, así que queda registrado en el CRM aunque no se programe la llamada.

## 15:19

> Huecos 25 a 27: sí, aplicalos. Un ajuste en cada uno.
>
> 25: te falta el argumento del contrato, el mismo método del 22. El OpenAPI describe cancelar_recordatorio como "Cancela un recordatorio pendiente". Eso también contradice "cancelar uno ya enviado sería inofensivo": según el contrato, la operación solo existe para los pendientes. Sacaría esa frase.
>
> 26: escribí el argumento de fondo como regla, porque explica las dos ramas de _callback. Cuando el lead fija el momento, manda lo que pidió. Cuando lo calculamos nosotros (sin hora, o "hoy" sin hora), se aplica la separación general.
>
> 27: el argumento 2 es el pilar de la decisión y ningún test lo cubre. Lo probé con el evento 03: tras un AuthenticationError no se escribe nada, y al volver a correr en el mismo thread con el clasificador funcionando sale persona_equivocada con cerrar_llamada y crear_tarea. Dejalo como test. Y la línea del README va arriba en "Cómo se ejecuta" y dice también el costo en aciertos de gpt-4o-mini (63 de 72, confunde cortada con visita_sin_confirmar), no solo el económico.

## 15:27

> Hueco 28: el punto 1 sí, el punto 2 no sirve. LANGGRAPH_STRICT_MSGPACK solo se consulta cuando no se pasa allowed_msgpack_modules, y se lee una vez, al importar LangGraph (_msgpack.py). Con la lista explícita, un tipo que no está en la lista ya se bloquea.
>
> Además, sacar un modelo de la lista no siempre hace fallar un test. El tipo bloqueado vuelve como dict y solo falla si algo lo usa como objeto. Lo probé con 09 más su reentrega: sin DatosConversacion la reentrega pasa y solo deja un log; sin LineaDecision falla con AttributeError. Así, los tests no protegen la lista.
>
> Y CLAUDE.md busca un mensaje que con lista explícita no aparece nunca: el que sale es "Blocked deserialization of…", no "Deserializing unregistered type".
>
> Hueco 29: de acuerdo con documentarlo. Dos precisiones. En la ventana 1 la reentrega también vuelve a pasar por el LLM y puede decidir distinto: la deduplicación conserva los cuerpos viejos mientras la decisión nueva muestra otra etiqueta, y las operaciones nuevas salen además de las viejas. En la ventana 2 también se vuelven a agregar los mismos reminder_id a la memoria, que es lo del hueco 19.

## 15:38

> Antes del cierre, tres cosas chicas:
>
> 1. AGENTS.md está sin commitear y es una copia vieja de CLAUDE.md (todavía dice "Deserializing unregistered type"). Borrala, que no entre en el repo.
> 2. La línea de Ocupado del README quedó vieja: dice "a los 60 minutos (mitad de 30–90)", pero si ahí la ventana está cerrada va al primer instante válido desde los 30 (19:10 → 19:40). El Decisión: del código ya lo dice.
> 3. Si sobra tiempo: en "Qué dejé fuera", que nota_contexto no se arrastra entre llamadas. Si después de una cortada viene una sin respuesta, la llamada siguiente pierde lo recogido.
>
> Y el "66 de 66" del README tiene que salir de verificar_lote.py en el clon limpio antes del push final.
