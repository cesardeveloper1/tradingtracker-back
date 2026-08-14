"""
Yape Tracker API - Backend completo
PostgreSQL (prod) / SQLite (dev local)
Autenticación por API Key, rate limiting, Swagger docs
"""

import os
import re
import sys
import time
import hashlib
import secrets
import logging
from functools import wraps
from datetime import datetime, date, timedelta
from dotenv import load_dotenv

load_dotenv()

from flask import Flask, jsonify, request, g
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from flask_migrate import Migrate
from sqlalchemy import func, extract, text
from agiliza_config import (
    CAPACITOR_CORS_ORIGINS,
    derived_secret,
    environment,
    is_production,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
app = Flask(__name__)

# --- Database ----------------------------------------------------------
database_url = os.environ.get('DATABASE_URL', '')
if database_url:
    # Railway/Heroku a veces usa postgres:// en vez de postgresql://
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///yape_tracker.db'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 300,
}

# --- CORS --------------------------------------------------------------
CORS(app, resources={r"/api/*": {"origins": CAPACITOR_CORS_ORIGINS}})

# --- Master key --------------------------------------------------------
MASTER_API_KEY = derived_secret('legacy-api-admin').hex()

# --- Uptime tracker ----------------------------------------------------
APP_START_TIME = time.time()

db = SQLAlchemy(app)
migrate = Migrate(app, db)

# ========================================================================
# MODELOS
# ========================================================================

class Ingreso(db.Model):
    __tablename__ = 'ingreso'
    id = db.Column(db.Integer, primary_key=True)
    monto = db.Column(db.Float, nullable=False)
    categoria = db.Column(db.String(100), nullable=False)
    descripcion = db.Column(db.String(250), default='')
    fecha = db.Column(db.Date, nullable=False, default=date.today)
    fuente = db.Column(db.String(150), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'monto': self.monto,
            'categoria': self.categoria,
            'descripcion': self.descripcion or '',
            'fecha': self.fecha.isoformat(),
            'fuente': self.fuente,
            'created_at': self.created_at.isoformat()
        }


class Egreso(db.Model):
    __tablename__ = 'egreso'
    id = db.Column(db.Integer, primary_key=True)
    monto = db.Column(db.Float, nullable=False)
    categoria = db.Column(db.String(100), nullable=False)
    descripcion = db.Column(db.String(250), default='')
    fecha = db.Column(db.Date, nullable=False, default=date.today)
    tienda = db.Column(db.String(150), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'monto': self.monto,
            'categoria': self.categoria,
            'descripcion': self.descripcion or '',
            'fecha': self.fecha.isoformat(),
            'tienda': self.tienda,
            'created_at': self.created_at.isoformat()
        }


class Conexion(db.Model):
    __tablename__ = 'conexion'
    id = db.Column(db.Integer, primary_key=True)
    ip_address = db.Column(db.String(45), nullable=False)
    user_agent = db.Column(db.String(500), default='')
    endpoint = db.Column(db.String(200), nullable=False)
    method = db.Column(db.String(10), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'ip_address': self.ip_address,
            'user_agent': self.user_agent or '',
            'endpoint': self.endpoint,
            'method': self.method,
            'timestamp': self.timestamp.isoformat()
        }


class Notificacion(db.Model):
    __tablename__ = 'notificacion'
    id = db.Column(db.Integer, primary_key=True)
    app_name = db.Column(db.String(100), nullable=False)
    titulo = db.Column(db.String(300), default='')
    cuerpo = db.Column(db.Text, default='')
    categoria = db.Column(db.String(100), default='')
    monto = db.Column(db.Float, default=0.0)
    tipo = db.Column(db.String(20), default='')  # ingreso / egreso / otro
    leido = db.Column(db.Boolean, default=False)
    fecha_recibido = db.Column(db.DateTime, default=datetime.utcnow)
    post_time = db.Column(db.BigInteger, nullable=True)  # ms Android postTime
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'app_name': self.app_name,
            'titulo': self.titulo or '',
            'cuerpo': self.cuerpo or '',
            'categoria': self.categoria or '',
            'monto': self.monto or 0.0,
            'tipo': self.tipo or '',
            'leido': self.leido,
            'fecha_recibido': self.fecha_recibido.isoformat(),
            'post_time': self.post_time,
            'created_at': self.created_at.isoformat()
        }


class ApiKey(db.Model):
    __tablename__ = 'api_key'
    id = db.Column(db.Integer, primary_key=True)
    key_hash = db.Column(db.String(64), unique=True, nullable=False)
    nombre = db.Column(db.String(100), nullable=False)
    permisos = db.Column(db.String(500), default='read')  # comma-separated: read,write,admin
    rate_limit = db.Column(db.Integer, default=100)  # requests per minute
    activa = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'nombre': self.nombre,
            'permisos': self.permisos,
            'rate_limit': self.rate_limit,
            'activa': self.activa,
            'created_at': self.created_at.isoformat()
        }


class Contacto(db.Model):
    __tablename__ = 'contacto'
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(200), nullable=False)
    frecuencia = db.Column(db.Integer, default=0)
    total_monto = db.Column(db.Float, default=0.0)
    ultima_transaccion = db.Column(db.DateTime, nullable=True)
    tipo_frecuente = db.Column(db.String(20), default='')  # ingreso / egreso

    def to_dict(self):
        return {
            'id': self.id,
            'nombre': self.nombre,
            'frecuencia': self.frecuencia,
            'total_monto': self.total_monto,
            'ultima_transaccion': self.ultima_transaccion.isoformat() if self.ultima_transaccion else None,
            'tipo_frecuente': self.tipo_frecuente
        }


# La canalizacion financiera vive separada del modelo analitico legacy.
from payment_pipeline import init_payment_pipeline

payment_pipeline = init_payment_pipeline(app, db)


# ========================================================================
# Crear tablas
# ========================================================================
def _ensure_schema():
    """Migraciones ligeras para columnas nuevas en SQLite/Postgres."""
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)
    if 'notificacion' not in insp.get_table_names():
        return
    cols = {c['name'] for c in insp.get_columns('notificacion')}
    if 'post_time' not in cols:
        db.session.execute(text('ALTER TABLE notificacion ADD COLUMN post_time BIGINT'))
        db.session.commit()
        logger.info('Columna notificacion.post_time agregada')


with app.app_context():
    auto_create = not is_production()
    if auto_create:
        db.create_all()
        _ensure_schema()
        logger.info("Base de datos local inicializada correctamente")
    else:
        logger.info("AUTO_CREATE_SCHEMA desactivado; se esperan migraciones aplicadas")

payment_pipeline.start_worker()

# ========================================================================
# Rate limiting (in-memory, simple)
# ========================================================================
_rate_limit_store: dict = {}  # key_hash -> [(timestamp, ...)]


def _check_rate_limit(key_hash: str, limit: int) -> bool:
    """Retorna True si la petición está dentro del límite."""
    now = time.time()
    window = 60.0  # 1 minute
    if key_hash not in _rate_limit_store:
        _rate_limit_store[key_hash] = []
    # Limpiar entradas viejas
    _rate_limit_store[key_hash] = [t for t in _rate_limit_store[key_hash] if now - t < window]
    if len(_rate_limit_store[key_hash]) >= limit:
        return False
    _rate_limit_store[key_hash].append(now)
    return True


# ========================================================================
# Swagger / Flasgger
# ========================================================================
try:
    from flasgger import Swagger

    swagger_config = {
        "headers": [],
        "specs": [
            {
                "endpoint": 'apispec',
                "route": '/apispec.json',
                "rule_filter": lambda rule: True,
                "model_filter": lambda tag: True,
            }
        ],
        "static_url_path": "/flasgger_static",
        "swagger_ui": True,
        "specs_route": "/api/docs"
    }

    swagger_template = {
        "info": {
            "title": "Yape Tracker API",
            "description": "API para rastreo de ingresos y egresos de Yape. "
                           "Endpoints de la app móvil no requieren autenticación. "
                           "Endpoints públicos de la API requieren header X-API-Key.",
            "version": "2.0.0",
            "contact": {
                "name": "Yape Tracker"
            }
        },
        "securityDefinitions": {
            "ApiKeyAuth": {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key"
            }
        },
        "basePath": "/",
        "schemes": ["https", "http"]
    }

    swagger = Swagger(app, config=swagger_config, template=swagger_template)
    logger.info("Swagger UI disponible en /api/docs")
except ImportError:
    logger.warning("flasgger no instalado — Swagger UI deshabilitado")

# ========================================================================
# HELPERS
# ========================================================================

def _hash_key(raw_key: str) -> str:
    """SHA-256 hash de un API key."""
    return hashlib.sha256(raw_key.encode('utf-8')).hexdigest()


def _json_error(message: str, status: int = 400):
    """Respuesta de error consistente."""
    return jsonify({'error': True, 'mensaje': message}), status


BANK_PACKAGE_ALIASES = {
    'com.bcp.innovacxion.yapeapp': 'yape',
}

YAPE_PACKAGE_ID = 'com.bcp.innovacxion.yapeapp'

BLOCKED_APP_FRAGMENTS = (
    'gmail', 'google.android.gm', 'whatsapp', 'outlook', 'correo',
    'com.google.', 'com.microsoft.office', 'com.samsung.android.email',
)


def _is_yape_notification(app_name: str, categoria: str = '') -> bool:
    """Solo acepta notificaciones cuyo origen es la app Yape."""
    raw = (app_name or '').lower().strip()
    if not raw:
        return False
    if any(blocked in raw for blocked in BLOCKED_APP_FRAGMENTS):
        return False
    if raw == YAPE_PACKAGE_ID or 'yapeapp' in raw:
        return True
    if raw == 'yape':
        return True
    return False


def _normalize_app_name(name: str) -> str:
    """Unifica paquete Android de Yape."""
    if not name:
        return ''
    lower = name.lower().strip()
    if lower in BANK_PACKAGE_ALIASES:
        return BANK_PACKAGE_ALIASES[lower]
    if lower == YAPE_PACKAGE_ID or 'yapeapp' in lower:
        return 'yape'
    if lower == 'yape':
        return 'yape'
    return lower


def _notificacion_fingerprint(data: dict, fecha: datetime) -> str:
    """Una notificación = un evento. Clave: post_time o timestamp con hora."""
    app = _normalize_app_name(data.get('app_name', ''))
    post_time = data.get('post_time')
    if post_time is not None and str(post_time).strip() not in ('', '0'):
        return f"{app}|pt|{int(post_time)}"
    if isinstance(fecha, datetime):
        ts = fecha.replace(microsecond=0).isoformat()
    else:
        ts = str(fecha)[:19]
    return f"{app}|ts|{ts}"


def _find_duplicate_notificacion(data: dict, fecha: datetime):
    fp = _notificacion_fingerprint(data, fecha)
    post_time = data.get('post_time')
    if post_time is not None and str(post_time).strip() not in ('', '0'):
        pt = int(post_time)
        existente = Notificacion.query.filter_by(post_time=pt).first()
        if existente:
            return existente
    recientes = Notificacion.query.order_by(Notificacion.fecha_recibido.desc()).limit(300).all()
    for n in recientes:
        existing = {
            'app_name': n.app_name,
            'post_time': n.post_time,
        }
        if _notificacion_fingerprint(existing, n.fecha_recibido) == fp:
            return n
    return None


def _ingreso_fingerprint(monto, fecha_mov, descripcion, fuente):
    desc = re.sub(r'\s+', ' ', (descripcion or '').lower().strip())[:100]
    fuente_l = (fuente or 'yape').lower()
    return f"{round(float(monto or 0), 2)}|{fecha_mov}|{fuente_l}|{desc}"


def _find_duplicate_ingreso(monto, fecha_mov, categoria, descripcion, fuente):
    fp = _ingreso_fingerprint(monto, fecha_mov, descripcion, fuente)
    candidatos = Ingreso.query.filter(Ingreso.fecha == fecha_mov).all()
    for ing in candidatos:
        if _ingreso_fingerprint(ing.monto, ing.fecha, ing.descripcion, ing.fuente) == fp:
            return ing
    return None


def _find_duplicate_egreso(monto, fecha_mov, categoria, descripcion, tienda):
    return Egreso.query.filter(
        Egreso.monto == monto,
        Egreso.fecha == fecha_mov,
        Egreso.categoria == categoria,
        Egreso.descripcion == (descripcion or ''),
        Egreso.tienda == tienda
    ).first()


def _dedupe_movimientos(movs):
    seen = set()
    result = []
    for m in movs or []:
        mid = m.get('id')
        key = f"{m.get('tipo')}|{mid}" if mid else f"{m.get('tipo')}|{m.get('fecha')}|{m.get('monto')}|{id(m)}"
        if key in seen:
            continue
        seen.add(key)
        result.append(m)
    result.sort(key=lambda x: x.get('fecha', ''), reverse=True)
    return result


def _dedupe_ingresos_totals():
    """Suma todos los ingresos (cada yapeo = un registro)."""
    total = db.session.query(func.sum(Ingreso.monto)).scalar() or 0.0
    hoy = date.today()
    hoy_total = db.session.query(func.sum(Ingreso.monto)).filter(
        Ingreso.fecha == hoy
    ).scalar() or 0.0
    return hoy_total, total


def require_api_key(f):
    """Decorador para endpoints que requieren API key."""
    @wraps(f)
    def decorated(*args, **kwargs):
        raw_key = request.headers.get('X-API-Key', '')
        if not raw_key:
            return _json_error('Se requiere header X-API-Key', 401)

        key_hash = _hash_key(raw_key)
        api_key = ApiKey.query.filter_by(key_hash=key_hash, activa=True).first()
        if not api_key:
            return _json_error('API key inválida o desactivada', 401)

        # Rate limiting
        if not _check_rate_limit(key_hash, api_key.rate_limit):
            return _json_error(
                f'Límite de velocidad excedido ({api_key.rate_limit} req/min)', 429
            )

        g.api_key = api_key
        return f(*args, **kwargs)
    return decorated


def _api_key_has_permission(api_key, permission: str) -> bool:
    permissions = {item.strip() for item in (api_key.permisos or '').split(',') if item.strip()}
    return 'admin' in permissions or permission in permissions


def _authenticate_api_key(permission: str):
    raw_key = request.headers.get('X-API-Key', '')
    if not raw_key:
        return None, _json_error('Se requiere header X-API-Key', 401)
    key_hash = _hash_key(raw_key)
    api_key = ApiKey.query.filter_by(key_hash=key_hash, activa=True).first()
    if not api_key:
        return None, _json_error('API key invalida o desactivada', 401)
    if not _api_key_has_permission(api_key, permission):
        return None, _json_error(f'La API key no posee el permiso {permission}', 403)
    if not _check_rate_limit(key_hash, api_key.rate_limit):
        return None, _json_error('Limite de velocidad excedido', 429)
    return api_key, None


# ========================================================================
# BEFORE REQUEST — log de conexiones
# ========================================================================

@app.before_request
def log_conexion():
    if request.path.startswith('/api/'):
        try:
            ip = request.headers.get('X-Forwarded-For', request.remote_addr)
            if ip and ',' in ip:
                ip = ip.split(',')[0].strip()
            conn = Conexion(
                ip_address=ip or 'desconocido',
                user_agent=request.headers.get('User-Agent', '')[:500],
                endpoint=request.path,
                method=request.method
            )
            db.session.add(conn)
            db.session.commit()
        except Exception as e:
            logger.warning(f"Error registrando conexión: {e}")
            db.session.rollback()


# ========================================================================
# ERROR HANDLERS
# ========================================================================

@app.errorhandler(404)
def not_found(e):
    return _json_error('Recurso no encontrado', 404)


@app.errorhandler(405)
def method_not_allowed(e):
    return _json_error('Método no permitido', 405)


@app.errorhandler(500)
def internal_error(e):
    db.session.rollback()
    return _json_error('Error interno del servidor', 500)


# ========================================================================
# ENDPOINTS — APP MÓVIL (sin autenticación)
# ========================================================================

# --- Ping / status rápido ---------------------------------------------

@app.route('/api/ping', methods=['GET'])
def ping():
    """Verificar que la API está activa.
    ---
    tags:
      - Sistema
    responses:
      200:
        description: API funcionando
    """
    return jsonify({"status": "ok", "message": "Yape Tracker API funcionando"})


# --- Conexiones --------------------------------------------------------

@app.route('/api/conexiones', methods=['GET'])
def get_conexiones():
    """Obtener últimas conexiones registradas.
    ---
    tags:
      - Conexiones
    responses:
      200:
        description: Lista de conexiones
    """
    conexiones = Conexion.query.order_by(Conexion.timestamp.desc()).limit(50).all()
    count = Conexion.query.count()
    ips_distintas = db.session.query(Conexion.ip_address).distinct().count()
    return jsonify({
        'total': count,
        'ips_distintas': ips_distintas,
        'conexiones': [c.to_dict() for c in conexiones]
    })


# --- Notificaciones ----------------------------------------------------

@app.route('/api/notificaciones', methods=['GET', 'POST'])
def handle_notificaciones():
    """Gestionar notificaciones capturadas.
    ---
    tags:
      - Notificaciones
    parameters:
      - name: body
        in: body
        required: false
        description: Datos de la notificación (POST)
        schema:
          type: object
          properties:
            app_name:
              type: string
            titulo:
              type: string
            cuerpo:
              type: string
            categoria:
              type: string
            monto:
              type: number
            tipo:
              type: string
              enum: [ingreso, egreso, otro]
            leido:
              type: boolean
            fecha_recibido:
              type: string
              format: date-time
    responses:
      200:
        description: Lista de notificaciones (GET)
      201:
        description: Notificación creada (POST)
    """
    if request.method == 'POST':
        _, auth_error = _authenticate_api_key('ingest')
        if auth_error:
            return auth_error
        data = request.json
        if not data or 'app_name' not in data:
            return _json_error('Se requiere app_name', 400)

        if not _is_yape_notification(data.get('app_name', ''), data.get('categoria', '')):
            return _json_error('Solo se aceptan notificaciones de la app Yape', 400)

        fecha = datetime.fromisoformat(data['fecha_recibido']) if 'fecha_recibido' in data else datetime.utcnow()
        post_time_raw = data.get('post_time')
        post_time = int(post_time_raw) if post_time_raw not in (None, '', 0, '0') else None

        existente = _find_duplicate_notificacion(data, fecha)
        if existente:
            return jsonify(existente.to_dict()), 200

        nueva = Notificacion(
            app_name=_normalize_app_name(data['app_name']),
            titulo=data.get('titulo', ''),
            cuerpo=data.get('cuerpo', ''),
            categoria=data.get('categoria', ''),
            monto=data.get('monto', 0.0),
            tipo=data.get('tipo', ''),
            leido=data.get('leido', False),
            fecha_recibido=fecha,
            post_time=post_time
        )
        db.session.add(nueva)
        db.session.commit()

        # Auto-crear ingreso por cada notificación Yape nueva (misma persona/monto = OK)
        if _is_yape_notification(nueva.app_name, nueva.categoria) and nueva.tipo == 'ingreso' and nueva.monto > 0:
            fuente = 'Yape'
            categoria = 'Yape'
            hora = fecha.strftime('%H:%M:%S')
            descripcion = (nueva.titulo or 'Yape').strip()
            if nueva.cuerpo and nueva.cuerpo not in descripcion:
                descripcion = f"{descripcion} — {nueva.cuerpo}".strip()[:200]
            descripcion = f"{descripcion} [{hora}]"
            ingreso = Ingreso(
                monto=nueva.monto,
                categoria=categoria,
                descripcion=descripcion,
                fecha=fecha.date(),
                fuente=fuente
            )
            db.session.add(ingreso)
            db.session.commit()

        return jsonify(nueva.to_dict()), 201
    else:
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 50, type=int)
        app_filter = request.args.get('app', '')
        tipo_filter = request.args.get('tipo', '')
        desde = request.args.get('desde', '')
        hasta = request.args.get('hasta', '')

        query = Notificacion.query

        if app_filter:
            query = query.filter(Notificacion.app_name == app_filter)
        if tipo_filter:
            query = query.filter(Notificacion.tipo == tipo_filter)
        if desde:
            query = query.filter(Notificacion.fecha_recibido >= datetime.fromisoformat(desde))
        if hasta:
            query = query.filter(Notificacion.fecha_recibido <= datetime.fromisoformat(hasta))

        all_items = query.order_by(Notificacion.fecha_recibido.desc()).all()
        yape_items = [n for n in all_items if _is_yape_notification(n.app_name, n.categoria)]
        total = len(yape_items)

        start = (page - 1) * per_page
        end = start + per_page
        page_items = yape_items[start:end]

        apps_list = ['yape']

        return jsonify({
            'total': total,
            'page': page,
            'per_page': per_page,
            'apps_disponibles': apps_list,
            'notificaciones': [n.to_dict() for n in page_items]
        })


@app.route('/api/notificaciones/<int:id>', methods=['DELETE'])
def delete_notificacion(id):
    """Eliminar una notificación.
    ---
    tags:
      - Notificaciones
    parameters:
      - name: id
        in: path
        type: integer
        required: true
    responses:
      200:
        description: Notificación eliminada
    """
    n = Notificacion.query.get_or_404(id)
    db.session.delete(n)
    db.session.commit()
    return jsonify({'mensaje': 'Notificación eliminada'})


@app.route('/api/notificaciones/limpiar', methods=['DELETE'])
def limpiar_notificaciones():
    """Eliminar todas las notificaciones.
    ---
    tags:
      - Notificaciones
    responses:
      200:
        description: Todas las notificaciones eliminadas
    """
    cnt = Notificacion.query.delete()
    db.session.commit()
    return jsonify({'mensaje': f'{cnt} notificaciones eliminadas'})


@app.route('/api/notificaciones/whatsapp', methods=['DELETE'])
def limpiar_whatsapp_notificaciones():
    """Eliminar notificaciones provenientes de WhatsApp."""
    items = Notificacion.query.all()
    eliminadas = 0
    for n in items:
        app_lower = (n.app_name or '').lower()
        texto = f"{n.titulo or ''} {n.cuerpo or ''}".lower()
        if 'whatsapp' in app_lower or ('whatsapp' in texto and 'yape' not in texto and 'plin' not in texto):
            db.session.delete(n)
            eliminadas += 1
    db.session.commit()
    return jsonify({'eliminadas': eliminadas})


@app.route('/api/datos/reset', methods=['DELETE'])
def reset_all_data():
    """Borrar ingresos, egresos, notificaciones y contactos."""
    ingresos = Ingreso.query.delete()
    egresos = Egreso.query.delete()
    notificaciones = Notificacion.query.delete()
    contactos = Contacto.query.delete()
    db.session.commit()
    logger.info('Reset datos: %s ingresos, %s egresos, %s notifs, %s contactos',
                ingresos, egresos, notificaciones, contactos)
    return jsonify({
        'ingresos': ingresos,
        'egresos': egresos,
        'notificaciones': notificaciones,
        'contactos': contactos
    })


# --- Ingresos ----------------------------------------------------------

@app.route('/api/ingresos', methods=['GET', 'POST'])
def handle_ingresos():
    """Gestionar ingresos.
    ---
    tags:
      - Ingresos
    parameters:
      - name: body
        in: body
        required: false
        schema:
          type: object
          properties:
            monto:
              type: number
            categoria:
              type: string
            descripcion:
              type: string
            fecha:
              type: string
              format: date
            fuente:
              type: string
    responses:
      200:
        description: Lista de ingresos (GET)
      201:
        description: Ingreso creado (POST)
    """
    if request.method == 'POST':
        data = request.json
        if not data:
            return _json_error('Se requiere body JSON', 400)
        required = ['monto', 'categoria', 'fecha', 'fuente']
        missing = [f for f in required if f not in data]
        if missing:
            return _json_error(f'Campos requeridos faltantes: {", ".join(missing)}', 400)

        nuevo = Ingreso(
            monto=data['monto'],
            categoria=data['categoria'],
            descripcion=data.get('descripcion', ''),
            fecha=datetime.fromisoformat(data['fecha']).date(),
            fuente=data['fuente']
        )
        db.session.add(nuevo)
        db.session.commit()
        return jsonify(nuevo.to_dict()), 201
    else:
        ingresos = Ingreso.query.order_by(Ingreso.fecha.desc(), Ingreso.created_at.desc()).all()
        return jsonify([i.to_dict() for i in ingresos])


@app.route('/api/ingresos/<int:id>', methods=['PUT', 'DELETE'])
def handle_ingreso_id(id):
    """Editar o eliminar un ingreso.
    ---
    tags:
      - Ingresos
    parameters:
      - name: id
        in: path
        type: integer
        required: true
    responses:
      200:
        description: Ingreso actualizado/eliminado
    """
    ingreso = Ingreso.query.get_or_404(id)
    if request.method == 'DELETE':
        db.session.delete(ingreso)
        db.session.commit()
        return jsonify({'mensaje': 'Ingreso eliminado'})
    else:
        data = request.json
        ingreso.monto = data.get('monto', ingreso.monto)
        ingreso.categoria = data.get('categoria', ingreso.categoria)
        ingreso.descripcion = data.get('descripcion', ingreso.descripcion)
        if 'fecha' in data:
            ingreso.fecha = datetime.fromisoformat(data['fecha']).date()
        ingreso.fuente = data.get('fuente', ingreso.fuente)
        db.session.commit()
        return jsonify(ingreso.to_dict())


# --- Egresos -----------------------------------------------------------

@app.route('/api/egresos', methods=['GET', 'POST'])
def handle_egresos():
    """Gestionar egresos.
    ---
    tags:
      - Egresos
    parameters:
      - name: body
        in: body
        required: false
        schema:
          type: object
          properties:
            monto:
              type: number
            categoria:
              type: string
            descripcion:
              type: string
            fecha:
              type: string
              format: date
            tienda:
              type: string
    responses:
      200:
        description: Lista de egresos (GET)
      201:
        description: Egreso creado (POST)
    """
    if request.method == 'POST':
        data = request.json
        if not data:
            return _json_error('Se requiere body JSON', 400)
        required = ['monto', 'categoria', 'fecha', 'tienda']
        missing = [f for f in required if f not in data]
        if missing:
            return _json_error(f'Campos requeridos faltantes: {", ".join(missing)}', 400)

        nuevo = Egreso(
            monto=data['monto'],
            categoria=data['categoria'],
            descripcion=data.get('descripcion', ''),
            fecha=datetime.fromisoformat(data['fecha']).date(),
            tienda=data['tienda']
        )
        db.session.add(nuevo)
        db.session.commit()
        return jsonify(nuevo.to_dict()), 201
    else:
        egresos = Egreso.query.order_by(Egreso.fecha.desc(), Egreso.created_at.desc()).all()
        return jsonify([e.to_dict() for e in egresos])


@app.route('/api/egresos/<int:id>', methods=['PUT', 'DELETE'])
def handle_egreso_id(id):
    """Editar o eliminar un egreso.
    ---
    tags:
      - Egresos
    parameters:
      - name: id
        in: path
        type: integer
        required: true
    responses:
      200:
        description: Egreso actualizado/eliminado
    """
    egreso = Egreso.query.get_or_404(id)
    if request.method == 'DELETE':
        db.session.delete(egreso)
        db.session.commit()
        return jsonify({'mensaje': 'Egreso eliminado'})
    else:
        data = request.json
        egreso.monto = data.get('monto', egreso.monto)
        egreso.categoria = data.get('categoria', egreso.categoria)
        egreso.descripcion = data.get('descripcion', egreso.descripcion)
        if 'fecha' in data:
            egreso.fecha = datetime.fromisoformat(data['fecha']).date()
        egreso.tienda = data.get('tienda', egreso.tienda)
        db.session.commit()
        return jsonify(egreso.to_dict())


# --- Dashboard ---------------------------------------------------------

@app.route('/api/dashboard', methods=['GET'])
def dashboard_stats():
    """Estadísticas del dashboard.
    ---
    tags:
      - Dashboard
    responses:
      200:
        description: Resumen de ingresos, egresos, saldo y últimos movimientos
    """
    hoy = date.today()

    ingresos_hoy, total_ingresos = _dedupe_ingresos_totals()
    total_egresos = db.session.query(func.sum(Egreso.monto)).scalar() or 0.0
    egresos_hoy = db.session.query(func.sum(Egreso.monto)).filter(
        Egreso.fecha == hoy
    ).scalar() or 0.0

    ultimos = []
    for i in Ingreso.query.order_by(Ingreso.fecha.desc(), Ingreso.created_at.desc()).limit(30).all():
        ultimos.append({'tipo': 'ingreso', **i.to_dict()})
    ultimos = _dedupe_movimientos(ultimos)[:10]

    return jsonify({
        'total_ingresos_hoy': ingresos_hoy,
        'total_egresos_hoy': egresos_hoy,
        'saldo_hoy': ingresos_hoy - egresos_hoy,
        'total_ingresos': total_ingresos,
        'total_egresos': total_egresos,
        'saldo_total': total_ingresos - total_egresos,
        'ultimos_movimientos': ultimos
    })


# ========================================================================
# ENDPOINTS — API PÚBLICA (requieren API key)
# ========================================================================

# --- Auth: Registrar API key -------------------------------------------

@app.route('/api/auth/register', methods=['POST'])
def register_api_key():
    """Registrar una nueva API key (requiere MASTER_API_KEY).
    ---
    tags:
      - Autenticación
    parameters:
      - name: X-API-Key
        in: header
        type: string
        required: true
        description: Master API key
      - name: body
        in: body
        required: true
        schema:
          type: object
          required:
            - nombre
          properties:
            nombre:
              type: string
              description: Nombre descriptivo para la API key
            permisos:
              type: string
              description: "Permisos separados por coma (read,write,admin)"
              default: read
            rate_limit:
              type: integer
              description: Límite de peticiones por minuto
              default: 100
    responses:
      201:
        description: API key creada exitosamente
      401:
        description: Master key inválida
    """
    master_key = request.headers.get('X-API-Key', '')
    if master_key != MASTER_API_KEY:
        return _json_error('Se requiere la master API key válida', 401)

    data = request.json
    if not data or 'nombre' not in data:
        return _json_error('Se requiere el campo "nombre"', 400)

    # Generar key
    raw_key = secrets.token_urlsafe(32)
    key_hash = _hash_key(raw_key)

    nueva = ApiKey(
        key_hash=key_hash,
        nombre=data['nombre'],
        permisos=data.get('permisos', 'read'),
        rate_limit=data.get('rate_limit', 100),
        activa=True
    )
    db.session.add(nueva)
    db.session.commit()

    return jsonify({
        'mensaje': 'API key creada exitosamente',
        'api_key': raw_key,
        'advertencia': 'Guarda esta key, no se mostrará de nuevo',
        'detalle': nueva.to_dict()
    }), 201


# --- Auth: Validar API key ---------------------------------------------

@app.route('/api/auth/validate', methods=['GET'])
@require_api_key
def validate_api_key():
    """Validar una API key existente.
    ---
    tags:
      - Autenticación
    security:
      - ApiKeyAuth: []
    responses:
      200:
        description: API key válida
      401:
        description: API key inválida
    """
    return jsonify({
        'valida': True,
        'detalle': g.api_key.to_dict()
    })


# --- Verificar pago (fuzzy matching) -----------------------------------

@app.route('/api/verify-payment', methods=['GET'])
@require_api_key
def verify_payment():
    """Verificar si un pago fue recibido (coincidencia aproximada).
    ---
    tags:
      - Verificación de Pagos
    security:
      - ApiKeyAuth: []
    parameters:
      - name: monto
        in: query
        type: number
        required: true
        description: Monto esperado del pago
      - name: nombre
        in: query
        type: string
        required: true
        description: Nombre del pagador (coincidencia parcial)
      - name: fecha
        in: query
        type: string
        format: date
        required: true
        description: Fecha aproximada del pago (YYYY-MM-DD)
    responses:
      200:
        description: Resultado de la verificación
      400:
        description: Parámetros faltantes
    """
    monto_str = request.args.get('monto', '')
    nombre = request.args.get('nombre', '')
    fecha_str = request.args.get('fecha', '')

    if not monto_str or not nombre or not fecha_str:
        return _json_error('Se requieren parámetros: monto, nombre, fecha', 400)

    try:
        monto = float(monto_str)
    except ValueError:
        return _json_error('El monto debe ser un número válido', 400)

    try:
        fecha = datetime.fromisoformat(fecha_str).date()
    except ValueError:
        return _json_error('Fecha inválida, usar formato YYYY-MM-DD', 400)

    # Tolerancias
    monto_min = monto * 0.99  # -1%
    monto_max = monto * 1.01  # +1%
    fecha_min = fecha - timedelta(days=1)
    fecha_max = fecha + timedelta(days=1)
    nombre_lower = nombre.lower()

    # Buscar en ingresos
    ingresos = Ingreso.query.filter(
        Ingreso.monto >= monto_min,
        Ingreso.monto <= monto_max,
        Ingreso.fecha >= fecha_min,
        Ingreso.fecha <= fecha_max
    ).all()

    coincidencias = []
    for ing in ingresos:
        # Match parcial case-insensitive en fuente o descripcion
        fuente_lower = (ing.fuente or '').lower()
        desc_lower = (ing.descripcion or '').lower()
        if nombre_lower in fuente_lower or nombre_lower in desc_lower or fuente_lower in nombre_lower:
            coincidencias.append({
                'id': ing.id,
                'monto': ing.monto,
                'fecha': ing.fecha.isoformat(),
                'fuente': ing.fuente,
                'descripcion': ing.descripcion or '',
                'diferencia_monto': round(abs(ing.monto - monto), 2),
                'diferencia_dias': abs((ing.fecha - fecha).days)
            })

    verificado = len(coincidencias) > 0

    return jsonify({
        'verificado': verificado,
        'monto_buscado': monto,
        'nombre_buscado': nombre,
        'fecha_buscada': fecha.isoformat(),
        'tolerancia_monto': '±1%',
        'tolerancia_fecha': '±1 día',
        'coincidencias': len(coincidencias),
        'resultados': coincidencias
    })


# --- Balance -----------------------------------------------------------

@app.route('/api/balance', methods=['GET'])
@require_api_key
def get_balance():
    """Obtener balance actual.
    ---
    tags:
      - Balance
    security:
      - ApiKeyAuth: []
    responses:
      200:
        description: Balance total y del mes actual
    """
    hoy = date.today()
    primer_dia_mes = hoy.replace(day=1)

    total_ingresos = db.session.query(func.sum(Ingreso.monto)).scalar() or 0.0
    total_egresos = db.session.query(func.sum(Egreso.monto)).scalar() or 0.0

    ingresos_mes = db.session.query(func.sum(Ingreso.monto)).filter(
        Ingreso.fecha >= primer_dia_mes
    ).scalar() or 0.0
    egresos_mes = db.session.query(func.sum(Egreso.monto)).filter(
        Egreso.fecha >= primer_dia_mes
    ).scalar() or 0.0

    ingresos_hoy = db.session.query(func.sum(Ingreso.monto)).filter(
        Ingreso.fecha == hoy
    ).scalar() or 0.0
    egresos_hoy = db.session.query(func.sum(Egreso.monto)).filter(
        Egreso.fecha == hoy
    ).scalar() or 0.0

    return jsonify({
        'balance_total': round(total_ingresos - total_egresos, 2),
        'total_ingresos': round(total_ingresos, 2),
        'total_egresos': round(total_egresos, 2),
        'mes_actual': {
            'ingresos': round(ingresos_mes, 2),
            'egresos': round(egresos_mes, 2),
            'balance': round(ingresos_mes - egresos_mes, 2),
            'periodo': primer_dia_mes.isoformat()
        },
        'hoy': {
            'ingresos': round(ingresos_hoy, 2),
            'egresos': round(egresos_hoy, 2),
            'balance': round(ingresos_hoy - egresos_hoy, 2),
            'fecha': hoy.isoformat()
        }
    })


# --- Analytics: Resumen mensual ----------------------------------------

@app.route('/api/analytics/monthly', methods=['GET'])
@require_api_key
def analytics_monthly():
    """Resumen mensual de ingresos y egresos.
    ---
    tags:
      - Analytics
    security:
      - ApiKeyAuth: []
    parameters:
      - name: months
        in: query
        type: integer
        default: 6
        description: Cantidad de meses a analizar
    responses:
      200:
        description: Resumen mensual
    """
    months = request.args.get('months', 6, type=int)
    months = min(max(months, 1), 24)

    hoy = date.today()
    fecha_inicio = (hoy.replace(day=1) - timedelta(days=1))
    for _ in range(months - 1):
        fecha_inicio = (fecha_inicio.replace(day=1) - timedelta(days=1))
    fecha_inicio = fecha_inicio.replace(day=1)

    # Ingresos por mes
    ingresos_q = db.session.query(
        extract('year', Ingreso.fecha).label('anio'),
        extract('month', Ingreso.fecha).label('mes'),
        func.sum(Ingreso.monto).label('total'),
        func.count(Ingreso.id).label('cantidad')
    ).filter(
        Ingreso.fecha >= fecha_inicio
    ).group_by(
        extract('year', Ingreso.fecha),
        extract('month', Ingreso.fecha)
    ).all()

    # Egresos por mes
    egresos_q = db.session.query(
        extract('year', Egreso.fecha).label('anio'),
        extract('month', Egreso.fecha).label('mes'),
        func.sum(Egreso.monto).label('total'),
        func.count(Egreso.id).label('cantidad')
    ).filter(
        Egreso.fecha >= fecha_inicio
    ).group_by(
        extract('year', Egreso.fecha),
        extract('month', Egreso.fecha)
    ).all()

    # Combinar en diccionario
    datos = {}
    for row in ingresos_q:
        key = f"{int(row.anio)}-{int(row.mes):02d}"
        datos[key] = {
            'periodo': key,
            'ingresos': round(float(row.total or 0), 2),
            'ingresos_count': int(row.cantidad or 0),
            'egresos': 0.0,
            'egresos_count': 0
        }

    for row in egresos_q:
        key = f"{int(row.anio)}-{int(row.mes):02d}"
        if key not in datos:
            datos[key] = {
                'periodo': key,
                'ingresos': 0.0,
                'ingresos_count': 0,
                'egresos': 0.0,
                'egresos_count': 0
            }
        datos[key]['egresos'] = round(float(row.total or 0), 2)
        datos[key]['egresos_count'] = int(row.cantidad or 0)

    # Agregar balance
    for key in datos:
        datos[key]['balance'] = round(datos[key]['ingresos'] - datos[key]['egresos'], 2)

    # Ordenar por periodo
    resultado = sorted(datos.values(), key=lambda x: x['periodo'])

    return jsonify({
        'meses_analizados': months,
        'desde': fecha_inicio.isoformat(),
        'hasta': hoy.isoformat(),
        'datos': resultado
    })


# --- Analytics: Categorías ---------------------------------------------

@app.route('/api/analytics/categories', methods=['GET'])
@require_api_key
def analytics_categories():
    """Desglose por categorías.
    ---
    tags:
      - Analytics
    security:
      - ApiKeyAuth: []
    parameters:
      - name: tipo
        in: query
        type: string
        enum: [ingreso, egreso]
        default: egreso
        description: Tipo de transacción
      - name: periodo
        in: query
        type: string
        enum: [week, month, year]
        default: month
        description: Periodo de análisis
    responses:
      200:
        description: Desglose por categorías
    """
    tipo = request.args.get('tipo', 'egreso')
    periodo = request.args.get('periodo', 'month')

    hoy = date.today()
    if periodo == 'week':
        fecha_inicio = hoy - timedelta(days=7)
    elif periodo == 'year':
        fecha_inicio = hoy.replace(month=1, day=1)
    else:  # month
        fecha_inicio = hoy.replace(day=1)

    if tipo == 'ingreso':
        resultados = db.session.query(
            Ingreso.categoria,
            func.sum(Ingreso.monto).label('total'),
            func.count(Ingreso.id).label('cantidad')
        ).filter(
            Ingreso.fecha >= fecha_inicio
        ).group_by(
            Ingreso.categoria
        ).order_by(
            func.sum(Ingreso.monto).desc()
        ).all()
    else:
        resultados = db.session.query(
            Egreso.categoria,
            func.sum(Egreso.monto).label('total'),
            func.count(Egreso.id).label('cantidad')
        ).filter(
            Egreso.fecha >= fecha_inicio
        ).group_by(
            Egreso.categoria
        ).order_by(
            func.sum(Egreso.monto).desc()
        ).all()

    gran_total = sum(float(r.total or 0) for r in resultados)

    categorias = []
    for r in resultados:
        total = round(float(r.total or 0), 2)
        categorias.append({
            'categoria': r.categoria,
            'total': total,
            'cantidad': int(r.cantidad or 0),
            'porcentaje': round((total / gran_total * 100) if gran_total > 0 else 0, 1)
        })

    return jsonify({
        'tipo': tipo,
        'periodo': periodo,
        'desde': fecha_inicio.isoformat(),
        'hasta': hoy.isoformat(),
        'total_general': round(gran_total, 2),
        'categorias': categorias
    })


# --- Analytics: Tendencias ---------------------------------------------

@app.route('/api/analytics/trends', methods=['GET'])
@require_api_key
def analytics_trends():
    """Análisis de tendencias (comparación mes a mes).
    ---
    tags:
      - Analytics
    security:
      - ApiKeyAuth: []
    parameters:
      - name: months
        in: query
        type: integer
        default: 3
        description: Cantidad de meses a comparar
    responses:
      200:
        description: Tendencias y comparaciones mensuales
    """
    months = request.args.get('months', 3, type=int)
    months = min(max(months, 2), 12)

    hoy = date.today()
    periodos = []
    for i in range(months):
        d = hoy.replace(day=1) - timedelta(days=1)
        for _ in range(i):
            d = d.replace(day=1) - timedelta(days=1)
        mes_inicio = d.replace(day=1) if i > 0 else hoy.replace(day=1)
        if i == 0:
            mes_fin = hoy
        else:
            next_month = mes_inicio.replace(day=28) + timedelta(days=4)
            mes_fin = next_month.replace(day=1) - timedelta(days=1)

        ing_total = db.session.query(func.sum(Ingreso.monto)).filter(
            Ingreso.fecha >= mes_inicio,
            Ingreso.fecha <= mes_fin
        ).scalar() or 0.0

        eg_total = db.session.query(func.sum(Egreso.monto)).filter(
            Egreso.fecha >= mes_inicio,
            Egreso.fecha <= mes_fin
        ).scalar() or 0.0

        periodos.append({
            'periodo': mes_inicio.strftime('%Y-%m'),
            'desde': mes_inicio.isoformat(),
            'hasta': mes_fin.isoformat(),
            'ingresos': round(float(ing_total), 2),
            'egresos': round(float(eg_total), 2),
            'balance': round(float(ing_total) - float(eg_total), 2)
        })

    # Calcular cambios porcentuales mes a mes
    for i in range(1, len(periodos)):
        prev = periodos[i]
        curr = periodos[i - 1]
        if prev['ingresos'] > 0:
            curr['cambio_ingresos_pct'] = round(
                ((curr['ingresos'] - prev['ingresos']) / prev['ingresos']) * 100, 1
            )
        else:
            curr['cambio_ingresos_pct'] = 0.0

        if prev['egresos'] > 0:
            curr['cambio_egresos_pct'] = round(
                ((curr['egresos'] - prev['egresos']) / prev['egresos']) * 100, 1
            )
        else:
            curr['cambio_egresos_pct'] = 0.0

    # Promedios
    avg_ingresos = sum(p['ingresos'] for p in periodos) / len(periodos) if periodos else 0
    avg_egresos = sum(p['egresos'] for p in periodos) / len(periodos) if periodos else 0

    return jsonify({
        'meses_analizados': months,
        'promedio_ingresos': round(avg_ingresos, 2),
        'promedio_egresos': round(avg_egresos, 2),
        'promedio_balance': round(avg_ingresos - avg_egresos, 2),
        'periodos': periodos
    })


# --- Analytics: Contactos frecuentes -----------------------------------

@app.route('/api/analytics/contacts', methods=['GET'])
@require_api_key
def analytics_contacts():
    """Contactos/fuentes más frecuentes.
    ---
    tags:
      - Analytics
    security:
      - ApiKeyAuth: []
    parameters:
      - name: limit
        in: query
        type: integer
        default: 10
        description: Cantidad máxima de contactos a retornar
    responses:
      200:
        description: Top contactos por frecuencia de transacciones
    """
    limit = request.args.get('limit', 10, type=int)
    limit = min(max(limit, 1), 50)

    # Top fuentes de ingresos
    top_fuentes = db.session.query(
        Ingreso.fuente,
        func.count(Ingreso.id).label('frecuencia'),
        func.sum(Ingreso.monto).label('total_monto'),
        func.max(Ingreso.fecha).label('ultima')
    ).group_by(
        Ingreso.fuente
    ).order_by(
        func.count(Ingreso.id).desc()
    ).limit(limit).all()

    # Top tiendas de egresos
    top_tiendas = db.session.query(
        Egreso.tienda,
        func.count(Egreso.id).label('frecuencia'),
        func.sum(Egreso.monto).label('total_monto'),
        func.max(Egreso.fecha).label('ultima')
    ).group_by(
        Egreso.tienda
    ).order_by(
        func.count(Egreso.id).desc()
    ).limit(limit).all()

    fuentes = [{
        'nombre': r.fuente,
        'tipo': 'ingreso',
        'frecuencia': int(r.frecuencia),
        'total_monto': round(float(r.total_monto or 0), 2),
        'ultima_transaccion': r.ultima.isoformat() if r.ultima else None
    } for r in top_fuentes]

    tiendas = [{
        'nombre': r.tienda,
        'tipo': 'egreso',
        'frecuencia': int(r.frecuencia),
        'total_monto': round(float(r.total_monto or 0), 2),
        'ultima_transaccion': r.ultima.isoformat() if r.ultima else None
    } for r in top_tiendas]

    # Combinar y ordenar por frecuencia
    todos = fuentes + tiendas
    todos.sort(key=lambda x: x['frecuencia'], reverse=True)

    return jsonify({
        'limit': limit,
        'total_contactos': len(todos),
        'contactos': todos[:limit],
        'top_fuentes_ingreso': fuentes,
        'top_tiendas_egreso': tiendas
    })


# --- Health check -------------------------------------------------------

@app.route('/api/health', methods=['GET'])
def health_check():
    """Estado de salud del sistema.
    ---
    tags:
      - Sistema
    responses:
      200:
        description: Estado del sistema
    """
    uptime_seconds = time.time() - APP_START_TIME
    days = int(uptime_seconds // 86400)
    hours = int((uptime_seconds % 86400) // 3600)
    minutes = int((uptime_seconds % 3600) // 60)

    # Verificar DB
    db_ok = False
    try:
        db.session.execute(text('SELECT 1'))
        db_ok = True
    except Exception as e:
        logger.error(f"Health check DB error: {e}")

    # Conteos
    try:
        counts = {
            'ingresos': Ingreso.query.count(),
            'egresos': Egreso.query.count(),
            'notificaciones': Notificacion.query.count(),
            'conexiones': Conexion.query.count(),
            'api_keys': ApiKey.query.count()
        }
    except Exception:
        counts = {}

    # Tipo de DB
    db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if 'postgresql' in db_uri:
        db_type = 'PostgreSQL'
    elif 'sqlite' in db_uri:
        db_type = 'SQLite'
    else:
        db_type = 'Desconocido'

    return jsonify({
        'status': 'ok' if db_ok else 'degradado',
        'base_de_datos': {
            'conectada': db_ok,
            'tipo': db_type
        },
        'uptime': {
            'segundos': int(uptime_seconds),
            'legible': f'{days}d {hours}h {minutes}m'
        },
        'conteos': counts,
        'version': '2.0.0',
        'timestamp': datetime.utcnow().isoformat()
    })


# ========================================================================
# MAIN
# ========================================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = environment() == 'development'
    app.run(debug=debug, host='0.0.0.0', port=port)
