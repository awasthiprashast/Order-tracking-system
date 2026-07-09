# Order & Delivery Tracking System (ODTS)
### BCSE301P — Prashast Awasthi | 23BCE0071

A full-stack web application for Order & Delivery Tracking.

---

## Tech Stack
- **Backend:** Python + FastAPI + SQLite
- **Frontend:** HTML / CSS / Vanilla JS (single file, no build step)

---

## Setup & Run

### Step 1 — Install Python dependencies
```bash
cd backend
pip install fastapi uvicorn pydantic
```

### Step 2 — Start the backend server
```bash
cd backend
uvicorn main:app --reload --port 8000
```
Backend runs at: http://localhost:8000  
API docs (Swagger): http://localhost:8000/docs

### Step 3 — Open the frontend
Simply open `frontend/index.html` in your browser.
(No server needed for frontend — it calls the local API directly.)

---

## Demo Accounts (auto-seeded)

| Role           | Email                  | Password   |
|----------------|------------------------|------------|
| Admin          | admin@odts.com         | admin123   |
| Customer       | prashast@odts.com      | pass123    |
| Delivery Agent | ravi@odts.com          | agent123   |

---

## Features by Role

### Customer
- Register / Login
- Browse products
- Place orders (multi-item cart)
- Select payment method (UPI / Card / NetBanking / COD)
- View order history with status tracking
- Cancel orders (before dispatch)
- View order timeline and details

### Admin
- Dashboard with live stats (orders, revenue, customers)
- View ALL orders from all customers
- Assign delivery agents to orders
- Update order status (Dispatch)
- View reports and analytics

### Delivery Agent
- View assigned deliveries
- Update delivery status: Assigned → Dispatched → Out for Delivery → Delivered

---

## System Architecture
Follows the **3-Tier Client-Server Architecture** as designed in the SE Lab:
- **Presentation:** `frontend/index.html`
- **Application:** `backend/main.py` (FastAPI REST API)
- **Data:** SQLite database (`ordertrack.db`, auto-created on first run)

## Entities (from ERD)
- User (Customer / Admin / DeliveryAgent)
- Product
- Order + OrderItems
- Payment
- Delivery
