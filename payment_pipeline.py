"""Canalizacion confiable de eventos de pago Android -> tracker -> SSGG."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import random
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from types import SimpleNamespace
from urllib.parse import urljoin

import requests
from flask import Blueprint, g, jsonify, request
from sqlalchemy import UniqueConstraint, and_, or_
from sqlalchemy.exc import IntegrityError
from agiliza_config import (
    DELIVERY_LEASE_SECONDS,
    DELIVERY_MAX_ATTEMPTS,
    DELIVERY_POLL_SECONDS,
    DELIVERY_TIMEOUT_SECONDS,
    DEVICE_TOKEN_TTL_DAYS,
    PAIRING_CLOCK_SKEW_SECONDS,
    SSGG_PAYMENT_WEBHOOK_PATH,
    derived_secret,
    environment,
)


logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _parse_iso(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field} debe ser una fecha ISO 8601')
    normalized = value.strip().replace('Z', '+00:00')
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError(f'{field} debe incluir zona horaria')
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _json_error(message: str, status: int):
    return jsonify({'error': True, 'mensaje': message}), status


def init_payment_pipeline(app, db):
    class CaptureDevice(db.Model):
        __tablename__ = 'capture_device'

        id = db.Column(db.String(36), primary_key=True)
        branch_id = db.Column(db.String(128), nullable=False, index=True)
        branch_name = db.Column(db.String(200), nullable=True)
        installation_id = db.Column(db.String(160), nullable=False, unique=True)
        public_key = db.Column(db.Text, nullable=False)
        token_hash = db.Column(db.String(64), nullable=False, unique=True)
        status = db.Column(db.String(20), nullable=False, default='active', index=True)
        scopes = db.Column(db.Text, nullable=False, default='payment:ingest,device:self')
        providers = db.Column(db.Text, nullable=False, default='yape')
        expires_at = db.Column(db.DateTime, nullable=False)
        last_seen_at = db.Column(db.DateTime, nullable=True)
        created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
        updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

        def provider_list(self):
            return [item for item in self.providers.split(',') if item]

        def scope_set(self):
            return {item for item in self.scopes.split(',') if item}


    class ConsumedPairingTicket(db.Model):
        __tablename__ = 'consumed_pairing_ticket'

        id = db.Column(db.String(128), primary_key=True)
        branch_id = db.Column(db.String(128), nullable=False, index=True)
        device_id = db.Column(db.String(36), nullable=False)
        expires_at = db.Column(db.DateTime, nullable=False)
        consumed_at = db.Column(db.DateTime, nullable=False, default=_utcnow)


    class PaymentEvent(db.Model):
        __tablename__ = 'payment_event'
        __table_args__ = (
            UniqueConstraint('source', 'provider_event_id', name='uq_payment_event_provider'),
            UniqueConstraint(
                'source', 'device_id', 'post_time', 'raw_payload_hash',
                name='uq_payment_event_fallback'
            ),
        )

        id = db.Column(db.String(36), primary_key=True)
        idempotency_key = db.Column(db.String(200), nullable=False)
        provider_event_id = db.Column(db.String(200), nullable=False)
        source = db.Column(db.String(40), nullable=False, index=True)
        amount_minor = db.Column(db.BigInteger, nullable=False)
        currency = db.Column(db.String(3), nullable=False)
        payer_name = db.Column(db.String(200), nullable=True)
        operation_code = db.Column(db.String(120), nullable=True)
        occurred_at = db.Column(db.DateTime, nullable=False, index=True)
        received_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
        device_id = db.Column(db.String(36), db.ForeignKey('capture_device.id'), nullable=False, index=True)
        branch_id = db.Column(db.String(128), nullable=False, index=True)
        post_time = db.Column(db.BigInteger, nullable=True)
        raw_payload_hash = db.Column(db.String(64), nullable=False)
        created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)


    class DeliveryOutbox(db.Model):
        __tablename__ = 'payment_delivery_outbox'

        id = db.Column(db.String(36), primary_key=True)
        event_id = db.Column(db.String(36), db.ForeignKey('payment_event.id'), nullable=False, unique=True)
        state = db.Column(db.String(20), nullable=False, default='pending', index=True)
        attempts = db.Column(db.Integer, nullable=False, default=0)
        next_attempt_at = db.Column(db.DateTime, nullable=False, default=_utcnow, index=True)
        lease_until = db.Column(db.DateTime, nullable=True, index=True)
        last_error_code = db.Column(db.String(100), nullable=True)
        last_error_detail = db.Column(db.String(500), nullable=True)
        delivered_at = db.Column(db.DateTime, nullable=True)
        created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
        updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)


    def validate_pairing_ticket(ticket: str) -> dict:
        secret = derived_secret('pairing-ticket')
        try:
            payload_part, signature_part = ticket.split('.', 1)
            expected = hmac.new(secret, payload_part.encode(), hashlib.sha256).digest()
            received = _b64url_decode(signature_part)
            if not hmac.compare_digest(expected, received):
                raise ValueError('Ticket de emparejamiento invalido')
            payload = json.loads(_b64url_decode(payload_part))
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error):
            raise ValueError('Ticket de emparejamiento invalido')
        if not payload.get('jti') or not payload.get('branchId') or not payload.get('exp'):
            raise ValueError('Ticket de emparejamiento incompleto')
        if int(payload['exp']) + PAIRING_CLOCK_SKEW_SECONDS < int(time.time()):
            raise ValueError('Ticket de emparejamiento expirado')
        return payload


    def require_device(scope: str):
        def decorator(fn):
            @wraps(fn)
            def wrapped(*args, **kwargs):
                authorization = request.headers.get('Authorization', '')
                if not authorization.startswith('Bearer '):
                    return _json_error('Se requiere credencial de dispositivo', 401)
                token = authorization[7:].strip()
                device = CaptureDevice.query.filter_by(token_hash=_hash_token(token)).first()
                if not device or device.status != 'active':
                    return _json_error('Credencial de dispositivo invalida o revocada', 401)
                if device.expires_at <= _utcnow():
                    return _json_error('Credencial de dispositivo expirada', 401)
                if scope not in device.scope_set():
                    return _json_error('La credencial no posee el alcance requerido', 403)
                device.last_seen_at = _utcnow()
                db.session.commit()
                g.capture_device = device
                return fn(*args, **kwargs)
            return wrapped
        return decorator


    def require_admin(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            configured = derived_secret('tracker-admin').hex()
            supplied = request.headers.get('X-Service-Key', '')
            if not supplied or not hmac.compare_digest(configured, supplied):
                return _json_error('Credencial administrativa invalida', 401)
            return fn(*args, **kwargs)
        return wrapped


    bp = Blueprint('payment_pipeline', __name__, url_prefix='/api')

    @bp.post('/device-pairings/exchange')
    def exchange_pairing():
        data = request.get_json(silent=True) or {}
        required = ('ticket', 'installationId', 'publicKey', 'platform')
        missing = [name for name in required if not isinstance(data.get(name), str) or not data[name].strip()]
        if missing:
            return _json_error(f'Campos requeridos faltantes: {", ".join(missing)}', 400)
        if data['platform'].lower() != 'android':
            return _json_error('Solo se admiten capturadores Android', 422)
        try:
            payload = validate_pairing_ticket(data['ticket'])
        except ValueError as exc:
            return _json_error(str(exc), 401)

        if db.session.get(ConsumedPairingTicket, str(payload['jti'])):
            return _json_error('El ticket ya fue utilizado', 409)

        providers = payload.get('providers') or ['yape']
        if not isinstance(providers, list) or not all(isinstance(item, str) for item in providers):
            return _json_error('Lista de proveedores invalida', 400)
        providers = sorted({item.strip().lower() for item in providers if item.strip()}) or ['yape']
        raw_token = secrets.token_urlsafe(48)
        expires_at = _utcnow() + timedelta(days=DEVICE_TOKEN_TTL_DAYS)
        installation_id = data['installationId'].strip()[:160]
        device = CaptureDevice.query.filter_by(installation_id=installation_id).first()
        if device:
            if device.branch_id != str(payload['branchId']):
                return _json_error('La instalacion ya pertenece a otro local', 409)
            device.public_key = data['publicKey'].strip()
            device.token_hash = _hash_token(raw_token)
            device.status = 'active'
            device.providers = ','.join(providers)
            device.expires_at = expires_at
            device.updated_at = _utcnow()
        else:
            device = CaptureDevice(
                id=str(uuid.uuid4()),
                branch_id=str(payload['branchId']),
                branch_name=(str(payload.get('branchName'))[:200] if payload.get('branchName') else None),
                installation_id=installation_id,
                public_key=data['publicKey'].strip(),
                token_hash=_hash_token(raw_token),
                status='active',
                scopes='payment:ingest,device:self',
                providers=','.join(providers),
                expires_at=expires_at,
            )
            db.session.add(device)
        db.session.flush()
        db.session.add(ConsumedPairingTicket(
            id=str(payload['jti']),
            branch_id=device.branch_id,
            device_id=device.id,
            expires_at=datetime.fromtimestamp(int(payload['exp']), timezone.utc).replace(tzinfo=None),
        ))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return _json_error('El ticket ya fue utilizado', 409)
        return jsonify({'data': {
            'deviceId': device.id,
            'deviceToken': raw_token,
            'branchId': device.branch_id,
            'branchName': device.branch_name,
            'expiresAt': _iso(device.expires_at),
            'providers': device.provider_list(),
        }}), 201

    def _existing_event(source: str, provider_event_id: str, device_id: str, post_time, raw_hash: str):
        query = PaymentEvent.query.filter_by(source=source, provider_event_id=provider_event_id)
        found = query.first()
        if found:
            return found
        if post_time is not None:
            return PaymentEvent.query.filter_by(
                source=source, device_id=device_id, post_time=post_time, raw_payload_hash=raw_hash
            ).first()
        return None

    @bp.post('/payment-events/v1')
    @require_device('payment:ingest')
    def ingest_payment_event():
        data = request.get_json(silent=True) or {}
        idempotency_key = request.headers.get('Idempotency-Key', '').strip()
        if not idempotency_key:
            return _json_error('Se requiere header Idempotency-Key', 400)
        required = ('providerEventId', 'source', 'amountMinor', 'currency', 'occurredAt', 'rawPayloadHash')
        missing = [name for name in required if data.get(name) in (None, '')]
        if missing:
            return _json_error(f'Campos requeridos faltantes: {", ".join(missing)}', 400)
        if data.get('schemaVersion') != 1:
            return _json_error('schemaVersion no soportado', 422)
        if isinstance(data['amountMinor'], bool) or not isinstance(data['amountMinor'], int) or data['amountMinor'] <= 0:
            return _json_error('amountMinor debe ser un entero positivo', 422)
        currency = str(data['currency']).upper()
        if len(currency) != 3:
            return _json_error('currency debe usar ISO 4217', 422)
        source = str(data['source']).strip().lower()[:40]
        if source not in g.capture_device.provider_list():
            return _json_error('Proveedor no habilitado para el dispositivo', 403)
        raw_hash = str(data['rawPayloadHash']).lower()
        if not len(raw_hash) == 64 or any(char not in '0123456789abcdef' for char in raw_hash):
            return _json_error('rawPayloadHash debe ser SHA-256 hexadecimal', 422)
        try:
            occurred_at = _parse_iso(data['occurredAt'], 'occurredAt')
        except ValueError as exc:
            return _json_error(str(exc), 422)
        provider_event_id = str(data['providerEventId']).strip()[:200]
        post_time = data.get('postTime')
        if post_time is not None and (isinstance(post_time, bool) or not isinstance(post_time, int)):
            return _json_error('postTime debe ser entero', 422)

        existing = _existing_event(source, provider_event_id, g.capture_device.id, post_time, raw_hash)
        if existing:
            return jsonify({'data': {'trackerEventId': existing.id, 'duplicate': True}}), 200

        event = PaymentEvent(
            id=str(uuid.uuid4()),
            idempotency_key=idempotency_key[:200],
            provider_event_id=provider_event_id,
            source=source,
            amount_minor=data['amountMinor'],
            currency=currency,
            payer_name=(str(data['payerName'])[:200] if data.get('payerName') else None),
            operation_code=(str(data['operationCode'])[:120] if data.get('operationCode') else None),
            occurred_at=occurred_at,
            received_at=_utcnow(),
            device_id=g.capture_device.id,
            branch_id=g.capture_device.branch_id,
            post_time=post_time,
            raw_payload_hash=raw_hash,
        )
        delivery = DeliveryOutbox(id=str(uuid.uuid4()), event_id=event.id, state='pending')
        db.session.add_all([event, delivery])
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            existing = _existing_event(source, provider_event_id, g.capture_device.id, post_time, raw_hash)
            if existing:
                return jsonify({'data': {'trackerEventId': existing.id, 'duplicate': True}}), 200
            raise
        return jsonify({'data': {'trackerEventId': event.id, 'duplicate': False}}), 202

    @bp.get('/devices/self')
    @require_device('device:self')
    def device_self():
        device = g.capture_device
        pending = DeliveryOutbox.query.join(PaymentEvent).filter(
            PaymentEvent.device_id == device.id,
            DeliveryOutbox.state.in_(('pending', 'delivering', 'retry')),
        ).count()
        failed = DeliveryOutbox.query.join(PaymentEvent).filter(
            PaymentEvent.device_id == device.id,
            DeliveryOutbox.state == 'dead_letter',
        ).count()
        return jsonify({'data': {
            'deviceId': device.id,
            'branchId': device.branch_id,
            'branchName': device.branch_name,
            'status': device.status,
            'providers': device.provider_list(),
            'lastSeenAt': _iso(device.last_seen_at),
            'queue': {'pending': pending, 'failed': failed},
        }})

    @bp.post('/devices/self/revoke')
    @require_device('device:self')
    def revoke_self():
        g.capture_device.status = 'revoked'
        g.capture_device.token_hash = _hash_token(secrets.token_urlsafe(48))
        db.session.commit()
        return '', 204

    @bp.get('/admin/deliveries')
    @require_admin
    def list_deliveries():
        state = request.args.get('state')
        query = DeliveryOutbox.query
        if state:
            query = query.filter_by(state=state)
        rows = query.order_by(DeliveryOutbox.created_at.desc()).limit(200).all()
        return jsonify({'data': [{
            'deliveryId': row.id,
            'eventId': row.event_id,
            'state': row.state,
            'attempts': row.attempts,
            'nextAttemptAt': _iso(row.next_attempt_at),
            'lastErrorCode': row.last_error_code,
            'deliveredAt': _iso(row.delivered_at),
        } for row in rows]})

    @bp.post('/admin/deliveries/<delivery_id>/retry')
    @require_admin
    def retry_delivery(delivery_id):
        delivery = DeliveryOutbox.query.get_or_404(delivery_id)
        if delivery.state == 'delivered':
            return _json_error('Una entrega completada no se reenvia sin crear una nueva auditoria', 409)
        delivery.state = 'retry'
        delivery.next_attempt_at = _utcnow()
        delivery.lease_until = None
        delivery.last_error_code = None
        delivery.last_error_detail = None
        db.session.commit()
        return jsonify({'data': {'deliveryId': delivery.id, 'state': delivery.state}})

    app.register_blueprint(bp)

    def _event_payload(delivery, event) -> dict:
        payload = {
            'schemaVersion': 1,
            'deliveryId': delivery.id,
            'eventId': event.id,
            'providerEventId': event.provider_event_id,
            'source': event.source,
            'amountMinor': event.amount_minor,
            'currency': event.currency,
            'payer': {'displayName': event.payer_name} if event.payer_name else {},
            'operationCode': event.operation_code,
            'occurredAt': _iso(event.occurred_at),
            'receivedAt': _iso(event.received_at),
            'deviceId': event.device_id,
            'branchId': event.branch_id,
            'rawPayloadHash': event.raw_payload_hash,
        }
        return payload

    def _schedule_failure(delivery, code: str, detail: str):
        delivery.last_error_code = code[:100]
        delivery.last_error_detail = detail[:500]
        delivery.lease_until = None
        if delivery.attempts >= DELIVERY_MAX_ATTEMPTS:
            delivery.state = 'dead_letter'
            delivery.next_attempt_at = _utcnow()
        else:
            delay = min(3600, (2 ** min(delivery.attempts, 10)) + random.uniform(0, 1.5))
            delivery.state = 'retry'
            delivery.next_attempt_at = _utcnow() + timedelta(seconds=delay)

    def deliver_due_once() -> bool:
        now = _utcnow()
        query = DeliveryOutbox.query.filter(
            or_(
                and_(DeliveryOutbox.state.in_(('pending', 'retry')), DeliveryOutbox.next_attempt_at <= now),
                and_(DeliveryOutbox.state == 'delivering', DeliveryOutbox.lease_until < now),
            )
        ).order_by(DeliveryOutbox.next_attempt_at.asc())
        try:
            delivery = query.with_for_update(skip_locked=True).first()
        except Exception:
            db.session.rollback()
            delivery = query.first()
        if not delivery:
            return False
        delivery.state = 'delivering'
        delivery.attempts += 1
        delivery.lease_until = now + timedelta(seconds=DELIVERY_LEASE_SECONDS)
        db.session.commit()

        event = db.session.get(PaymentEvent, delivery.event_id)
        if not event:
            _schedule_failure(delivery, 'event_missing', 'No existe el evento asociado')
            db.session.commit()
            return True
        secret = derived_secret('payment-webhook')
        base_url = os.environ.get('SSGG_BASE_URL', '')
        if not base_url:
            _schedule_failure(delivery, 'configuration_error', 'Falta SSGG_BASE_URL')
            db.session.commit()
            return True
        body = json.dumps(_event_payload(delivery, event), separators=(',', ':'), sort_keys=True).encode('utf-8')
        timestamp = str(int(time.time()))
        signature = hmac.new(secret, timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
        url = urljoin(base_url.rstrip('/') + '/', SSGG_PAYMENT_WEBHOOK_PATH.lstrip('/'))
        try:
            response = requests.post(
                url,
                data=body,
                headers={
                    'Content-Type': 'application/json',
                    'X-Delivery-Id': delivery.id,
                    'X-Timestamp': timestamp,
                    'X-Signature': f'sha256={signature}',
                },
                timeout=DELIVERY_TIMEOUT_SECONDS,
            )
            if 200 <= response.status_code < 300 or response.status_code == 409:
                delivery.state = 'delivered'
                delivery.delivered_at = _utcnow()
                delivery.lease_until = None
                delivery.last_error_code = None
                delivery.last_error_detail = None
            elif response.status_code == 429 or response.status_code >= 500:
                _schedule_failure(delivery, f'http_{response.status_code}', response.text)
            else:
                delivery.state = 'dead_letter'
                delivery.lease_until = None
                delivery.last_error_code = f'http_{response.status_code}'
                delivery.last_error_detail = response.text[:500]
        except requests.RequestException as exc:
            _schedule_failure(delivery, 'network_error', type(exc).__name__)
        db.session.commit()
        return True

    worker_started = threading.Event()

    def start_worker():
        if environment() == 'test' or worker_started.is_set():
            return
        worker_started.set()

        def run():
            while True:
                try:
                    with app.app_context():
                        worked = deliver_due_once()
                        db.session.remove()
                    if not worked:
                        time.sleep(DELIVERY_POLL_SECONDS)
                except Exception:
                    logger.exception('Fallo inesperado en worker de entregas')
                    with app.app_context():
                        db.session.rollback()
                        db.session.remove()
                    time.sleep(DELIVERY_POLL_SECONDS)

        threading.Thread(target=run, name='payment-delivery-worker', daemon=True).start()
        logger.info('Worker de entregas de pagos iniciado')

    return SimpleNamespace(
        CaptureDevice=CaptureDevice,
        ConsumedPairingTicket=ConsumedPairingTicket,
        PaymentEvent=PaymentEvent,
        DeliveryOutbox=DeliveryOutbox,
        validate_pairing_ticket=validate_pairing_ticket,
        deliver_due_once=deliver_due_once,
        start_worker=start_worker,
    )
