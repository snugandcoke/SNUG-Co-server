"""Minimal HTML with per-page Open Graph and Twitter tags, for link-preview crawlers only.

Link-preview crawlers (WhatsApp, Facebook, Twitter/X, LinkedIn, Slack, Telegram, Discord, Reddit,
Pinterest, Skype, VK, the W3C validator) read only the first HTML response for a URL; they don't
run JavaScript, and they only ever scrape it for a share preview, never for search ranking. The
storefront is a client-rendered SPA, so a crawler fetching /product/<slug> would otherwise only
ever see index.html's static homepage tags. client/vercel.json rewrites traffic from exactly
those crawlers (matched by User-Agent) for /product/<slug>, /shop/<category> and
/collections/<slug> to these routes instead; everyone else, including search crawlers, still
gets the normal SPA.

Deliberately NOT in that User-Agent list: Googlebot, Bingbot, and other search crawlers. This
page's body is metadata only (title, description, a link) with none of the photos, pricing UI,
variant options, or related products a visitor sees, so it is not "substantially equivalent"
content under Google's guidance on dynamic rendering - serving it to a search crawler instead of
the full page would be cloaking. Modern Googlebot already executes the SPA's JavaScript like a
real browser and indexes the full page correctly, so it doesn't need this route at all. If a full
server-rendered (not metadata-only) product page is ever built, search crawlers can be added back
here safely; until then, leave them out.

The copy here mirrors what client/src/pages/Product.jsx and Shop.jsx set client-side via useSeo(),
so a human who taps a shared link sees the same title and description the preview already showed.
"""

import json
from html import escape

from flask import Blueprint, abort, current_app, request

from ..extensions import db
from ..images import cloudinary_url
from ..models import Category, Collection, Product, ProductImage

bp = Blueprint("share", __name__, url_prefix="/share")

DEFAULT_IMAGE_PATH = "/og-default.jpg"
DEFAULT_IMAGE_DIMS = (1200, 630)
BRAND = "Snug & Co."
BRAND_ESC = escape(BRAND)

# Keep in sync with client/src/data/seoCopy.js's CATEGORY_SEO - same reasoning is documented there
# (why no "ladies"/"oversized", why lounge-sets covers the short-sets/cosy-collection keywords).
CATEGORY_SEO = {
    "lounge-sets": {
        "title": "Matching Sets, Loungewear & Short Sets in Kenya",
        "description": "Shop two-piece matching sets, short sets and cosy loungewear in Kenya. Soft, effortless pieces from Snug & Co.",
    },
    "tracksuits": {
        "title": "Tracksuits in Kenya",
        "description": "Shop two-piece tracksuit sets in Kenya. Sleek, comfortable and made for everyday wear.",
    },
    "jackets": {
        "title": "Jackets & Outerwear in Kenya",
        "description": "Shop bomber jackets, retro track jackets and puff jackets in Nairobi. Statement outerwear from Snug & Co.",
    },
    "sweatshirts": {
        "title": "Hoodies & Sweatshirts in Kenya",
        "description": "Shop hoodies, sweatshirts and fleece jerseys in Kenya. Everyday comfort from Snug & Co.",
    },
}
# Keep in sync with client/src/data/seoCopy.js's COLLECTION_SEO.
COLLECTION_SEO = {
    "kenya": {
        "title": "Kenya-Inspired Streetwear",
        "description": "Shop the Kenya collection: bombers, jerseys and streetwear inspired by home. Wear your roots with Snug & Co.",
    },
    "matchday": {
        "title": "Football Streetwear in Kenya",
        "description": "Shop football-inspired streetwear in Kenya: club jackets, hoodies and jerseys for matchday and everyday, from Snug & Co.",
    },
    "his-and-hers": {
        "title": "Unisex Matching Sets in Kenya",
        "description": "Shop unisex matching sets and tracksuits in Kenya, made for him or her. Style has no gender at Snug & Co.",
    },
}
# Matches the public API's cache headers (public.py): a minute fresh, long stale-while-revalidate.
CACHE = "public, max-age=60, s-maxage=60"
CDN_CACHE = "public, max-age=60, stale-while-revalidate=604800, stale-if-error=604800"


def _site_url():
    return (current_app.config["SITE_URL"] or request.host_url).rstrip("/")


def _absolute(path):
    return path if path.startswith("http") else f"{_site_url()}{path}"


def _image_for(image):
    """(url, width, height) for a stored photo's social-preview size, or None without one.

    Mirrors client/src/lib/images.js's imageSrc(): never upscale past the photo's own width.
    image.width/height are already the as-shown 4:5 size, so the ratio holds at any width.
    """
    if image is None:
        return None
    cfg = current_app.config
    if image.cloudinary_public_id and cfg["CLOUDINARY_CLOUD_NAME"]:
        width = min(1200, image.width) if image.width else 1200
        url = cloudinary_url(cfg["CLOUDINARY_CLOUD_NAME"], image.cloudinary_public_id, image.cloudinary_version, width, image.crop)
        height = round(width * image.height / image.width) if image.width and image.height else None
        return url, width, height
    if image.is_upload and image.widths:
        width = image.widths[-1]
        height = round(width * image.height / image.width) if image.width and image.height else None
        return _absolute(f"{cfg['UPLOAD_URL_BASE']}/{image.id}-{width}.webp"), width, height
    return None


def _safe_json_ld(data):
    """json.dumps(), with <, > and & swapped for JSON-compatible \\uXXXX escapes.

    json.dumps() never escapes these by default, but this output is interpolated into a raw
    <script type="application/ld+json"> string (not set via a DOM API), so a value containing a
    literal "</script>" - a product, category or collection name is admin-set, not fixed text -
    would close the script element early and let anything after it run as markup/script. The
    \\uXXXX forms are valid inside any JSON string and decode back to the original characters,
    so this only changes the HTML's raw bytes, never the parsed JSON-LD data.
    """
    return json.dumps(data).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def _page(*, title, description, path, kind, image, jsonld):
    url = _absolute(path)
    full_title = escape(f"{title} | {BRAND}" if title else BRAND)
    image_url, width, height = (_absolute(image[0]), image[1], image[2]) if image else (_absolute(DEFAULT_IMAGE_PATH), *DEFAULT_IMAGE_DIMS)
    dims_tags = ""
    if width and height:
        dims_tags = f'<meta property="og:image:width" content="{width}"><meta property="og:image:height" content="{height}">'
    alt = escape(title or BRAND)
    desc = escape(description)
    head = (
        '<meta charset="UTF-8">'
        f"<title>{full_title}</title>"
        f'<meta name="description" content="{desc}">'
        f'<link rel="canonical" href="{escape(url)}">'
        '<meta name="robots" content="index,follow">'
        f'<meta property="og:site_name" content="{BRAND_ESC}">'
        f'<meta property="og:type" content="{kind}">'
        f'<meta property="og:title" content="{full_title}">'
        f'<meta property="og:description" content="{desc}">'
        f'<meta property="og:url" content="{escape(url)}">'
        f'<meta property="og:image" content="{escape(image_url)}">'
        f"{dims_tags}"
        f'<meta property="og:image:alt" content="{alt}">'
        '<meta name="twitter:card" content="summary_large_image">'
        f'<meta name="twitter:title" content="{full_title}">'
        f'<meta name="twitter:description" content="{desc}">'
        f'<meta name="twitter:image" content="{escape(image_url)}">'
    )
    if jsonld:
        head += f'<script type="application/ld+json">{_safe_json_ld(jsonld)}</script>'
    body = f'<h1>{full_title}</h1><p>{desc}</p><p><a href="{escape(url)}">View on {BRAND_ESC}</a></p>'
    html = f'<!doctype html><html lang="en-KE"><head>{head}</head><body>{body}</body></html>'
    response = current_app.response_class(html, mimetype="text/html")
    response.headers["Cache-Control"] = CACHE
    response.headers["CDN-Cache-Control"] = CDN_CACHE
    return response


@bp.get("/product/<slug>")
def product(slug):
    item = Product.query.filter_by(slug=slug, published=True).first()
    if item is None:
        abort(404)
    image = _image_for(item.images[0]) if item.images else None
    price = f"KSh {item.price_kes:,}. " if item.price_kes is not None else ""
    # Real colour names from the product's own data, when it has any - never invented.
    colors = ", ".join(c["name"] for c in item.colors) if item.colors else None
    colors_line = f" Available in {colors}." if colors else ""
    description = f"{item.description.split('. ')[0]}.{colors_line} {price}Order on WhatsApp from {BRAND}, Nairobi."
    url = _absolute(f"/product/{item.slug}")
    product_data = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": item.name,
        "description": item.description,
        "sku": item.id,
        "url": url,
        "image": [image[0]] if image else [_absolute(DEFAULT_IMAGE_PATH)],
        "brand": {"@type": "Brand", "name": BRAND},
        "category": item.category.name,
    }
    if colors:
        product_data["color"] = colors
    breadcrumb = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Shop", "item": _absolute("/shop")},
            {"@type": "ListItem", "position": 2, "name": item.category.name, "item": _absolute(f"/shop/{item.category.slug}")},
            {"@type": "ListItem", "position": 3, "name": item.name, "item": url},
        ],
    }
    return _page(
        title=item.name,
        description=description,
        path=f"/product/{item.slug}",
        kind="product",
        image=image,
        jsonld=[product_data, breadcrumb],
    )


@bp.get("/category/<slug>")
def category(slug):
    item = Category.query.filter_by(slug=slug).first()
    if item is None:
        abort(404)
    photo = db.session.get(ProductImage, item.image_id) if item.image_id else None
    image = _image_for(photo)
    seo = CATEGORY_SEO.get(item.slug)
    description = (seo["description"] if seo else item.description) or "Lounge sets, tracksuits, jackets and matchday pieces. Order any piece on WhatsApp."
    breadcrumb = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Shop", "item": _absolute("/shop")},
            {"@type": "ListItem", "position": 2, "name": item.name, "item": _absolute(f"/shop/{item.slug}")},
        ],
    }
    return _page(
        title=seo["title"] if seo else item.name,
        description=description,
        path=f"/shop/{item.slug}",
        kind="website",
        image=image,
        jsonld=breadcrumb,
    )


@bp.get("/collection/<slug>")
def collection(slug):
    item = Collection.query.filter_by(slug=slug).first()
    if item is None:
        abort(404)
    photo = db.session.get(ProductImage, item.image_id) if item.image_id else None
    image = _image_for(photo)
    seo = COLLECTION_SEO.get(item.slug)
    description = (seo["description"] if seo else item.description) or "Shop this collection from Snug & Co. Order any piece on WhatsApp."
    breadcrumb = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Shop", "item": _absolute("/shop")},
            {"@type": "ListItem", "position": 2, "name": item.name, "item": _absolute(f"/collections/{item.slug}")},
        ],
    }
    return _page(
        title=seo["title"] if seo else item.name,
        description=description,
        path=f"/collections/{item.slug}",
        kind="website",
        image=image,
        jsonld=breadcrumb,
    )
