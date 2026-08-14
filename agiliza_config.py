"""Configuracion versionada de la integracion interna Agiliza360."""

import hashlib
import hmac
import os


SSGG_PAYMENT_WEBHOOK_PATH = '/api/v3/internal/payment-events/v1'
CAPACITOR_CORS_ORIGINS = ('capacitor://localhost', 'http://localhost')

DELIVERY_POLL_SECONDS = 2.0
DELIVERY_MAX_ATTEMPTS = 10
DELIVERY_TIMEOUT_SECONDS = 8.0
DELIVERY_LEASE_SECONDS = 30

DEVICE_TOKEN_TTL_DAYS = 365
PAIRING_CLOCK_SKEW_SECONDS = 30


def environment() -> str:
    return os.environ.get('ENVIRONMENT', 'development').strip().lower()


def is_production() -> bool:
    return environment() == 'production'


def integration_secret() -> bytes:
    raw = os.environ.get('PAYMENT_TRACKER_SHARED_SECRET', '').strip()
    if not raw:
        if is_production():
            raise RuntimeError('PAYMENT_TRACKER_SHARED_SECRET es obligatorio')
        raw = 'agiliza360-local-development-only'
    if is_production() and len(raw) < 32:
        raise RuntimeError('PAYMENT_TRACKER_SHARED_SECRET debe tener al menos 32 caracteres')
    return raw.encode('utf-8')


def derived_secret(purpose: str) -> bytes:
    """Separa criptograficamente pairing, webhook y administracion."""
    return hmac.new(
        integration_secret(),
        f'agiliza360:tracker:v1:{purpose}'.encode('utf-8'),
        hashlib.sha256,
    ).digest()
