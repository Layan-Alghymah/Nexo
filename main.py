import os
import uuid
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from supabase import create_client

# ✅ حمّلي .env من نفس مكان main.py (مهم)
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")

def require_admin(x_admin_key: str | None):
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=500, detail="ADMIN_API_KEY not set")
    if x_admin_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------- Supabase client (Service role) ----------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "payment-proofs")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in .env")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# ---------- DB connection (Supabase Postgres) ----------
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "postgres")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD")

if not DB_HOST or not DB_PASSWORD:
    raise RuntimeError("Missing DB_HOST or DB_PASSWORD in .env")

DATABASE_URL = f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# ---------- FastAPI ----------
app = FastAPI(title="Nexo API (MVP)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # لاحقًا نقفلها على الدومين/التطبيق
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Schemas ----------
class ProductOut(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    price_sar: float
    image_url: Optional[str] = None

class OrderItemIn(BaseModel):
    product_id: str
    qty: int = Field(ge=1, default=1)

class OrderCreateIn(BaseModel):
    customer_name: str
    customer_phone: str
    address_text: str
    items: List[OrderItemIn]

@app.get("/health")
def health():
    return {"ok": True}

# ---------- Products ----------
@app.get("/api/products", response_model=List[ProductOut])
def list_products():
    with SessionLocal() as db:
        rows = db.execute(text("""
            select id::text, name, description, price_sar::float, image_url
            from products
            where is_active = true
            order by created_at desc
        """)).mappings().all()
        return [dict(r) for r in rows]

@app.get("/api/products/{product_id}", response_model=ProductOut)
def get_product(product_id: str):
    with SessionLocal() as db:
        row = db.execute(text("""
            select id::text, name, description, price_sar::float, image_url
            from products
            where id = :id and is_active = true
            limit 1
        """), {"id": product_id}).mappings().first()

        if not row:
            raise HTTPException(status_code=404, detail="Product not found")
        return dict(row)

# ---------- Orders ----------

@app.post("/api/orders")
def create_order(payload: OrderCreateIn):
    if not payload.items:
        raise HTTPException(status_code=400, detail="Empty items")

    with SessionLocal() as db:
        product_ids = list({i.product_id.strip() for i in payload.items})

        placeholders = ",".join([f":p{i}" for i in range(len(product_ids))])
        params = {f"p{i}": product_ids[i] for i in range(len(product_ids))}

        rows = db.execute(text(f"""
            select id::text as id, price_sar::float as price_sar
            from public.products
            where is_active = true
              and id in ({placeholders})
        """), params).mappings().all()

        price_map = {r["id"]: r["price_sar"] for r in rows}
        if len(price_map) != len(product_ids):
            missing = [pid for pid in product_ids if pid not in price_map]
            raise HTTPException(status_code=400, detail=f"Products not found: {missing}")

        order_id = str(uuid.uuid4())
        total = 0.0
        for it in payload.items:
            pid = it.product_id.strip()
            total += price_map[pid] * it.qty

        db.execute(text("""
            insert into public.orders (id, status, total_sar, customer_name, customer_phone, address_text)
            values (CAST(:id AS uuid), 'pending_payment', :total, :name, :phone, :addr)

        """), {
            "id": order_id,
            "total": total,
            "name": payload.customer_name,
            "phone": payload.customer_phone,
            "addr": payload.address_text
        })

        for it in payload.items:
            item_id = str(uuid.uuid4())
            pid = it.product_id.strip()
            db.execute(text("""
                insert into public.order_items (id, order_id, product_id, qty, price_sar)
                values (CAST(:item_id AS uuid), CAST(:order_id AS uuid), CAST(:product_id AS uuid), :qty, :price)
            """), {
                "item_id": item_id,
                "order_id": order_id,
                "product_id": pid,
                "qty": it.qty,
                "price": price_map[pid]
            })

        db.commit()

    return {"order_id": order_id, "status": "pending_payment", "total_sar": total}


@app.get("/api/orders/{order_id}")
def get_order(order_id: str):
    with SessionLocal() as db:
        order = db.execute(text("""
            select id::text, status, total_sar::float, customer_name, customer_phone, address_text, created_at
            from orders
            where id = CAST(:id AS uuid)
        """), {"id": order_id}).mappings().first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        items = db.execute(text("""
            select product_id::text, qty, price_sar::float
            from order_items
            where order_id = CAST(:id AS uuid)
        """), {"id": order_id}).mappings().all()

        proof = db.execute(text("""
            select status, amount_sar::float, file_path
            from payment_proofs
            where order_id = CAST(:id AS uuid)
        """), {"id": order_id}).mappings().first()

        return {
            "order": dict(order),
            "items": [dict(i) for i in items],
            "payment_proof": None if not proof else dict(proof)
        }

# ---------- Upload payment proof ----------

@app.post("/api/orders/{order_id}/payment-proof")
async def upload_payment_proof(
    order_id: str,
    file: UploadFile = File(...),
    amount_sar: float | None = Form(None),
    note: str | None = Form(None),
):
    allowed_types = {"image/jpeg", "image/png", "application/pdf"}
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="Unsupported file type (jpg/png/pdf only)")

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:  # 5MB
        raise HTTPException(status_code=400, detail="File too large (max 5MB)")

    # ✅ Generate SAFE filename (ignores Arabic/original name)
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in [".jpg", ".jpeg", ".png", ".pdf"]:
        # fallback based on content-type
        if file.content_type == "image/jpeg":
            ext = ".jpg"
        elif file.content_type == "image/png":
            ext = ".png"
        elif file.content_type == "application/pdf":
            ext = ".pdf"
        else:
            ext = ".bin"

    safe_name = f"{uuid.uuid4().hex}{ext}"
    path = f"{order_id}/{safe_name}"

    # Upload to Supabase Storage
    res = supabase.storage.from_(SUPABASE_BUCKET).upload(
        path=path,
        file=content,
        file_options={"content-type": file.content_type}
    )
    if isinstance(res, dict) and res.get("error"):
        raise HTTPException(status_code=500, detail=f"Upload failed: {res['error']}")

    with SessionLocal() as db:
        exists = db.execute(
            text("select 1 from orders where id = CAST(:id AS uuid)"),
            {"id": order_id}
        ).first()
        if not exists:
            raise HTTPException(status_code=404, detail="Order not found")

        already = db.execute(
            text("select 1 from payment_proofs where order_id = CAST(:id AS uuid)"),
            {"id": order_id}
        ).first()
        if already:
            raise HTTPException(status_code=400, detail="Payment proof already submitted")

        # ✅ Save amount_sar properly (can be null if user didn't provide it)
        db.execute(text("""
            insert into payment_proofs (order_id, file_path, amount_sar, note, status)
            values (CAST(:id AS uuid), :path, :amount, :note, 'submitted')
        """), {"id": order_id, "path": path, "amount": amount_sar, "note": note})

        # ✅ DO NOT approve. Only mark as submitted for admin review.
        db.execute(text("""
            update orders set status = 'proof_submitted'
            where id = CAST(:id AS uuid)
        """), {"id": order_id})

        db.commit()

    return {"ok": True, "order_id": order_id, "status": "proof_submitted", "file_path": path, "amount_sar": amount_sar}


class ReviewIn(BaseModel):
    decision: str  # "approve" or "reject"
    note: str | None = None

@app.post("/admin/orders/{order_id}/review")
def review_order(order_id: str, payload: ReviewIn, x_admin_key: str | None = Header(None)):
    require_admin(x_admin_key)

    decision = payload.decision.lower().strip()
    if decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="decision must be approve or reject")

    new_order_status = "approved" if decision == "approve" else "rejected"
    new_proof_status = "approved" if decision == "approve" else "rejected"

    with SessionLocal() as db:
        # must exist + must have submitted proof
        order = db.execute(text("""
            select status from orders where id = CAST(:id AS uuid)
        """), {"id": order_id}).mappings().first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        proof = db.execute(text("""
            select 1 from payment_proofs where order_id = CAST(:id AS uuid)
        """), {"id": order_id}).first()
        if not proof:
            raise HTTPException(status_code=400, detail="No payment proof to review")

        # Update proof + order
        db.execute(text("""
            update payment_proofs
            set status = :ps, note = coalesce(:note, note)
            where order_id = CAST(:id AS uuid)
        """), {"id": order_id, "ps": new_proof_status, "note": payload.note})

        db.execute(text("""
            update orders
            set status = :os
            where id = CAST(:id AS uuid)
        """), {"id": order_id, "os": new_order_status})

        db.commit()

    return {"ok": True, "order_id": order_id, "order_status": new_order_status, "proof_status": new_proof_status}

@app.get("/admin/orders")
def admin_list_orders(status: str = "proof_submitted", x_admin_key: str | None = Header(None)):
    require_admin(x_admin_key)
    with SessionLocal() as db:
        rows = db.execute(text("""
            select id::text as id, status, total_sar::float, customer_name, customer_phone, created_at
            from orders
            where status = :st
            order by created_at desc
            limit 100
        """), {"st": status}).mappings().all()
    return {"orders": list(rows)}
