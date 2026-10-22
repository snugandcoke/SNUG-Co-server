from functools import wraps

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .. import public_cache
from ..errors import ApiError
from ..extensions import db
from ..models import Category, Collection, Product, SiteSettings, with_relations
from ..serializers import image_registry, product_json, product_summary, settings_json, taxonomy_json

bp = Blueprint("public", __name__, url_prefix="/api")

# Browsers may keep a copy but must check it with the server before each use (a 304 when nothing
# changed, answered from the server's cache), so an admin change shows on the next page load. The
# CDN (Vercel honours CDN-Cache-Control) does the same. While the server is failing it may serve
# its copy for at most five minutes, so an outage can't keep old prices or stock on screen for long.
PUBLIC_CACHE = "public, no-cache"
CDN_CACHE = "public, max-age=0, must-revalidate, stale-if-error=300"

HOME_NEW_ARRIVALS = 4
RELATED_LIMIT = 4


def cached(view):
    """Serve this storefront route from the shared cache (see app/public_cache.py)."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        cache = public_cache.get_cache()
        if not cache.enabled or request.method not in ("GET", "HEAD"):
            return _conditional(view(*args, **kwargs))
        body, generation = cache.get(request.path)
        if body is not None:
            response = current_app.response_class(body, mimetype="application/json")
            response.headers["Cache-Control"] = PUBLIC_CACHE
            response.headers["CDN-Cache-Control"] = CDN_CACHE
            return _conditional(response)
        response = view(*args, **kwargs)
        # Only complete, public answers: never errors, never anything that sets a cookie.
        if response.status_code == 200 and "Set-Cookie" not in response.headers:
            cache.put(request.path, generation, response.get_data())
        return _conditional(response)

    return wrapper


def _conditional(response):
    if response.status_code == 200:
        response.add_etag()
        response.make_conditional(request)
    return response


def _public(payload):
    response = jsonify(payload)
    response.headers["Cache-Control"] = PUBLIC_CACHE
    response.headers["CDN-Cache-Control"] = CDN_CACHE
    return response


def _published():
    return with_relations(Product.query.filter_by(published=True)).order_by(Product.sort_order, Product.id).all()


def _summary_images(products):
    return [i.id for p in products for i in p.images[:2]]


def _loaded_images(products):
    """Photo rows already fetched with the products, so the registry needn't query them again."""
    return [i for p in products for i in p.images]


@bp.get("/health")
def health():
    """Liveness: the process answers. Doesn't touch the database, so a database outage doesn't
    restart the web process."""
    return jsonify({"status": "ok"})


@bp.get("/ready")
def ready():
    """Readiness: the database answers a trivial query, so the app can serve real traffic. Cloudinary
    isn't checked here (a check would need an upload or a paid call); see the server README.
    Never cached, and the error text is generic so no internal detail leaks."""
    try:
        db.session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("Readiness check failed: database did not answer")
        response = jsonify({"status": "unavailable", "database": "unavailable"})
        status = 503
    else:
        response = jsonify({"status": "ready", "database": "ok"})
        status = 200
    response.headers["Cache-Control"] = "no-store"
    return response, status


@bp.get("/settings")
@cached
def settings():
    return _public(settings_json(db.session.get(SiteSettings, 1)))


@bp.get("/catalog")
@cached
def catalog():
    """Full published catalog. The shop now loads /home, /products and /products/<slug>; this stays
    for pages still open from an older build."""
    products = _published()
    categories = Category.query.order_by(Category.sort_order, Category.id).all()
    collections = Collection.query.order_by(Collection.sort_order, Collection.id).all()

    image_ids = [i.id for p in products for i in p.images]
    image_ids += [c.image_id for c in categories] + [c.image_id for c in collections]

    return _public(
        {
            "products": [product_json(p) for p in products],
            "categories": [taxonomy_json(c) for c in categories],
            "collections": [taxonomy_json(c) for c in collections],
            "images": image_registry(image_ids, loaded=_loaded_images(products)),
        }
    )


@bp.get("/home")
@cached
def home():
    """Only what the home page shows: new arrivals, category tiles and the His & Hers pair."""
    products = _published()
    counts = {}
    for p in products:
        counts[p.category_id] = counts.get(p.category_id, 0) + 1
    categories = [c for c in Category.query.order_by(Category.sort_order, Category.id).all() if counts.get(c.id)]
    new_arrivals = [p for p in products if p.new_arrival][:HOME_NEW_ARRIVALS]
    pair = [p for p in products if p.images and any(c.slug == "his-and-hers" for c in p.collections)][:2]

    return _public(
        {
            "newArrivals": [product_summary(p) for p in new_arrivals],
            "hisAndHers": [product_summary(p) for p in pair],
            "categories": [{**taxonomy_json(c), "productCount": counts[c.id]} for c in categories],
            "images": image_registry(
                _summary_images(new_arrivals + pair) + [c.image_id for c in categories], loaded=_loaded_images(products)
            ),
        }
    )


@bp.get("/products")
@cached
def products():
    """Card-sized data for every published product: the shop grid, search and the bag."""
    items = _published()
    categories = Category.query.order_by(Category.sort_order, Category.id).all()
    collections = Collection.query.order_by(Collection.sort_order, Collection.id).all()
    return _public(
        {
            "products": [product_summary(p) for p in items],
            "categories": [taxonomy_json(c) for c in categories],
            "collections": [taxonomy_json(c) for c in collections],
            "images": image_registry(_summary_images(items), loaded=_loaded_images(items)),
        }
    )


def related_to(product, candidates, limit=RELATED_LIMIT):
    """Sharing a collection scores 2, the same category 1. Ties keep shop order."""
    mine = {c.id for c in product.collections}

    def score(p):
        return (2 if mine & {c.id for c in p.collections} else 0) + (1 if p.category_id == product.category_id else 0)

    scored = [(score(p), p) for p in candidates if p.id != product.id]
    scored = sorted((s for s in scored if s[0] > 0), key=lambda s: -s[0])
    return [p for _, p in scored[:limit]]


@bp.get("/products/<slug>")
@cached
def product_detail(slug):
    """One product with everything its page needs, plus a few related pieces."""
    items = _published()
    product = next((p for p in items if p.slug == slug), None)
    if product is None:
        raise ApiError(404, "That piece isn't available.")
    related = related_to(product, items)
    return _public(
        {
            "product": product_json(product),
            "category": taxonomy_json(product.category),
            "related": [product_summary(p) for p in related],
            "images": image_registry(
                [i.id for i in product.images] + _summary_images(related), loaded=_loaded_images([product, *related])
            ),
        }
    )
