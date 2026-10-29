"""Input cleaning for the admin API."""

import re
import unicodedata
from urllib.parse import urlparse

from .errors import ValidationError

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

AVAILABILITY = ("available", "low-stock", "sold-out", "coming-soon")
VARIATION_TYPES = ("none", "color", "design")
MAX_DESIGNS = 12
BADGES = ("new", "bestseller", "limited", "sold-out")
MAX_PRICE = 10_000_000


def slugify(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = text.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:80]


class Cleaner:
    def __init__(self, data, partial):
        if not isinstance(data, dict):
            raise ValidationError({"_": "Send a JSON object."})
        self.data = data
        self.partial = partial
        self.errors = {}
        self.out = {}

    def _present(self, key, default=None, required=False):
        """Return (present, value)."""
        if key in self.data:
            return True, self.data[key]
        if self.partial:
            return False, None
        if required:
            self.errors[key] = "This is required."
            return False, None
        self.out[key] = default
        return False, None

    def text(self, key, *, required=False, max_len=200, label=None):
        present, value = self._present(key, None, required)
        if not present:
            return
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                self.errors[key] = "This is required."
            else:
                self.out[key] = None
            return
        if not isinstance(value, str):
            self.errors[key] = "Enter text."
            return
        value = value.strip()
        if len(value) > max_len:
            self.errors[key] = f"Keep this under {max_len} characters."
            return
        self.out[key] = value

    def slug(self, key="slug"):
        present, value = self._present(key, None)
        if not present:
            return
        if value in (None, ""):
            self.out[key] = None
            return
        value = str(value).strip().lower()
        if not SLUG_RE.match(value) or len(value) > 80:
            self.errors[key] = "Use lowercase letters, numbers and single hyphens only."
            return
        self.out[key] = value

    def integer(self, key, *, minimum=1, maximum=MAX_PRICE):
        present, value = self._present(key, None)
        if not present:
            return
        if value in (None, ""):
            self.out[key] = None
            return
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        if isinstance(value, bool) or not isinstance(value, int):
            self.errors[key] = "Enter a whole number."
            return
        if value < minimum or value > maximum:
            self.errors[key] = f"Enter a number from {minimum:,} to {maximum:,}."
            return
        self.out[key] = value

    def boolean(self, key, default):
        present, value = self._present(key, default)
        if not present:
            return
        if not isinstance(value, bool):
            self.errors[key] = "Choose yes or no."
            return
        self.out[key] = value

    def choice(self, key, allowed, *, default=None, nullable=False):
        present, value = self._present(key, default)
        if not present:
            return
        if value in (None, "") and nullable:
            self.out[key] = None
            return
        if value not in allowed:
            self.errors[key] = "Choose one of: " + ", ".join(allowed) + "."
            return
        self.out[key] = value

    def str_list(self, key, *, max_items, item_len, nullable=False, lower=False, unique=True):
        present, value = self._present(key, None if nullable else [])
        if not present:
            return
        if value is None:
            self.out[key] = None if nullable else []
            return
        if not isinstance(value, list) or len(value) > max_items:
            self.errors[key] = f"Add up to {max_items} items."
            return
        items = []
        for raw in value:
            if not isinstance(raw, str):
                self.errors[key] = "Each item must be text."
                return
            item = raw.strip().lower() if lower else raw.strip()
            if not item:
                continue
            if len(item) > item_len:
                self.errors[key] = f"Keep each item under {item_len} characters."
                return
            items.append(item)
        if unique:
            seen, deduped = set(), []
            for item in items:
                if item.lower() not in seen:
                    seen.add(item.lower())
                    deduped.append(item)
            items = deduped
        self.out[key] = items or (None if nullable else [])

    def url(self, key):
        present, value = self._present(key, None)
        if not present:
            return
        if value in (None, ""):
            self.out[key] = None
            return
        parsed = urlparse(str(value).strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc or len(str(value)) > 300:
            self.errors[key] = "Enter a full web address starting with https://."
            return
        self.out[key] = str(value).strip()

    def link(self, key):
        """Site path (/shop) or https URL."""
        present, value = self._present(key, None)
        if not present:
            return
        if value in (None, ""):
            self.out[key] = None
            return
        value = str(value).strip()
        if len(value) > 300:
            self.errors[key] = "Keep this under 300 characters."
            return
        if value.startswith("/"):
            # "//x.com" and "/\\x.com" leave the site.
            if value.startswith("//") or "\\" in value:
                self.errors[key] = "Enter a page on this site (starting with /) or a full https:// address."
                return
            self.out[key] = value
            return
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            self.errors[key] = "Enter a page on this site (starting with /) or a full https:// address."
            return
        self.out[key] = value

    def colors(self, key="colors"):
        present, value = self._present(key, None)
        if not present:
            return
        if value in (None, []):
            self.out[key] = None
            return
        if not isinstance(value, list) or len(value) > 12:
            self.errors[key] = "Add up to 12 colours."
            return
        cleaned, names = [], set()
        for entry in value:
            name = (entry.get("name") if isinstance(entry, dict) else None) or ""
            name = name.strip() if isinstance(name, str) else ""
            swatch = entry.get("swatch") if isinstance(entry, dict) else None
            if not name or len(name) > 40:
                self.errors[key] = "Give every colour a name of up to 40 characters."
                return
            if name.lower() in names:
                self.errors[key] = f"“{name}” is listed twice."
                return
            names.add(name.lower())
            if swatch in (None, []):
                swatch = []
            if (
                not isinstance(swatch, list)
                or len(swatch) > 2
                or not all(isinstance(s, str) and HEX_RE.match(s) for s in swatch)
            ):
                self.errors[key] = f"Swatch colours for “{name}” must look like #1F4A35 (one or two)."
                return
            cleaned.append({"name": name, "swatch": [s.lower() for s in swatch]})
        self.out[key] = cleaned

    def designs(self, key="designs"):
        """[{name, image, available}]. Whether each image is one of this product's design photos is
        checked by the route, which can see the product."""
        present, value = self._present(key, None)
        if not present:
            return
        if value in (None, []):
            self.out[key] = None
            return
        if not isinstance(value, list) or len(value) > MAX_DESIGNS:
            self.errors[key] = f"Add up to {MAX_DESIGNS} designs."
            return
        cleaned, names = [], set()
        for entry in value:
            entry = entry if isinstance(entry, dict) else {}
            name = entry.get("name")
            name = name.strip() if isinstance(name, str) else ""
            image = entry.get("image")
            available = entry.get("available", True)
            if not name or len(name) > 40:
                self.errors[key] = "Give every design a name of up to 40 characters."
                return
            if name.lower() in names:
                self.errors[key] = f"“{name}” is listed twice."
                return
            names.add(name.lower())
            if not isinstance(image, str) or not image or len(image) > 80:
                self.errors[key] = f"Add a photo for “{name}”."
                return
            if not isinstance(available, bool):
                self.errors[key] = f"Choose whether “{name}” is available."
                return
            cleaned.append({"name": name, "image": image, "available": available})
        self.out[key] = cleaned

    def options(self, key="options"):
        present, value = self._present(key, [])
        if not present:
            return
        if value in (None, []):
            self.out[key] = []
            return
        if not isinstance(value, list) or len(value) > 5:
            self.errors[key] = "Add up to 5 extra options."
            return
        cleaned, names = [], set()
        for entry in value:
            if not isinstance(entry, dict):
                self.errors[key] = "Each option needs a name."
                return
            name = entry.get("name")
            name = name.strip() if isinstance(name, str) else ""
            if not name or len(name) > 40:
                self.errors[key] = "Give every option a name of up to 40 characters."
                return
            if name.lower() in names:
                self.errors[key] = f"The option “{name}” is listed twice."
                return
            names.add(name.lower())
            required = entry.get("required", True)
            if not isinstance(required, bool):
                self.errors[key] = "Choose whether each option is required."
                return
            if entry.get("type") == "text":
                placeholder = entry.get("placeholder")
                placeholder = placeholder.strip()[:80] if isinstance(placeholder, str) and placeholder.strip() else None
                item = {"name": name, "type": "text", "required": required}
                if placeholder:
                    item["placeholder"] = placeholder
                cleaned.append(item)
                continue
            values = entry.get("values")
            if not isinstance(values, list):
                self.errors[key] = f"“{name}” needs a list of choices."
                return
            choices, seen = [], set()
            for v in values:
                if not isinstance(v, str) or not v.strip() or len(v.strip()) > 40:
                    self.errors[key] = f"Choices for “{name}” must be text of up to 40 characters."
                    return
                if v.strip().lower() not in seen:
                    seen.add(v.strip().lower())
                    choices.append(v.strip())
            if not 1 <= len(choices) <= 20:
                self.errors[key] = f"“{name}” needs between 1 and 20 choices."
                return
            cleaned.append({"name": name, "values": choices, "required": required})
        self.out[key] = cleaned


def clean_product(data, partial=False):
    c = Cleaner(data, partial)
    c.text("name", required=True, max_len=120)
    c.slug("slug")
    c.text("description", required=True, max_len=1200)
    c.text("category", required=True, max_len=80)
    c.str_list("collections", max_items=10, item_len=80)
    c.integer("priceKES")
    c.integer("compareAtPriceKES")
    c.choice("variationType", VARIATION_TYPES, nullable=True)
    c.colors()
    c.designs()
    c.str_list("sizes", max_items=20, item_len=12, nullable=True)
    c.text("sizesNote", max_len=200)
    c.options()
    c.str_list("details", max_items=12, item_len=200)
    c.text("material", max_len=200)
    c.text("care", max_len=400)
    c.str_list("tags", max_items=30, item_len=40, lower=True)
    c.choice("availability", AVAILABILITY, default="available")
    c.choice("badge", BADGES, nullable=True)
    c.boolean("madeToOrder", False)
    c.boolean("featured", False)
    c.boolean("newArrival", True)
    c.boolean("published", True)
    c.boolean("nameConfirmed", True)
    c.url("sourcePost")

    price, compare = c.out.get("priceKES"), c.out.get("compareAtPriceKES")
    if "compareAtPriceKES" not in c.errors and "priceKES" not in c.errors and compare is not None:
        if price is None:
            c.errors["compareAtPriceKES"] = "Set a price before adding a compare-at price."
        elif compare <= price:
            c.errors["compareAtPriceKES"] = "The compare-at price must be higher than the price."
    if c.errors:
        raise ValidationError(c.errors)
    return c.out


def clean_taxonomy(data, partial=False):
    """Shared by categories and collections."""
    c = Cleaner(data, partial)
    c.text("name", required=True, max_len=80)
    c.slug("slug")
    c.text("description", max_len=300)
    c.text("image", max_len=80)
    if c.errors:
        raise ValidationError(c.errors)
    return c.out


def clean_settings(data, partial=True):
    c = Cleaner(data, partial)
    c.text("announcementText", max_len=200)
    c.link("announcementHref")
    c.text("heroAlt", max_len=300)
    for key in ("heroImage", "featureImage", "featureImageSmall"):
        c.text(key, max_len=80)
    if c.errors:
        raise ValidationError(c.errors)
    return c.out
