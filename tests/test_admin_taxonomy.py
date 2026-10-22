"""Admin category and collection responses: cover-photo metadata, grouped product counts, and the
product list reusing photo rows it already loaded."""

import pytest
from sqlalchemy import event

from app.extensions import db
from app.models import Category, Collection, Product, ProductImage

from conftest import HEADERS


@pytest.fixture()
def cloud_covers(app):
    """Give two photos Cloudinary details (as live photos have) and use them as covers."""
    app.config["CLOUDINARY_CLOUD_NAME"] = "snug-test"
    with app.app_context():
        first, second = ProductImage.query.order_by(ProductImage.id).limit(2).all()
        for i, row in enumerate((first, second)):
            row.cloudinary_public_id, row.cloudinary_version = f"snug-co/products/{row.id}", 100 + i
            row.width, row.height = 800, 1000
        category = Category.query.order_by(Category.sort_order).first()
        collection = Collection.query.order_by(Collection.sort_order).first()
        category.image_id, collection.image_id = first.id, second.id
        db.session.commit()
        return {"category": (category.id, first.id), "collection": (collection.id, second.id), "other": first.id}


def _queries(app):
    counter = {"n": 0}
    with app.app_context():
        event.listen(db.engine, "before_cursor_execute", lambda *a, **k: counter.__setitem__("n", counter["n"] + 1))
    return counter


@pytest.mark.parametrize("kind", ["categories", "collections"])
def test_admin_lists_include_cover_metadata_for_covers_only(admin, cloud_covers, kind):
    item_id, image_id = cloud_covers["category" if kind == "categories" else "collection"]
    res = admin.get(f"/api/admin/{kind}")
    assert res.headers["Cache-Control"] == "no-store"
    data = res.get_json()
    item = next(i for i in data["items"] if i["id"] == item_id)
    assert item["image"] == image_id
    meta = data["images"][image_id]
    assert meta["width"] == 800 and meta["height"] == 1000
    assert "res.cloudinary.com/snug-test/" in meta["src"] and "{w}" in meta["src"]
    # Only the covers in use, never the whole photo library.
    assert set(data["images"]) <= {i["image"] for i in data["items"] if i["image"]}


@pytest.mark.parametrize("kind", ["categories", "collections"])
def test_create_and_update_responses_include_cover_metadata(admin, cloud_covers, kind):
    image_id = cloud_covers["other"]
    made = admin.post(f"/api/admin/{kind}", json={"name": "Caps", "image": image_id}, headers=HEADERS)
    assert made.status_code == 201
    body = made.get_json()
    assert body["item"]["image"] == image_id and body["item"]["productCount"] == 0
    assert body["images"][image_id]["src"]
    assert made.headers["Cache-Control"] == "no-store"

    cleared = admin.patch(f"/api/admin/{kind}/{body['item']['id']}", json={"image": None}, headers=HEADERS)
    assert cleared.get_json() == {"item": {**body["item"], "image": None}, "images": {}}
    updated = admin.patch(f"/api/admin/{kind}/{body['item']['id']}", json={"image": image_id}, headers=HEADERS)
    assert updated.get_json()["images"][image_id]["src"]
    assert updated.headers["Cache-Control"] == "no-store"


def test_product_counts_include_unpublished_and_empty_categories(app, admin):
    made = admin.post("/api/admin/categories", json={"name": "Empty shelf"}, headers=HEADERS).get_json()["item"]
    with app.app_context():
        expected_cat = {c.id: len(c.products) for c in Category.query.all()}
        expected_col = {c.id: len(c.products) for c in Collection.query.all()}
        product = Product.query.filter(Product.collections.any()).first()
        product.published = False
        db.session.commit()
    cats = {c["id"]: c["productCount"] for c in admin.get("/api/admin/categories").get_json()["items"]}
    cols = {c["id"]: c["productCount"] for c in admin.get("/api/admin/collections").get_json()["items"]}
    assert cats == expected_cat and cols == expected_col
    assert cats[made["id"]] == 0
    assert sum(cats.values()) > 0 and sum(cols.values()) > 0


@pytest.mark.parametrize("kind", ["categories", "collections"])
def test_listing_cost_does_not_grow_with_the_number_of_rows(app, admin, kind):
    queries = _queries(app)
    queries["n"] = 0
    admin.get(f"/api/admin/{kind}")
    before = queries["n"]
    for n in range(8):
        admin.post(f"/api/admin/{kind}", json={"name": f"Extra {n}"}, headers=HEADERS)
    queries["n"] = 0
    admin.get(f"/api/admin/{kind}")
    assert queries["n"] == before


def test_admin_product_list_reuses_loaded_photo_rows(app, admin, cloud_covers):
    queries = _queries(app)
    statements = []
    with app.app_context():
        event.listen(db.engine, "before_cursor_execute", lambda conn, cur, sql, *a: statements.append(sql))
    queries["n"] = 0
    data = admin.get("/api/admin/products").get_json()
    assert cloud_covers["other"] in data["images"]
    image_queries = [s for s in statements if "FROM product_images" in s]
    assert len(image_queries) == 1, "photos are loaded once, with the products"
