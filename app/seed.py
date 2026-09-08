"""Demo seed at realistic scale (hardcoded mock, DETERMINISTIC generation).

Calibrated to the design mock figures: July 2026 with ~1,284 orders worth
~USD 512.4k equivalent; May and June provide the month-over-month deltas.
Same seed => same database on every machine.

User-facing data content (customer names, expense descriptions, chat texts)
is intentionally in Spanish — that is data, not code.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import ADMIN_EMAIL, ADMIN_NAME, ADMIN_PASSWORD, FX_TO_USD
from .models import ADMIN_ROLE, Conversation, Expense, Message, Order, User
from .services import security


def mulberry32(seed_value: int):
    """Small deterministic PRNG (same family as the original JS seed)."""
    a = seed_value & 0xFFFFFFFF

    def rand() -> float:
        nonlocal a
        a = (a + 0x6D2B79F5) & 0xFFFFFFFF
        t = a
        t = _imul(t ^ (t >> 15), 1 | t) & 0xFFFFFFFF
        t = (t + _imul(t ^ (t >> 7), 61 | t)) ^ t
        t &= 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296

    return rand


def _imul(x: int, y: int) -> int:
    """32-bit integer multiplication (JS Math.imul semantics, unsigned)."""
    return ((x & 0xFFFFFFFF) * (y & 0xFFFFFFFF)) & 0xFFFFFFFF


CUSTOMERS = [
    "Grupo Andino SAS", "Nortech Logistics", "Distribuidora Maya", "Café del Valle Ltda",
    "Bluepeak Media", "Constructora Ríos", "Textiles Monterrey", "Andes Analytics",
    "Farmacenter Bogotá", "Logística Pacífico", "Comercial El Dorado", "Grupo Zenit",
    "Alimentos La Cima", "Muebles Cedro", "Ferretería Central", "Bio Farma MX",
    "Cloud Andina", "Grupo Pacífico Azul", "Estudio Nébula", "Textil Norte",
    "Distribuciones Kapa", "Panadería San Blas", "Óptica Horizonte", "AgroExport Cauca",
    "Metalúrgica Díaz", "Software Latitud", "Clínica Del Prado", "Editorial Cardinal",
    "Transportes Vega", "Hotel Mirador Real", "Vinos & Licores CDMX", "Calzado Rondinela",
    "Energía Solar Andina", "Lácteos Montenegro", "Autopartes Jiménez", "Marketing Colibrí",
    "Seguros La Cumbre", "Papelería Origami", "Frutas del Trópico", "TecnoHogar Bogotá",
]

# Per-month config: order count and target approved revenue (USD equivalent),
# so the panel KPIs and deltas match the design mock (+8.2% revenue, +96 orders).
MONTHS = [
    {"period": "2026-05", "days": 31, "orders": 1082, "revenue": 438500},
    {"period": "2026-06", "days": 30, "orders": 1188, "revenue": 473600},
    {"period": "2026-07", "days": 31, "orders": 1284, "revenue": 512400},
]


def seed_if_empty(db: Session) -> bool:
    """Populate the database once; no-op when orders already exist."""
    if db.scalar(select(func.count(Order.id))):
        return False
    _seed(db)
    db.commit()
    return True


def ensure_admin(db: Session) -> dict | None:
    """Create the first account on a database with no users.

    Without ADMIN_PASSWORD a password is generated and returned so the caller
    can print it once; it is never stored in the clear or logged again.
    """
    if db.scalar(select(func.count(User.id))):
        return None

    password = ADMIN_PASSWORD or security.random_password()
    admin = User(
        email=ADMIN_EMAIL, name=ADMIN_NAME, role=ADMIN_ROLE, is_active=True,
        password_hash=security.hash_password(password),
        # A generated password must be replaced on first sign-in.
        must_change_password=ADMIN_PASSWORD is None,
        created_at=security.stamp(),
    )
    db.add(admin)
    db.commit()
    return {"email": admin.email,
            "password": None if ADMIN_PASSWORD else password}


def _seed(db: Session) -> None:
    rand = mulberry32(20260731)
    pick = lambda arr: arr[int(rand() * len(arr))]  # noqa: E731

    generated: list[dict] = []
    for cfg in MONTHS:
        month: list[dict] = []
        for _ in range(cfg["orders"]):
            # Status mix: ~90.5% approved, 5.4% pending, 3.4% rejected, 0.7% refunded.
            r_s = rand()
            status = ("approved" if r_s < 0.905 else
                      "pending" if r_s < 0.959 else
                      "rejected" if r_s < 0.993 else "refunded")
            # Gateway mix: Wompi 42%, ePayco 27%, Bold 19%, Stripe 12%.
            # Rejections concentrate on ePayco (~61%), as in the mock.
            if status == "rejected" and rand() < 0.61:
                gateway = "ePayco"
            else:
                r_g = rand()
                gateway = ("Wompi" if r_g < 0.42 else
                           "ePayco" if r_g < 0.69 else
                           "Bold" if r_g < 0.88 else "Stripe (USD)")
            if gateway == "Stripe (USD)":
                currency = "USD"
            elif gateway == "ePayco":
                currency = "MXN" if rand() < 0.55 else "COP"
            else:
                currency = "COP" if rand() < 0.85 else "USD"
            usd_base = 60 + rand() * rand() * 2400  # skewed toward small tickets
            day = 1 + int(rand() * cfg["days"])
            month.append({
                "customer": pick(CUSTOMERS), "usd_base": usd_base, "currency": currency,
                "status": status, "gateway": gateway,
                "date": f"{cfg['period']}-{day:02d}",
            })
        # Scale amounts so the approved sum matches the month's revenue target.
        approved_sum = sum(o["usd_base"] for o in month if o["status"] == "approved") or 1.0
        k = cfg["revenue"] / approved_sum
        for o in month:
            usd = o["usd_base"] * k
            if o["currency"] == "COP":
                o["amount"] = round(usd / FX_TO_USD["COP"] / 100) * 100
            elif o["currency"] == "MXN":
                o["amount"] = round(usd / FX_TO_USD["MXN"], 2)
            else:
                o["amount"] = round(usd, 2)
        generated.extend(month)

    # Sequential ids by date: the most recent order gets the highest id.
    generated.sort(key=lambda o: o["date"])
    for i, o in enumerate(generated):
        db.add(Order(id=f"ORD-{1001 + i}", customer=o["customer"], amount=o["amount"],
                     currency=o["currency"], status=o["status"], gateway=o["gateway"],
                     date=o["date"]))

    # Hardcoded monthly expenses (July ≈ USD 29k eq.; two missing receipts).
    expenses = [
        # ---- July 2026 ----
        ("Infraestructura cloud", "Infraestructura", "AWS", 6180, "USD", "D. Torres", "2026-07-30", "aws_julio.pdf"),
        ("Plataforma de datos", "Infraestructura", "Google Cloud", 2410, "USD", "D. Torres", "2026-07-29", "gcp_julio.pdf"),
        ("Monitoreo y alertas", "Infraestructura", "Datadog", 1190, "USD", "D. Torres", "2026-07-27", "datadog_julio.pdf"),
        ("Nómina desarrollo externo", "Nómina", "DevHouse Co", 7200, "USD", "S. Bermúdez", "2026-07-28", "devhouse_julio.pdf"),
        ("Campaña performance Q3", "Marketing", "Meta Ads", 14000000, "COP", "L. Prieto", "2026-07-26", "meta_q3.pdf"),
        ("Search y display", "Marketing", "Google Ads", 1590, "USD", "L. Prieto", "2026-07-24", "gads_julio.pdf"),
        ("Arriendo oficina", "Operación", "Inmobiliaria K7", 6900000, "COP", "M. Salgado", "2026-07-05", "arriendo_julio.pdf"),
        ("Viáticos equipo comercial", "Operación", "Interno", 2860000, "COP", "M. Salgado", "2026-07-22", None),
        ("Seguros corporativos", "Operación", "La Cumbre", 4680000, "COP", "M. Salgado", "2026-07-15", "seguros_julio.pdf"),
        ("Mensajería y logística", "Operación", "Interrapidísimo", 1560000, "COP", "M. Salgado", "2026-07-11", "mensajeria_julio.pdf"),
        ("Asesoría contable", "Servicios", "BDO México", 18400, "MXN", "S. Bermúdez", "2026-07-18", "bdo_julio.pdf"),
        ("Asesoría legal", "Servicios", "Rojas & Asociados", 1270, "USD", "S. Bermúdez", "2026-07-09", "legal_julio.pdf"),
        ("Licencias SaaS", "Software", "Atlassian", 640, "USD", "D. Torres", "2026-07-24", None),
        # ---- June 2026 ----
        ("Infraestructura cloud", "Infraestructura", "AWS", 5950, "USD", "D. Torres", "2026-06-30", "aws_junio.pdf"),
        ("Plataforma de datos", "Infraestructura", "Google Cloud", 2330, "USD", "D. Torres", "2026-06-28", "gcp_junio.pdf"),
        ("Monitoreo y alertas", "Infraestructura", "Datadog", 1150, "USD", "D. Torres", "2026-06-27", "datadog_junio.pdf"),
        ("Nómina desarrollo externo", "Nómina", "DevHouse Co", 7200, "USD", "S. Bermúdez", "2026-06-28", "devhouse_junio.pdf"),
        ("Campaña performance Q2", "Marketing", "Meta Ads", 13200000, "COP", "L. Prieto", "2026-06-20", "meta_q2.pdf"),
        ("Search y display", "Marketing", "Google Ads", 1480, "USD", "L. Prieto", "2026-06-18", "gads_junio.pdf"),
        ("Arriendo oficina", "Operación", "Inmobiliaria K7", 6900000, "COP", "M. Salgado", "2026-06-05", "arriendo_junio.pdf"),
        ("Seguros corporativos", "Operación", "La Cumbre", 4680000, "COP", "M. Salgado", "2026-06-15", "seguros_junio.pdf"),
        ("Viáticos equipo comercial", "Operación", "Interno", 2900000, "COP", "M. Salgado", "2026-06-19", "viaticos_junio.pdf"),
        ("Mensajería y logística", "Operación", "Interrapidísimo", 1450000, "COP", "M. Salgado", "2026-06-11", "mensajeria_junio.pdf"),
        ("Asesoría legal", "Servicios", "Rojas & Asociados", 1080, "USD", "S. Bermúdez", "2026-06-09", "legal_junio.pdf"),
        ("Asesoría contable", "Servicios", "BDO México", 18400, "MXN", "S. Bermúdez", "2026-06-16", "bdo_junio.pdf"),
        ("Licencias SaaS", "Software", "Atlassian", 640, "USD", "D. Torres", "2026-06-24", "atlassian_junio.pdf"),
        # ---- May 2026 ----
        ("Infraestructura cloud", "Infraestructura", "AWS", 5700, "USD", "D. Torres", "2026-05-30", "aws_mayo.pdf"),
        ("Plataforma de datos", "Infraestructura", "Google Cloud", 2280, "USD", "D. Torres", "2026-05-28", "gcp_mayo.pdf"),
        ("Nómina desarrollo externo", "Nómina", "DevHouse Co", 7200, "USD", "S. Bermúdez", "2026-05-28", "devhouse_mayo.pdf"),
        ("Campaña performance Q2", "Marketing", "Meta Ads", 12400000, "COP", "L. Prieto", "2026-05-19", "meta_mayo.pdf"),
        ("Arriendo oficina", "Operación", "Inmobiliaria K7", 6900000, "COP", "M. Salgado", "2026-05-05", "arriendo_mayo.pdf"),
        ("Seguros corporativos", "Operación", "La Cumbre", 4680000, "COP", "M. Salgado", "2026-05-15", "seguros_mayo.pdf"),
        ("Asesoría contable", "Servicios", "BDO México", 18400, "MXN", "S. Bermúdez", "2026-05-14", "bdo_mayo.pdf"),
        ("Licencias SaaS", "Software", "Atlassian", 640, "USD", "D. Torres", "2026-05-22", "atlassian_mayo.pdf"),
    ]
    for description, category, vendor, amount, currency, owner, date, receipt in expenses:
        db.add(Expense(description=description, category=category, vendor=vendor,
                       amount=amount, currency=currency, owner=owner, date=date,
                       receipt_name=receipt))

    # Preloaded "Recent" conversations (as in the design mock). Message content
    # is user-facing and therefore in Spanish.
    threads = [
        ("Cierre financiero de julio", "2026-07-31T18:20:00", [
            ("user", "¿Cómo cerró julio?"),
            ("agent", "Resumen de Julio 2026:\n\n• Ingresos: USD 512,4 k (+8,2% vs. Junio 2026)\n• Gastos: USD 29,0 k (+3,1%)\n• Margen operativo alto por el modelo de comisiones\n• Órdenes: 1.284 (+96)\n\nEl mayor volumen vino de Wompi. Puede exportar el detalle en la vista Reportes (PDF o CSV)."),
        ]),
        ("Rechazos ePayco tarjetas internacionales", "2026-07-30T16:05:00", [
            ("user", "¿Por qué suben los rechazos?"),
            ("agent", "Los rechazos del mes se concentran en ePayco (~61% del total de rechazadas). El patrón coincide con tarjetas de crédito internacionales. Sugerencia: revisar reglas antifraude del gateway y reintentos con Stripe (USD) para clientes fuera de Colombia."),
        ]),
        ("Gastos sin comprobante", "2026-07-29T10:42:00", [
            ("user", "¿Qué gastos están sin comprobante?"),
            ("agent", "Hay 2 gastos de julio sin comprobante cargado:\n\n• Licencias SaaS (Atlassian, USD 640)\n• Viáticos equipo comercial (COP 2.860.000)\n\nRecomiendo cargarlos antes del cierre contable."),
        ]),
        ("Proyección de margen Q3", "2026-07-25T09:15:00", [
            ("user", "¿Cómo viene el margen para el Q3?"),
            ("agent", "Con los datos de julio, los ingresos crecen +8,2% m/m mientras los gastos crecen +3,1%. Si la tendencia se mantiene en agosto y septiembre, el margen operativo del Q3 mejoraría entre 1 y 2 puntos vs. Q2. Puedo recalcular cuando entren datos de agosto."),
        ]),
        ("Conciliación Wompi semana 4", "2026-07-24T15:30:00", [
            ("user", "Resumen del volumen de Wompi en la semana 4"),
            ("agent", "En la semana 4 de julio, Wompi siguió siendo el gateway líder (~42% del volumen del mes). El detalle semanal está en Reportes → Ingresos por semana; puede exportar el CSV para conciliar contra el extracto del gateway."),
        ]),
        ("Alertas de gasto cloud", "2026-07-20T11:00:00", [
            ("user", "¿Hay alertas en infraestructura?"),
            ("agent", "El gasto de Infraestructura de julio (AWS + Google Cloud + Datadog ≈ USD 9,8 k) es la mayor categoría del mes (~34% del total). Creció frente a junio; vale la pena revisar el detalle de AWS en el comprobante aws_julio.pdf."),
        ]),
    ]
    for title, ts, messages in threads:
        conv = Conversation(title=title, created_at=ts)
        db.add(conv)
        db.flush()  # obtain conv.id before inserting messages
        for i, (role, content) in enumerate(messages):
            db.add(Message(conversation_id=conv.id, role=role, content=content,
                           created_at=f"{ts[:17]}{10 + i:02d}"))
