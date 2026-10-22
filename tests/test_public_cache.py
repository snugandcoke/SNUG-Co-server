"""The storefront response cache: what it stores, and that admin changes show up at once on every
worker, not only the one that made the change."""

import pytest
from sqlalchemy import event

from app import create_app, public_cache
from app.extensions import db
from app.models import Category, Product, SiteSettings

from conftest import ADMIN_EMAIL, ADMIN_PASSWORD, HEADERS, NEW_PRODUCT


@pytest.fixture()
def workers(app, tmp_path):
    """Two apps on the same database and stamp file: two gunicorn workers on one instance.
    Each has its own in-memory cache, exactly like separate processes."""
    config = {
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "SQLALCHEMY_DATABASE_URI": app.config["SQLALCHEMY_DATABASE_URI"],
        "LOCAL_UPLOADS": True,
        "UPLOAD_DIR": app.config["UPLOAD_DIR"],
        "SESSION_COOKIE_SECURE": False,
        "CLOUDINARY_CLOUD_NAME": "",
        "CLOUDINARY_API_KEY": "",
        "CLOUDINARY_API_SECRET": "",
        "PUBLIC_CACHE_SECONDS": 30,
        "PUBLIC_CACHE_STAMP": str(tmp_path / "public-cache-stamp"),
    }
    one, two = create_app(config), create_app(config)
    assert public_cache.get_cache(one) is not public_cache.get_cache(two)
    return one, two


def _admin(worker):
    client = worker.test_client()
    res = client.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers=HEADERS)
    assert res.status_code == 200
    return client


def _count_queries(app):
    counter = {"n": 0}

    def count(*args, **kwargs):
        counter["n"] += 1

    with app.app_context():
        event.listen(db.engine, "before_cursor_execute", count)
    return counter


def _first(products):
    return products[0]


# --- what is cached -------------------------------------------------------------------------------


def test_tests_run_without_the_cache_by_default(app):
    assert public_cache.get_cache(app).enabled is False


def test_repeat_requests_skip_the_database_and_keep_the_response_format(workers):
    one, _ = workers
    client = one.test_client()
    queries = _count_queries(one)
    first = client.get("/api/products")
    used = queries["n"]
    assert used > 0
    second = client.get("/api/products")
    assert queries["n"] == used, "the second request was answered without a query"
    assert second.data == first.data
    assert second.headers["Content-Type"] == "application/json"
    assert second.headers["Cache-Control"] == "public, no-cache"
    assert "stale-while-revalidate" not in second.headers["CDN-Cache-Control"]


def test_browsers_revalidate_and_get_304_when_nothing_changed(workers):
    one, _ = workers
    client = one.test_client()
    etag = client.get("/api/home").headers["ETag"]
    again = client.get("/api/home", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.data == b""
    with one.app_context():
        db.session.get(SiteSettings, 1) or db.session.add(SiteSettings(id=1))
        Product.query.first().name = "Renamed"
        db.session.commit()
    changed = client.get("/api/home", headers={"If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["ETag"] != etag


def test_errors_are_not_cached(workers):
    one, _ = workers
    client = one.test_client()
    assert client.get("/api/products/no-such-piece").status_code == 404
    assert public_cache.get_cache(one)._entries == {}


def test_admin_login_session_and_private_routes_are_never_cached(workers):
    one, _ = workers
    admin = _admin(one)
    for path in ("/api/admin/me", "/api/admin/products", "/api/admin/settings"):
        res = admin.get(path)
        assert res.headers["Cache-Control"] == "no-store", path
    login = one.test_client().post(
        "/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers=HEADERS
    )
    assert "public" not in login.headers["Cache-Control"]
    assert all(key.startswith("/api/") and "/admin" not in key for key in public_cache.get_cache(one)._entries)


def test_entries_expire(workers, monkeypatch):
    one, _ = workers
    client = one.test_client()
    now = [1000.0]
    monkeypatch.setattr(public_cache.time, "monotonic", lambda: now[0])
    client.get("/api/products")
    assert public_cache.get_cache(one).get("/api/products")[0] is not None
    now[0] += 31
    assert public_cache.get_cache(one).get("/api/products")[0] is None


def test_memory_is_bounded(workers):
    one, _ = workers
    cache = public_cache.get_cache(one)
    generation = cache.generation()
    for i in range(cache.max_entries + 50):
        cache.put(f"/api/products/p{i}", generation, b"{}")
    assert len(cache._entries) <= cache.max_entries


def test_a_response_built_while_the_catalog_changed_is_never_served(workers):
    """A worker reads the generation, then the database; a commit lands in between."""
    one, _ = workers
    cache = public_cache.get_cache(one)
    _, generation = cache.get("/api/products")
    with one.app_context():
        cache.invalidate()  # the admin's commit, from any worker
    cache.put("/api/products", generation, b'{"old": true}')
    assert cache.get("/api/products")[0] is None


# --- admin changes reach every worker immediately -------------------------------------------------


def _warm(worker, *paths):
    client = worker.test_client()
    for path in paths:
        assert client.get(path).status_code == 200
    return client


def test_product_creation_shows_on_the_other_worker(workers):
    one, two = workers
    shop = _warm(two, "/api/products", "/api/home")
    res = _admin(one).post("/api/admin/products", json=NEW_PRODUCT, headers=HEADERS)
    assert res.status_code == 201
    slugs = [p["slug"] for p in shop.get("/api/products").get_json()["products"]]
    assert "test-lounge-set" in slugs
    assert shop.get("/api/products/test-lounge-set").status_code == 200


def test_product_edit_price_and_stock_changes_show_on_the_other_worker(workers):
    one, two = workers
    shop = _warm(two, "/api/products")
    product = _first(shop.get("/api/products").get_json()["products"])
    slug = product["slug"]
    shop.get(f"/api/products/{slug}")
    with one.app_context():
        pid = Product.query.filter_by(slug=slug).one().id

    admin = _admin(one)
    res = admin.patch(
        f"/api/admin/products/{pid}",
        json={"name": "Renamed piece", "priceKES": 1234, "availability": "sold-out"},
        headers=HEADERS,
    )
    assert res.status_code == 200

    listed = next(p for p in shop.get("/api/products").get_json()["products"] if p["slug"] == slug)
    detail = shop.get(f"/api/products/{slug}").get_json()["product"]
    for item in (listed, detail):
        assert item["name"] == "Renamed piece"
        assert item["priceKES"] == 1234
        assert item["availability"] == "sold-out"


def test_product_deletion_and_unpublishing_show_on_the_other_worker(workers):
    one, two = workers
    shop = _warm(two, "/api/products")
    first, second = shop.get("/api/products").get_json()["products"][:2]
    for p in (first, second):
        shop.get(f"/api/products/{p['slug']}")
    with one.app_context():
        ids = {s: Product.query.filter_by(slug=s).one().id for s in (first["slug"], second["slug"])}

    admin = _admin(one)
    assert admin.delete(f"/api/admin/products/{ids[first['slug']]}", headers=HEADERS).status_code in (200, 204)
    assert admin.patch(f"/api/admin/products/{ids[second['slug']]}", json={"published": False}, headers=HEADERS).status_code == 200

    slugs = [p["slug"] for p in shop.get("/api/products").get_json()["products"]]
    assert first["slug"] not in slugs and second["slug"] not in slugs
    assert shop.get(f"/api/products/{first['slug']}").status_code == 404
    assert shop.get(f"/api/products/{second['slug']}").status_code == 404


def test_settings_update_shows_on_the_other_worker(workers):
    one, two = workers
    shop = _warm(two, "/api/settings")
    res = _admin(one).patch("/api/admin/settings", json={"announcementText": "Fresh news"}, headers=HEADERS)
    assert res.status_code == 200
    assert shop.get("/api/settings").get_json()["announcementText"] == "Fresh news"


def test_category_change_shows_on_the_other_worker(workers):
    one, two = workers
    shop = _warm(two, "/api/products", "/api/home")
    with one.app_context():
        category = Category.query.join(Product).filter(Product.published.is_(True)).first()
        cid, old = category.id, category.name
    res = _admin(one).patch(f"/api/admin/categories/{cid}", json={"name": f"{old} renamed"}, headers=HEADERS)
    assert res.status_code == 200
    names = [c["name"] for c in shop.get("/api/products").get_json()["categories"]]
    assert f"{old} renamed" in names
    assert f"{old} renamed" in [c["name"] for c in shop.get("/api/home").get_json()["categories"]]


def test_changes_outside_the_admin_api_also_clear_the_cache(workers):
    """A CLI command or script committing catalog rows, not just admin requests."""
    one, two = workers
    shop = _warm(two, "/api/products")
    with one.app_context():
        product = Product.query.filter_by(published=True).order_by(Product.sort_order, Product.id).first()
        product.price_kes = 999
        slug = product.slug
        db.session.commit()
    listed = next(p for p in shop.get("/api/products").get_json()["products"] if p["slug"] == slug)
    assert listed["priceKES"] == 999


def test_bulk_updates_clear_the_cache(workers):
    one, two = workers
    shop = _warm(two, "/api/products")
    with one.app_context():
        Product.query.update({"price_kes": 777}, synchronize_session=False)
        db.session.commit()
    assert {p["priceKES"] for p in shop.get("/api/products").get_json()["products"]} == {777}


def test_shopper_and_rolled_back_writes_leave_the_cache_alone(workers):
    one, two = workers
    _warm(two, "/api/products")
    stamp = public_cache.get_cache(two).generation()
    with one.app_context():
        Product.query.first().price_kes = 1
        db.session.rollback()
        db.session.commit()
    assert public_cache.get_cache(two).generation() == stamp


# --- final safety review ----------------------------------------------------------------------------


def _max_age(header, directive):
    for part in header.split(","):
        name, _, value = part.strip().partition("=")
        if name == directive:
            return int(value)
    return 0


def test_outage_allowance_for_prices_and_stock_is_short(workers):
    one, _ = workers
    client = one.test_client()
    slug = client.get("/api/products").get_json()["products"][0]["slug"]
    for path in ("/api/settings", "/api/home", "/api/products", f"/api/products/{slug}", "/api/catalog", f"/share/product/{slug}"):
        res = client.get(path)
        assert res.status_code == 200, path
        cdn = res.headers["CDN-Cache-Control"]
        assert _max_age(cdn, "stale-if-error") <= 300, path
        assert _max_age(cdn, "stale-while-revalidate") == 0, path


def test_etag_changes_with_price_and_stock_and_304_only_when_unchanged(workers):
    one, two = workers
    shop = two.test_client()
    slug = shop.get("/api/products").get_json()["products"][0]["slug"]
    path = f"/api/products/{slug}"
    etag = shop.get(path).headers["ETag"]
    assert shop.get(path, headers={"If-None-Match": etag}).status_code == 304
    with one.app_context():
        pid = Product.query.filter_by(slug=slug).one().id
    admin = _admin(one)

    assert admin.patch(f"/api/admin/products/{pid}", json={"priceKES": 4321}, headers=HEADERS).status_code == 200
    after_price = shop.get(path, headers={"If-None-Match": etag})
    assert after_price.status_code == 200
    assert after_price.get_json()["product"]["priceKES"] == 4321
    price_etag = after_price.headers["ETag"]
    assert price_etag != etag

    assert admin.patch(f"/api/admin/products/{pid}", json={"availability": "sold-out"}, headers=HEADERS).status_code == 200
    after_stock = shop.get(path, headers={"If-None-Match": price_etag})
    assert after_stock.status_code == 200
    assert after_stock.get_json()["product"]["availability"] == "sold-out"
    assert after_stock.headers["ETag"] != price_etag


def test_weak_etags_from_compressing_proxies_still_get_304(workers):
    """Cloudflare (in front of Render, and the Worker) weakens ETags when it compresses a response;
    browsers then send W/"..." back. That must still revalidate, and only while unchanged."""
    one, _ = workers
    client = one.test_client()
    etag = client.get("/api/products").headers["ETag"]
    assert client.get("/api/products", headers={"If-None-Match": f"W/{etag}"}).status_code == 304
    with one.app_context():
        Product.query.filter_by(published=True).first().price_kes = 5
        db.session.commit()
    assert client.get("/api/products", headers={"If-None-Match": f"W/{etag}"}).status_code == 200


def _break_stamp_writes(monkeypatch, stamp_path):
    real_replace = public_cache.os.replace

    def failing_replace(src, dst):
        if str(dst) == str(stamp_path):
            raise OSError(28, "No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(public_cache.os, "replace", failing_replace)


def test_a_failed_stamp_write_stops_other_workers_serving_old_prices(workers, monkeypatch):
    one, two = workers
    shop = _warm(two, "/api/products")
    stamp = public_cache.get_cache(one).stamp_path
    _break_stamp_writes(monkeypatch, stamp)

    with one.app_context():
        product = Product.query.filter_by(published=True).order_by(Product.sort_order, Product.id).first()
        product.price_kes, slug = 2468, product.slug
        db.session.commit()

    # The stamp was removed instead, so worker two can't trust its copy...
    import os

    assert not os.path.exists(stamp)
    listed = next(p for p in shop.get("/api/products").get_json()["products"] if p["slug"] == slug)
    assert listed["priceKES"] == 2468
    # ...and while the stamp can't be written, nothing is cached at all.
    assert public_cache.get_cache(two).get("/api/products")[0] is None


def test_caching_resumes_safely_once_the_stamp_can_be_written_again(workers, monkeypatch):
    one, two = workers
    _warm(two, "/api/products")
    with monkeypatch.context() as m:
        _break_stamp_writes(m, public_cache.get_cache(one).stamp_path)
        with one.app_context():
            public_cache.get_cache(one).invalidate()
    shop = two.test_client()
    shop.get("/api/products")  # recreates the stamp with a new token
    queries = _count_queries(two)
    shop.get("/api/products")
    assert queries["n"] == 0, "cached again after recovery"


def test_when_the_stamp_cannot_be_changed_at_all_caching_stops_and_it_is_logged(workers, monkeypatch):
    import logging

    one, _ = workers
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    one.logger.addHandler(handler)
    monkeypatch.setattr(one.logger, "disabled", False)
    one.test_client().get("/api/products")
    cache = public_cache.get_cache(one)
    _break_stamp_writes(monkeypatch, cache.stamp_path)

    def failing_remove(path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(public_cache.os, "remove", failing_remove)
    with one.app_context():
        cache.invalidate()
    assert cache.enabled is False
    one.logger.removeHandler(handler)
    assert any(r.levelname == "CRITICAL" and "can't be updated or removed" in r.getMessage() for r in records)
    queries = _count_queries(one)
    one.test_client().get("/api/products")
    assert queries["n"] > 0, "this worker no longer serves from memory"


def test_an_unwritable_stamp_location_means_no_caching(app, tmp_path):
    worker = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "SQLALCHEMY_DATABASE_URI": app.config["SQLALCHEMY_DATABASE_URI"],
            "PUBLIC_CACHE_SECONDS": 30,
            "PUBLIC_CACHE_STAMP": str(tmp_path / "missing-dir" / "stamp"),
        }
    )
    client = worker.test_client()
    assert client.get("/api/products").status_code == 200
    assert client.get("/api/products").status_code == 200
    assert public_cache.get_cache(worker)._entries == {}
