from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Optional, List
import sqlite3, hashlib, secrets, json
from datetime import datetime

app = FastAPI(title="Order & Delivery Tracking System")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB = "ordertrack.db"

# ── DB init ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT DEFAULT 'Customer',
        contact_number TEXT
    );
    CREATE TABLE IF NOT EXISTS products (
        product_id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_name TEXT NOT NULL,
        price REAL NOT NULL,
        stock_quantity INTEGER DEFAULT 0
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
        role TEXT
    );
    """)
    conn.commit()

    # seed data
    pw = hashlib.sha256("admin123".encode()).hexdigest()
    try:
        c.execute("INSERT INTO users(name,email,password,role,contact_number) VALUES(?,?,?,?,?)",
                  ("Admin User","admin@odts.com",pw,"Admin","9000000001"))
        c.execute("INSERT INTO users(name,email,password,role,contact_number) VALUES(?,?,?,?,?)",
                  ("Prashast Awasthi","prashast@odts.com",hashlib.sha256("pass123".encode()).hexdigest(),"Customer","9000000002"))
        c.execute("INSERT INTO users(name,email,password,role,contact_number) VALUES(?,?,?,?,?)",
                  ("Ravi Kumar","ravi@odts.com",hashlib.sha256("agent123".encode()).hexdigest(),"DeliveryAgent","9000000003"))
        c.execute("INSERT INTO products(product_name,price,stock_quantity) VALUES(?,?,?)",("Wireless Headphones",2499.00,50))
        c.execute("INSERT INTO products(product_name,price,stock_quantity) VALUES(?,?,?)",("Mechanical Keyboard",3999.00,30))
        c.execute("INSERT INTO products(product_name,price,stock_quantity) VALUES(?,?,?)",("USB-C Hub",1299.00,100))
        c.execute("INSERT INTO products(product_name,price,stock_quantity) VALUES(?,?,?)",("Laptop Stand",899.00,75))
        c.execute("INSERT INTO products(product_name,price,stock_quantity) VALUES(?,?,?)",("Webcam HD",1799.00,40))
        conn.commit()
    except:
        pass
    conn.close()

init_db()

# ── Auth ──────────────────────────────────────────────────────────────────────
security = HTTPBearer(auto_error=False)

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

def get_current_user(creds: HTTPAuthorizationCredentials = Depends(security)):
    if not creds: raise HTTPException(401, "Not authenticated")
    conn = get_db()
    row = conn.execute("SELECT * FROM sessions WHERE token=?", (creds.credentials,)).fetchone()
    conn.close()
    if not row: raise HTTPException(401, "Invalid token")
    return {"user_id": row["user_id"], "role": row["role"]}

# ── Schemas ───────────────────────────────────────────────────────────────────
class RegisterIn(BaseModel):
    name: str; email: str; password: str
    role: str = "Customer"; contact_number: str = ""

class LoginIn(BaseModel):
    email: str; password: str

class OrderIn(BaseModel):
    items: List[dict]  # [{product_id, quantity}]
    payment_method: str = "UPI"

class StatusIn(BaseModel):
    status: str

class AssignIn(BaseModel):
    agent_id: int

# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/auth/register")
def register(data: RegisterIn):
    conn = get_db()
    try:
        conn.execute("INSERT INTO users(name,email,password,role,contact_number) VALUES(?,?,?,?,?)",
                     (data.name, data.email, hash_pw(data.password), data.role, data.contact_number))
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(400, "Email already exists")
    finally:
        conn.close()
    return {"message": "Registered successfully"}

@app.post("/auth/login")
def login(data: LoginIn):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE email=? AND password=?",
                        (data.email, hash_pw(data.password))).fetchone()
    if not user:
        conn.close(); raise HTTPException(401, "Invalid credentials")
    token = secrets.token_hex(32)
    conn.execute("INSERT OR REPLACE INTO sessions(token,user_id,role) VALUES(?,?,?)",
                 (token, user["user_id"], user["role"]))
    conn.commit()
    result = {"token": token, "role": user["role"], "name": user["name"], "user_id": user["user_id"]}
    conn.close()
    return result

@app.post("/auth/logout")
def logout(user=Depends(get_current_user)):
    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user["user_id"],))
    conn.commit(); conn.close()
    return {"message": "Logged out"}

@app.get("/products")
def list_products():
    conn = get_db()
    rows = conn.execute("SELECT * FROM products").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/orders")
def place_order(data: OrderIn, user=Depends(get_current_user)):
    if user["role"] != "Customer": raise HTTPException(403, "Only customers can place orders")
    conn = get_db()
    total = 0
    for item in data.items:
        p = conn.execute("SELECT * FROM products WHERE product_id=?", (item["product_id"],)).fetchone()
        if not p: raise HTTPException(404, f"Product {item['product_id']} not found")
        if p["stock_quantity"] < item["quantity"]: raise HTTPException(400, f"Insufficient stock for {p['product_name']}")
        total += p["price"] * item["quantity"]
    cur = conn.execute("INSERT INTO orders(order_date,total_amount,order_status,customer_id) VALUES(?,?,?,?)",
                       (datetime.now().isoformat(), total, "Placed", user["user_id"]))
    order_id = cur.lastrowid
    for item in data.items:
        conn.execute("INSERT INTO order_items(order_id,product_id,quantity) VALUES(?,?,?)",
                     (order_id, item["product_id"], item["quantity"]))
        conn.execute("UPDATE products SET stock_quantity=stock_quantity-? WHERE product_id=?",
                     (item["quantity"], item["product_id"]))
    conn.execute("INSERT INTO payments(order_id,payment_method,payment_status) VALUES(?,?,?)",
                 (order_id, data.payment_method, "Completed"))
    conn.execute("INSERT INTO deliveries(order_id,delivery_status) VALUES(?,?)", (order_id, "Pending"))
    conn.commit(); conn.close()
    return {"order_id": order_id, "total_amount": total, "status": "Placed"}

@app.get("/orders")
def get_orders(user=Depends(get_current_user)):
    conn = get_db()
    if user["role"] == "Customer":
        rows = conn.execute("""
            SELECT o.*, u.name as customer_name,
                   d.delivery_status, d.delivery_agent_id, d.delivery_id,
                   p.payment_method, p.payment_status
            FROM orders o
            LEFT JOIN users u ON o.customer_id=u.user_id
            LEFT JOIN deliveries d ON o.order_id=d.order_id
            LEFT JOIN payments p ON o.order_id=p.order_id
            WHERE o.customer_id=?
            ORDER BY o.order_id DESC""", (user["user_id"],)).fetchall()
    elif user["role"] == "DeliveryAgent":
        rows = conn.execute("""
            SELECT o.*, u.name as customer_name,
                   d.delivery_status, d.delivery_agent_id, d.delivery_id,
                   p.payment_method, p.payment_status
            FROM orders o
            LEFT JOIN users u ON o.customer_id=u.user_id
            LEFT JOIN deliveries d ON o.order_id=d.order_id
            LEFT JOIN payments p ON o.order_id=p.order_id
            WHERE d.delivery_agent_id=?
            ORDER BY o.order_id DESC""", (user["user_id"],)).fetchall()
    else:
        rows = conn.execute("""
            SELECT o.*, u.name as customer_name,
                   d.delivery_status, d.delivery_agent_id, d.delivery_id,
                   p.payment_method, p.payment_status
            FROM orders o
            LEFT JOIN users u ON o.customer_id=u.user_id
            LEFT JOIN deliveries d ON o.order_id=d.order_id
            LEFT JOIN payments p ON o.order_id=p.order_id
            ORDER BY o.order_id DESC""").fetchall()
    orders = []
    for r in rows:
        o = dict(r)
        items = conn.execute("""
            SELECT oi.quantity, pr.product_name, pr.price
            FROM order_items oi JOIN products pr ON oi.product_id=pr.product_id
            WHERE oi.order_id=?""", (o["order_id"],)).fetchall()
        o["items"] = [dict(i) for i in items]
        if o.get("delivery_agent_id"):
            ag = conn.execute("SELECT name FROM users WHERE user_id=?", (o["delivery_agent_id"],)).fetchone()
            o["agent_name"] = ag["name"] if ag else None
        orders.append(o)
    conn.close()
    return orders

@app.patch("/orders/{order_id}/status")
def update_order_status(order_id: int, data: StatusIn, user=Depends(get_current_user)):
    valid = ["Placed","Confirmed","Dispatched","Out for Delivery","Delivered","Cancelled"]
    if data.status not in valid: raise HTTPException(400, "Invalid status")
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if not order: conn.close(); raise HTTPException(404, "Order not found")
    if user["role"] == "Customer" and data.status != "Cancelled":
        conn.close(); raise HTTPException(403, "Customers can only cancel orders")
    conn.execute("UPDATE orders SET order_status=? WHERE order_id=?", (data.status, order_id))
    if data.status in ["Dispatched","Out for Delivery","Delivered"]:
        conn.execute("UPDATE deliveries SET delivery_status=? WHERE order_id=?", (data.status, order_id))
    if data.status == "Delivered":
        conn.execute("UPDATE deliveries SET delivery_date=? WHERE order_id=?",
                     (datetime.now().isoformat(), order_id))
    conn.commit(); conn.close()
    return {"message": f"Status updated to {data.status}"}

@app.post("/orders/{order_id}/assign")
def assign_agent(order_id: int, data: AssignIn, user=Depends(get_current_user)):
    if user["role"] != "Admin": raise HTTPException(403, "Admin only")
    conn = get_db()
    agent = conn.execute("SELECT * FROM users WHERE user_id=? AND role='DeliveryAgent'", (data.agent_id,)).fetchone()
    if not agent: conn.close(); raise HTTPException(404, "Delivery agent not found")
    conn.execute("UPDATE deliveries SET delivery_agent_id=?, delivery_status='Assigned' WHERE order_id=?",
                 (data.agent_id, order_id))
    conn.execute("UPDATE orders SET order_status='Confirmed' WHERE order_id=?", (order_id,))
    conn.commit(); conn.close()
    return {"message": "Agent assigned successfully"}

@app.get("/users/agents")
def get_agents(user=Depends(get_current_user)):
    if user["role"] != "Admin": raise HTTPException(403)
    conn = get_db()
    rows = conn.execute("SELECT user_id, name, email, contact_number FROM users WHERE role='DeliveryAgent'").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/reports/summary")
def get_summary(user=Depends(get_current_user)):
    if user["role"] != "Admin": raise HTTPException(403)
    conn = get_db()
    total_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    delivered = conn.execute("SELECT COUNT(*) FROM orders WHERE order_status='Delivered'").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM orders WHERE order_status NOT IN ('Delivered','Cancelled')").fetchone()[0]
    revenue = conn.execute("SELECT COALESCE(SUM(total_amount),0) FROM orders WHERE order_status='Delivered'").fetchone()[0]
    customers = conn.execute("SELECT COUNT(*) FROM users WHERE role='Customer'").fetchone()[0]
    conn.close()
    return {"total_orders": total_orders, "delivered": delivered, "pending": pending,
            "revenue": revenue, "customers": customers}
