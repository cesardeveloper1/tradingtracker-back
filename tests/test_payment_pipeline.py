import base64
import hashlib
import hmac
import json
import os
import time
import unittest
from unittest.mock import patch


os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['ENVIRONMENT'] = 'test'
os.environ['PAYMENT_TRACKER_SHARED_SECRET'] = 'integration-test-secret-with-32-characters'
os.environ['SSGG_BASE_URL'] = 'https://ssgg.test'

from app import app, db, payment_pipeline
from agiliza_config import derived_secret


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()


def pairing_ticket(jti='ticket-1', branch_id='branch-1') -> str:
    payload = {
        'jti': jti,
        'branchId': branch_id,
        'branchName': 'Local Centro',
        'providers': ['yape'],
        'exp': int(time.time()) + 120,
    }
    encoded = _b64url(json.dumps(payload, separators=(',', ':')).encode())
    signature = hmac.new(
        derived_secret('pairing-ticket'),
        encoded.encode(),
        hashlib.sha256,
    ).digest()
    return f'{encoded}.{_b64url(signature)}'


class PaymentPipelineTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        with app.app_context():
            db.session.remove()
            db.engine.dispose()

    def setUp(self):
        self.client = app.test_client()
        with app.app_context():
            db.drop_all()
            db.create_all()

    def tearDown(self):
        with app.app_context():
            db.session.remove()

    def pair(self, ticket_id='ticket-1'):
        response = self.client.post('/api/device-pairings/exchange', json={
            'ticket': pairing_ticket(ticket_id),
            'installationId': 'android-installation-1',
            'publicKey': 'base64-public-key',
            'platform': 'android',
        })
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()['data']

    @staticmethod
    def event_payload():
        return {
            'schemaVersion': 1,
            'providerEventId': 'yape:device:123456',
            'source': 'yape',
            'amountMinor': 2550,
            'currency': 'PEN',
            'occurredAt': '2026-08-13T12:00:00-05:00',
            'postTime': 123456,
            'rawPayloadHash': 'a' * 64,
            'branchId': 'branch-forged-by-client',
        }

    def test_pairing_is_single_use_and_token_is_not_persisted_in_plaintext(self):
        paired = self.pair()
        repeated = self.client.post('/api/device-pairings/exchange', json={
            'ticket': pairing_ticket(),
            'installationId': 'another-installation',
            'publicKey': 'key',
            'platform': 'android',
        })
        self.assertEqual(repeated.status_code, 409)
        with app.app_context():
            device = db.session.get(payment_pipeline.CaptureDevice, paired['deviceId'])
            self.assertNotEqual(device.token_hash, paired['deviceToken'])
            self.assertEqual(device.token_hash, hashlib.sha256(paired['deviceToken'].encode()).hexdigest())

    def test_malformed_pairing_ticket_is_rejected_without_server_error(self):
        response = self.client.post('/api/device-pairings/exchange', json={
            'ticket': 'not-base64.***',
            'installationId': 'android-installation-1',
            'publicKey': 'key',
            'platform': 'android',
        })
        self.assertEqual(response.status_code, 401)

    def test_ingest_is_idempotent_and_branch_comes_from_device(self):
        paired = self.pair()
        headers = {
            'Authorization': f"Bearer {paired['deviceToken']}",
            'Idempotency-Key': 'yape:device:123456',
        }
        first = self.client.post('/api/payment-events/v1', json=self.event_payload(), headers=headers)
        second = self.client.post('/api/payment-events/v1', json=self.event_payload(), headers=headers)
        self.assertEqual(first.status_code, 202, first.get_json())
        self.assertEqual(second.status_code, 200, second.get_json())
        self.assertTrue(second.get_json()['data']['duplicate'])
        with app.app_context():
            self.assertEqual(payment_pipeline.PaymentEvent.query.count(), 1)
            event = payment_pipeline.PaymentEvent.query.one()
            self.assertEqual(event.branch_id, 'branch-1')
            self.assertEqual(event.amount_minor, 2550)
            self.assertEqual(payment_pipeline.DeliveryOutbox.query.count(), 1)

    def test_worker_signs_exact_body_and_marks_delivery(self):
        paired = self.pair()
        response = self.client.post(
            '/api/payment-events/v1',
            json=self.event_payload(),
            headers={
                'Authorization': f"Bearer {paired['deviceToken']}",
                'Idempotency-Key': 'event-1',
            },
        )
        self.assertEqual(response.status_code, 202)

        class Accepted:
            status_code = 204
            text = ''

        with patch('payment_pipeline.requests.post', return_value=Accepted()) as post:
            with app.app_context():
                self.assertTrue(payment_pipeline.deliver_due_once())
                delivery = payment_pipeline.DeliveryOutbox.query.one()
                self.assertEqual(delivery.state, 'delivered')

        sent_body = post.call_args.kwargs['data']
        headers = post.call_args.kwargs['headers']
        expected = hmac.new(
            derived_secret('payment-webhook'),
            headers['X-Timestamp'].encode() + b'.' + sent_body,
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(headers['X-Signature'], f'sha256={expected}')
        self.assertEqual(json.loads(sent_body)['branchId'], 'branch-1')

    def test_device_can_only_revoke_itself(self):
        paired = self.pair()
        headers = {'Authorization': f"Bearer {paired['deviceToken']}"}
        self.assertEqual(self.client.get('/api/devices/self', headers=headers).status_code, 200)
        self.assertEqual(self.client.post('/api/devices/self/revoke', headers=headers).status_code, 204)
        self.assertEqual(self.client.get('/api/devices/self', headers=headers).status_code, 401)

    def test_legacy_public_ingest_is_closed_by_default(self):
        response = self.client.post('/api/notificaciones', json={'app_name': 'yape'})
        self.assertEqual(response.status_code, 401)


if __name__ == '__main__':
    unittest.main()
