"""The admin photo list: its query count must not grow with the number of products."""

from sqlalchemy import event

from app.extensions import db
from app.models import ProductImage
from conftest import HEADERS, NEW_PRODUCT


def count_statements(app, request):
    """How many SQL statements `request()` runs."""
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    with app.app_context():
        event.listen(db.engine, "before_cursor_execute", record)
        try:
            request()
        finally:
            event.remove(db.engine, "before_cursor_execute", record)
    return len(statements)


def add_products_with_photos(app, client, count, photos_each, start=0):
    """Add `count` products, each with `photos_each` photos, through the admin API and the database."""
    for n in range(start, start + count):
        res = client.post("/api/admin/products", json={**NEW_PRODUCT, "name": f"Bulk piece {n}"}, headers=HEADERS)
        assert res.status_code == 201, res.get_json()
        product_id = res.get_json()["product"]["id"]
        with app.app_context():
            for position in range(photos_each):
                db.session.add(
                    ProductImage(id=f"bulk-{product_id}-{position}", product_id=product_id, position=position, alt="Photo")
                )
            db.session.commit()


def test_admin_image_list_query_count_stays_bounded_as_products_grow(app, admin):
    def listing():
        res = admin.get("/api/admin/images")
        assert res.status_code == 200

    add_products_with_photos(app, admin, count=3, photos_each=2)
    before = count_statements(app, listing)

    add_products_with_photos(app, admin, count=12, photos_each=2, start=3)
    after = count_statements(app, listing)

    # One query for the photos, one for their products, one for the registry at most. A
    # per-product query would add about 12 here.
    assert after <= 4, f"{after} queries for the photo list"
    assert after - before <= 1, f"query count grew from {before} to {after} with 12 more products"


def test_admin_image_list_keeps_its_shape_and_order(app, admin):
    add_products_with_photos(app, admin, count=2, photos_each=2)
    rows = admin.get("/api/admin/images").get_json()["images"]
    assert set(rows[0]) == {"id", "alt", "product"}

    # The photos added here, in the order the query gives: product id, then position.
    ours = [row["product"] for row in rows if row["product"].startswith("Bulk piece")]
    assert ours == ["Bulk piece 0", "Bulk piece 0", "Bulk piece 1", "Bulk piece 1"]
    assert isinstance(admin.get("/api/admin/images").get_json()["registry"], dict)


def test_admin_image_list_still_requires_sign_in(app, client):
    assert client.get("/api/admin/images").status_code in (401, 403)
