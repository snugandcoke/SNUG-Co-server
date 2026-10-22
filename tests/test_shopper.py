"""Wishlist and cart kept in the signed shopper cookie."""

import secrets

import pytest

from conftest import ADMIN_EMAIL, ADMIN_PASSWORD, HEADERS, NEW_PRODUCT, PROD_SECRET

SHOP = {"X-Requested-With": "snug-shop"}
EMPTY = {"wishlist": [], "cart": [], "products": {}}


def public_id(client, slug):
    return next(p["id"] for p in client.get("/api/catalog").get_json()["products"] if p["slug"] == slug)


@pytest.fixture()
def shopper(admin):
    """A product with sizes and colours, one with a required choice, and one with a text option."""
    client = admin
    client.post(
        "/api/admin/products",
        json={**NEW_PRODUCT, "colors": [{"name": "Cream", "swatch": ["#f2eadf"]}, {"name": "Black", "swatch": ["#111111"]}],
              "sizes": ["S", "M", "L"]},
        headers=HEADERS,
    )
    client.post("/api/admin/logout", headers=HEADERS)
    products = {p["slug"]: p for p in client.get("/api/catalog").get_json()["products"]}
    sized = products["test-lounge-set"]
    choice = next(p for p in products.values() if any(o.get("values") for o in p["options"]))
    text = next(p for p in products.values() if any(o.get("type") == "text" for o in p["options"]))
    return client, sized, choice, text


def full_line(product, quantity=1):
    return {
        "productId": product["id"],
        "color": product["colors"][0]["name"],
        "size": product["sizes"][0],
        "quantity": quantity,
    }


def test_new_visitor_has_an_empty_wishlist_and_cart(client):
    res = client.get("/api/shopper")
    assert res.get_json() == EMPTY
    assert "no-store" in res.headers["Cache-Control"]
    assert "Set-Cookie" not in res.headers


def test_wishlist_add_is_idempotent_and_survives_in_the_cookie(shopper):
    client, sized, choice, _ = shopper
    res = client.put(f"/api/shopper/wishlist/{sized['id']}", headers=SHOP)
    cookie = res.headers["Set-Cookie"]
    assert cookie.startswith("snug_shopper=") and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert "Path=/api/shopper" in cookie and "Max-Age=7776000" in cookie
    client.put(f"/api/shopper/wishlist/{choice['id']}", headers=SHOP)
    client.put(f"/api/shopper/wishlist/{sized['id']}", headers=SHOP)
    assert client.get("/api/shopper").get_json()["wishlist"] == [choice["id"], sized["id"]]

    res = client.delete(f"/api/shopper/wishlist/{sized['id']}", headers=SHOP)
    assert res.get_json()["wishlist"] == [choice["id"]]


def test_unknown_or_hidden_products_are_refused_and_dropped(admin):
    client = admin
    pid = public_id(client, "green-tracksuit")
    assert client.put("/api/shopper/wishlist/p99999", headers=SHOP).status_code == 404
    assert client.put("/api/shopper/wishlist/nonsense", headers=SHOP).status_code == 404
    client.put(f"/api/shopper/wishlist/{pid}", headers=SHOP)

    admin.patch(f"/api/admin/products/{pid[1:]}", json={"published": False}, headers=HEADERS)
    res = client.get("/api/shopper")
    assert res.get_json()["wishlist"] == []
    assert "snug_shopper=" in res.headers["Set-Cookie"]  # the stale id is cleaned out


def test_cart_checks_choices_against_the_product(shopper):
    client, sized, choice, text = shopper
    missing = client.post("/api/shopper/cart", json={"productId": sized["id"]}, headers=SHOP)
    assert missing.status_code == 422 and set(missing.get_json()["fields"]) == {"color", "size"}
    wrong = client.post("/api/shopper/cart", json={**full_line(sized), "size": "XXXXL"}, headers=SHOP)
    assert wrong.status_code == 422 and "size" in wrong.get_json()["fields"]
    too_many = client.post("/api/shopper/cart", json=full_line(sized, quantity=11), headers=SHOP)
    assert too_many.status_code == 422 and "quantity" in too_many.get_json()["fields"]

    option = next(o for o in choice["options"] if o.get("values"))
    bad = client.post(
        "/api/shopper/cart", json={"productId": choice["id"], "options": {option["name"]: "Not a choice"}}, headers=SHOP
    )
    assert bad.status_code == 422
    text_option = next(o for o in text["options"] if o.get("type") == "text")
    long = client.post(
        "/api/shopper/cart", json={"productId": text["id"], "options": {text_option["name"]: "x" * 61}}, headers=SHOP
    )
    assert long.status_code == 422
    assert client.get("/api/shopper").get_json()["cart"] == []

    ok = client.post(
        "/api/shopper/cart",
        json={"productId": text["id"], "options": {text_option["name"]: "  Arsenal "}, "quantity": 2},
        headers=SHOP,
    )
    assert ok.status_code == 200
    line = ok.get_json()["cart"][0]
    assert line["options"] == {text_option["name"]: "Arsenal"} and line["quantity"] == 2 and line["key"]


def test_same_choices_merge_and_quantity_is_capped(shopper):
    client, sized, _, _ = shopper
    client.post("/api/shopper/cart", json=full_line(sized, 6), headers=SHOP)
    res = client.post("/api/shopper/cart", json=full_line(sized, 6), headers=SHOP)
    assert [line["quantity"] for line in res.get_json()["cart"]] == [10]
    other_size = {**full_line(sized), "size": sized["sizes"][-1]}
    if other_size["size"] != sized["sizes"][0]:
        assert len(client.post("/api/shopper/cart", json=other_size, headers=SHOP).get_json()["cart"]) == 2


def test_update_remove_and_clear_cart_lines(shopper):
    client, sized, choice, _ = shopper
    option = next(o for o in choice["options"] if o.get("values"))
    client.post("/api/shopper/cart", json=full_line(sized), headers=SHOP)
    cart = client.post(
        "/api/shopper/cart", json={"productId": choice["id"], "options": {option["name"]: option["values"][0]}}, headers=SHOP
    ).get_json()["cart"]
    first, second = cart[0]["key"], cart[1]["key"]

    updated = client.patch(f"/api/shopper/cart/{first}", json={"quantity": 4}, headers=SHOP).get_json()["cart"]
    assert updated[0]["quantity"] == 4
    assert client.patch(f"/api/shopper/cart/{first}", json={"quantity": 0}, headers=SHOP).status_code == 422
    assert client.patch("/api/shopper/cart/nope", json={"quantity": 1}, headers=SHOP).status_code == 404

    left = client.delete(f"/api/shopper/cart/{first}", headers=SHOP).get_json()["cart"]
    assert [line["key"] for line in left] == [second]
    client.put(f"/api/shopper/wishlist/{sized['id']}", headers=SHOP)
    cleared = client.delete("/api/shopper/cart", headers=SHOP).get_json()
    assert cleared["wishlist"] == [sized["id"]] and cleared["cart"] == []
    assert list(cleared["products"]) == [sized["id"]]


def test_unavailable_pieces_can_be_saved_but_not_added(admin):
    pid = public_id(admin, "green-tracksuit")
    admin.patch(f"/api/admin/products/{pid[1:]}", json={"availability": "sold-out"}, headers=HEADERS)
    assert admin.put(f"/api/shopper/wishlist/{pid}", headers=SHOP).status_code == 200
    res = admin.post("/api/shopper/cart", json={"productId": pid}, headers=SHOP)
    assert res.status_code == 409


def test_cart_has_a_size_limit(shopper):
    client, _, _, text = shopper
    name = next(o for o in text["options"] if o.get("type") == "text")["name"]
    for i in range(20):
        res = client.post("/api/shopper/cart", json={"productId": text["id"], "options": {name: f"Club {i}"}}, headers=SHOP)
        assert res.status_code == 200
    full = client.post("/api/shopper/cart", json={"productId": text["id"], "options": {name: "One more"}}, headers=SHOP)
    assert full.status_code == 422 and "bag" in full.get_json()["error"]


def test_writes_need_the_shop_header_and_a_known_origin(shopper):
    client, sized, _, _ = shopper
    assert client.put(f"/api/shopper/wishlist/{sized['id']}").status_code == 403
    assert client.put(f"/api/shopper/wishlist/{sized['id']}", headers=HEADERS).status_code == 403
    foreign = client.put(f"/api/shopper/wishlist/{sized['id']}", headers={**SHOP, "Origin": "https://evil.example"})
    assert foreign.status_code == 403
    assert client.get("/api/shopper").get_json()["wishlist"] == []


def test_tampered_cookie_is_ignored(shopper):
    client, sized, _, _ = shopper
    client.set_cookie("snug_shopper", '{"w":[1,2,3]}', path="/api/shopper")
    assert client.get("/api/shopper").get_json() == EMPTY
    assert client.put(f"/api/shopper/wishlist/{sized['id']}", headers=SHOP).status_code == 200


def test_admin_sign_in_and_out_leave_the_cart_alone(shopper):
    client, sized, _, _ = shopper
    client.post("/api/shopper/cart", json=full_line(sized), headers=SHOP)
    client.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers=HEADERS)
    client.post("/api/admin/logout", headers=HEADERS)
    assert len(client.get("/api/shopper").get_json()["cart"]) == 1


def test_production_shopper_cookie_is_secure(tmp_path):
    import json
    from pathlib import Path

    from app import create_app
    from app.cli import load_catalog
    from app.extensions import db

    prod = create_app(
        {"SECRET_KEY": PROD_SECRET, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'p.db'}", "UPLOAD_DIR": str(tmp_path / "up"),
         "CLOUDINARY_CLOUD_NAME": "", "CLOUDINARY_API_KEY": "", "CLOUDINARY_API_SECRET": ""}
    )
    with prod.app_context():
        db.create_all()
        load_catalog(json.loads((Path(__file__).resolve().parent.parent / "seed" / "catalog.json").read_text()))
    client = prod.test_client()
    pid = public_id(client, "green-tracksuit")
    res = client.put(f"/api/shopper/wishlist/{pid}", headers=SHOP, base_url="https://example.com")
    assert "Secure" in res.headers["Set-Cookie"]


def test_full_cookie_is_refused_cleanly_and_the_cart_stays_as_it_was(admin):
    """A product with five long text choices fills the cookie before the 20-line limit. That must be a clear 422, never a 500."""
    client = admin
    names = [f"Engraving line {n}" for n in range(1, 6)]
    made = client.post(
        "/api/admin/products",
        json={**NEW_PRODUCT, "name": "Custom Set", "options": [{"name": n, "type": "text", "required": True} for n in names]},
        headers=HEADERS,
    )
    assert made.status_code == 201
    client.post("/api/admin/logout", headers=HEADERS)
    pid = public_id(client, "custom-set")

    def add(i):
        # Random text: the cookie is compressed, so repeated characters would barely grow it.
        options = {n: secrets.token_urlsafe(45) for n in names}
        return client.post("/api/shopper/cart", json={"productId": pid, "options": options}, headers=SHOP)

    refused = None
    for i in range(20):
        res = add(i)
        if res.status_code != 200:
            refused = res
            break
    assert refused is not None and refused.status_code == 422 and "bag is full" in refused.get_json()["error"]
    cart = client.get("/api/shopper").get_json()["cart"]
    assert 3 <= len(cart) < 20
    # Removing a line makes room again.
    client.delete(f"/api/shopper/cart/{cart[0]['key']}", headers=SHOP)
    assert add(99).status_code == 200


def test_returning_visitor_gets_the_same_wishlist_and_cart_on_a_new_session(app, shopper):
    client, sized, choice, _ = shopper
    client.put(f"/api/shopper/wishlist/{choice['id']}", headers=SHOP)
    client.post("/api/shopper/cart", json=full_line(sized, 3), headers=SHOP)
    cookie = client.get_cookie("snug_shopper", path="/api/shopper")

    returning = app.test_client()
    assert returning.get("/api/shopper").get_json() == EMPTY
    returning.set_cookie("snug_shopper", cookie.value, path="/api/shopper")
    state = returning.get("/api/shopper").get_json()
    assert state["wishlist"] == [choice["id"]]
    assert [(line["productId"], line["quantity"]) for line in state["cart"]] == [(sized["id"], 3)]
    # The cookie can't be edited into someone else's cart.
    returning.set_cookie("snug_shopper", cookie.value[:-3] + "abc", path="/api/shopper")
    assert returning.get("/api/shopper").get_json() == EMPTY


def test_cart_line_outlives_a_sold_out_change_but_not_a_hidden_or_deleted_product(admin):
    client = admin
    pid = public_id(client, "green-tracksuit")
    product = next(p for p in client.get("/api/catalog").get_json()["products"] if p["id"] == pid)
    line = {"productId": pid, "color": product["colors"][0]["name"], "quantity": 2}
    assert client.post("/api/shopper/cart", json=line, headers=SHOP).status_code == 200

    client.patch(f"/api/admin/products/{pid[1:]}", json={"availability": "sold-out"}, headers=HEADERS)
    assert len(client.get("/api/shopper").get_json()["cart"]) == 1  # the page flags it as unavailable

    client.patch(f"/api/admin/products/{pid[1:]}", json={"published": False}, headers=HEADERS)
    assert client.get("/api/shopper").get_json()["cart"] == []


def test_bag_carries_current_prices_and_availability(admin):
    client = admin
    pid = public_id(client, "green-tracksuit")
    product = next(p for p in client.get("/api/catalog").get_json()["products"] if p["id"] == pid)
    client.post("/api/shopper/cart", json={"productId": pid, "color": product["colors"][0]["name"]}, headers=SHOP)

    client.patch(f"/api/admin/products/{pid[1:]}", json={"priceKES": 6100, "availability": "low-stock"}, headers=HEADERS)
    state = client.get("/api/shopper").get_json()
    assert state["products"][pid] == {
        "id": pid, "slug": "green-tracksuit", "name": product["name"], "priceKES": 6100,
        "availability": "low-stock", "madeToOrder": product["madeToOrder"],
        "colors": product["colors"], "sizes": product["sizes"], "sizesNote": product["sizesNote"], "options": product["options"],
    }


def test_check_returns_fresh_order_data_and_leaves_out_hidden_pieces(admin, client):
    first, second = public_id(admin, "green-tracksuit"), public_id(admin, "kenya-bomber-jacket")
    admin.patch(f"/api/admin/products/{first[1:]}", json={"priceKES": 7300}, headers=HEADERS)
    admin.patch(f"/api/admin/products/{second[1:]}", json={"published": False}, headers=HEADERS)

    res = client.get(f"/api/shopper/check?ids={first},{second},nonsense")
    assert res.status_code == 200
    assert res.headers["Cache-Control"] == "private, no-store"
    assert "Set-Cookie" not in res.headers
    products = res.get_json()["products"]
    assert list(products) == [first] and products[first]["priceKES"] == 7300

    too_many = ",".join(f"p{i}" for i in range(1, 22))
    assert client.get(f"/api/shopper/check?ids={too_many}").status_code == 422
    assert client.get("/api/shopper/check").get_json() == {"products": {}}


@pytest.mark.parametrize("bad", ["p" + "9" * 30, "p0", "p01", "p-1", "p1.5", "1", "p١", "P1", "p 1", "%27%20OR%201=1--"])
def test_malformed_product_ids_are_rejected_cleanly(client, bad):
    check = client.get(f"/api/shopper/check?ids={bad}")
    assert check.status_code == 200 and check.get_json() == {"products": {}}
    assert client.put(f"/api/shopper/wishlist/{bad}", headers=SHOP).status_code == 404
    assert client.post("/api/shopper/cart", json={"productId": bad}, headers=SHOP).status_code == 404


def test_quick_changes_in_order_end_in_the_right_state(shopper):
    """Heart on, heart off, heart on — each reply carries the cookie the next request needs."""
    client, sized, _, _ = shopper
    for method in ("put", "delete", "put", "delete", "put"):
        getattr(client, method)(f"/api/shopper/wishlist/{sized['id']}", headers=SHOP)
    assert client.get("/api/shopper").get_json()["wishlist"] == [sized["id"]]


# ---- Choices checked against the piece as it is now -----------------------


def admin_edit(client, product_id, body):
    """Change a piece as the admin, the way a shop owner would, then sign out again."""
    client.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers=HEADERS)
    res = client.patch(f"/api/admin/products/{product_id[1:]}", json=body, headers=HEADERS)
    client.post("/api/admin/logout", headers=HEADERS)
    assert res.status_code == 200, res.get_json()


def choose(product, **choices):
    """A cart body for this piece with its first colour and size, unless others are given."""
    body = {"productId": product["id"], "quantity": 1}
    if product["colors"]:
        body["color"] = product["colors"][0]["name"]
    if product["sizes"]:
        body["size"] = product["sizes"][0]
    body.update(choices)
    return body


def bag_line(client):
    return client.get("/api/shopper").get_json()["cart"][0]


def test_unchanged_valid_selection_has_no_issues(shopper):
    client, sized, choice, _ = shopper
    client.post("/api/shopper/cart", json=full_line(sized), headers=SHOP)
    assert bag_line(client)["issues"] == {}
    check = client.post(
        "/api/shopper/check",
        json={"items": [{"productId": sized["id"], "color": sized["colors"][0]["name"], "size": sized["sizes"][0]}]},
        headers=SHOP,
    )
    assert check.get_json()["items"] == [{"productId": sized["id"], "orderable": True, "issues": {}}]


def test_removed_colour_is_reported_on_the_bag_and_at_checkout(shopper):
    client, sized, _, _ = shopper
    gone, kept = sized["colors"][0]["name"], sized["colors"][1]
    client.post("/api/shopper/cart", json=full_line(sized), headers=SHOP)
    admin_edit(client, sized["id"], {"colors": [kept]})

    assert bag_line(client)["issues"] == {"color": "That colour is no longer available. Choose another."}
    check = client.post(
        "/api/shopper/check",
        json={"items": [{"productId": sized["id"], "color": gone, "size": sized["sizes"][0]}]},
        headers=SHOP,
    ).get_json()
    assert check["items"][0]["issues"] == {"color": "That colour is no longer available. Choose another."}
    assert check["products"][sized["id"]]["colors"] == [kept]


def test_removed_size_is_reported_on_the_bag_and_at_checkout(shopper):
    client, sized, _, _ = shopper
    gone = sized["sizes"][0]
    client.post("/api/shopper/cart", json=full_line(sized), headers=SHOP)
    admin_edit(client, sized["id"], {"sizes": sized["sizes"][1:]})

    assert bag_line(client)["issues"] == {"size": "That size is no longer available. Choose another."}
    check = client.post(
        "/api/shopper/check",
        json={"items": [{"productId": sized["id"], "color": sized["colors"][0]["name"], "size": gone}]},
        headers=SHOP,
    ).get_json()
    assert check["items"][0]["issues"] == {"size": "That size is no longer available. Choose another."}


def test_removed_option_and_removed_option_value_are_reported(shopper):
    client, _, choice, _ = shopper
    option = next(o for o in choice["options"] if o.get("values"))
    name, value = option["name"], option["values"][0]
    client.post("/api/shopper/cart", json=choose(choice, options={name: value}), headers=SHOP)
    assert bag_line(client)["issues"] == {}

    admin_edit(client, choice["id"], {"options": [{**o, "values": o["values"][1:]} if o["name"] == name else o for o in choice["options"]]})
    assert bag_line(client)["issues"] == {f"option:{name}": f"That {name.lower()} is no longer available. Choose another."}

    admin_edit(client, choice["id"], {"options": [o for o in choice["options"] if o["name"] != name]})
    assert bag_line(client)["issues"] == {f"option:{name}": f"{name} is no longer offered."}


def test_removed_required_choice_asks_the_customer_to_choose_again(shopper):
    client, sized, _, _ = shopper
    client.post("/api/shopper/cart", json=full_line(sized), headers=SHOP)
    admin_edit(client, sized["id"], {"colors": []})
    # The line still holds a colour the piece no longer has colours for.
    assert bag_line(client)["issues"] == {"color": "This piece no longer has colour choices."}


def test_checkout_check_ignores_client_price_and_checks_every_choice(shopper):
    client, sized, choice, _ = shopper
    body = {"items": [{"productId": sized["id"], "color": "Purple", "size": "XXL", "priceKES": 1, "orderable": True}]}
    res = client.post("/api/shopper/check", json=body, headers=SHOP)
    assert res.status_code == 200
    assert res.headers["Cache-Control"] == "private, no-store"
    item = res.get_json()["items"][0]
    assert item["issues"] == {
        "color": "That colour is no longer available. Choose another.",
        "size": "That size is no longer available. Choose another.",
    }
    assert res.get_json()["products"][sized["id"]]["priceKES"] != 1

    missing = client.post("/api/shopper/check", json={"items": [{"productId": sized["id"]}]}, headers=SHOP).get_json()
    assert missing["items"][0]["issues"] == {
        "color": "Choose a colour to continue.",
        "size": "Choose a size to continue.",
    }


def test_unpublished_and_sold_out_pieces_are_not_orderable(shopper):
    client, sized, choice, _ = shopper
    admin_edit(client, sized["id"], {"published": False})
    admin_edit(client, choice["id"], {"availability": "sold-out"})
    body = {"items": [{"productId": sized["id"]}, {"productId": choice["id"]}]}
    data = client.post("/api/shopper/check", json=body, headers=SHOP).get_json()
    hidden, sold = data["items"]
    assert hidden == {"productId": sized["id"], "orderable": False, "issues": {}}
    assert sized["id"] not in data["products"]
    assert sold["orderable"] is False and data["products"][choice["id"]]["availability"] == "sold-out"


def test_check_requires_the_shop_header_and_a_list_of_pieces(shopper):
    client, sized, _, _ = shopper
    item = {"productId": sized["id"]}
    assert client.post("/api/shopper/check", json={"items": [item]}).status_code == 403
    assert client.post("/api/shopper/check", json={"items": []}, headers=SHOP).status_code == 422
    assert client.post("/api/shopper/check", json={"items": [{"productId": "nonsense"}]}, headers=SHOP).status_code == 422
    assert client.post("/api/shopper/check", json={"items": [item] * 21}, headers=SHOP).status_code == 422
    assert client.post("/api/shopper/check", json={"items": "p1"}, headers=SHOP).status_code == 422


def test_changing_choices_in_the_bag_is_checked_and_merges_identical_lines(shopper):
    client, sized, _, _ = shopper
    cream, black = sized["colors"][0]["name"], sized["colors"][1]["name"]
    size = sized["sizes"][0]
    first = client.post("/api/shopper/cart", json=full_line(sized), headers=SHOP).get_json()["cart"][0]
    client.post("/api/shopper/cart", json={**full_line(sized), "color": black}, headers=SHOP)

    bad = client.patch(f"/api/shopper/cart/{first['key']}", json={"color": "Purple", "size": size, "options": {}}, headers=SHOP)
    assert bad.status_code == 422 and "color" in bad.get_json()["fields"]

    merged = client.patch(f"/api/shopper/cart/{first['key']}", json={"color": black, "size": size, "options": {}}, headers=SHOP)
    lines = merged.get_json()["cart"]
    assert len(lines) == 1 and lines[0]["color"] == black and lines[0]["quantity"] == 2

    quantity = client.patch(f"/api/shopper/cart/{lines[0]['key']}", json={"quantity": 3}, headers=SHOP)
    assert quantity.get_json()["cart"][0]["quantity"] == 3
    assert client.patch(f"/api/shopper/cart/{lines[0]['key']}", json={}, headers=SHOP).status_code == 422


def test_bag_choice_change_is_refused_for_a_sold_out_piece(shopper):
    client, sized, _, _ = shopper
    client.post("/api/shopper/cart", json=full_line(sized), headers=SHOP)
    key = bag_line(client)["key"]
    admin_edit(client, sized["id"], {"availability": "sold-out"})
    res = client.patch(f"/api/shopper/cart/{key}", json={"color": sized["colors"][1]["name"], "size": sized["sizes"][0]}, headers=SHOP)
    assert res.status_code == 409
    assert "can’t be ordered" in res.get_json()["error"]
