"""Product variation type, designs and design photos

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-10-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    # Every column is nullable and read as "no change" when empty, so existing products (colour or none)
    # keep working without a data update: a null variation_type reads as colour when colors is set.
    with op.batch_alter_table('products') as batch_op:
        batch_op.add_column(sa.Column('variation_type', sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column('designs', sa.JSON(), nullable=True))
    with op.batch_alter_table('product_images') as batch_op:
        batch_op.add_column(sa.Column('role', sa.String(length=10), nullable=True))


def downgrade():
    with op.batch_alter_table('product_images') as batch_op:
        batch_op.drop_column('role')
    with op.batch_alter_table('products') as batch_op:
        batch_op.drop_column('designs')
        batch_op.drop_column('variation_type')
