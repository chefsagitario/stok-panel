import os
import time
import threading
import requests

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session
)

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.pool import NullPool
from dotenv import load_dotenv


# =========================================================
# ENV
# =========================================================

env_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".env"
)

load_dotenv(
    env_path,
    override=True
)

DATABASE_URL = os.getenv(
    "DATABASE_URL"
)

SHOPIER_API_KEY = os.getenv(
    "SHOPIER_API_KEY"
)


# =========================================================
# LOGIN
# =========================================================

USERNAME = os.getenv(
    "ADMIN_USERNAME",
    "admin"
)

PASSWORD = os.getenv(
    "ADMIN_PASSWORD",
    "admin123"
)


# =========================================================
# SHOPIER
# =========================================================

SHOPIER_CHECK_INTERVAL = 60


# =========================================================
# FLASK
# =========================================================

app = Flask(__name__)

app.secret_key = os.getenv(
    "FLASK_SECRET_KEY",
    "stok-panel-secret-key"
)


# =========================================================
# DATABASE
# =========================================================
#
# NullPool kullanıyoruz.
#
# Render + Supabase Pooler + uzun süre açık worker
# kombinasyonunda eski/stale SSL bağlantılarının
# tekrar kullanılmasını engeller.
# =========================================================

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "poolclass": NullPool,
    "pool_pre_ping": True
}

db = SQLAlchemy(app)


# =========================================================
# SHOPIER LOCK
# =========================================================

shopier_lock = threading.Lock()


# =========================================================
# PRODUCT MODEL
# =========================================================

class Product(db.Model):

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    model = db.Column(
        db.String(100),
        nullable=False
    )

    brand = db.Column(
        db.String(100)
    )

    color = db.Column(
        db.String(50)
    )

    size = db.Column(
        db.String(20)
    )

    stock = db.Column(
        db.Integer,
        default=0
    )

    purchase_price = db.Column(
        db.Float,
        default=0
    )

    sale_price = db.Column(
        db.Float,
        default=0
    )

    image = db.Column(
        db.String(255)
    )

    shopier_product_id = db.Column(
        db.String(50)
    )


# =========================================================
# SHOPIER ORDER MODEL
# =========================================================

class ShopierOrder(db.Model):

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    shopier_order_id = db.Column(
        db.String(50),
        unique=True,
        nullable=False
    )

    processed_at = db.Column(
        db.DateTime,
        default=db.func.now()
    )


# =========================================================
# LOGIN REQUIRED
# =========================================================

def login_required():

    return session.get(
        "logged_in"
    ) is True


# =========================================================
# HOME / LOGIN
# =========================================================

@app.route(
    "/",
    methods=["GET", "POST"]
)
def home():

    if request.method == "POST":

        username = request.form.get(
            "username"
        )

        password = request.form.get(
            "password"
        )

        if (
            username == USERNAME
            and password == PASSWORD
        ):

            session["logged_in"] = True

            return redirect(
                url_for("dashboard")
            )

        return render_template(
            "login.html",
            error="Kullanıcı adı veya şifre yanlış!"
        )

    return render_template(
        "login.html"
    )


# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("home")
    )


# =========================================================
# SHOPIER ORDERS API
# =========================================================

def get_shopier_orders():

    if not SHOPIER_API_KEY:

        print(
            "Shopier API key bulunamadı!"
        )

        return []

    url = (
        "https://api.shopier.com/v1/orders"
    )

    headers = {

        "Authorization":
            f"Bearer {SHOPIER_API_KEY}",

        "Accept":
            "application/json"
    }

    try:

        response = requests.get(
            url,
            headers=headers,
            timeout=30
        )

        print(
            "Shopier status:",
            response.status_code
        )

        if response.status_code != 200:

            print(
                "Shopier response:",
                response.text
            )

            return []

        orders = response.json()

        if not isinstance(
            orders,
            list
        ):

            print(
                "Shopier'den beklenmeyen veri geldi."
            )

            return []

        return orders

    except requests.RequestException as e:

        print(
            "Shopier bağlantı hatası:",
            e
        )

        return []

    except ValueError:

        print(
            "Shopier cevabı JSON olarak okunamadı."
        )

        return []


# =========================================================
# CHECK PROCESSED ORDER
# =========================================================

def order_already_processed(
    shopier_order_id
):

    existing_order = (
        ShopierOrder.query
        .filter_by(
            shopier_order_id=str(
                shopier_order_id
            )
        )
        .first()
    )

    return existing_order is not None


# =========================================================
# SAVE PROCESSED ORDER
# =========================================================

def save_processed_order(
    shopier_order_id
):

    existing_order = (
        ShopierOrder.query
        .filter_by(
            shopier_order_id=str(
                shopier_order_id
            )
        )
        .first()
    )

    if existing_order:

        return

    new_order = ShopierOrder(
        shopier_order_id=str(
            shopier_order_id
        )
    )

    db.session.add(
        new_order
    )


# =========================================================
# PROCESS SHOPIER ORDERS
# =========================================================

def process_shopier_orders():

    if not shopier_lock.acquire(
        blocking=False
    ):

        print(
            "Shopier kontrolü zaten devam ediyor."
        )

        return

    try:

        orders = get_shopier_orders()

        if not orders:

            print(
                "Shopier'de işlenecek sipariş yok."
            )

            return

        new_orders = 0
        skipped_orders = 0

        for order in orders:

            order_id = order.get(
                "id"
            )

            if not order_id:

                continue

            # =================================================
            # DAHA ÖNCE İŞLENDİ Mİ?
            # =================================================

            if order_already_processed(
                order_id
            ):

                skipped_orders += 1

                continue

            print(
                f"Yeni Shopier siparişi: {order_id}"
            )

            # =================================================
            # ÖDEME
            # =================================================

            payment_status = order.get(
                "paymentStatus"
            )

            if payment_status != "paid":

                print(
                    f"Sipariş {order_id} "
                    f"ödenmemiş, atlanıyor."
                )

                continue

            # =================================================
            # İADE
            # =================================================

            refunds = order.get(
                "refunds",
                []
            )

            successful_refund = False

            for refund in refunds:

                if refund.get(
                    "status"
                ) == "succeeded":

                    successful_refund = True

                    break

            if successful_refund:

                print(
                    f"Sipariş {order_id} "
                    f"iade edilmiş, atlanıyor."
                )

                continue

            # =================================================
            # ÜRÜNLER
            # =================================================

            line_items = order.get(
                "lineItems",
                []
            )

            if not line_items:

                print(
                    f"Sipariş {order_id} "
                    f"ürün içermiyor."
                )

                continue

            order_can_be_processed = True

            matched_products = []

            # =================================================
            # TÜM ÜRÜNLERİ ÖNCE KONTROL ET
            # =================================================

            for item in line_items:

                product_id = item.get(
                    "productId"
                )

                title = item.get(
                    "title",
                    "Bilinmeyen ürün"
                )

                quantity = item.get(
                    "quantity",
                    0
                )

                print(
                    f"  Ürün: {title}"
                )

                print(
                    f"  Shopier Product ID: "
                    f"{product_id}"
                )

                print(
                    f"  Adet: {quantity}"
                )

                # -------------------------------------------------
                # PANELDE ÜRÜNÜ BUL
                # -------------------------------------------------

                product = (
                    Product.query
                    .filter_by(
                        shopier_product_id=str(
                            product_id
                        )
                    )
                    .first()
                )

                if not product:

                    print(
                        f"  ⚠️ Panelde eşleşen ürün "
                        f"bulunamadı: {product_id}"
                    )

                    order_can_be_processed = False

                    continue

                # -------------------------------------------------
                # ADET
                # -------------------------------------------------

                try:

                    quantity = int(
                        quantity
                    )

                except (
                    TypeError,
                    ValueError
                ):

                    print(
                        f"  ⚠️ Geçersiz adet: "
                        f"{quantity}"
                    )

                    order_can_be_processed = False

                    continue

                if quantity <= 0:

                    print(
                        "  ⚠️ Geçersiz ürün adedi."
                    )

                    order_can_be_processed = False

                    continue

                # -------------------------------------------------
                # STOK
                # -------------------------------------------------

                if product.stock < quantity:

                    print(
                        f"  ⚠️ Yetersiz stok!"
                    )

                    print(
                        f"  Ürün: {product.model}"
                    )

                    print(
                        f"  Mevcut stok: "
                        f"{product.stock}"
                    )

                    print(
                        f"  Satılan: "
                        f"{quantity}"
                    )

                    order_can_be_processed = False

                    continue

                matched_products.append(
                    (
                        product,
                        quantity
                    )
                )

            # =================================================
            # ÜRÜN EŞLEŞMEDİ / STOK YETERSİZ
            # =================================================

            if not order_can_be_processed:

                print(
                    f"⚠️ Sipariş {order_id} "
                    f"stoktan düşülmedi."
                )

                print(
                    "Shopier Product ID veya "
                    "stok miktarını kontrol et."
                )

                continue

            # =================================================
            # STOK DÜŞ
            # =================================================

            for product, quantity in matched_products:

                old_stock = product.stock

                product.stock = (
                    product.stock - quantity
                )

                print(
                    f"📦 {product.model}: "
                    f"{old_stock} → "
                    f"{product.stock}"
                )

            # =================================================
            # SİPARİŞİ İŞLENDİ OLARAK KAYDET
            # =================================================

            save_processed_order(
                order_id
            )

            new_orders += 1

            print(
                f"✅ Sipariş {order_id} işlendi."
            )

        # =====================================================
        # TEK COMMIT
        # =====================================================

        db.session.commit()

        print(
            f"Yeni sipariş: {new_orders}"
        )

        print(
            f"Atlanan/eski sipariş: "
            f"{skipped_orders}"
        )

    except Exception as e:

        db.session.rollback()

        print(
            "❌ Shopier işlem hatası:",
            e
        )

    finally:

        db.session.remove()

        shopier_lock.release()


# =========================================================
# BACKGROUND WORKER
# =========================================================

def shopier_background_worker():

    print(
        "🔄 Shopier otomatik kontrol sistemi başladı."
    )

    print(
        f"⏱️ Kontrol aralığı: "
        f"{SHOPIER_CHECK_INTERVAL} saniye"
    )

    while True:

        try:

            with app.app_context():

                process_shopier_orders()

        except Exception as e:

            print(
                "❌ Otomatik Shopier kontrol hatası:",
                e
            )

        time.sleep(
            SHOPIER_CHECK_INTERVAL
        )


def start_shopier_background_worker():

    worker = threading.Thread(
        target=shopier_background_worker,
        daemon=True
    )

    worker.start()

    return worker


# =========================================================
# DASHBOARD
# =========================================================

@app.route("/dashboard")
def dashboard():

    if not login_required():

        return redirect(
            url_for("home")
        )

    # =====================================================
    # ÖNEMLİ:
    # Burada artık process_shopier_orders()
    # ÇAĞRILMIYOR.
    #
    # Shopier kontrolünü sadece background worker yapıyor.
    # =====================================================

    products = Product.query.all()

    total_products = len(
        products
    )

    total_stock = sum(
        product.stock
        for product in products
    )

    low_stock = sum(
        1
        for product in products
        if product.stock <= 3
    )

    # =====================================================
    # SHOPIER SİPARİŞLERİNİ GÖSTER
    # =====================================================

    shopier_orders = get_shopier_orders()

    recent_orders = []

    for order in shopier_orders:

        order_id = order.get(
            "id"
        )

        if not order_id:

            continue

        line_items = order.get(
            "lineItems",
            []
        )

        for item in line_items:

            recent_orders.append({

                "id":
                    order_id,

                "title":
                    item.get(
                        "title",
                        "Bilinmeyen ürün"
                    ),

                "product_id":
                    item.get(
                        "productId"
                    ),

                "quantity":
                    item.get(
                        "quantity",
                        0
                    ),

                "payment_status":
                    order.get(
                        "paymentStatus"
                    )
            })

    return render_template(
        "dashboard.html",

        products=products,

        total_products=
            total_products,

        total_stock=
            total_stock,

        low_stock=
            low_stock,

        recent_orders=
            recent_orders
    )


# =========================================================
# ADD PRODUCT
# =========================================================

@app.route(
    "/add-product",
    methods=["GET", "POST"]
)
def add_product():

    if not login_required():

        return redirect(
            url_for("home")
        )

    if request.method == "POST":

        product = Product(

            model=request.form[
                "model"
            ],

            brand=request.form.get(
                "brand"
            ),

            color=request.form.get(
                "color"
            ),

            size=request.form.get(
                "size"
            ),

            stock=int(
                request.form.get(
                    "stock",
                    0
                ) or 0
            ),

            purchase_price=float(
                request.form.get(
                    "purchase_price",
                    0
                ) or 0
            ),

            sale_price=float(
                request.form.get(
                    "sale_price",
                    0
                ) or 0
            ),

            shopier_product_id=(
                request.form.get(
                    "shopier_product_id"
                ) or None
            )
        )

        db.session.add(
            product
        )

        db.session.commit()

        return redirect(
            url_for("dashboard")
        )

    return render_template(
        "add_product.html"
    )


# =========================================================
# EDIT PRODUCT
# =========================================================

@app.route(
    "/edit-product/<int:product_id>",
    methods=["GET", "POST"]
)
def edit_product(product_id):

    if not login_required():

        return redirect(
            url_for("home")
        )

    product = Product.query.get_or_404(
        product_id
    )

    if request.method == "POST":

        product.model = request.form[
            "model"
        ]

        product.brand = request.form.get(
            "brand"
        )

        product.color = request.form.get(
            "color"
        )

        product.size = request.form.get(
            "size"
        )

        product.stock = int(
            request.form.get(
                "stock",
                0
            ) or 0
        )

        product.purchase_price = float(
            request.form.get(
                "purchase_price",
                0
            ) or 0
        )

        product.sale_price = float(
            request.form.get(
                "sale_price",
                0
            ) or 0
        )

        product.shopier_product_id = (
            request.form.get(
                "shopier_product_id"
            ) or None
        )

        db.session.commit()

        return redirect(
            url_for("dashboard")
        )

    return render_template(
        "edit_product.html",
        product=product
    )


# =========================================================
# DELETE PRODUCT
# =========================================================

@app.route(
    "/delete-product/<int:product_id>",
    methods=["POST"]
)
def delete_product(product_id):

    if not login_required():

        return redirect(
            url_for("home")
        )

    product = Product.query.get_or_404(
        product_id
    )

    db.session.delete(
        product
    )

    db.session.commit()

    return redirect(
        url_for("dashboard")
    )


# =========================================================
# DATABASE
# =========================================================

with app.app_context():

    db.create_all()


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    print(
        "Shopier key bulundu mu:",
        bool(SHOPIER_API_KEY)
    )

    if SHOPIER_API_KEY:

        print(
            "Shopier key uzunluğu:",
            len(SHOPIER_API_KEY)
        )

    print(
        "Shopier otomatik kontrol:",
        f"{SHOPIER_CHECK_INTERVAL} saniyede bir"
    )

    start_shopier_background_worker()

    app.run(
        debug=False
    )