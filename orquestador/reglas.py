"""Reglas de negocio: qué órdenes emite cada evento (casos.md y reglas N1 a N5 del enunciado).

Funciones puras: reciben el evento, su clasificación, la memoria del lead y la campaña, y
devuelven las órdenes y la memoria actualizada. No escriben nada ni conocen LangGraph.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from orquestador.calendario import (
    describir,
    formatear,
    primer_instante_valido,
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
            # Decisión: el reintento corto cae a mitad del rango ocupado_minutos_min..max
            # (30..90 → 60 min), como en el ejemplo resuelto. Manda sobre la separación general.
            medio = (reintentos.ocupado_minutos_min + reintentos.ocupado_minutos_max) // 2
            plan.llamar(
                sumar(ocurrio, timedelta(minutes=medio), campana),
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
                f"{clasificacion.motivo}. Sin reintentos por voz.",
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
            "Revisar la llamada",
            f"Segunda llamada cortada con este lead: {clasificacion.motivo}",
        )
    return plan.resultado()


def decidir_mensaje(evento: Evento, memoria: MemoriaLead, campana: Campana) -> Plan:
    """R7: cuando el lead escribe, se cancela cada recordatorio pendiente que se le programó."""
    plan = _Planificador(evento, campana, memoria)
    plan.cancelar_recordatorios("el lead respondió por WhatsApp", evento.occurred_at)
    return plan.resultado()


def _visita_reservada(plan: "_Planificador", evento: Evento, campana: Campana) -> None:
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


def _documentacion_enviada(plan: "_Planificador", evento: Evento, campana: Campana) -> None:
    ocurrio, recordatorios = evento.occurred_at, campana.recordatorios
    if plan.whatsapp_permitido:
        # Decisión: las 48 horas son un mínimo y el mensaje al lead sale dentro de la ventana (R3):
        # documentación el viernes por la tarde → recordatorio el lunes a las 10:00, no el domingo.
        plazo = timedelta(hours=recordatorios.documentacion_lead_horas)
        plan.recordatorio(
            "whatsapp_lead",
            "lead",
            primer_instante_valido(sumar(ocurrio, plazo, campana), campana),
            plantilla="recordatorio_documentacion",
        )
    plan.recordatorio(
        "tarea_comercial",
        "comercial",
        sumar_dias_habiles(ocurrio, recordatorios.seguimiento_comercial_dias_habiles, campana),
        tipo_tarea="llamar_a_mano",
    )


def _callback(plan: "_Planificador", evento: Evento, datos: DatosConversacion, campana: Campana) -> None:
    pedido = _instante_pedido(datos, campana)
    if pedido is None or pedido <= evento.occurred_at:
        # Decisión: sin una hora concreta (o con una ya pasada) se usa la separación general.
        separacion = timedelta(hours=campana.reintentos.separacion_minima_horas)
        plan.llamar(
            sumar(evento.occurred_at, separacion, campana), "callback sin hora concreta", datos.nota_contexto
        )
        return
    programada = plan.llamar(pedido, "callback solicitado", datos.nota_contexto)
    if programada is not None and programada != pedido and plan.whatsapp_permitido:
        plan.whatsapp(
            "aviso_cambio_hora",
            {
                "hora_pedida": describir(pedido, campana),
                "hora_propuesta": describir(programada, campana),
            },
        )


def _instante_pedido(datos: DatosConversacion, campana: Campana) -> datetime | None:
    if datos.callback_fecha is None or datos.callback_hora is None:
        return None
    return datetime.combine(datos.callback_fecha, datos.callback_hora, tzinfo=campana.zona)


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
        self.memoria = self.memoria.model_copy(update=cambios)

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
        if self.memoria.respaldo_enviado:
            return
        if campana.canal_respaldo == "whatsapp" and self.whatsapp_permitido:
            self.whatsapp("primer_toque_respaldo", {})
            self.recordar(respaldo_enviado=True)
        else:
            # Decisión: sin canal de respaldo utilizable, una persona decide cómo seguir.
            self.tarea(
                "revisar_llamada",
                "Revisar la llamada",
                f"{motivo}; el lead no admite el canal de respaldo ({campana.canal_respaldo})",
            )

    def whatsapp(self, plantilla: Plantilla, parametros: dict[str, str]) -> None:
        lead = self._evento.lead
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
            ),
        )

    def tarea(self, tipo: TipoTarea, titulo: str, detalle: str, vence: datetime | None = None) -> None:
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
        if orden is not None:
            # El CRM respondería con un reminder_id; lo generamos estable y lo persistimos, porque
            # el cancelar_recordatorio llegará en otro proceso.
            pendiente = RecordatorioPendiente(
                reminder_id=f"rem_{huella(orden.idempotency_key)}", canal=canal, cuando=cuando
            )
            self.recordar(recordatorios_pendientes=(*self.memoria.recordatorios_pendientes, pendiente))

    def cancelar_recordatorios(self, motivo: str, ahora: datetime) -> None:
        """Cancela los que aún no han salido; los ya enviados no se pueden cancelar (R7)."""
        for pendiente in self.memoria.recordatorios_pendientes:
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

    def _emitir(
        self, operacion: Operacion, cuerpo: CuerpoOrden, distintivo: str | None = None
    ) -> Orden | None:
        orden = crear_orden(
            self._evento.event_id, self._evento.idempotency_key, operacion, cuerpo, distintivo
        )
        if any(o.idempotency_key == orden.idempotency_key for o in self._ordenes):
            return None  # la misma orden dos veces en un evento: se emite una
        self._ordenes.append(orden)
        return orden
