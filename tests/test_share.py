"""Bot-facing metadata pages (app/routes/share.py): static per-page Open Graph/Twitter tags for
social crawlers that don't run JavaScript. See client/vercel.json for how production routes
crawler traffic here."""

import json

from app.extensions import db
from app.models import Category, Collection, Product, ProductImage


def test_product_share_page_has_real_title_description_and_canonical(client):
    res = client.get("/share/product/the-black-tracksuit")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "<title>The Black Tracksuit | Snug &amp; Co.</title>" in html
    assert 'property="og:title" content="The Black Tracksuit | Snug &amp; Co."' in html
    assert 'rel="canonical" href="https://snugandco.co.ke/product/the-black-tracksuit"' in html
    assert 'property="og:url" content="https://snugandco.co.ke/product/the-black-tracksuit"' in html
    assert 'property="og:type" content="product"' in html
    assert 'name="twitter:card" content="summary_large_image"' in html
    assert '"@type": "Product"' in html


def test_product_share_page_falls_back_to_default_image_without_a_resolvable_photo(client):
    # Seeded products reference bundled client-only photos, not ones this server can resolve.
    res = client.get("/share/product/the-black-tracksuit")
    html = res.get_data(as_text=True)
    assert 'property="og:image" content="https://snugandco.co.ke/og-default.jpg"' in html
    assert 'property="og:image:width" content="1200"' in html
    assert 'property="og:image:height" content="630"' in html


def test_product_share_page_uses_the_actual_cloudinary_photo_when_one_is_stored(client, app):
    with app.app_context():
        product = Product.query.filter_by(slug="the-black-tracksuit").first()
        db.session.add(
            ProductImage(
                id="test-cloudinary-photo",
                product_id=product.id,
                position=-1,
                alt="",
                cloudinary_public_id="snug-co/test-photo",
                cloudinary_version=42,
                width=864,
                height=1080,
            )
        )
        db.session.commit()
        app.config["CLOUDINARY_CLOUD_NAME"] = "demo"

    res = client.get("/share/product/the-black-tracksuit")
    html = res.get_data(as_text=True)
    assert "res.cloudinary.com/demo/image/private/f_auto,q_auto,c_limit,w_864/v42/snug-co/test-photo" in html
    assert 'property="og:image:width" content="864"' in html
    assert 'property="og:image:height" content="1080"' in html


def test_product_share_page_404s_for_missing_and_unpublished_products(client, admin):
    assert client.get("/share/product/not-a-real-slug").status_code == 404
    products = admin.get("/api/admin/products").get_json()["products"]
    pid = next(p["id"] for p in products if p["slug"] == "zebra-print-set")
    assert admin.patch(f"/api/admin/products/{pid}", json={"published": False}, headers={"X-Requested-With": "snug-admin"}).status_code == 200
    assert client.get("/share/product/zebra-print-set").status_code == 404


def test_category_share_page_has_its_own_title_and_canonical(client):
    res = client.get("/share/category/tracksuits")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "<title>Tracksuits in Kenya | Snug &amp; Co.</title>" in html
    assert 'rel="canonical" href="https://snugandco.co.ke/shop/tracksuits"' in html
    assert 'property="og:type" content="website"' in html
    assert '"@type": "BreadcrumbList"' in html


def test_product_share_page_lists_real_colours_when_the_product_has_any(client, app):
    with app.app_context():
        product = Product.query.filter_by(slug="green-tracksuit").first()
        assert [c["name"] for c in product.colors] == ["Bottle green", "Teal green"]

    html = client.get("/share/product/green-tracksuit").get_data(as_text=True)
    assert "Available in Bottle green, Teal green." in html
    assert '"color": "Bottle green, Teal green"' in html
    assert '"@type": "BreadcrumbList"' in html


def test_category_share_page_404s_for_an_unknown_slug(client):
    assert client.get("/share/category/not-a-real-category").status_code == 404


def test_collection_share_page_has_its_own_title_canonical_and_breadcrumb(client):
    res = client.get("/share/collection/matchday")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "<title>Football Streetwear in Kenya | Snug &amp; Co.</title>" in html
    assert 'rel="canonical" href="https://snugandco.co.ke/collections/matchday"' in html
    assert 'property="og:url" content="https://snugandco.co.ke/collections/matchday"' in html
    assert 'property="og:type" content="website"' in html
    assert '"@type": "BreadcrumbList"' in html


def test_collection_share_page_404s_for_an_unknown_slug(client):
    assert client.get("/share/collection/not-a-real-collection").status_code == 404


def test_share_paths_are_disallowed_in_robots_txt(client):
    body = client.get("/robots.txt").get_data(as_text=True)
    assert "Disallow: /share" in body


# ---- JSON-LD HTML-injection safety ------------------------------------------------------------
# A product, category or collection name/description is admin-set text, not fixed copy, and it's
# embedded in a raw <script type="application/ld+json"> string (_safe_json_ld in share.py).
# Reproduced against the pre-fix code with a scratch product set via the admin API: a name
# containing "</script>" closed the script element early, so anything after it in the name ran as
# real markup. _safe_json_ld swaps < > & for \u003c \u003e \u0026, which round-trip through
# JSON.parse back to the original characters but can't be read as markup by an HTML parser.

SCRIPT_BREAKOUT = 'Evil</script><script>alert(1)</script>Tracksuit'
QUOTES_AND_SPECIAL = """She said "hi" <b>bold</b> & 'quoted' \\ backslash"""
UNICODE_NAME = "Kéñyå Édition 🔥 — 新しい"
AMPERSAND_NAME = "Snug & Co. x Arsenal & Friends"
MALICIOUS_DESCRIPTION = "Soft and warm.</script><script>document.location='https://evil.example'</script> Order today."


def _jsonld(html):
    """The page's one JSON-LD payload, parsed. Fails loudly if it isn't valid JSON - exactly
    what an unescaped "</script>" in the source data would cause."""
    import re

    match = re.search(r'<script type="application/ld\+json">(.*)</script>', html)
    assert match, "no JSON-LD script tag found"
    return json.loads(match.group(1))


def test_product_share_page_escapes_a_script_breakout_in_the_name(client, app):
    with app.app_context():
        product = Product.query.filter_by(slug="the-black-tracksuit").first()
        product.name = SCRIPT_BREAKOUT
        db.session.commit()

    res = client.get("/share/product/the-black-tracksuit")
    assert res.status_code == 200
    html = res.get_data(as_text=True)

    # No raw, parseable script element reaches the page from the injected name.
    assert "<script>alert(1)</script>" not in html
    assert "</script><script>" not in html
    # The literal backslash-u escapes appear in the HTML source (not real < > characters).
    assert "\\u003c/script\\u003e\\u003cscript\\u003e" in html

    # JSON-LD still parses, and still carries the real (if malicious-looking) name through -
    # the fix changes the HTML's bytes, never the data.
    data = _jsonld(html)
    assert data[0]["name"] == SCRIPT_BREAKOUT
    assert data[1]["itemListElement"][-1]["name"] == SCRIPT_BREAKOUT

    # The HTML-attribute contexts (title, OG, Twitter) use ordinary HTML-entity escaping, not
    # \u escapes - both are safe, but they're different mechanisms for different contexts.
    assert "<title>Evil&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;Tracksuit |" in html


def test_product_share_page_escapes_a_script_breakout_in_the_description(client, app):
    with app.app_context():
        product = Product.query.filter_by(slug="the-black-tracksuit").first()
        product.description = MALICIOUS_DESCRIPTION
        db.session.commit()

    html = client.get("/share/product/the-black-tracksuit").get_data(as_text=True)
    assert "<script>document.location" not in html
    data = _jsonld(html)
    assert data[0]["description"] == MALICIOUS_DESCRIPTION
    # No sentence break in this description, so it's the whole meta/OG/Twitter description too -
    # HTML-entity escaped there (not \u-escaped; that's only inside the JSON-LD script).
    assert 'content="Soft and warm.&lt;/script&gt;&lt;script&gt;' in html


def test_category_share_page_escapes_a_script_breakout_in_the_name(client, app):
    with app.app_context():
        item = Category.query.filter_by(slug="jackets").first()
        item.name = SCRIPT_BREAKOUT[:80]
        db.session.commit()

    html = client.get("/share/category/jackets").get_data(as_text=True)
    assert "</script><script>" not in html
    data = _jsonld(html)
    assert data["itemListElement"][-1]["name"] == SCRIPT_BREAKOUT[:80]


def test_collection_share_page_escapes_a_script_breakout_in_the_name(client, app):
    with app.app_context():
        item = Collection.query.filter_by(slug="matchday").first()
        item.name = SCRIPT_BREAKOUT[:80]
        db.session.commit()

    html = client.get("/share/collection/matchday").get_data(as_text=True)
    assert "</script><script>" not in html
    data = _jsonld(html)
    assert data["itemListElement"][-1]["name"] == SCRIPT_BREAKOUT[:80]


def test_product_share_page_round_trips_quotes_special_chars_and_unicode(client, app):
    with app.app_context():
        product = Product.query.filter_by(slug="the-black-tracksuit").first()
        product.name = QUOTES_AND_SPECIAL
        db.session.commit()

    res = client.get("/share/product/the-black-tracksuit")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    data = _jsonld(html)
    assert data[0]["name"] == QUOTES_AND_SPECIAL  # exact round trip, nothing mangled


def test_product_share_page_round_trips_unicode_characters(client, app):
    with app.app_context():
        product = Product.query.filter_by(slug="the-black-tracksuit").first()
        product.name = UNICODE_NAME
        db.session.commit()

    html = client.get("/share/product/the-black-tracksuit").get_data(as_text=True)
    data = _jsonld(html)
    assert data[0]["name"] == UNICODE_NAME


def test_product_share_page_escapes_ampersands_everywhere(client, app):
    with app.app_context():
        product = Product.query.filter_by(slug="the-black-tracksuit").first()
        product.name = AMPERSAND_NAME
        db.session.commit()

    html = client.get("/share/product/the-black-tracksuit").get_data(as_text=True)
    # HTML-entity context: a bare "&" would start a malformed/ambiguous entity reference.
    assert "Snug & Co. x Arsenal & Friends |" not in html
    assert "Snug &amp; Co. x Arsenal &amp; Friends" in html
    # JSON-LD context: the \u0026 form round-trips back to a literal "&".
    data = _jsonld(html)
    assert data[0]["name"] == AMPERSAND_NAME


def test_valid_share_routes_still_return_200_after_the_escaping_fix(client):
    assert client.get("/share/product/the-black-tracksuit").status_code == 200
    assert client.get("/share/category/jackets").status_code == 200
    assert client.get("/share/collection/matchday").status_code == 200


def test_unknown_and_unpublished_products_still_404_after_the_escaping_fix(client, admin):
    assert client.get("/share/product/not-a-real-slug-at-all").status_code == 404
    products = admin.get("/api/admin/products").get_json()["products"]
    pid = next(p["id"] for p in products if p["slug"] == "striped-short-set")
    assert admin.patch(f"/api/admin/products/{pid}", json={"published": False}, headers={"X-Requested-With": "snug-admin"}).status_code == 200
    assert client.get("/share/product/striped-short-set").status_code == 404


def test_product_share_page_still_shows_the_real_price_and_photo_after_the_fix(client, app):
    with app.app_context():
        product = Product.query.filter_by(slug="tropical-vibes-short-set").first()
        assert product.price_kes == 4500
        db.session.add(
            ProductImage(
                id="xss-fix-sanity-photo",
                product_id=product.id,
                position=-1,
                alt="",
                cloudinary_public_id="snug-co/sanity-photo",
                cloudinary_version=7,
                width=800,
                height=1000,
            )
        )
        db.session.commit()
        app.config["CLOUDINARY_CLOUD_NAME"] = "demo"

    html = client.get("/share/product/tropical-vibes-short-set").get_data(as_text=True)
    assert "KSh 4,500" in html
    assert "res.cloudinary.com/demo/image/private/f_auto,q_auto,c_limit,w_800/v7/snug-co/sanity-photo" in html


def test_product_share_page_json_ld_has_a_kes_offer_matching_the_client(client):
    import json
    import re

    from app.extensions import db
    from app.models import Product

    with client.application.app_context():
        Product.query.filter_by(slug="the-black-tracksuit").one().price_kes = 4500
        db.session.commit()
    html = client.get("/share/product/the-black-tracksuit").get_data(as_text=True)
    blocks = [json.loads(b.replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&"))
              for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html)]
    product = next(b for block in blocks for b in (block if isinstance(block, list) else [block]) if b["@type"] == "Product")
    offer = product["offers"]
    assert offer["@type"] == "Offer" and offer["priceCurrency"] == "KES"
    assert offer["url"] == "https://snugandco.co.ke/product/the-black-tracksuit"
    assert offer["price"] == 4500


# ---- Round trips per share page ---------------------------------------------------------------
# The live database is ~140 ms from Render, so every statement, BEGIN and ROLLBACK is a round trip
# the Worker's 1.5 s prerender budget pays for. Each share page is one SELECT, run in autocommit.


def _statements(app, client, path):
    from sqlalchemy import event

    seen = []

    def record(conn, cursor, statement, *args):
        seen.append(conn.get_execution_options().get("isolation_level"))

    with app.app_context():
        engine = db.engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        status = client.get(path).status_code
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return status, seen


def test_each_share_page_is_a_single_autocommit_query(client, app):
    for path, status in (
        ("/share/product/the-black-tracksuit", 200),
        ("/share/category/tracksuits", 200),
        ("/share/collection/matchday", 200),
        ("/share/product/not-a-real-slug", 404),
        ("/share/category/not-a-real-category", 404),
        ("/share/collection/not-a-real-collection", 404),
    ):
        got, isolation = _statements(app, client, path)
        assert got == status, path
        assert isolation == ["AUTOCOMMIT"], (path, isolation)


def test_joined_photos_keep_gallery_order_and_skip_design_photos(client, app):
    with app.app_context():
        product = Product.query.filter_by(slug="the-black-tracksuit").first()
        common = dict(product_id=product.id, alt="", cloudinary_version=7, width=864, height=1080)
        # Added out of order, plus a design photo ahead of both: the share image must be the first gallery photo.
        db.session.add(ProductImage(id="second", position=-2, cloudinary_public_id="snug-co/second", **common))
        db.session.add(ProductImage(id="first", position=-3, cloudinary_public_id="snug-co/first", **common))
        db.session.add(ProductImage(id="design", position=-9, role="design", cloudinary_public_id="snug-co/design", **common))
        db.session.commit()
        app.config["CLOUDINARY_CLOUD_NAME"] = "demo"

    html = client.get("/share/product/the-black-tracksuit").get_data(as_text=True)
    assert 'property="og:image" content="https://res.cloudinary.com/demo/image/private/f_auto,q_auto,c_limit,w_864/v7/snug-co/first"' in html
    assert "snug-co/design" not in html and "snug-co/second" not in html


def test_category_and_collection_cover_photo_comes_from_the_same_query(client, app):
    with app.app_context():
        product = Product.query.filter_by(slug="the-black-tracksuit").first()
        db.session.add(ProductImage(id="cover", product_id=product.id, position=50, alt="", cloudinary_public_id="snug-co/cover",
                                    cloudinary_version=9, width=864, height=1080))
        Category.query.filter_by(slug="tracksuits").first().image_id = "cover"
        Collection.query.filter_by(slug="matchday").first().image_id = "no-such-photo"
        db.session.commit()
        app.config["CLOUDINARY_CLOUD_NAME"] = "demo"

    category = client.get("/share/category/tracksuits").get_data(as_text=True)
    assert "res.cloudinary.com/demo/image/private/f_auto,q_auto,c_limit,w_864/v9/snug-co/cover" in category
    # A cover pointing at a photo that no longer exists falls back to the default image, as before.
    collection = client.get("/share/collection/matchday")
    assert collection.status_code == 200
    assert 'property="og:image" content="https://snugandco.co.ke/og-default.jpg"' in collection.get_data(as_text=True)


def test_autocommit_is_skipped_when_the_request_already_opened_a_transaction(app):
    from sqlalchemy import text

    from app.routes.share import _read_without_transaction

    with app.test_request_context("/share/product/x"):
        db.session.execute(text("SELECT 1"))  # transaction already begun
        _read_without_transaction()  # must not raise
        assert db.session.connection().get_execution_options().get("isolation_level") is None
        db.session.rollback()
