import uuid

from conftest import login


def order(client, headers, items, method="UPI"):
    return client.post("/api/orders", headers=headers, json={"items": items, "payment_method": method})


def stock(client, pid):
    return next(p for p in client.get("/api/products").json() if p["product_id"] == pid)["stock_quantity"]


def test_health_and_frontend(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    assert "ODTS" in client.get("/").text


def test_cannot_register_as_admin(client):
    email = f"{uuid.uuid4().hex[:8]}@x.com"
    r = client.post("/api/auth/register", json={"name": "Eve", "email": email, "password": "secret1", "role": "Admin"})
    assert r.status_code == 422


def test_register_login_and_duplicate(client):
    email = f"{uuid.uuid4().hex[:8]}@x.com"
    body = {"name": "Eve", "email": email, "password": "secret1"}
    assert client.post("/api/auth/register", json=body).status_code == 201
    assert client.post("/api/auth/register", json=body).status_code == 400
    assert login(client, email, "secret1")


def test_bad_login(client):
    r = client.post("/api/auth/login", json={"email": "admin@odts.com", "password": "wrong"})
    assert r.status_code == 401


def test_requires_auth(client):
    assert client.get("/api/orders").status_code == 401


def test_rejects_invalid_quantities(client, customer):
    for qty in (0, -5):
        assert order(client, customer, [{"product_id": 3, "quantity": qty}]).status_code == 422
    assert order(client, customer, []).status_code == 422


def test_insufficient_stock_changes_nothing(client, customer):
    before = stock(client, 3)
    assert order(client, customer, [{"product_id": 3, "quantity": 1},
                                    {"product_id": 1, "quantity": 100}, ]).status_code in (400, 422)
    assert stock(client, 3) == before


def test_order_lifecycle_and_restock_on_cancel(client, customer, admin, agent):
    before = stock(client, 4)
    r = order(client, customer, [{"product_id": 4, "quantity": 2}])
    assert r.status_code == 201
    oid = r.json()["order_id"]
    assert stock(client, 4) == before - 2

    # agent not assigned yet -> cannot touch it; admin cannot skip steps
    assert client.patch(f"/api/orders/{oid}/status", headers=agent, json={"status": "Delivered"}).status_code == 403
    assert client.patch(f"/api/orders/{oid}/status", headers=admin, json={"status": "Delivered"}).status_code == 400

    agent_id = client.get("/api/users/agents", headers=admin).json()[0]["user_id"]
    assert client.post(f"/api/orders/{oid}/assign", headers=admin, json={"agent_id": agent_id}).status_code == 200
    for status in ("Dispatched", "Out for Delivery", "Delivered"):
        assert client.patch(f"/api/orders/{oid}/status", headers=agent, json={"status": status}).status_code == 200

    # delivered orders are final
    assert client.patch(f"/api/orders/{oid}/status", headers=customer, json={"status": "Cancelled"}).status_code == 400

    r2 = order(client, customer, [{"product_id": 4, "quantity": 3}])
    oid2 = r2.json()["order_id"]
    assert client.patch(f"/api/orders/{oid2}/status", headers=customer, json={"status": "Cancelled"}).status_code == 200
    assert stock(client, 4) == before - 2  # the cancelled order's stock came back
    row = next(o for o in client.get("/api/orders", headers=customer).json() if o["order_id"] == oid2)
    assert row["payment_status"] == "Refunded"


def test_customer_cannot_touch_other_customers_order(client, customer):
    email = f"{uuid.uuid4().hex[:8]}@x.com"
    client.post("/api/auth/register", json={"name": "Mallory", "email": email, "password": "secret1"})
    other = login(client, email, "secret1")
    oid = order(client, customer, [{"product_id": 3, "quantity": 1}]).json()["order_id"]
    r = client.patch(f"/api/orders/{oid}/status", headers=other, json={"status": "Cancelled"})
    assert r.status_code == 404
    assert client.get("/api/orders", headers=other).json() == []


def test_role_restrictions(client, customer, admin):
    assert client.get("/api/reports/summary", headers=customer).status_code == 403
    assert client.get("/api/users/agents", headers=customer).status_code == 403
    assert order(client, admin, [{"product_id": 3, "quantity": 1}]).status_code == 403
    assert client.get("/api/reports/summary", headers=admin).status_code == 200


def test_cod_payment_pending_until_delivered(client, customer):
    oid = order(client, customer, [{"product_id": 3, "quantity": 1}], method="COD").json()["order_id"]
    row = next(o for o in client.get("/api/orders", headers=customer).json() if o["order_id"] == oid)
    assert row["payment_status"] == "Pending"


def test_logout_invalidates_only_that_token(client):
    a = login(client, "prashast@odts.com", "pass123")
    b = login(client, "prashast@odts.com", "pass123")
    assert client.post("/api/auth/logout", headers=a).status_code == 200
    assert client.get("/api/orders", headers=a).status_code == 401
    assert client.get("/api/orders", headers=b).status_code == 200
