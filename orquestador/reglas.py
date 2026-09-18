"""Reglas de negocio: qué órdenes emite cada evento (casos.md y reglas N1 a N5 del enunciado).

Funciones puras: reciben el evento, su clasificación, la memoria del lead y la campaña, y
devuelven las órdenes y la memoria actualizada. No escriben nada ni conocen LangGraph.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from orquestador.calendario import (
    a_las,
    dentro_de_ventana,
    describir,
    describir_dia,
    en_zona,
    formatear,
    primer_instante_valido,
    proxima_vez,
    sumar,
    sumar_dias_habiles,
    sumar_dias_naturales,
)
from orquestador.catalogo import (
    ESTADO_COLA,
    ETIQUETAS_CORTADA,
    CanalRecordatorio,
    Clasificacion,
    DatosConversacion,
    Plantilla,
    TipoTarea,
)
from orquestador.config import Campana
from orquestador.evento import Evento, Telefonia
from orquestador.memoria import MemoriaLead, RecordatorioPendiente
from orquestador.ordenes import (
    CancelarRecordatorio,
    CerrarLlamada,
    CrearTarea,
    CuerpoOrden,
    EnviarPlantillaWhatsapp,
    MarcarNoContactar,
    Operacion,
    Orden,
    ProgramarLlamada,
    ProgramarRecordatorio,
    crear_orden,
    huella,
)

_SIN_HABLAR = "no se llegó a hablar con el lead"


@dataclass(frozen=True)
class Plan:
    ordenes: list[Orden]
    memoria: MemoriaLead


def decidir_llamada(
    evento: Evento,
    clasificacion: Clasificacion,
    datos: DatosConversacion,
    memoria: MemoriaLead,
    campana: Campana,
) -> Plan:
    """Órdenes de un call.ended que se procesa por primera vez. Cuenta como un intento más."""
    etiqueta = clasificacion.etiqueta
    if etiqueta == "no_aplica" or evento.telephony is None:
        raise ValueError("decidir_llamada solo acepta un call.ended con etiqueta de llamada")
    plan = _Planificador(evento, campana, memoria.model_copy(update={"intentos": memoria.intentos + 1}))
    plan.cerrar(clasificacion, evento.telephony)

    # N2: una baja manda sobre cualquier otra etiqueta, y un lead dado de baja no recibe ninguna
    # orden saliente. Solo se cierra la llamada y, la primera vez, se registra la baja.
    if etiqueta == "no_contactar" or memoria.no_contactar:
        if not memoria.no_contactar:
            plan.marcar_no_contactar(clasificacion.motivo)
            # Decisión: los recordatorios que le quedaban pendientes le llegarían igual; se cancelan.
            # Cancelar no es una orden saliente, así que no contradice «ninguna otra orden» (caso 10).
            plan.cancelar_recordatorios("el lead pidió no ser contactado", evento.occurred_at)
        return plan.resultado()

    if datos.rechaza_whatsapp or etiqueta == "documentacion_pendiente":
        plan.recordar(rechaza_whatsapp=True)
    if etiqueta in ETIQUETAS_CORTADA:
        plan.recordar(llamadas_cortadas=memoria.llamadas_cortadas + 1)

    ocurrio, reintentos = evento.occurred_at, campana.reintentos
    match etiqueta:
        case "visita_reservada":
            _visita_reservada(plan, evento, campana)
        case "documentacion_enviada":
            _documentacion_enviada(plan, evento, campana)
        case "documentacion_pendiente":
            email = datos.email or evento.agent_outcome.slots_snapshot.get("email_declarado")
            destino = f" a {email}" if email else ""
            plan.tarea(
                "enviar_documentacion_email",
                "Enviar la documentación por email",
                f"Pidió la documentación por email{destino}. Rechazó WhatsApp: no usarlo.",
            )
        case "callback":
            _callback(plan, evento, datos, campana)
        case "sin_respuesta":
            plan.llamar(
                sumar(ocurrio, timedelta(hours=reintentos.separacion_minima_horas), campana),
                "sin respuesta, nuevo intento",
                _SIN_HABLAR,
            )
        case "buzon":
            plan.llamar(
                sumar(ocurrio, timedelta(hours=reintentos.separacion_minima_horas), campana),
                "saltó el buzón de voz, nuevo intento",
                _SIN_HABLAR,
            )
        case "ocupado":
            # Decisión: el reintento corto va a mitad del rango ocupado_minutos_min..max (30..90 → 60
            # min), como en el ejemplo resuelto. Si ahí la ventana está cerrada, el primer instante
            # válido desde el mínimo, para quedarse en el rango si se puede: 19:10 → 19:40, no al día
            # siguiente. Manda sobre la separación general.
            medio = (reintentos.ocupado_minutos_min + reintentos.ocupado_minutos_max) // 2
            preferido = sumar(ocurrio, timedelta(minutes=medio), campana)
            minimo = sumar(ocurrio, timedelta(minutes=reintentos.ocupado_minutos_min), campana)
            plan.llamar(
                preferido if dentro_de_ventana(preferido, campana) else minimo,
                "línea comunicando, reintento corto",
                _SIN_HABLAR,
            )
        case "cortada" | "visita_sin_confirmar":
            # N5: una visita acordada de palabra no se reserva después; se vuelve a llamar.
            # Decisión: lo antes posible (cortada_minutos_min) dentro de la ventana. Si la ventana
            # ya cerró, la primera franja válida aunque supere cortada_horas_max.
            motivo = (
                "visita acordada sin reservar, volver a llamar"
                if etiqueta == "visita_sin_confirmar"
                else "llamada cortada, recuperarla lo antes posible"
            )
            plan.llamar(
                sumar(ocurrio, timedelta(minutes=reintentos.cortada_minutos_min), campana),
                motivo,
                datos.nota_contexto,
            )
        case "persona_equivocada":
            plan.tarea(
                "verificar_telefono",
                "Verificar el teléfono del lead",
                # El motivo del modelo puede terminar o no en punto: se normaliza antes de añadir la frase.
                f"{clasificacion.motivo.rstrip('. ')}. Sin reintentos por voz.",
            )
        case "rechazada":
            plan.respaldo("rechazó la llamada antes de descolgar (603)")
        case "descartado" | "otro":
            pass

    # N4: la etiqueta otro, o la segunda llamada cortada con el mismo lead, abre una revisión.
    if etiqueta == "otro":
        plan.tarea("revisar_llamada", "Revisar la llamada", clasificacion.motivo)
    elif etiqueta in ETIQUETAS_CORTADA and plan.memoria.llamadas_cortadas >= 2:
        plan.tarea(
            "revisar_llamada",
            "Revisar la llamada: segunda cortada con este lead",
            f"Segunda llamada cortada con este lead: {clasificacion.motivo}",
        )
    return plan.resultado()


def decidir_mensaje(evento: Evento, memoria: MemoriaLead, campana: Campana) -> Plan:
    """R7: cuando el lead escribe, se cancela cada recordatorio pendiente que se le programó."""
    plan = _Planificador(evento, campana, memoria)
    plan.cancelar_recordatorios("el lead respondió por WhatsApp", evento.occurred_at)
    return plan.resultado()


def _visita_reservada(plan: _Planificador, evento: Evento, campana: Campana) -> None:
    cita = evento.agent_outcome.appointment
    if cita is None:
        raise ValueError("visita_reservada requiere agent_outcome.appointment")
    margen = timedelta(hours=campana.tareas.confirmar_visita_margen_horas)
    # Como muy tarde N horas antes de la visita; si ya no hay margen, en el acto.
    vence = max(sumar(cita.start_time, -margen, campana), evento.occurred_at)
    lead = evento.lead
    visita = f"Visita {cita.appointment_id} el {describir(cita.start_time, campana)}"
    if lead.property_address:
        detalle = f"{visita} en {lead.property_address}. Confirmar la dirección exacta al lead."
    else:
        detalle = (
            f"{visita}. El inmueble {lead.property_ref} no tiene dirección en el CRM: "
            "comprobarla antes de confirmársela al lead."
        )
    plan.tarea("confirmar_visita_direccion", "Confirmar la dirección de la visita", detalle, vence)


def _documentacion_enviada(plan: _Planificador, evento: Evento, campana: Campana) -> None:
    ocurrio, recordatorios = evento.occurred_at, campana.recordatorios
    if plan.whatsapp_permitido:
        # Decisión (revisada: una versión anterior lo ajustaba a la ventana de llamadas): el recordatorio
        # al lead sale a las 48 horas exactas, aunque caiga fuera de la ventana. Documentación el
        # viernes a las 16:42 → domingo a las 16:42.
        # - El OpenAPI escribe la restricción de la ventana solo para programar_llamada.no_antes_de
        #   («Tiene que caer dentro de la ventana de llamadas»); programar_recordatorio.cuando es un
        #   date-time sin restricción.
        # - Apoyo: campana.yaml define la ventana como las franjas «en las que se puede llamar» y
        #   cuenta las 48 horas como naturales.
        # - Contraargumento: casos.md exime de forma explícita a las tareas («vence_el es a cualquier
        #   hora del día») y no dice nada de los recordatorios, así que se puede leer que R3 («respetan
        #   la ventana de llamadas») les aplica. No es un caso cerrado; es la lectura más sólida,
        #   porque la ventana es de llamadas y el contrato del CRM solo la exige a las llamadas.
        plan.recordatorio(
            "whatsapp_lead",
            "lead",
            sumar(ocurrio, timedelta(hours=recordatorios.documentacion_lead_horas), campana),
            plantilla="recordatorio_documentacion",
        )
    plan.recordatorio(
        "tarea_comercial",
        "comercial",
        sumar_dias_habiles(ocurrio, recordatorios.seguimiento_comercial_dias_habiles, campana),
        tipo_tarea="llamar_a_mano",
    )


def _callback(plan: _Planificador, evento: Evento, datos: DatosConversacion, campana: Campana) -> None:
    """Casos 3 y 12: la llamada va al momento que pidió el lead, ajustado a la ventana.

    Decisión: se usa todo lo que dijo el lead y se completa lo que falta.
    - Con hora: ese momento. Sin fecha, o si ya pasó, la próxima vez que llegue esa hora: el lead no
      puede estar pidiendo el pasado.
    - Día sin hora («el jueves»): la apertura de la ventana ese día.
    - Ni día ni hora, o solo «hoy»: la separación general.
    Si la llamada no cae en lo que pidió (otra hora; con día sin hora, otro día), se le avisa.
    """
    ocurrio, fecha, hora = evento.occurred_at, datos.callback_fecha, datos.callback_hora
    if hora is not None:
        pedido = a_las(fecha, hora, campana) if fecha is not None else ocurrio
        if pedido <= ocurrio:
            pedido = proxima_vez(hora, ocurrio, campana)
        programada = plan.llamar(pedido, "callback solicitado", datos.nota_contexto)
        if programada is not None and programada != pedido:
            plan.avisar_cambio_hora(describir(pedido, campana), programada)
        return
    if fecha is not None and fecha > en_zona(ocurrio, campana).date():
        dia_pedido = a_las(fecha, time(0), campana)
        programada = plan.llamar(dia_pedido, "callback solicitado", datos.nota_contexto)
        if programada is not None and en_zona(programada, campana).date() != fecha:
            plan.avisar_cambio_hora(describir_dia(dia_pedido, campana), programada)
        return
    separacion = timedelta(hours=campana.reintentos.separacion_minima_horas)
    plan.llamar(sumar(ocurrio, separacion, campana), "callback sin hora concreta", datos.nota_contexto)


class _Planificador:
    """Acumula las órdenes de un evento aplicando las reglas transversales (N1, N2, N3, R4)."""

    def __init__(self, evento: Evento, campana: Campana, memoria: MemoriaLead) -> None:
        self._evento = evento
        self._campana = campana
        self.memoria = memoria
        self._ordenes: list[Orden] = []

    def resultado(self) -> Plan:
        return Plan(ordenes=list(self._ordenes), memoria=self.memoria)

    def recordar(self, **cambios: object) -> None:
        self.memoria = MemoriaLead.model_validate({**self.memoria.model_dump(), **cambios})

    @property
    def whatsapp_permitido(self) -> bool:
        """N1: ni documentación ni respuestas por WhatsApp a quien rechazó ese canal."""
        return not self.memoria.rechaza_whatsapp and not self.memoria.no_contactar

    def cerrar(self, clasificacion: Clasificacion, telefonia: Telefonia) -> None:
        etiqueta = clasificacion.etiqueta
        if etiqueta == "no_aplica":
            raise ValueError("no_aplica no cierra ninguna llamada")
        self._emitir(
            "cerrar_llamada",
            CerrarLlamada(
                entry_id=self._evento.campaign.entry_id,
                status=ESTADO_COLA[etiqueta],
                etiqueta=etiqueta,
                motivo=clasificacion.motivo,
                confianza=clasificacion.confianza,
                duration_seconds=telefonia.duration_seconds,
            ),
        )

    def llamar(self, desde: datetime, motivo: str, nota: str | None) -> datetime | None:
        """Programa otra llamada en la ventana. N3: con los intentos agotados, canal de respaldo."""
        campana = self._campana
        if self.memoria.intentos >= campana.reintentos.max_intentos:
            self.respaldo(
                f"intentos de voz agotados ({self.memoria.intentos} de {campana.reintentos.max_intentos})"
            )
            return None
        cuando = primer_instante_valido(desde, campana)
        self._emitir(
            "programar_llamada",
            ProgramarLlamada(
                entry_id=self._evento.campaign.entry_id,
                telefono=self._evento.lead.phone,
                no_antes_de=formatear(cuando, campana),
                motivo=motivo,
                nota_contexto=nota,
            ),
        )
        return cuando

    def respaldo(self, motivo: str) -> None:
        campana = self._campana
        if self.memoria.respaldo_resuelto:
            return
        if campana.canal_respaldo == "whatsapp" and self.whatsapp_permitido:
            self.whatsapp("primer_toque_respaldo", {})
        else:
            # Decisión: sin canal de respaldo utilizable, una persona decide cómo seguir. Lleva su
            # propio distintivo: en el mismo evento puede convivir con la revisión de N4.
            self.tarea(
                "revisar_llamada",
                "Decidir cómo seguir: sin reintento por voz ni WhatsApp",
                f"{motivo}; el lead no admite el canal de respaldo ({campana.canal_respaldo})",
                distintivo="respaldo",
            )
        self.recordar(respaldo_resuelto=True)

    def avisar_cambio_hora(self, pedido: str, programada: datetime) -> None:
        """Caso 12: si la llamada no cae cuando la pidió el lead, se le avisa (salvo N1)."""
        if self.whatsapp_permitido:
            self.whatsapp(
                "aviso_cambio_hora",
                {"hora_pedida": pedido, "hora_propuesta": describir(programada, self._campana)},
            )

    def whatsapp(self, plantilla: Plantilla, parametros: dict[str, str]) -> None:
        # Decisión: la especificación no conecta lead.language con el idioma de la plantilla; lo
        # conectamos, con "es" (el default del OpenAPI) si falta o viene vacío. Consecuencias:
        # - Los parámetros que armamos siguen en español: a un lead en "ca" le llega la plantilla en
        #   catalán con «jueves 17 a las 10:00» dentro. Se acepta porque los parámetros no se comparan.
        # - El recordatorio al lead (programar_recordatorio) también es un WhatsApp, pero su cuerpo no
        #   tiene idioma en el OpenAPI: ese sale en el idioma que decida el CRM.
        lead = self._evento.lead
        idioma = (lead.language or "").strip() or "es"
        base = {
            "nombre": (lead.full_name or "").split(" ")[0],
            "inmueble": lead.property_address or lead.property_ref or "",
        }
        self._emitir(
            "enviar_plantilla_whatsapp",
            EnviarPlantillaWhatsapp(
                organization_id=self._evento.organization_id,
                telefono=lead.phone,
                plantilla=plantilla,
                parametros={clave: valor for clave, valor in (base | parametros).items() if valor},
                idioma=idioma,
            ),
        )

    def tarea(
        self,
        tipo: TipoTarea,
        titulo: str,
        detalle: str,
        vence: datetime | None = None,
        distintivo: str | None = None,
    ) -> None:
        campana = self._campana
        vence = vence or sumar_dias_naturales(
            self._evento.occurred_at, campana.tareas.vencimiento_por_defecto_dias, campana
        )
        self._emitir(
            "crear_tarea",
            CrearTarea(
                contact_id=self._evento.lead.contact_id,
                call_id=self._evento.call_id,
                tipo=tipo,
                titulo=titulo,
                detalle=detalle,
                vence_el=formatear(vence, campana),
            ),
            distintivo,
        )

    def recordatorio(
        self,
        canal: CanalRecordatorio,
        distintivo: str,
        cuando: datetime,
        plantilla: Plantilla | None = None,
        tipo_tarea: TipoTarea | None = None,
    ) -> None:
        orden = self._emitir(
            "programar_recordatorio",
            ProgramarRecordatorio(
                contact_id=self._evento.lead.contact_id,
                canal=canal,
                plantilla=plantilla,
                tipo_tarea=tipo_tarea,
                cuando=formatear(cuando, self._campana),
                cancelar_si="lead_responde",
            ),
            distintivo,
        )
        # El CRM respondería con un reminder_id; lo generamos estable y lo persistimos, porque el
        # cancelar_recordatorio llegará en otro proceso. Si ya está en la memoria (una reentrega
        # tras caerse entre el store y el checkpoint vuelve a pasar por aquí), no se duplica.
        reminder_id = f"rem_{huella(orden.idempotency_key)}"
        if all(p.reminder_id != reminder_id for p in self.memoria.recordatorios_pendientes):
            pendiente = RecordatorioPendiente(reminder_id=reminder_id, canal=canal, cuando=cuando)
            self.recordar(recordatorios_pendientes=(*self.memoria.recordatorios_pendientes, pendiente))

    def cancelar_recordatorios(self, motivo: str, ahora: datetime) -> None:
        """Cancela los que aún no han salido; los ya enviados no se pueden cancelar (R7)."""
        # Ids únicos: una memoria que ya tenga un reminder_id repetido no rompe nada.
        unicos = {p.reminder_id: p for p in self.memoria.recordatorios_pendientes}
        for pendiente in unicos.values():
            if pendiente.cuando > ahora:
                self._emitir(
                    "cancelar_recordatorio",
                    CancelarRecordatorio(reminder_id=pendiente.reminder_id, motivo=motivo),
                    pendiente.reminder_id,
                )
        self.recordar(recordatorios_pendientes=())

    def marcar_no_contactar(self, motivo: str) -> None:
        lead = self._evento.lead
        self._emitir(
            "marcar_no_contactar",
            MarcarNoContactar(
                telefono=lead.phone,
                contact_id=lead.contact_id,
                canal="todos",
                motivo=motivo,
                origen="llamada_saliente",
            ),
        )
        self.recordar(no_contactar=True)

    def _emitir(self, operacion: Operacion, cuerpo: CuerpoOrden, distintivo: str | None = None) -> Orden:
        """Una misma clave, una misma orden: repetirla es idempotente; con otro contenido, un defecto."""
        orden = crear_orden(
            self._evento.event_id, self._evento.idempotency_key, operacion, cuerpo, distintivo
        )
        for existente in self._ordenes:
            if existente.idempotency_key == orden.idempotency_key:
                if existente != orden:
                    raise ValueError(f"dos órdenes distintas con la misma clave: {orden.idempotency_key}")
                return existente
        self._ordenes.append(orden)
        return orden
