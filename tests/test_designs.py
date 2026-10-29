"""Design variations: a product can offer designs (a name and a photo each) instead of colours."""

from conftest import HEADERS, NEW_PRODUCT, photo

SHOP = {"X-Requested-With": "snug-shop"}


def create(client, **extra):
    res = client.post("/api/admin/products", json={**NEW_PRODUCT, **extra}, headers=HEADERS)
    assert res.status_code == 201, res.get_json()
    return res.get_json()["product"]


def upload_design_photo(client, product_id, role="design"):
    res = client.post(
        f"/api/admin/products/{product_id}/images",
        data={"files": (photo(), "pattern.jpg"), "role": role, "focus": "0.5"},
        headers=HEADERS,
        content_type="multipart/form-data",
    )
    assert res.status_code == 201, res.get_json()
    return res.get_json()


def design_product(client, names=("Pink floral", "Blue floral")):
    product = create(client, variationType="design")
    images = [upload_design_photo(client, product["id"])["uploaded"][0] for _ in names]
    designs = [{"name": n, "image": i, "available": True} for n, i in zip(names, images)]
    res = client.patch(
        f"/api/admin/products/{product['id']}", json={"variationType": "design", "designs": designs}, headers=HEADERS
    )
    assert res.status_code == 200, res.get_json()
    return res.get_json()["product"]


def test_design_photos_stay_out_of_the_gallery(admin):
    product = design_product(admin)
    assert product["variationType"] == "design"
    assert [d["name"] for d in product["designs"]] == ["Pink floral", "Blue floral"]
    assert product["images"] == []  # the gallery is untouched
    assert product["colors"] is None

    admin.post(
        f"/api/admin/products/{product['id']}/images",
        data={"files": (photo(), "main.jpg")},
        headers=HEADERS,
        content_type="multipart/form-data",
    )
    shown = admin.get(f"/api/admin/products/{product['id']}").get_json()["product"]
    assert len(shown["images"]) == 1
    assert not {i["id"] for i in shown["images"]} & {d["image"] for d in shown["designs"]}


def test_storefront_returns_designs_and_their_photos(admin):
    product = design_product(admin)
    admin.post("/api/admin/logout", headers=HEADERS)
    page = admin.get(f"/api/products/{product['slug']}").get_json()
    assert page["product"]["variationType"] == "design"
    for design in page["product"]["designs"]:
        assert design["image"] in page["images"]


def test_colour_products_are_unchanged_and_read_as_colour(admin):
    colors = [{"name": "Black", "swatch": ["#111111"]}]
    product = create(admin, colors=colors)
    assert product["variationType"] == "color" and product["designs"] is None
    plain = create(admin, name="Plain piece")
    assert plain["variationType"] == "none" and plain["colors"] is None

    # An older admin page that only sends colours still works.
    res = admin.patch(f"/api/admin/products/{plain['id']}", json={"colors": colors}, headers=HEADERS)
    assert res.get_json()["product"]["variationType"] == "color"


def test_switching_type_keeps_only_the_chosen_list_and_removes_unused_photos(admin):
    product = design_product(admin)
    res = admin.patch(
        f"/api/admin/products/{product['id']}",
        json={"variationType": "color", "colors": [{"name": "Cream", "swatch": ["#f2eadf"]}]},
        headers=HEADERS,
    )
    updated = res.get_json()["product"]
    assert updated["variationType"] == "color" and updated["designs"] is None
    assert updated["colors"][0]["name"] == "Cream"
    # Design photos nobody uses are removed with them.
    from app.models import ProductImage

    with admin.application.app_context():
        assert ProductImage.query.filter_by(product_id=product["id"], role="design").count() == 0

    none = admin.patch(f"/api/admin/products/{product['id']}", json={"variationType": "none"}, headers=HEADERS)
    assert none.get_json()["product"]["colors"] is None and none.get_json()["product"]["variationType"] == "none"


def test_removing_and_replacing_designs_cleans_up_photos(admin):
    product = design_product(admin)
    first, second = product["designs"]
    new_image = upload_design_photo(admin, product["id"])["uploaded"][0]
    res = admin.patch(
        f"/api/admin/products/{product['id']}",
        json={"designs": [{"name": "Pink floral", "image": new_image, "available": False}]},
        headers=HEADERS,
    )
    updated = res.get_json()["product"]
    assert updated["designs"] == [{"name": "Pink floral", "image": new_image, "available": False}]
    from app.models import ProductImage

    with admin.application.app_context():
        kept = {i.id for i in ProductImage.query.filter_by(product_id=product["id"], role="design")}
    assert kept == {new_image}


def test_design_validation(admin):
    product = design_product(admin)
    pid = product["id"]
    photo_id = product["designs"][0]["image"]

    def patch(designs):
        return admin.patch(f"/api/admin/products/{pid}", json={"variationType": "design", "designs": designs}, headers=HEADERS)

    assert patch([{"name": "", "image": photo_id}]).status_code == 422
    assert patch([{"name": "A", "image": photo_id}, {"name": "a", "image": photo_id}]).status_code == 422
    assert patch([{"name": "A"}]).status_code == 422
    # A photo that is not one of this product's design photos.
    assert patch([{"name": "A", "image": "nope"}]).status_code == 422
    assert patch([{"name": f"D{n}", "image": photo_id} for n in range(13)]).status_code == 422
    # A new product has no photos yet, so designs can't be saved with it.
    res = admin.post(
        "/api/admin/products",
        json={**NEW_PRODUCT, "name": "Fresh", "variationType": "design", "designs": [{"name": "A", "image": photo_id}]},
        headers=HEADERS,
    )
    assert res.status_code == 422


def test_a_design_photo_in_use_cannot_be_deleted_on_its_own(admin):
    product = design_product(admin)
    image = product["designs"][0]["image"]
    assert admin.delete(f"/api/admin/products/{product['id']}/images/{image}", headers=HEADERS).status_code == 409


def test_cover_picker_leaves_out_design_photos(admin):
    product = design_product(admin)
    ids = {row["id"] for row in admin.get("/api/admin/images").get_json()["images"]}
    assert not ids & {d["image"] for d in product["designs"]}


def test_cart_keeps_the_design_and_checks_it(admin):
    product = design_product(admin)
    admin.patch(
        f"/api/admin/products/{product['id']}",
        json={"designs": [{**product["designs"][0]}, {**product["designs"][1], "available": False}]},
        headers=HEADERS,
    )
    admin.post("/api/admin/logout", headers=HEADERS)
    pid = f"p{product['id']}"

    missing = admin.post("/api/shopper/cart", json={"productId": pid}, headers=SHOP)
    assert missing.status_code == 422 and "design" in missing.get_json()["fields"]
    unknown = admin.post("/api/shopper/cart", json={"productId": pid, "design": "Green"}, headers=SHOP)
    assert unknown.status_code == 422
    sold_out = admin.post("/api/shopper/cart", json={"productId": pid, "design": "Blue floral"}, headers=SHOP)
    assert sold_out.status_code == 422 and "sold out" in sold_out.get_json()["fields"]["design"]

    added = admin.post("/api/shopper/cart", json={"productId": pid, "design": "Pink floral", "quantity": 2}, headers=SHOP)
    assert added.status_code == 200
    state = added.get_json()
    line = state["cart"][0]
    assert line["design"] == "Pink floral" and line["color"] is None and line["quantity"] == 2 and line["issues"] == {}
    assert state["products"][pid]["designs"][0]["name"] == "Pink floral"
    assert product["designs"][0]["image"] in state["images"]

    # Same design merges; the choice survives a reload (the cookie) and a quantity change.
    again = admin.post("/api/shopper/cart", json={"productId": pid, "design": "Pink floral"}, headers=SHOP).get_json()
    assert len(again["cart"]) == 1 and again["cart"][0]["quantity"] == 3
    bumped = admin.patch(f"/api/shopper/cart/{line['key']}", json={"quantity": 4}, headers=SHOP).get_json()
    assert bumped["cart"][0]["design"] == "Pink floral" and bumped["cart"][0]["quantity"] == 4
    assert admin.get("/api/shopper").get_json()["cart"][0]["design"] == "Pink floral"

    # The check used right before WhatsApp opens reports a design that is gone.
    checked = admin.post(
        "/api/shopper/check", json={"items": [{"productId": pid, "design": "Blue floral"}]}, headers=SHOP
    ).get_json()
    assert "design" in checked["items"][0]["issues"]


def test_removed_design_is_reported_on_the_bag(admin):
    product = design_product(admin)
    admin.post("/api/admin/logout", headers=HEADERS)
    pid = f"p{product['id']}"
    admin.post("/api/shopper/cart", json={"productId": pid, "design": "Pink floral"}, headers=SHOP)
    from conftest import ADMIN_EMAIL, ADMIN_PASSWORD

    admin.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers=HEADERS)
    admin.patch(
        f"/api/admin/products/{product['id']}", json={"designs": [product["designs"][1]]}, headers=HEADERS
    )
    line = admin.get("/api/shopper").get_json()["cart"][0]
    assert line["design"] == "Pink floral" and "design" in line["issues"]
