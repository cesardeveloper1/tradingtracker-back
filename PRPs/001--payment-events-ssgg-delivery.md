# PRP: Entrega confiable de eventos de pago hacia SSGG

> **Proyecto:** tradingtracker-back  
> **Versión:** 1.0  
> **Fecha:** 2026-08-12  
> **Estado:** Implementado (pendiente integracion E2E con SSGG)  
> **Patrón:** B — integración distribuida con datos financieros  
> **Dependencias:** epic móvil `018` y PRP hijos `019`, `020`, `021` de `panel-admin-ag360ai-movile`, más `ssgg/PRPs/202--payment-reconciliation-tradingtracker.md`

## 1. Project Overview

Convertir el tracker actual de notificaciones Yape en el backend receptor de la app `panel-admin-ag360ai-movile`. La variante Android/Capacitor capturará las notificaciones y las enviará directamente al tracker con una credencial de dispositivo. El tracker normalizará y entregará a `ssgg` un contrato mediante comunicación server-to-server autenticada, idempotente y reintentable.

Usuarios: operadores de locales que cobran por billeteras digitales, soporte y administradores técnicos. El MVP admite Yape y deja el contrato preparado para Plin y otros proveedores sin añadir columnas específicas por marca.

### Decisión de arquitectura recomendada

```text
panel-admin-ag360ai-movile (Android) → tradingtracker-back
                    credencial dispositivo ↓ webhook firmado + outbox
                                           ssgg
                         ↙ resultados                 ↘ dato analizado
       panel-admin-ag360ai-movile                  panel-admin-ag360ai
```

La app móvil sí tendrá conexión directa con el tracker, pero mediante una credencial revocable y limitada a un dispositivo/local, nunca con una API key maestra. El panel web normal no administrará ni consultará eventos crudos del tracker: solo recibirá desde `ssgg` el estado final ya analizado.

Alternativas evaluadas:

1. **Panel web → tracker directo:** expone credenciales y duplica RBAC. No recomendado.
2. **App móvil → tracker con credencial de dispositivo:** necesaria para la captura Android; el alcance queda limitado a ingest/estado del dispositivo. Recomendada.
3. **SSGG consulta periódicamente al tracker:** simple, pero aumenta latencia y riesgo de perder/duplicar páginas. Útil solo como recuperación.
4. **Tracker empuja a SSGG con outbox y SSGG responde idempotentemente:** baja latencia y recuperación automática. Recomendado.

## 2. Problem Statement

El tracker persiste `Notificacion` e `Ingreso`, pero no tiene relación con negocio/local de Agiliza360, no conserva un evento financiero canónico y no entrega eventos a `ssgg`. `verify-payment` usa `Float`, fecha por día y tolerancia de ±1 %, devuelve coincidencias reutilizables y no reserva un movimiento; por ello no puede garantizar que un ingreso valide una sola orden.

Además, los endpoints móviles actuales no requieren autenticación, CORS acepta cualquier origen, los permisos de API key no se aplican por operación y `db.create_all()` no sustituye migraciones de producción.

## 3. Success Criteria

- 100 % de eventos aceptados poseen `providerEventId`/clave idempotente y `amountMinor` entero.
- Ningún reintento crea un segundo evento lógico en tracker ni en `ssgg`.
- 99 % de entregas exitosas llegan a `ssgg` en menos de 10 segundos, excluyendo indisponibilidad externa.
- Una caída de `ssgg` no pierde eventos: quedan pendientes y se reintentan.
- Cada dispositivo está vinculado explícitamente a un `branchId`; el cliente no puede suplantarlo en cada evento.
- La captura no autenticada queda deshabilitada en producción.
- Añadir Plin requiere un parser/adaptador, no cambiar el contrato de entrega.

## 4. User Stories (Jobs-to-be-Done)

- Cuando Yape notifica un ingreso, quiero que el evento llegue una sola vez a Agiliza360, para conciliarlo con una orden.
- Cuando `ssgg` está temporalmente caído, quiero que el tracker reintente, para no perder pagos.
- Cuando instalo el capturador en un local, quiero vincularlo con un código de un solo uso, para impedir que reporte pagos a otra sucursal.
- Cuando añadamos Plin, quiero reutilizar la misma canalización, para evitar otro servicio paralelo.
- Cuando soporte investiga un fallo, quiero ver el estado y los intentos de entrega sin exponer innecesariamente datos personales.

## 5. Functional Requirements

### P0

- **FR-001:** Introducir `PaymentEvent` separado de los modelos analíticos `Ingreso`/`Notificacion`.
- **FR-002:** Guardar dinero como `amountMinor: bigint/int` y `currency` ISO 4217; no usar `Float` para conciliación.
- **FR-003:** Modelar `source`, `providerEventId`, `operationCode`, `payerName`, `occurredAt`, `receivedAt`, `deviceId`, `branchId`, `rawPayloadHash` y estado de entrega.
- **FR-004:** Deduplicar con índice único por `(source, providerEventId)` y fallback estable `(source, deviceId, postTime/rawPayloadHash)`.
- **FR-005:** Incorporar registro de dispositivos: `deviceId`, `branchId`, estado, último contacto y proveedores habilitados.
- **FR-006:** Emparejar la app móvil mediante un ticket de un solo uso emitido/autorizado por `ssgg`, con expiración y consumo único; al canjearlo, emitir credencial revocable con scopes `payment:ingest` y `device:self`.
- **FR-007:** Ignorar cualquier `branchId` libre enviado durante captura; resolverlo desde el dispositivo autenticado.
- **FR-008:** Publicar el evento normalizado a `ssgg` con `deliveryId`, timestamp y firma HMAC SHA-256 del cuerpo exacto.
- **FR-009:** Implementar outbox persistente con estados `pending`, `delivering`, `delivered`, `retry`, `dead_letter`, conteo e instante del siguiente intento.
- **FR-010:** Aplicar reintento exponencial con jitter; tratar 2xx/409 idempotente como entrega terminal y 429/5xx/timeouts como reintentables.
- **FR-011:** Permitir reentrega manual de elementos `dead_letter` desde API administrativa.
- **FR-012:** Exponer a la app móvil, usando su credencial, solo el estado de su propio dispositivo/cola recibida; exponer a `ssgg` administración agregada mediante credencial server-to-server.
- **FR-013:** Proteger captura y administración; eliminar el modo público en producción.
- **FR-014:** Aplicar realmente permisos de API key (`ingest`, `manage`, `read`) o sustituirlos por credenciales de servicio con scopes.
- **FR-015:** Mantener `Ingreso` y analytics como proyección opcional; no usarlos para decidir conciliación.
- **FR-016:** Mantener `/api/verify-payment` solo como legacy de lectura y marcarlo deprecado; nunca debe confirmar ni reservar órdenes.

### P1

- **FR-017:** Añadir adaptador Plin y tabla configurable paquete Android → fuente.
- **FR-018:** Endpoint de replay por rango/identificador para recuperación controlada.
- **FR-019:** Métricas de atraso, reintentos, duplicados y dead letters.
- **FR-020:** Rotación de secretos sin interrupción mediante clave activa/anterior.

## 6. Non-Functional Requirements

- **Seguridad:** TLS obligatorio; HMAC con protección de replay (timestamp máximo 5 min + `deliveryId` único); secretos solo en variables de entorno.
- **Confiabilidad:** semántica at-least-once, consumidor idempotente; nunca prometer exactly-once entre servicios.
- **Rendimiento:** respuesta de captura <300 ms p95; la entrega a `ssgg` ocurre fuera del request mediante worker.
- **Retención:** payload crudo configurable, cifrado o redactado; metadatos financieros/auditoría según política acordada.
- **Observabilidad:** logs estructurados sin nombres completos, teléfonos, cuerpo crudo ni secretos.
- **Escalabilidad:** índices por estado/`nextAttemptAt`, dispositivo/fecha y fuente/id externo.

## 7. Technical Constraints

- Stack actual Flask + SQLAlchemy y PostgreSQL en producción.
- Adoptar migraciones reales (Alembic/Flask-Migrate); no confiar en `db.create_all()` para cambios productivos.
- Separar gradualmente `app.py` en módulos (`models`, `ingest`, `delivery`, `admin`) sin exigir reescritura completa para el MVP.
- Variables de integracion: `SSGG_BASE_URL` y `PAYMENT_TRACKER_SHARED_SECRET`. Las claves de pairing/webhook/admin se derivan por proposito; rutas, reintentos, TTL y seguridad son politica versionada en `agiliza_config.py`.
- El tracker no consulta ni modifica órdenes. Solo produce eventos y administra dispositivos.

## 8. Data Requirements

Contrato de salida recomendado:

```json
{
  "schemaVersion": 1,
  "deliveryId": "uuid",
  "eventId": "uuid",
  "providerEventId": "yape:device:postTime",
  "source": "yape",
  "amountMinor": 2550,
  "currency": "PEN",
  "payer": { "displayName": "Juan Pérez" },
  "operationCode": "optional",
  "occurredAt": "2026-08-12T16:40:10-05:00",
  "receivedAt": "2026-08-12T16:40:12Z",
  "deviceId": "device-id",
  "branchId": "mongo-branch-id",
  "rawPayloadHash": "sha256"
}
```

La zona horaria debe preservarse al capturar y normalizarse a UTC para comparar. `payer.displayName` es señal auxiliar, no identificador confiable. `operationCode`, si existe y es verificable, tiene mayor prioridad de deduplicación.

## 9. UI/UX Requirements

Este repo no contiene UI. Sus APIs deben soportar en la app móvil:

- Estado conectado/desconectado y último evento del dispositivo.
- Proveedores habilitados.
- Conteo de entregas pendientes/fallidas.
- Canje de tickets y revocación de la credencial/dispositivo.
- Reintento manual sin mostrar cuerpo crudo por defecto.

El panel web normal no consumirá estas APIs; obtiene exclusivamente el resultado analizado desde `ssgg`.

## 10. Risks & Assumptions

- **Notificación Android falsificable:** mitigar autenticando y vinculando dispositivo; el evento confirma recepción observada, no liquidación bancaria certificada.
- **Notificación tardía:** usar `occurredAt`, no `created_at` del servidor, y conservar desfase observado.
- **Mismo monto repetido:** el tracker no decide la orden; `ssgg` resuelve o marca ambiguo.
- **Pérdida de proceso worker:** usar outbox persistente y bloqueo/lease recuperable.
- **Proveedor cambia texto:** parsers versionados con fixtures reales anonimizados.
- **Supuesto:** la app capturadora puede enviar un token/dispositivo estable y `postTime`.

## 11. Out of Scope

- Consultar directamente APIs privadas de Yape/Plin.
- Confirmar órdenes desde el tracker.
- Guardar credenciales del panel en el tracker.
- Garantizar identidad del pagador solo por nombre.
- Conciliación parcial, devoluciones o contracargos en el MVP.

## 12. Open Questions

- ¿La app Agiliza360 reemplazará al capturador Android anterior o habrá una migración con convivencia? Responsable: Mobile/Infra.
- ¿Cuánto tiempo conservar payload crudo? Responsable: Seguridad/Legal.
- ¿Railway ejecutará worker separado o proceso interno? Responsable: Infra.
- ¿Yape entrega código de operación estable en todas las versiones? Responsable: QA con muestras reales.
- ¿El local puede tener varios dispositivos para la misma cuenta? Responsable: Producto; el diseño lo permite, pero debe definirse UX.

## Implementation Blueprint

1. Migraciones y nuevos modelos financieros/outbox/dispositivo.
2. Adaptador Yape hacia contrato canónico y deduplicación.
3. Pairing y autenticación de dispositivo.
4. Cliente firmado hacia `ssgg` y worker de reintentos.
5. Endpoints administrativos server-to-server.
6. Desactivar captura pública y deprecar verificación fuzzy.
7. Piloto shadow mode: entregar sin confirmar órdenes y comparar resultados.

## Implementation Notes (2026-08-13)

- Se agregaron `CaptureDevice`, `ConsumedPairingTicket`, `PaymentEvent` y
  `DeliveryOutbox`, junto con una migracion Alembic aplicable a la base legacy.
- El ticket usa el formato compacto `base64url(payload).base64url(HMAC-SHA256)`;
  exige `jti`, `branchId` y `exp`, y solo puede consumirse una vez.
- La app Android recibe un bearer token opaco; el tracker conserva unicamente su
  hash SHA-256 y deriva siempre `branchId` desde el dispositivo autenticado.
- El webhook firma `X-Timestamp + "." + bodyExacto` y reintenta fuera del request
  mediante una outbox persistente con lease recuperable.
- La escritura legacy de `/api/notificaciones` permanece autenticada y no se
  habilita mediante variables. La app Android usa exclusivamente el contrato v1.
- Reintentos, rutas, TTL y CORS de Capacitor estan versionados en
  `agiliza_config.py`. Solo se despliegan URL, base de datos y un secreto maestro.
- La integracion completa queda a la espera del consumidor y emisor de tickets
  definido en `ssgg/PRPs/202--payment-reconciliation-tradingtracker.md`.

## Validation Loop

- Tests de parser con variantes reales anonimizadas.
- Tests de idempotencia concurrente y claves duplicadas.
- Tests de firma, replay, clock skew y rotación.
- Tests de caída/reinicio durante entrega.
- Test contractual contra fixture OpenAPI de `ssgg`.
- Prueba E2E: captura → outbox → webhook → ACK, con `ssgg` intermitente.
