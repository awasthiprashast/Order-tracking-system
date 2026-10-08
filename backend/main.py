"""ODTS - Order & Delivery Tracking System (FastAPI + SQLite).

The API lives under /api and the single-page frontend is served from /.
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

BASE_DIR = Path(__file__).resolve().parent
DB = os.environ.get("DB_PATH", str(BASE_DIR / "ordertrack.db"))
FRONTEND_DIR = BASE_DIR.parent / "frontend"
SESSION_TTL = timedelta(hours=12)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = FastAPI(title="Order & Delivery Tracking System", version="1.0.0")

# The frontend is served from the same origin, so CORS is only needed when the
# UI is hosted elsewhere. Opt in explicitly: CORS_ORIGINS="https://a.com,https://b.com"
_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(CORSMiddleware, allow_origins=_origins,
                       allow_methods=["*"], allow_headers=["*"])

api = APIRouter(prefix="/api")


# ── DB ───────────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB, isolation_level=None)  # explicit transactions below
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db(write=False):
    """Yield a connection; writes run inside one BEGIN IMMEDIATE transaction."""
    conn = get_db()
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if write:
            conn.execute("COMMIT")
    except BaseException:
        if write and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'Customer',
    contact_number TEXT
);
CREATE TABLE IF NOT EXISTS products (
    product_id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_name TEXT NOT NULL,
    price REAL NOT NULL,
    stock_quantity INTEGER NOT NULL DEFAULT 0 CHECK (stock_quantity >= 0)
);
CREATE TABLE IF NOT EXISTS orders (
    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_date TEXT NOT NULL,
    total_amount REAL,
    order_status TEXT DEFAULT 'Placed',
    customer_id INTEGER,
    FOREIGN KEY (customer_id) REFERENCES users(user_id)
);
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER,
    product_id INTEGER,
    quantity INTEGER,
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    FOREIGN KEY (product_id) REFERENCES products(product_id)
);
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER UNIQUE,
    delivery_agent_id INTEGER,
    delivery_status TEXT DEFAULT 'Pending',
    delivery_date TEXT,
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    FOREIGN KEY (delivery_agent_id) REFERENCES users(user_id)
);
CREATE TABLE IF NOT EXISTS payments (
    payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER UNIQUE,
    payment_method TEXT,
    payment_status TEXT DEFAULT 'Pending',
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER,
    role TEXT,
    expires_at TEXT NOT NULL
);
"""

DEMO_USERS = [
    ("Admin User", "admin@odts.com", "admin123", "Admin", "9000000001"),
    ("Prashast Awasthi", "prashast@odts.com", "pass123", "Customer", "9000000002"),
    ("Ravi Kumar", "ravi@odts.com", "agent123", "DeliveryAgent", "9000000003"),
]
DEMO_PRODUCTS = [
    ("Wireless Headphones", 2499.00, 50),
    ("Mechanical Keyboard", 3999.00, 30),
    ("USB-C Hub", 1299.00, 100),
    ("Laptop Stand", 899.00, 75),
    ("Webcam HD", 1799.00, 40),
]


# ── Auth helpers ─────────────────────────────────────────────────────────────
def hash_pw(password: str, salt: bytes | None = None) -> str:
    """Salted PBKDF2-SHA256, stored as 'salt$hash' (hex)."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_pw(password: str, stored: str) -> bool:
    try:
        salt_hex, _ = stored.split("$", 1)
        return hmac.compare_digest(hash_pw(password, bytes.fromhex(salt_hex)), stored)
    except ValueError:
        return False


def init_db():
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
    # Seed demo data only into an empty database.
    with db(write=True) as conn:
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            for name, email, pw, role, phone in DEMO_USERS:
                conn.execute("INSERT INTO users(name,email,password,role,contact_number) VALUES(?,?,?,?,?)",
                             (name, email, hash_pw(pw), role, phone))
            conn.executemany("INSERT INTO products(product_name,price,stock_quantity) VALUES(?,?,?)",
                             DEMO_PRODUCTS)


init_db()

security = HTTPBearer(auto_error=False)


def get_current_user(creds: HTTPAuthorizationCredentials = Depends(security)):
    if not creds:
        raise HTTPException(401, "Not authenticated")
    with db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE token=?", (creds.credentials,)).fetchone()
    if not row or datetime.fromisoformat(row["expires_at"]) < datetime.now():
        raise HTTPException(401, "Session expired, please sign in again")
    return {"user_id": row["user_id"], "role": row["role"], "token": row["token"]}


# Naive in-memory brute-force guard for login (per client IP + email).
_LOGIN_WINDOW, _LOGIN_MAX = 300, 10
_login_attempts: dict[str, deque] = defaultdict(deque)


def _check_login_rate(key: str):
    now = time.monotonic()
    q = _login_attempts[key]
    while q and now - q[0] > _LOGIN_WINDOW:
        q.popleft()
    if len(q) >= _LOGIN_MAX:
        raise HTTPException(429, "Too many login attempts. Try again in a few minutes.")
    q.append(now)


# ── Schemas ──────────────────────────────────────────────────────────────────
class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: str = Field(max_length=120)
    password: str = Field(min_length=6, max_length=128)
    # Admin accounts can never be self-registered.
    role: Literal["Customer", "DeliveryAgent"] = "Customer"
    contact_number: str = Field(default="", max_length=20)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Name is required")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        v = v.strip().lower()
        if not EMAIL_RE.match(v):
            raise ValueError("Invalid email address")
        return v


class LoginIn(BaseModel):
    email: str
    password: str


class ItemIn(BaseModel):
    product_id: int
    quantity: int = Field(ge=1, le=100)


class OrderIn(BaseModel):
    items: List[ItemIn] = Field(min_length=1, max_length=50)
    payment_method: Literal["UPI", "Card", "NetBanking", "COD"] = "UPI"


class StatusIn(BaseModel):
    status: Literal["Confirmed", "Dispatched", "Out for Delivery", "Delivered", "Cancelled"]


class AssignIn(BaseModel):
    agent_id: int


# ── Auth routes ──────────────────────────────────────────────────────────────
@api.post("/auth/register", status_code=201)
def register(data: RegisterIn):
    try:
        with db(write=True) as conn:
            conn.execute("INSERT INTO users(name,email,password,role,contact_number) VALUES(?,?,?,?,?)",
                         (data.name, data.email, hash_pw(data.password), data.role, data.contact_number))
    except sqlite3.IntegrityError:
        raise HTTPException(400, "Email already exists")
    return {"message": "Registered successfully"}


@api.post("/auth/login")
def login(data: LoginIn, request: Request):
    email = data.email.strip().lower()
    _check_login_rate(f"{request.client.host if request.client else '?'}:{email}")
    with db(write=True) as conn:
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not user or not verify_pw(data.password, user["password"]):
            raise HTTPException(401, "Invalid credentials")
        now = datetime.now()
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),))
        token = secrets.token_hex(32)
        conn.execute("INSERT INTO sessions(token,user_id,role,expires_at) VALUES(?,?,?,?)",
                     (token, user["user_id"], user["role"], (now + SESSION_TTL).isoformat()))
    return {"token": token, "role": user["role"], "name": user["name"], "user_id": user["user_id"]}


@api.post("/auth/logout")
def logout(user=Depends(get_current_user)):
    with db(write=True) as conn:
        conn.execute("DELETE FROM sessions WHERE token=?", (user["token"],))
    return {"message": "Logged out"}


# ── Products ─────────────────────────────────────────────────────────────────
@api.get("/products")
def list_products():
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM products")]


# ── Orders ───────────────────────────────────────────────────────────────────
@api.post("/orders", status_code=201)
def place_order(data: OrderIn, user=Depends(get_current_user)):
    if user["role"] != "Customer":
        raise HTTPException(403, "Only customers can place orders")

    # Merge duplicate lines for the same product.
    wanted: dict[int, int] = {}
    for it in data.items:
        wanted[it.product_id] = wanted.get(it.product_id, 0) + it.quantity

    with db(write=True) as conn:
        total = 0.0
        for pid, qty in wanted.items():
            p = conn.execute("SELECT * FROM products WHERE product_id=?", (pid,)).fetchone()
            if not p:
                raise HTTPException(404, f"Product {pid} not found")
            # Atomic check-and-decrement so concurrent orders cannot oversell.
            cur = conn.execute("UPDATE products SET stock_quantity=stock_quantity-? "
                               "WHERE product_id=? AND stock_quantity>=?", (qty, pid, qty))
            if cur.rowcount == 0:
                raise HTTPException(400, f"Insufficient stock for {p['product_name']}")
            total += p["price"] * qty
        cur = conn.execute("INSERT INTO orders(order_date,total_amount,order_status,customer_id) VALUES(?,?,?,?)",
                           (datetime.now().isoformat(), total, "Placed", user["user_id"]))
        order_id = cur.lastrowid
        conn.executemany("INSERT INTO order_items(order_id,product_id,quantity) VALUES(?,?,?)",
                         [(order_id, pid, qty) for pid, qty in wanted.items()])
        # COD is paid on delivery; everything else is captured up front (simulated).
        pay_status = "Pending" if data.payment_method == "COD" else "Completed"
        conn.execute("INSERT INTO payments(order_id,payment_method,payment_status) VALUES(?,?,?)",
                     (order_id, data.payment_method, pay_status))
        conn.execute("INSERT INTO deliveries(order_id,delivery_status) VALUES(?,?)", (order_id, "Pending"))
    return {"order_id": order_id, "total_amount": total, "status": "Placed"}


ORDER_SELECT = """
    SELECT o.*, u.name AS customer_name,
           d.delivery_status, d.delivery_agent_id, d.delivery_id, d.delivery_date,
           a.name AS agent_name,
           p.payment_method, p.payment_status
    FROM orders o
    LEFT JOIN users u ON o.customer_id=u.user_id
    LEFT JOIN deliveries d ON o.order_id=d.order_id
    LEFT JOIN users a ON d.delivery_agent_id=a.user_id
    LEFT JOIN payments p ON o.order_id=p.order_id
"""


@api.get("/orders")
def get_orders(user=Depends(get_current_user)):
    where, params = "", ()
    if user["role"] == "Customer":
        where, params = "WHERE o.customer_id=?", (user["user_id"],)
    elif user["role"] == "DeliveryAgent":
        where, params = "WHERE d.delivery_agent_id=?", (user["user_id"],)
    with db() as conn:
        orders = [dict(r) for r in conn.execute(f"{ORDER_SELECT} {where} ORDER BY o.order_id DESC", params)]
        if orders:
            ids = [o["order_id"] for o in orders]
            marks = ",".join("?" * len(ids))
            by_order: dict[int, list] = defaultdict(list)
            for r in conn.execute(
                    f"SELECT oi.order_id, oi.quantity, pr.product_name, pr.price FROM order_items oi "
                    f"JOIN products pr ON oi.product_id=pr.product_id WHERE oi.order_id IN ({marks})", ids):
                by_order[r["order_id"]].append({"quantity": r["quantity"],
                                                "product_name": r["product_name"], "price": r["price"]})
            for o in orders:
                o["items"] = by_order[o["order_id"]]
    return orders


# Allowed order-status transitions. "Confirmed" is only reached by assigning an agent.
TRANSITIONS = {
    "Placed": {"Cancelled"},
    "Confirmed": {"Dispatched", "Cancelled"},
    "Dispatched": {"Out for Delivery"},
    "Out for Delivery": {"Delivered"},
}
AGENT_STATUSES = {"Dispatched", "Out for Delivery", "Delivered"}


@api.patch("/orders/{order_id}/status")
def update_order_status(order_id: int, data: StatusIn, user=Depends(get_current_user)):
    with db(write=True) as conn:
        row = conn.execute(f"{ORDER_SELECT} WHERE o.order_id=?", (order_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Order not found")

        role = user["role"]
        if role == "Customer":
            if row["customer_id"] != user["user_id"]:
                raise HTTPException(404, "Order not found")
            if data.status != "Cancelled":
                raise HTTPException(403, "Customers can only cancel orders")
        elif role == "DeliveryAgent":
            if row["delivery_agent_id"] != user["user_id"]:
                raise HTTPException(403, "This order is not assigned to you")
            if data.status not in AGENT_STATUSES:
                raise HTTPException(403, "Agents can only update delivery progress")
        elif data.status == "Confirmed":
            raise HTTPException(400, "Assign a delivery agent to confirm an order")

        current = row["order_status"]
        if data.status not in TRANSITIONS.get(current, set()):
            raise HTTPException(400, f"Cannot change an order from '{current}' to '{data.status}'")

        conn.execute("UPDATE orders SET order_status=? WHERE order_id=?", (data.status, order_id))
        if data.status == "Cancelled":
            conn.execute("UPDATE deliveries SET delivery_status='Cancelled' WHERE order_id=?", (order_id,))
            # Return reserved stock and refund any captured payment.
            conn.execute("UPDATE products SET stock_quantity=stock_quantity + "
                         "(SELECT COALESCE(SUM(quantity),0) FROM order_items oi "
                         " WHERE oi.order_id=? AND oi.product_id=products.product_id) "
                         "WHERE product_id IN (SELECT product_id FROM order_items WHERE order_id=?)",
                         (order_id, order_id))
            conn.execute("UPDATE payments SET payment_status=CASE payment_status "
                         "WHEN 'Completed' THEN 'Refunded' ELSE 'Cancelled' END WHERE order_id=?", (order_id,))
        else:
            conn.execute("UPDATE deliveries SET delivery_status=? WHERE order_id=?", (data.status, order_id))
        if data.status == "Delivered":
            conn.execute("UPDATE deliveries SET delivery_date=? WHERE order_id=?",
                         (datetime.now().isoformat(), order_id))
            conn.execute("UPDATE payments SET payment_status='Completed' WHERE order_id=?", (order_id,))
    return {"message": f"Status updated to {data.status}"}


@api.post("/orders/{order_id}/assign")
def assign_agent(order_id: int, data: AssignIn, user=Depends(get_current_user)):
    if user["role"] != "Admin":
        raise HTTPException(403, "Admin only")
    with db(write=True) as conn:
        order = conn.execute("SELECT order_status FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if not order:
            raise HTTPException(404, "Order not found")
        if order["order_status"] not in ("Placed", "Confirmed"):
            raise HTTPException(400, f"Cannot assign an agent to a '{order['order_status']}' order")
        agent = conn.execute("SELECT 1 FROM users WHERE user_id=? AND role='DeliveryAgent'",
                             (data.agent_id,)).fetchone()
        if not agent:
            raise HTTPException(404, "Delivery agent not found")
        conn.execute("UPDATE deliveries SET delivery_agent_id=?, delivery_status='Assigned' WHERE order_id=?",
                     (data.agent_id, order_id))
        conn.execute("UPDATE orders SET order_status='Confirmed' WHERE order_id=?", (order_id,))
    return {"message": "Agent assigned successfully"}


@api.get("/users/agents")
def get_agents(user=Depends(get_current_user)):
    if user["role"] != "Admin":
        raise HTTPException(403, "Admin only")
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT user_id, name, email, contact_number FROM users WHERE role='DeliveryAgent'")]


@api.get("/reports/summary")
def get_summary(user=Depends(get_current_user)):
    if user["role"] != "Admin":
        raise HTTPException(403, "Admin only")
    with db() as conn:
        one = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
        return {
            "total_orders": one("SELECT COUNT(*) FROM orders"),
            "delivered": one("SELECT COUNT(*) FROM orders WHERE order_status='Delivered'"),
            "pending": one("SELECT COUNT(*) FROM orders WHERE order_status NOT IN ('Delivered','Cancelled')"),
            "revenue": one("SELECT COALESCE(SUM(total_amount),0) FROM orders WHERE order_status='Delivered'"),
            "customers": one("SELECT COUNT(*) FROM users WHERE role='Customer'"),
        }


@api.get("/health")
def health():
    return {"status": "ok"}


app.include_router(api)

# Serve the single-page frontend (registered last so /api routes win).
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
