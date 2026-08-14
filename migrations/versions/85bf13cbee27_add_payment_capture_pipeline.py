"""add payment capture pipeline

Revision ID: 85bf13cbee27
Revises: 
Create Date: 2026-08-13 12:14:47.313106

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '85bf13cbee27'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'capture_device',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('branch_id', sa.String(length=128), nullable=False),
        sa.Column('branch_name', sa.String(length=200), nullable=True),
        sa.Column('installation_id', sa.String(length=160), nullable=False),
        sa.Column('public_key', sa.Text(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('scopes', sa.Text(), nullable=False),
        sa.Column('providers', sa.Text(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('installation_id'),
        sa.UniqueConstraint('token_hash'),
    )
    op.create_index('ix_capture_device_branch_id', 'capture_device', ['branch_id'])
    op.create_index('ix_capture_device_status', 'capture_device', ['status'])

    op.create_table(
        'consumed_pairing_ticket',
        sa.Column('id', sa.String(length=128), nullable=False),
        sa.Column('branch_id', sa.String(length=128), nullable=False),
        sa.Column('device_id', sa.String(length=36), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('consumed_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_consumed_pairing_ticket_branch_id',
        'consumed_pairing_ticket',
        ['branch_id'],
    )

    op.create_table(
        'payment_event',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('idempotency_key', sa.String(length=200), nullable=False),
        sa.Column('provider_event_id', sa.String(length=200), nullable=False),
        sa.Column('source', sa.String(length=40), nullable=False),
        sa.Column('amount_minor', sa.BigInteger(), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('payer_name', sa.String(length=200), nullable=True),
        sa.Column('operation_code', sa.String(length=120), nullable=True),
        sa.Column('occurred_at', sa.DateTime(), nullable=False),
        sa.Column('received_at', sa.DateTime(), nullable=False),
        sa.Column('device_id', sa.String(length=36), nullable=False),
        sa.Column('branch_id', sa.String(length=128), nullable=False),
        sa.Column('post_time', sa.BigInteger(), nullable=True),
        sa.Column('raw_payload_hash', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['device_id'], ['capture_device.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source', 'provider_event_id', name='uq_payment_event_provider'),
        sa.UniqueConstraint(
            'source', 'device_id', 'post_time', 'raw_payload_hash',
            name='uq_payment_event_fallback',
        ),
    )
    op.create_index('ix_payment_event_branch_id', 'payment_event', ['branch_id'])
    op.create_index('ix_payment_event_device_id', 'payment_event', ['device_id'])
    op.create_index('ix_payment_event_occurred_at', 'payment_event', ['occurred_at'])
    op.create_index('ix_payment_event_source', 'payment_event', ['source'])

    op.create_table(
        'payment_delivery_outbox',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('event_id', sa.String(length=36), nullable=False),
        sa.Column('state', sa.String(length=20), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=False),
        sa.Column('lease_until', sa.DateTime(), nullable=True),
        sa.Column('last_error_code', sa.String(length=100), nullable=True),
        sa.Column('last_error_detail', sa.String(length=500), nullable=True),
        sa.Column('delivered_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['event_id'], ['payment_event.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('event_id'),
    )
    op.create_index(
        'ix_payment_delivery_outbox_lease_until',
        'payment_delivery_outbox',
        ['lease_until'],
    )
    op.create_index(
        'ix_payment_delivery_outbox_next_attempt_at',
        'payment_delivery_outbox',
        ['next_attempt_at'],
    )
    op.create_index('ix_payment_delivery_outbox_state', 'payment_delivery_outbox', ['state'])


def downgrade():
    op.drop_index('ix_payment_delivery_outbox_state', table_name='payment_delivery_outbox')
    op.drop_index('ix_payment_delivery_outbox_next_attempt_at', table_name='payment_delivery_outbox')
    op.drop_index('ix_payment_delivery_outbox_lease_until', table_name='payment_delivery_outbox')
    op.drop_table('payment_delivery_outbox')
    op.drop_index('ix_payment_event_source', table_name='payment_event')
    op.drop_index('ix_payment_event_occurred_at', table_name='payment_event')
    op.drop_index('ix_payment_event_device_id', table_name='payment_event')
    op.drop_index('ix_payment_event_branch_id', table_name='payment_event')
    op.drop_table('payment_event')
    op.drop_index('ix_consumed_pairing_ticket_branch_id', table_name='consumed_pairing_ticket')
    op.drop_table('consumed_pairing_ticket')
    op.drop_index('ix_capture_device_status', table_name='capture_device')
    op.drop_index('ix_capture_device_branch_id', table_name='capture_device')
    op.drop_table('capture_device')
