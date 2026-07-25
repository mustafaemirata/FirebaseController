"""
Firebase Kurumsal Yönetim Konsolu
==================================
Gemini destekli, gerçek zamanlı Firebase Authentication verileriyle çalışan
masaüstü yönetim paneli.

Yapı:
  - FirebaseService  : Tüm Firebase Auth verisini tek merkezden, önbellekli
                        şekilde çeker (Dashboard, Kullanıcılar ve Asistan
                        sekmeleri hep AYNI, tutarlı veriyi kullanır).
  - tool_*  fonksiyonları : Gemini'nin çağırabildiği araçlar. Her biri
                        yapılandırılmış (table/stats/text) bir sonuç
                        döndürür; bu sonuç DOĞRUDAN arayüzde tabloya
                        çizilir. Sayılar/e-postalar asla LLM'in serbest
                        metin üretiminden geçmez -> "yanlış veri" riski
                        ortadan kalkar. LLM sadece kısa bağlam cümlesi kurar.
  - DashboardView / UsersView / ChatView : Sol menüden geçilen üç ekran.
"""

import base64
import csv
import json
import os
import sys
import threading
import tkinter
from collections import Counter
from datetime import datetime, timedelta, timezone
from tkinter import filedialog, messagebox, ttk

try:
    from zoneinfo import ZoneInfo
except Exception:  # noqa: BLE001
    ZoneInfo = None

import customtkinter as ctk
import firebase_admin
from dotenv import load_dotenv
from firebase_admin import auth, credentials, firestore
from google import genai
from google.genai import types


def resource_path(filename: str) -> str:
    candidates = []
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        candidates += [
            os.path.join(exe_dir, filename),
            os.path.join(exe_dir, "_internal", filename),
        ]
    src_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(src_dir, filename))
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[-1]


# servis başlat
load_dotenv(resource_path(".env"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

FIREBASE_READY = True
FIREBASE_INIT_ERROR = None
try:
    if not firebase_admin._apps:
        cred = credentials.Certificate(resource_path("firebase_key.json"))
        firebase_admin.initialize_app(cred)
except Exception as exc:  # noqa: BLE001 - başlangıçta net hata göstermek istiyoruz
    FIREBASE_READY = False
    FIREBASE_INIT_ERROR = str(exc)

# firestore istemcisi 
try:
    from google.cloud.firestore_v1 import GeoPoint as _GeoPoint
except Exception:  
    _GeoPoint = None

_db = None
FIRESTORE_INIT_ERROR = None


def get_db():
    global _db, FIRESTORE_INIT_ERROR
    if _db is None:
        _db = firestore.client()
    return _db

# tablodaki türler
PROVIDER_LABELS = {
    "password": "E-posta / Şifre",
    "google.com": "Google",
    "apple.com": "Apple",
    "phone": "Telefon",
    "": "Anonim",
}

#yerel saate çevir
if ZoneInfo is not None:
    try:
        LOCAL_TZ = ZoneInfo("Europe/Istanbul")
    except Exception: 
        LOCAL_TZ = timezone(timedelta(hours=3))
else:
    LOCAL_TZ = timezone(timedelta(hours=3))


# 2. veri bellek katmanı
class FirebaseService:
    CACHE_TTL_SECONDS = 45

    def __init__(self):
        self._lock = threading.Lock()
        self._cache = None
        self._cache_time = None

    def _fetch_all_users_raw(self):
        if not FIREBASE_READY:
            raise RuntimeError(f"Firebase başlatılamadı: {FIREBASE_INIT_ERROR}")
        users = []
        page = auth.list_users()
        while page:
            for user in page.users:
                created = None
                last_signin = None
                meta = user.user_metadata
                if meta and meta.creation_timestamp:
                    created = datetime.fromtimestamp(meta.creation_timestamp / 1000, tz=timezone.utc)
                if meta and meta.last_sign_in_timestamp:
                    last_signin = datetime.fromtimestamp(meta.last_sign_in_timestamp / 1000, tz=timezone.utc)
                provider = user.provider_data[0].provider_id if user.provider_data else ""
                users.append(
                    {
                        "uid": user.uid,
                        "email": user.email or "—",
                        "phone": user.phone_number or "—",
                        "created": created,
                        "last_signin": last_signin,
                        "provider": provider,
                        "provider_label": PROVIDER_LABELS.get(provider, provider or "Anonim"),
                        "verified": bool(user.email_verified),
                        "disabled": bool(user.disabled),
                    }
                )
            page = page.get_next_page()
        return users

    # kullanıcı döndür
    def get_all_users(self, force_refresh=False):
        with self._lock:
            now = datetime.now(timezone.utc)
            stale = (
                self._cache is None
                or self._cache_time is None
                or (now - self._cache_time).total_seconds() > self.CACHE_TTL_SECONDS
            )
            if force_refresh or stale:
                self._cache = self._fetch_all_users_raw()
                self._cache_time = now
            return list(self._cache)

    @staticmethod
    def users_created_on(users, date):
        return [u for u in users if u["created"] and u["created"].astimezone(LOCAL_TZ).date() == date]

    @staticmethod
    def signup_trend(users, days=7):
        today = datetime.now(LOCAL_TZ).date()
        counts = {today - timedelta(days=i): 0 for i in range(days)}
        for u in users:
            if u["created"]:
                d = u["created"].astimezone(LOCAL_TZ).date()
                if d in counts:
                    counts[d] += 1
        return sorted(counts.items())

    @staticmethod
    def provider_breakdown(users):
        return Counter(u["provider_label"] for u in users).most_common()


fb = FirebaseService()


# firestore -> json
def fs_to_jsonable(obj):
    if isinstance(obj, dict):
        return {k: fs_to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [fs_to_jsonable(x) for x in obj]
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, datetime):
        return {"__fs__": "timestamp", "value": obj.isoformat()}
    if isinstance(obj, bytes):
        return {"__fs__": "bytes", "b64": base64.b64encode(obj).decode("ascii")}
    tn = type(obj).__name__
    if tn == "GeoPoint":
        return {"__fs__": "geopoint", "lat": obj.latitude, "lng": obj.longitude}
    if tn == "DocumentReference":
        return {"__fs__": "ref", "path": obj.path}
    return obj


def fs_from_jsonable(obj):
    if isinstance(obj, dict):
        tag = obj.get("__fs__")
        if tag == "timestamp":
            return datetime.fromisoformat(obj["value"])
        if tag == "bytes":
            return base64.b64decode(obj["b64"])
        if tag == "geopoint":
            if _GeoPoint is not None:
                return _GeoPoint(obj["lat"], obj["lng"])
            return {"latitude": obj["lat"], "longitude": obj["lng"]}
        if tag == "ref":
            return get_db().document(obj["path"])
        return {k: fs_from_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [fs_from_jsonable(x) for x in obj]
    return obj


class FirestoreService:
    """Firestore CRUD katmanı. Koleksiyon/doküman yolları '/' ile ayrılır
    (ör. 'users' veya 'users/uid123/orders')."""

    def list_collections(self, parent_doc_path=None):
        db = get_db()
        cols = db.document(parent_doc_path).collections() if parent_doc_path else db.collections()
        return sorted(c.id for c in cols)

    def list_documents(self, collection_path, limit=300):
        db = get_db()
        return [d.id for d in db.collection(collection_path).limit(limit).stream()]

    def list_documents_with_data(self, collection_path, limit=300):
        db = get_db()
        return [(d.id, d.to_dict() or {}) for d in db.collection(collection_path).limit(limit).stream()]

    def get_document(self, collection_path, doc_id):
        snap = get_db().collection(collection_path).document(doc_id).get()
        return snap.to_dict() if snap.exists else None

    def set_document(self, collection_path, doc_id, data):
        get_db().collection(collection_path).document(doc_id).set(data)

    def add_document(self, collection_path, data, doc_id=None):
        col = get_db().collection(collection_path)
        if doc_id:
            col.document(doc_id).set(data)
            return doc_id
        ref = col.document()
        ref.set(data)
        return ref.id

    def delete_document(self, collection_path, doc_id):
        get_db().collection(collection_path).document(doc_id).delete()

    def list_subcollections(self, collection_path, doc_id):
        cols = get_db().collection(collection_path).document(doc_id).collections()
        return sorted(c.id for c in cols)


fsvc = FirestoreService()


# gemini
def tool_get_daily_signups(date: str = "") -> dict:
    """Belirtilen tarihte (YYYY-AA-GG formatında, Türkiye saatine göre) veya
    tarih verilmezse bugün kayıt olan yeni kullanıcı sayısını döndürür."""
    users = fb.get_all_users()
    target = datetime.strptime(date, "%Y-%m-%d").date() if date else datetime.now(LOCAL_TZ).date()
    matches = fb.users_created_on(users, target)
    return {
        "type": "stats",
        "title": f"{target.isoformat()} Kayıt Sayısı (TR Saati)",
        "items": {"Yeni Kayıt": len(matches)},
    }


def tool_get_today_users_emails(date: str = "") -> dict:
    """Belirtilen tarihte (YYYY-AA-GG, Türkiye saatine göre) veya tarih
    verilmezse bugün kayıt olan kullanıcıların e-posta adreslerini, kayıt
    yöntemini ve doğrulama durumunu tablo halinde döndürür."""
    users = fb.get_all_users()
    target = datetime.strptime(date, "%Y-%m-%d").date() if date else datetime.now(LOCAL_TZ).date()
    matches = fb.users_created_on(users, target)
    if not matches:
        return {"type": "text", "text": f"{target.isoformat()} tarihinde kayıt olan kullanıcı yok."}
    rows = [
        {
            "E-posta": m["email"],
            "Saat (TR)": m["created"].astimezone(LOCAL_TZ).strftime("%H:%M"),
            "Kayıt Yöntemi": m["provider_label"],
            "Doğrulandı": "Evet" if m["verified"] else "Hayır",
        }
        for m in matches
    ]
    return {"type": "table", "title": f"{target.isoformat()} Kayıtları ({len(rows)})", "rows": rows}


def tool_get_total_user_count() -> dict:
    """Sistemdeki toplam kayıtlı kullanıcı sayısını döndürür."""
    users = fb.get_all_users()
    return {"type": "stats", "title": "Toplam Kullanıcı", "items": {"Toplam Kullanıcı": len(users)}}


def tool_get_signup_trend(days: int = 7) -> dict:
    """Son N gündeki (varsayılan 7, en fazla 90) günlük kayıt sayılarını
    tarih sırasıyla tablo halinde döndürür."""
    try:
        days = max(1, min(int(days), 90))
    except (TypeError, ValueError):
        days = 7
    users = fb.get_all_users()
    trend = fb.signup_trend(users, days)
    rows = [{"Tarih": d.isoformat(), "Kayıt": c} for d, c in trend]
    return {"type": "table", "title": f"Son {days} Günlük Kayıt Trendi", "rows": rows}


def tool_get_provider_breakdown() -> dict:
    """Kullanıcıların giriş yöntemine göre (Google, Apple, e-posta/şifre,
    telefon, anonim) dağılımını tablo halinde döndürür."""
    users = fb.get_all_users()
    rows = [{"Yöntem": label, "Kullanıcı Sayısı": count} for label, count in fb.provider_breakdown(users)]
    return {"type": "table", "title": "Giriş Yöntemi Dağılımı", "rows": rows}


def tool_get_disabled_users() -> dict:
    """Devre dışı bırakılmış (disabled) kullanıcıların e-posta ve kayıt
    tarihi listesini döndürür."""
    users = fb.get_all_users()
    disabled = [u for u in users if u["disabled"]]
    if not disabled:
        return {"type": "text", "text": "Devre dışı bırakılmış kullanıcı yok."}
    rows = [
        {
            "E-posta": u["email"],
            "Kayıt Tarihi (TR)": u["created"].astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M") if u["created"] else "—",
            "Yöntem": u["provider_label"],
        }
        for u in disabled
    ]
    return {"type": "table", "title": f"Devre Dışı Kullanıcılar ({len(rows)})", "rows": rows}


def tool_get_unverified_users_count() -> dict:
    """E-postası doğrulanmamış kullanıcı sayısını döndürür (anonim/telefon
    kullanıcıları hariç)."""
    users = fb.get_all_users()
    count = sum(1 for u in users if not u["verified"] and u["email"] != "—")
    return {"type": "stats", "title": "Doğrulanmamış E-postalar", "items": {"Doğrulanmamış": count}}


def tool_search_user_by_email(email: str) -> dict:
    """Verilen e-posta adresine tam veya kısmi eşleşen kullanıcı(lar)ı arar
    ve kayıt tarihi, yöntem, doğrulama ve hesap durumunu tablo halinde
    döndürür."""
    users = fb.get_all_users()
    needle = (email or "").lower().strip()
    matches = [u for u in users if needle and needle in u["email"].lower()]
    if not matches:
        return {"type": "text", "text": f"'{email}' ile eşleşen kullanıcı bulunamadı."}
    rows = [
        {
            "E-posta": u["email"],
            "Kayıt Tarihi (TR)": u["created"].astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M") if u["created"] else "—",
            "Yöntem": u["provider_label"],
            "Doğrulandı": "Evet" if u["verified"] else "Hayır",
            "Durum": "Aktif" if not u["disabled"] else "Devre Dışı",
        }
        for u in matches[:25]
    ]
    return {"type": "table", "title": f"'{email}' Arama Sonucu ({len(matches)})", "rows": rows}


def tool_get_signups_in_range(start_date: str, end_date: str) -> dict:
    """Verilen başlangıç ve bitiş tarihleri (YYYY-AA-GG, Türkiye saatine
    göre, her iki uç da dahil) arasındaki günlük kayıt sayılarını ve
    toplamı tablo halinde döndürür. 'Geçen hafta', 'bu ay' gibi göreli
    aralıklar için başlangıç/bitiş tarihini bugünün tarihine göre hesapla."""
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return {"type": "text", "text": "Tarih formatı hatalı. YYYY-AA-GG kullanın."}
    if start > end:
        start, end = end, start
    users = fb.get_all_users()
    counts = {}
    d = start
    while d <= end:
        counts[d] = 0
        d += timedelta(days=1)
    total = 0
    for u in users:
        if u["created"]:
            cd = u["created"].astimezone(LOCAL_TZ).date()
            if cd in counts:
                counts[cd] += 1
                total += 1
    rows = [{"Tarih": d.isoformat(), "Kayıt": c} for d, c in sorted(counts.items())]
    return {"type": "table", "title": f"{start.isoformat()} – {end.isoformat()} Kayıtları (Toplam {total})", "rows": rows}


def tool_compare_weekly_growth() -> dict:
    """Son 7 gün ile ondan önceki 7 günü karşılaştırıp kayıt sayısındaki
    büyüme/düşüş yüzdesini döndürür."""
    users = fb.get_all_users()
    today = datetime.now(LOCAL_TZ).date()
    this_week_start = today - timedelta(days=6)
    last_week_start = today - timedelta(days=13)
    last_week_end = today - timedelta(days=7)
    this_week = sum(
        1 for u in users if u["created"] and this_week_start <= u["created"].astimezone(LOCAL_TZ).date() <= today
    )
    last_week = sum(
        1 for u in users if u["created"] and last_week_start <= u["created"].astimezone(LOCAL_TZ).date() <= last_week_end
    )
    if last_week == 0:
        change_str = "—" if this_week == 0 else "yeni"
    else:
        change_str = f"{((this_week - last_week) / last_week) * 100:+.0f}%"
    return {
        "type": "stats",
        "title": "Haftalık Büyüme (Son 7 Gün ve Önceki 7 Gün)",
        "items": {"Bu Hafta": this_week, "Geçen Hafta": last_week, "Değişim": change_str},
    }


def tool_get_monthly_signup_count(month: str = "") -> dict:
    """Belirtilen ayda (YYYY-AA formatında) veya belirtilmezse içinde
    bulunulan ayda kayıt olan kullanıcı sayısını döndürür."""
    users = fb.get_all_users()
    now_local = datetime.now(LOCAL_TZ)
    if month:
        try:
            year, mon = (int(x) for x in month.split("-"))
        except ValueError:
            return {"type": "text", "text": "Ay formatı hatalı. YYYY-AA kullanın (örn: 2026-07)."}
    else:
        year, mon = now_local.year, now_local.month
    count = sum(
        1
        for u in users
        if u["created"]
        and u["created"].astimezone(LOCAL_TZ).year == year
        and u["created"].astimezone(LOCAL_TZ).month == mon
    )
    return {"type": "stats", "title": f"{year}-{mon:02d} Kayıt Sayısı", "items": {"Kayıt": count}}


def tool_firestore_list_collections() -> dict:
    """Firestore veritabanındaki üst düzey koleksiyonların listesini döndürür."""
    try:
        cols = fsvc.list_collections()
    except Exception as exc:  # noqa: BLE001
        return {"type": "text", "text": f"Firestore erişim hatası: {exc}"}
    if not cols:
        return {"type": "text", "text": "Hiç koleksiyon bulunamadı."}
    return {"type": "table", "title": "Firestore Koleksiyonları", "rows": [{"Koleksiyon": c} for c in cols]}


def tool_firestore_list_documents(collection: str, limit: int = 50) -> dict:
    """Belirtilen Firestore koleksiyonundaki doküman ID'lerini listeler."""
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 50
    try:
        ids = fsvc.list_documents(collection, limit)
    except Exception as exc:  # noqa: BLE001
        return {"type": "text", "text": f"Firestore erişim hatası: {exc}"}
    if not ids:
        return {"type": "text", "text": f"'{collection}' koleksiyonunda doküman yok."}
    return {"type": "table", "title": f"'{collection}' Dokümanları ({len(ids)})", "rows": [{"Doküman ID": i} for i in ids]}


def tool_firestore_get_document(collection: str, doc_id: str) -> dict:
    """Belirtilen Firestore koleksiyonundaki bir dokümanın tüm alanlarını
    (iç içe map/array dahil) JSON olarak getirir."""
    try:
        data = fsvc.get_document(collection, doc_id)
    except Exception as exc:  # noqa: BLE001
        return {"type": "text", "text": f"Firestore erişim hatası: {exc}"}
    if data is None:
        return {"type": "text", "text": f"'{collection}/{doc_id}' dokümanı bulunamadı."}
    pretty = json.dumps(fs_to_jsonable(data), ensure_ascii=False, indent=2, default=str)
    return {"type": "text", "text": f"{collection}/{doc_id}:\n{pretty}"}


TOOL_FUNCTIONS = [
    tool_get_daily_signups,
    tool_get_today_users_emails,
    tool_get_total_user_count,
    tool_get_signup_trend,
    tool_get_provider_breakdown,
    tool_get_disabled_users,
    tool_get_unverified_users_count,
    tool_search_user_by_email,
    tool_get_signups_in_range,
    tool_compare_weekly_growth,
    tool_get_monthly_signup_count,
    tool_firestore_list_collections,
    tool_firestore_list_documents,
    tool_firestore_get_document,
]
TOOL_MAP = {f.__name__: f for f in TOOL_FUNCTIONS}


# tema
COLORS = {
    "bg": "#f1f5f9",
    "sidebar": "#0f172a",
    "sidebar_hover": "#1e293b",
    "accent": "#2563eb",
    "accent_dark": "#1d4ed8",
    "card": "#ffffff",
    "border": "#e2e8f0",
    "text": "#0f172a",
    "muted": "#64748b",
    "green": "#16a34a",
    "amber": "#d97706",
    "red": "#dc2626",
}

ctk.set_appearance_mode("Light")
ctk.set_default_color_theme("blue")


def configure_ttk_style():
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:  
        pass
    style.configure(
        "Corp.Treeview",
        background="white",
        fieldbackground="white",
        foreground=COLORS["text"],
        rowheight=30,
        borderwidth=0,
        font=("Helvetica", 12),
    )
    style.configure(
        "Corp.Treeview.Heading",
        background="#f1f5f9",
        foreground="#334155",
        font=("Helvetica", 12, "bold"),
        relief="flat",
        padding=6,
    )
    style.map(
        "Corp.Treeview",
        background=[("selected", "#dbeafe")],
        foreground=[("selected", "#1e3a8a")],
    )


# dashboard
class DashboardView(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color=COLORS["bg"])
        self.app = app
        self._loading = False

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=28, pady=(24, 8))
        ctk.CTkLabel(header, text="Genel Bakış", font=ctk.CTkFont(size=22, weight="bold"), text_color=COLORS["text"]).pack(side="left")
        self.updated_label = ctk.CTkLabel(header, text="", font=ctk.CTkFont(size=11), text_color=COLORS["muted"])
        self.updated_label.pack(side="right", padx=(0, 12))
        self.refresh_btn = ctk.CTkButton(header, text="Yenile", width=90, fg_color=COLORS["accent"], command=lambda: self.refresh(force=True))
        self.refresh_btn.pack(side="right")

        self.cards_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.cards_frame.pack(fill="x", padx=28, pady=8)
        self.card_widgets = {}
        card_defs = [
            ("total", "Toplam Kullanıcı", COLORS["accent"]),
            ("today", "Bugünkü Kayıt", COLORS["green"]),
            ("week", "Son 7 Gün", COLORS["amber"]),
            ("unverified", "Doğrulanmamış", COLORS["red"]),
        ]
        for key, label, color in card_defs:
            card = ctk.CTkFrame(self.cards_frame, fg_color=COLORS["card"], corner_radius=14, border_width=1, border_color=COLORS["border"])
            card.pack(side="left", expand=True, fill="both", padx=8)
            val_lbl = ctk.CTkLabel(card, text="—", font=ctk.CTkFont(size=28, weight="bold"), text_color=color)
            val_lbl.pack(padx=20, pady=(20, 2), anchor="w")
            ctk.CTkLabel(card, text=label, font=ctk.CTkFont(size=12), text_color=COLORS["muted"]).pack(padx=20, pady=(0, 18), anchor="w")
            self.card_widgets[key] = val_lbl

        tables_row = ctk.CTkFrame(self, fg_color="transparent")
        tables_row.pack(fill="both", expand=True, padx=28, pady=(8, 24))

        trend_card = ctk.CTkFrame(tables_row, fg_color=COLORS["card"], corner_radius=14, border_width=1, border_color=COLORS["border"])
        trend_card.pack(side="left", fill="both", expand=True, padx=(0, 8))
        ctk.CTkLabel(trend_card, text="Son 7 Günlük Kayıt Trendi", font=ctk.CTkFont(size=14, weight="bold"), text_color=COLORS["text"]).pack(anchor="w", padx=16, pady=(14, 8))
        self.trend_tree = ttk.Treeview(trend_card, columns=("date", "count"), show="headings", style="Corp.Treeview", height=7)
        self.trend_tree.heading("date", text="Tarih")
        self.trend_tree.column("date", width=140)
        self.trend_tree.heading("count", text="Kayıt")
        self.trend_tree.column("count", width=80, anchor="center")
        self.trend_tree.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        provider_card = ctk.CTkFrame(tables_row, fg_color=COLORS["card"], corner_radius=14, border_width=1, border_color=COLORS["border"])
        provider_card.pack(side="left", fill="both", expand=True, padx=(8, 0))
        ctk.CTkLabel(provider_card, text="Giriş Yöntemi Dağılımı", font=ctk.CTkFont(size=14, weight="bold"), text_color=COLORS["text"]).pack(anchor="w", padx=16, pady=(14, 8))
        self.provider_tree = ttk.Treeview(provider_card, columns=("method", "count"), show="headings", style="Corp.Treeview", height=7)
        self.provider_tree.heading("method", text="Yöntem")
        self.provider_tree.column("method", width=160)
        self.provider_tree.heading("count", text="Kullanıcı")
        self.provider_tree.column("count", width=90, anchor="center")
        self.provider_tree.pack(fill="both", expand=True, padx=16, pady=(0, 16))

    def refresh(self, force=True):
        if self._loading:
            return
        self._loading = True
        self.refresh_btn.configure(state="disabled", text="Yükleniyor...")
        threading.Thread(target=self._load, args=(force,), daemon=True).start()

    def _load(self, force):
        try:
            users = fb.get_all_users(force_refresh=force)
            today = datetime.now(LOCAL_TZ).date()
            today_count = len(fb.users_created_on(users, today))
            now = datetime.now(timezone.utc)
            week_count = sum(1 for u in users if u["created"] and (now - u["created"]).days < 7)
            unverified = sum(1 for u in users if not u["verified"] and u["email"] != "—")
            trend = fb.signup_trend(users, 7)
            providers = fb.provider_breakdown(users)
            self.after(0, self._apply, len(users), today_count, week_count, unverified, trend, providers)
        except Exception as exc:  # noqa: BLE001
            self.after(0, self._apply_error, str(exc))

    def _apply(self, total, today_count, week_count, unverified, trend, providers):
        self.card_widgets["total"].configure(text=str(total))
        self.card_widgets["today"].configure(text=str(today_count))
        self.card_widgets["week"].configure(text=str(week_count))
        self.card_widgets["unverified"].configure(text=str(unverified))
        for row in self.trend_tree.get_children():
            self.trend_tree.delete(row)
        for d, c in trend:
            self.trend_tree.insert("", "end", values=(d.isoformat(), c))
        for row in self.provider_tree.get_children():
            self.provider_tree.delete(row)
        for label, count in providers:
            self.provider_tree.insert("", "end", values=(label, count))
        self.updated_label.configure(text=f"Son güncelleme: {datetime.now().strftime('%H:%M:%S')}", text_color=COLORS["muted"])
        self.refresh_btn.configure(state="normal", text="Yenile")
        self._loading = False

    def _apply_error(self, msg):
        self.updated_label.configure(text=f"Hata: {msg}", text_color=COLORS["red"])
        self.refresh_btn.configure(state="normal", text="Yenile")
        self._loading = False


# dışa aktar
class UsersView(ctk.CTkFrame):
    COLUMNS = ("email", "uid", "created", "last_signin", "provider", "verified", "status")
    HEADERS = {
        "email": "E-posta",
        "uid": "UID",
        "created": "Kayıt Tarihi (TR)",
        "last_signin": "Son Giriş (TR)",
        "provider": "Yöntem",
        "verified": "Doğrulandı",
        "status": "Durum",
    }
    WIDTHS = {"email": 210, "uid": 240, "created": 150, "last_signin": 150, "provider": 120, "verified": 100, "status": 110}

    def __init__(self, parent, app):
        super().__init__(parent, fg_color=COLORS["bg"])
        self.app = app
        self._all_rows = []
        self._loading = False
        self._sort_state = {}

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=28, pady=(24, 8))
        ctk.CTkLabel(header, text="Kullanıcılar", font=ctk.CTkFont(size=22, weight="bold"), text_color=COLORS["text"]).pack(side="left")
        self.count_label = ctk.CTkLabel(header, text="", font=ctk.CTkFont(size=12), text_color=COLORS["muted"])
        self.count_label.pack(side="left", padx=12)
        ctk.CTkButton(header, text="CSV Dışa Aktar", width=140, fg_color=COLORS["sidebar"], hover_color=COLORS["sidebar_hover"], command=self.export_csv).pack(side="right")
        self.refresh_btn = ctk.CTkButton(header, text="Yenile", width=90, fg_color=COLORS["accent"], command=lambda: self.refresh(force=True))
        self.refresh_btn.pack(side="right", padx=(0, 8))

        search_row = ctk.CTkFrame(self, fg_color="transparent")
        search_row.pack(fill="x", padx=28, pady=(0, 2))
        self.search_entry = ctk.CTkEntry(search_row, placeholder_text="E-posta veya UID ile ara...")
        self.search_entry.pack(fill="x")
        self.search_entry.bind("<KeyRelease>", lambda e: self._filter())
        ctk.CTkLabel(self, text="Kopyalamak için bir satıra sağ tıklayın · çift tık UID'yi kopyalar", font=ctk.CTkFont(size=10), text_color=COLORS["muted"]).pack(anchor="w", padx=28, pady=(0, 6))

        table_card = ctk.CTkFrame(self, fg_color=COLORS["card"], corner_radius=14, border_width=1, border_color=COLORS["border"])
        table_card.pack(fill="both", expand=True, padx=28, pady=(0, 24))

        self.tree = ttk.Treeview(table_card, columns=self.COLUMNS, show="headings", style="Corp.Treeview")
        for c in self.COLUMNS:
            self.tree.heading(c, text=self.HEADERS[c], command=lambda cc=c: self._sort_by(cc))
            self.tree.column(c, width=self.WIDTHS[c], anchor="w")
        vsb = ttk.Scrollbar(table_card, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(16, 0), pady=16)
        vsb.pack(side="right", fill="y", pady=16, padx=(0, 16))

        # sağ tık
        self._menu = tkinter.Menu(self, tearoff=0)
        self._menu.add_command(label="UID Kopyala", command=lambda: self._copy_field("uid"))
        self._menu.add_command(label="E-posta Kopyala", command=lambda: self._copy_field("email"))
        self._menu.add_command(label="Satırı Kopyala", command=self._copy_row)
        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<Button-2>", self._on_right_click)  # macOS
        self.tree.bind("<Double-1>", lambda e: self._copy_field("uid"))

    # yenile
    def refresh(self, force=True):
        if self._loading:
            return
        self._loading = True
        self.refresh_btn.configure(state="disabled", text="Yükleniyor...")
        threading.Thread(target=self._load, args=(force,), daemon=True).start()

    def _load(self, force):
        try:
            users = fb.get_all_users(force_refresh=force)
          
            users_sorted = sorted(
                users, key=lambda u: u["created"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True
            )
            today_local = datetime.now(LOCAL_TZ).date()
            rows = []
            for u in users_sorted:
                created_local = u["created"].astimezone(LOCAL_TZ) if u["created"] else None
                last_local = u["last_signin"].astimezone(LOCAL_TZ) if u["last_signin"] else None
                rows.append(
                    {
                        "email": u["email"],
                        "uid": u["uid"],
                        "created": created_local.strftime("%Y-%m-%d %H:%M") if created_local else "—",
                        "last_signin": last_local.strftime("%Y-%m-%d %H:%M") if last_local else "—",
                        "provider": u["provider_label"],
                        "verified": "Evet" if u["verified"] else "Hayır",
                        "status": "Devre Dışı" if u["disabled"] else "Aktif",
                        "_is_today": bool(created_local and created_local.date() == today_local),
                    }
                )
            self.after(0, self._apply, rows)
        except Exception as exc:  # noqa: BLE001
            self.after(0, self._apply_error, str(exc))

    def _apply(self, rows):
        self._all_rows = rows
        self._render(rows)
        self.count_label.configure(text=f"{len(rows)} kullanıcı", text_color=COLORS["muted"])
        self.refresh_btn.configure(state="normal", text="Yenile")
        self._loading = False

    def _apply_error(self, msg):
        self.count_label.configure(text=f"Hata: {msg}", text_color=COLORS["red"])
        self.refresh_btn.configure(state="normal", text="Yenile")
        self._loading = False

    def _render(self, rows):
        for row in self.tree.get_children():
            self.tree.delete(row)
      
        self.tree.tag_configure("odd", background="#f8fafc")
        self.tree.tag_configure("even", background="white")
        self.tree.tag_configure("today", background="#dcfce7")
        self.tree.tag_configure("disabled", foreground="#b91c1c")
        for i, r in enumerate(rows):
            values = [r.get(c, "") for c in self.COLUMNS]
            tags = []
            if r.get("_is_today"):
                tags.append("today")
            if r.get("status") == "Devre Dışı":
                tags.append("disabled")
            tags.append("odd" if i % 2 else "even")
            self.tree.insert("", "end", values=values, tags=tuple(tags))

    def _filter(self):
        q = self.search_entry.get().lower().strip()
        if not q:
            self._render(self._all_rows)
        else:
            self._render([r for r in self._all_rows if q in r["email"].lower() or q in r.get("uid", "").lower()])

    # kopyala
    def _on_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            try:
                self._menu.tk_popup(event.x_root, event.y_root)
            finally:
                self._menu.grab_release()

    def _selected_values(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self.tree.item(sel[0], "values")

    def _copy_to_clipboard(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.count_label.configure(text="Panoya kopyalandı", text_color=COLORS["green"])

    def _copy_field(self, field):
        vals = self._selected_values()
        if not vals:
            return
        idx = self.COLUMNS.index(field)
        self._copy_to_clipboard(str(vals[idx]))

    def _copy_row(self):
        vals = self._selected_values()
        if not vals:
            return
        line = "\t".join(str(v) for v in vals)
        self._copy_to_clipboard(line)

    def _sort_by(self, col):
        reverse = self._sort_state.get(col, False)
        rows = sorted(self._all_rows, key=lambda r: r[col], reverse=reverse)
        self._sort_state[col] = not reverse
        self._render(rows)

    def export_csv(self):
        if not self._all_rows:
            messagebox.showinfo("Boş", "Dışa aktarılacak kullanıcı verisi yok. Önce yenileyin.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")], initialfile="kullanicilar.csv")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=list(self.COLUMNS), extrasaction="ignore")
                writer.writerow(self.HEADERS)
                writer.writerows(self._all_rows)
            messagebox.showinfo("Dışa Aktarıldı", f"{len(self._all_rows)} kullanıcı CSV olarak kaydedildi.")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Hata", str(exc))


# asistan - balon
class ChatView(ctk.CTkFrame):
    QUICK_ACTIONS = [
        ("Bugünkü Kayıtlar", "Bugün kaç kişi kayıt oldu?"),
        ("Dünkü Kayıtlar", "Dün kaç kişi kayıt oldu?"),
        ("Toplam Kullanıcı", "Toplam kaç kullanıcı var?"),
        ("7 Günlük Trend", "Son 7 günün kayıt trendini göster."),
        ("Haftalık Büyüme", "Bu haftaki kayıt sayısını geçen haftayla karşılaştır."),
        ("Bu Ay", "Bu ay kaç kişi kayıt oldu?"),
        ("Giriş Yöntemleri", "Kullanıcıların giriş yöntemi dağılımını göster."),
        ("Devre Dışı Kullanıcılar", "Devre dışı bırakılmış kullanıcıları listele."),
        ("Doğrulanmamışlar", "E-postası doğrulanmamış kaç kullanıcı var?"),
    ]

    WEEKDAYS_TR = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]


    @classmethod
    def _build_system_instruction(cls):
        #tarih belirteci
        now_local = datetime.now(LOCAL_TZ)
        today_str = now_local.strftime("%Y-%m-%d")
        yesterday_str = (now_local - timedelta(days=1)).strftime("%Y-%m-%d")
        gun_adi = cls.WEEKDAYS_TR[now_local.weekday()]
        return (
            f"Bugünün gerçek tarihi (Türkiye saati, UTC+3): {today_str} ({gun_adi}). "
            f"Dünün tarihi: {yesterday_str}. Kendi bilgindeki veya eğitim verindeki farklı "
            "bir 'bugün' tarihini ASLA kullanma; sadece burada verilen tarihi baz al. "
            "Kullanıcı 'bugün', 'dün', 'bu hafta', 'geçen hafta', 'bu ay' gibi göreli "
            "ifadeler kullandığında bu tarihten hesaplayarak araç çağrısına kesin bir "
            "YYYY-AA-GG tarihi (veya aralık) gönder. Sayısal, listeye ya da tarihe dayalı "
            "HER soruda mutlaka uygun aracı çağır; asla sayı, e-posta ya da tarih uydurma "
            "veya tahmin etme. Araç sonucu kullanıcıya zaten ayrı bir tabloda gösterilecek; "
            "sen yalnızca çok kısa, doğal bir bağlam cümlesi ya da genel sohbet yanıtı ver. "
            "Türkçe yanıt ver."
        )

    def __init__(self, parent, app):
        super().__init__(parent, fg_color=COLORS["bg"])
        self.app = app
        self._typing_row = None

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=28, pady=(24, 8))
        ctk.CTkLabel(header, text="Asistan", font=ctk.CTkFont(size=22, weight="bold"), text_color=COLORS["text"]).pack(side="left")

        chips = ctk.CTkFrame(self, fg_color="transparent")
        chips.pack(fill="x", padx=28, pady=(0, 8))
        for label, prompt in self.QUICK_ACTIONS:
            ctk.CTkButton(
                chips,
                text=label,
                fg_color="white",
                text_color="#334155",
                border_width=1,
                border_color=COLORS["border"],
                hover_color="#e2e8f0",
                corner_radius=16,
                height=28,
                font=ctk.CTkFont(size=11),
                command=lambda p=prompt: self.trigger_quick_action(p),
            ).pack(side="left", padx=(0, 8), pady=2)

        chat_card = ctk.CTkFrame(self, fg_color=COLORS["card"], corner_radius=14, border_width=1, border_color=COLORS["border"])
        chat_card.pack(fill="both", expand=True, padx=28, pady=(0, 12))

        self.chat_scroll = ctk.CTkScrollableFrame(chat_card, fg_color="transparent")
        self.chat_scroll.pack(fill="both", expand=True, padx=4, pady=4)

        input_row = ctk.CTkFrame(self, fg_color="transparent")
        input_row.pack(fill="x", padx=28, pady=(0, 24))
        self.entry = ctk.CTkEntry(input_row, placeholder_text="Bir soru yazın... (örn: Bu hafta kaç kişi kayıt oldu?)", height=40)
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.entry.bind("<Return>", self.send_message)
        self.send_btn = ctk.CTkButton(input_row, text="Gönder", width=100, height=40, fg_color=COLORS["accent"], command=self.send_message)
        self.send_btn.pack(side="right")

    def trigger_quick_action(self, prompt):
        self.entry.delete(0, "end")
        self.entry.insert(0, prompt)
        self.send_message()

    #mesaj gönderme servisi
    def send_message(self, event=None):
        text = self.entry.get().strip()
        if not text:
            return
        if client is None:
            self.add_bubble("error", "GEMINI_API_KEY tanımlı değil. .env dosyasını kontrol edin.")
            return
        self.add_bubble("user", text)
        self.entry.delete(0, "end")
        self.send_btn.configure(state="disabled")
        self._show_typing()
        threading.Thread(target=self._process, args=(text,), daemon=True).start()

    def _show_typing(self):
        row = ctk.CTkFrame(self.chat_scroll, fg_color="transparent")
        row.pack(fill="x", pady=6, padx=10)
        bubble = ctk.CTkFrame(row, fg_color="#f1f5f9", corner_radius=16)
        bubble.pack(anchor="w")
        ctk.CTkLabel(bubble, text="Asistan yazıyor...", text_color=COLORS["muted"], font=ctk.CTkFont(size=12, slant="italic")).pack(padx=14, pady=8)
        self._typing_row = row
        self._scroll_to_bottom()

    def _hide_typing(self):
        if self._typing_row is not None:
            self._typing_row.destroy()
            self._typing_row = None

    def _process(self, text):
        try:
            history = self.app.chat_history
            history.append(types.Content(role="user", parts=[types.Part(text=text)]))
            config = types.GenerateContentConfig(
                system_instruction=self._build_system_instruction(),
                tools=TOOL_FUNCTIONS,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                temperature=0.2,
            )
            response = client.models.generate_content(model="gemini-2.5-flash", contents=history, config=config)
            calls = response.function_calls
            if calls:
                summaries = []
                for fc in calls:
                    fn = TOOL_MAP.get(fc.name)
                    if fn is None:
                        result = {"type": "text", "text": f"Bilinmeyen araç çağrısı: {fc.name}"}
                    else:
                        try:
                            result = fn(**(fc.args or {}))
                        except Exception as exc:  
                            result = {"type": "text", "text": f"'{fc.name}' çalıştırılırken hata oluştu: {exc}"}
                    self.after(0, self.add_result_bubble, result)
                    summaries.append(self._summarize(result))
                history.append(types.Content(role="model", parts=[types.Part(text=" ".join(summaries))]))
            else:
                reply = response.text or "Üzgünüm, bir yanıt oluşturamadım."
                self.after(0, self.add_bubble, "bot", reply)
                history.append(types.Content(role="model", parts=[types.Part(text=reply)]))
            del history[:-20]
        except Exception as exc:  
            self.after(0, self.add_bubble, "error", f"Sistem hatası: {exc}")
        finally:
            self.after(0, self._finish)

    def _finish(self):
        self._hide_typing()
        self.send_btn.configure(state="normal")

    @staticmethod
    def _summarize(result):
        rtype = result.get("type")
        if rtype == "table":
            return f"[{result.get('title')} tablo halinde gösterildi, {len(result.get('rows', []))} kayıt.]"
        if rtype == "stats":
            items = ", ".join(f"{k}: {v}" for k, v in result.get("items", {}).items())
            return f"[{result.get('title')}: {items}]"
        return result.get("text", "")

    # balon
    def add_bubble(self, sender, text):
        row = ctk.CTkFrame(self.chat_scroll, fg_color="transparent")
        row.pack(fill="x", pady=6, padx=10)
        inner = ctk.CTkFrame(row, fg_color="transparent")
        if sender == "user":
            inner.pack(anchor="e")
            bubble = ctk.CTkFrame(inner, fg_color=COLORS["accent"], corner_radius=16)
            avatar = self._avatar(inner, "S", COLORS["accent_dark"])
            bubble.pack(side="left", padx=(0, 8))
            avatar.pack(side="left")
            ctk.CTkLabel(bubble, text=text, text_color="white", wraplength=420, justify="left", font=ctk.CTkFont(size=13)).pack(padx=14, pady=10)
        else:
            inner.pack(anchor="w")
            avatar = self._avatar(inner, "AI", COLORS["sidebar"])
            avatar.pack(side="left", padx=(0, 8))
            is_error = sender == "error"
            color = "#fef2f2" if is_error else "white"
            border = "#fecaca" if is_error else COLORS["border"]
            text_color = "#b91c1c" if is_error else COLORS["text"]
            bubble = ctk.CTkFrame(inner, fg_color=color, border_width=1, border_color=border, corner_radius=16)
            bubble.pack(side="left")
            prefix = "Hata: " if is_error else ""
            ctk.CTkLabel(bubble, text=prefix + text, text_color=text_color, wraplength=420, justify="left", font=ctk.CTkFont(size=13)).pack(padx=14, pady=10)
        self._scroll_to_bottom()

    def add_result_bubble(self, result):
        row = ctk.CTkFrame(self.chat_scroll, fg_color="transparent")
        row.pack(fill="x", pady=6, padx=10)
        inner = ctk.CTkFrame(row, fg_color="transparent")
        inner.pack(anchor="w", fill="x")
        avatar = self._avatar(inner, "AI", COLORS["sidebar"])
        avatar.pack(side="left", padx=(0, 8), anchor="n")
        bubble = ctk.CTkFrame(inner, fg_color="white", border_width=1, border_color=COLORS["border"], corner_radius=16)
        bubble.pack(side="left", fill="x", expand=True, padx=(0, 60))

        rtype = result.get("type")
        if rtype == "table":
            title = result.get("title", "Sonuçlar")
            rows = result.get("rows", [])
            ctk.CTkLabel(bubble, text=title, font=ctk.CTkFont(size=13, weight="bold"), text_color=COLORS["text"]).pack(anchor="w", padx=14, pady=(12, 6))
            if rows:
                cols = list(rows[0].keys())
                tv = ttk.Treeview(bubble, columns=cols, show="headings", style="Corp.Treeview", height=min(len(rows), 8))
                for c in cols:
                    tv.heading(c, text=c)
                    tv.column(c, width=150, anchor="w")
                for i, r in enumerate(rows):
                    tv.insert("", "end", values=[r[c] for c in cols], tags=("odd" if i % 2 else "even",))
                tv.tag_configure("odd", background="#f8fafc")
                tv.tag_configure("even", background="white")
                tv.pack(padx=14, pady=(0, 14), fill="x")
            else:
                ctk.CTkLabel(bubble, text="Kayıt bulunamadı.", text_color=COLORS["muted"]).pack(padx=14, pady=(0, 14))
        elif rtype == "stats":
            title = result.get("title", "Sonuç")
            items = result.get("items", {})
            ctk.CTkLabel(bubble, text=title, font=ctk.CTkFont(size=13, weight="bold"), text_color=COLORS["text"]).pack(anchor="w", padx=14, pady=(12, 8))
            stat_row = ctk.CTkFrame(bubble, fg_color="transparent")
            stat_row.pack(padx=14, pady=(0, 14), fill="x")
            for k, v in items.items():
                card = ctk.CTkFrame(stat_row, fg_color="#eff6ff", corner_radius=10)
                card.pack(side="left", padx=(0, 10))
                ctk.CTkLabel(card, text=str(v), font=ctk.CTkFont(size=20, weight="bold"), text_color=COLORS["accent_dark"]).pack(padx=16, pady=(10, 0))
                ctk.CTkLabel(card, text=k, font=ctk.CTkFont(size=11), text_color=COLORS["muted"]).pack(padx=16, pady=(0, 10))
        else:
            ctk.CTkLabel(bubble, text=result.get("text", ""), wraplength=420, justify="left", text_color=COLORS["text"]).pack(padx=14, pady=10)
        self._scroll_to_bottom()

    @staticmethod
    def _avatar(parent, letter, color):
        av = ctk.CTkFrame(parent, width=32, height=32, corner_radius=16, fg_color=color)
        av.pack_propagate(False)
        ctk.CTkLabel(av, text=letter, text_color="white", font=ctk.CTkFont(size=11, weight="bold")).pack(expand=True)
        return av

    def _scroll_to_bottom(self):
        self.after(50, lambda: self.chat_scroll._parent_canvas.yview_moveto(1.0))


# firestore 
class FirestoreView(ctk.CTkFrame):
    TYPE_COLORS = {
        "string": "#15803d",
        "number": "#2563eb",
        "boolean": "#9333ea",
        "timestamp": "#c2410c",
        "map": "#64748b",
        "array": "#64748b",
        "null": "#94a3b8",
        "reference": "#0891b2",
        "geopoint": "#db2777",
        "bytes": "#a16207",
    }

    def __init__(self, parent, app):
        super().__init__(parent, fg_color=COLORS["bg"])
        self.app = app
        self._loaded_once = False
        self.parent_doc_path = None  
        self.selected_collection = None  
        self.selected_doc_id = None
        self._doc_map = {}           
        self._doc_order = []         
        self._current_data = None    
        self._edit_mode = False
        self._edit_entry = None    

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=28, pady=(24, 4))
        ctk.CTkLabel(header, text="Firestore", font=ctk.CTkFont(size=22, weight="bold"), text_color=COLORS["text"]).pack(side="left")
        self.status = ctk.CTkLabel(header, text="", font=ctk.CTkFont(size=11), text_color=COLORS["muted"])
        self.status.pack(side="left", padx=12)
        ctk.CTkButton(header, text="Yenile", width=90, fg_color=COLORS["accent"], command=self.reload).pack(side="right")

        # breadcrumb
        self.breadcrumb = ctk.CTkFrame(self, fg_color="transparent")
        self.breadcrumb.pack(fill="x", padx=28, pady=(0, 8))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=28, pady=(0, 24))
        body.grid_columnconfigure(0, weight=1, uniform="fs")
        body.grid_columnconfigure(1, weight=1, uniform="fs")
        body.grid_columnconfigure(2, weight=2)
        body.grid_rowconfigure(0, weight=1)

        # koleksiyonlar
        col1 = ctk.CTkFrame(body, fg_color=COLORS["card"], corner_radius=14, border_width=1, border_color=COLORS["border"])
        col1.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ctk.CTkLabel(col1, text="Koleksiyonlar", font=ctk.CTkFont(size=13, weight="bold"), text_color=COLORS["text"]).pack(anchor="w", padx=14, pady=(12, 6))
        self.col_search = ctk.CTkEntry(col1, placeholder_text="Koleksiyon ara...", height=30)
        self.col_search.pack(fill="x", padx=12, pady=(0, 6))
        self.col_search.bind("<KeyRelease>", lambda e: self._render_collections())
        self.col_list = ctk.CTkScrollableFrame(col1, fg_color="transparent")
        self.col_list.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        ctk.CTkButton(col1, text="+ Yeni Koleksiyon", height=30, fg_color=COLORS["sidebar"], hover_color=COLORS["sidebar_hover"], command=self._new_collection).pack(fill="x", padx=12, pady=(0, 12))

        # doküman ve arama kısmı
        col2 = ctk.CTkFrame(body, fg_color=COLORS["card"], corner_radius=14, border_width=1, border_color=COLORS["border"])
        col2.grid(row=0, column=1, sticky="nsew", padx=8)
        self.doc_header = ctk.CTkLabel(col2, text="Dokümanlar", font=ctk.CTkFont(size=13, weight="bold"), text_color=COLORS["text"])
        self.doc_header.pack(anchor="w", padx=14, pady=(12, 4))
        search_row = ctk.CTkFrame(col2, fg_color="transparent")
        search_row.pack(fill="x", padx=12, pady=(0, 2))
        self.doc_search = ctk.CTkEntry(search_row, placeholder_text="Ara: ID · değer · alan:değer", height=30)
        self.doc_search.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.doc_search.bind("<KeyRelease>", lambda e: self._render_documents())
        ctk.CTkButton(search_row, text="Filtre", width=64, height=30, fg_color=COLORS["accent"], command=self._open_filter_dialog).pack(side="right")
        ctk.CTkLabel(col2, text="Örn: aktif · lastMessage:sa · participants:RrQy", font=ctk.CTkFont(size=10), text_color=COLORS["muted"]).pack(anchor="w", padx=14, pady=(0, 4))
        self.doc_list = ctk.CTkScrollableFrame(col2, fg_color="transparent")
        self.doc_list.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.new_doc_btn = ctk.CTkButton(col2, text="+ Yeni Doküman", height=30, fg_color=COLORS["sidebar"], hover_color=COLORS["sidebar_hover"], command=self._new_document, state="disabled")
        self.new_doc_btn.pack(fill="x", padx=12, pady=(0, 12))

        # json düzenleme
        col3 = ctk.CTkFrame(body, fg_color=COLORS["card"], corner_radius=14, border_width=1, border_color=COLORS["border"])
        col3.grid(row=0, column=2, sticky="nsew", padx=(8, 0))
        self.editor_title = ctk.CTkLabel(col3, text="Doküman", font=ctk.CTkFont(size=13, weight="bold"), text_color=COLORS["text"])
        self.editor_title.pack(anchor="w", padx=14, pady=(12, 2))
        ctk.CTkLabel(col3, text="Değeri düzenlemek için üzerine çift tıklayın · sağ tık ile alan silin", font=ctk.CTkFont(size=10), text_color=COLORS["muted"]).pack(anchor="w", padx=14, pady=(0, 6))

        # ağaç
        self.viewer_frame = ctk.CTkFrame(col3, fg_color="transparent")
        self.viewer_frame.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        self.field_search = ctk.CTkEntry(self.viewer_frame, placeholder_text="Alan/değer ara ve vurgula...", height=30)
        self.field_search.pack(fill="x", pady=(0, 6))
        self.field_search.bind("<KeyRelease>", lambda e: self._field_search())
        tree_wrap = ctk.CTkFrame(self.viewer_frame, fg_color="transparent")
        tree_wrap.pack(fill="both", expand=True)
        self.field_tree = ttk.Treeview(tree_wrap, columns=("type", "value"), show="tree headings", style="Corp.Treeview")
        self.field_tree.heading("#0", text="Alan")
        self.field_tree.heading("type", text="Tür")
        self.field_tree.heading("value", text="Değer")
        self.field_tree.column("#0", width=200, stretch=False)
        self.field_tree.column("type", width=90, stretch=False, anchor="w")
        self.field_tree.column("value", width=260, anchor="w")
        for tp, col in self.TYPE_COLORS.items():
            self.field_tree.tag_configure(f"t_{tp}", foreground=col)
        self.field_tree.tag_configure("match", background="#fde68a")
        fv = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.field_tree.yview)
        self.field_tree.configure(yscrollcommand=fv.set)
        self.field_tree.pack(side="left", fill="both", expand=True)
        fv.pack(side="right", fill="y")

        
        self._ctx_menu = tkinter.Menu(self, tearoff=0)
        self._ctx_menu.add_command(label="Değeri Düzenle", command=self._edit_selected_cell)
        self._ctx_menu.add_command(label="Alanı Sil", command=self._delete_selected_field)
        self.field_tree.bind("<Button-3>", self._on_field_right_click)
        self.field_tree.bind("<Button-2>", self._on_field_right_click)  # macOS
        self.field_tree.bind("<Double-1>", self._on_field_double_click)
        self._node_path = {}
        self._show_placeholder()

        # düzenleme mod
        self.edit_frame = ctk.CTkFrame(col3, fg_color="transparent")
        self.editor = ctk.CTkTextbox(self.edit_frame, wrap="none", font=ctk.CTkFont(family="Menlo", size=12))
        self.editor.pack(fill="both", expand=True)

        # butonlar
        self.view_btns = ctk.CTkFrame(col3, fg_color="transparent")
        self.view_btns.pack(fill="x", padx=14, pady=(0, 12))
        self.addfield_btn = ctk.CTkButton(self.view_btns, text="+ Alan Ekle", width=90, fg_color=COLORS["green"], hover_color="#15803d", command=self._add_field, state="disabled")
        self.addfield_btn.pack(side="left")
        self.subcol_btn = ctk.CTkButton(self.view_btns, text="Alt Koleksiyonlar", width=130, fg_color=COLORS["sidebar"], hover_color=COLORS["sidebar_hover"], command=self._enter_subcollections, state="disabled")
        self.subcol_btn.pack(side="left", padx=8)
        self.edit_btn = ctk.CTkButton(self.view_btns, text="JSON Düzenle", width=110, fg_color=COLORS["accent"], command=self._enter_edit_mode, state="disabled")
        self.edit_btn.pack(side="left")
        self.delete_btn = ctk.CTkButton(self.view_btns, text="Dokümanı Sil", width=110, fg_color=COLORS["red"], hover_color="#b91c1c", command=self._delete_document, state="disabled")
        self.delete_btn.pack(side="right")
        self.delfield_btn = ctk.CTkButton(self.view_btns, text="Alanı Sil", width=80, fg_color="#f97316", hover_color="#ea580c", command=self._delete_selected_field, state="disabled")
        self.delfield_btn.pack(side="right", padx=8)

        self.edit_btns = ctk.CTkFrame(col3, fg_color="transparent")
        self.save_btn = ctk.CTkButton(self.edit_btns, text="Kaydet", width=90, fg_color=COLORS["green"], hover_color="#15803d", command=self._save_document)
        self.save_btn.pack(side="left")
        self.cancel_btn = ctk.CTkButton(self.edit_btns, text="İptal", width=80, fg_color=COLORS["muted"], hover_color="#475569", command=self._cancel_edit)
        self.cancel_btn.pack(side="left", padx=8)


    def ensure_loaded(self):
        if not self._loaded_once:
            self._loaded_once = True
            self.reload()

    def reload(self):
        self._render_breadcrumb()
        self._set_status("Koleksiyonlar yükleniyor...")
        threading.Thread(target=self._load_collections, daemon=True).start()

    def _load_collections(self):
        try:
            cols = fsvc.list_collections(self.parent_doc_path)
            self._all_collections = cols
            self.after(0, self._render_collections)
        except Exception as exc:  # noqa: BLE001
            self.after(0, self._set_status, f"Hata: {exc}", True)

    def _render_collections(self):
        cols = getattr(self, "_all_collections", [])
        q = self.col_search.get().lower().strip()
        shown = [c for c in cols if q in c.lower()] if q else cols
        for w in self.col_list.winfo_children():
            w.destroy()
        if not shown:
            ctk.CTkLabel(self.col_list, text="Koleksiyon yok.", text_color=COLORS["muted"]).pack(anchor="w", padx=6, pady=6)
        for cid in shown:
            full = f"{self.parent_doc_path}/{cid}" if self.parent_doc_path else cid
            active = full == self.selected_collection
            ctk.CTkButton(
                self.col_list, text=cid, anchor="w", height=30,
                fg_color="#dbeafe" if active else "transparent",
                text_color=COLORS["accent_dark"] if active else COLORS["text"], hover_color="#e2e8f0",
                command=lambda p=full: self._select_collection(p),
            ).pack(fill="x", pady=2)
        self._set_status(f"{len(cols)} koleksiyon")

    def _select_collection(self, collection_path):
        self.selected_collection = collection_path
        self.selected_doc_id = None
        self.doc_header.configure(text=f"Dokümanlar · {collection_path.split('/')[-1]}")
        self.new_doc_btn.configure(state="normal")
        self._clear_editor()
        self._render_collections()
        self._set_status("Dokümanlar yükleniyor...")
        threading.Thread(target=self._load_documents, args=(collection_path,), daemon=True).start()

    def _load_documents(self, collection_path):
        try:
            docs = fsvc.list_documents_with_data(collection_path)
            self.after(0, self._store_documents, collection_path, docs)
        except Exception as exc:  # noqa: BLE001
            self.after(0, self._set_status, f"Hata: {exc}", True)

    def _store_documents(self, collection_path, docs):
        if collection_path != self.selected_collection:
            return
        self._doc_map = {did: data for did, data in docs}
        self._doc_order = [did for did, _ in docs]
        self._render_documents()
        self._set_status(f"{len(docs)} doküman")

    def _doc_matches(self, doc_id, data, query):
      
        if not query:
            return True
        if ":" in query:
            field, _, val = query.partition(":")
            field, val = field.strip(), val.strip().lower()
            if field:
                fval = None
                if isinstance(data, dict):
                    if field in data:
                        fval = data[field]
                    else:  
                        for k in data:
                            if k.lower() == field.lower():
                                fval = data[k]
                                break
                blob = json.dumps(fs_to_jsonable(fval), ensure_ascii=False, default=str).lower()
                return val in blob if val else True
        ql = query.lower()
        if ql in doc_id.lower():
            return True
        blob = json.dumps(fs_to_jsonable(data), ensure_ascii=False, default=str).lower()
        return ql in blob

    def _render_documents(self):
        for w in self.doc_list.winfo_children():
            w.destroy()
        q = self.doc_search.get().strip()
        shown = [did for did in self._doc_order if self._doc_matches(did, self._doc_map.get(did, {}), q)]
        if not self._doc_order:
            ctk.CTkLabel(self.doc_list, text="Doküman yok.", text_color=COLORS["muted"]).pack(anchor="w", padx=6, pady=6)
        elif not shown:
            ctk.CTkLabel(self.doc_list, text="Eşleşen doküman yok.", text_color=COLORS["muted"]).pack(anchor="w", padx=6, pady=6)
        for did in shown:
            active = did == self.selected_doc_id
            ctk.CTkButton(
                self.doc_list, text=did, anchor="w", height=30,
                fg_color="#dbeafe" if active else "transparent",
                text_color=COLORS["accent_dark"] if active else COLORS["text"], hover_color="#e2e8f0",
                command=lambda d=did: self._select_document(d),
            ).pack(fill="x", pady=2)
        if q and self._doc_order:
            self._set_status(f"{len(shown)}/{len(self._doc_order)} doküman (filtre)")

    def _select_document(self, doc_id):
        self.selected_doc_id = doc_id
        if self._edit_mode:
            self._exit_edit_mode()
        data = self._doc_map.get(doc_id)
        if data is None:
            self._set_status(f"'{doc_id}' yükleniyor...")
            threading.Thread(target=self._fetch_and_show, args=(self.selected_collection, doc_id), daemon=True).start()
        else:
            self._show_document(self.selected_collection, doc_id, data)
        self._render_documents()

    def _fetch_and_show(self, collection_path, doc_id):
        try:
            data = fsvc.get_document(collection_path, doc_id) or {}
            self._doc_map[doc_id] = data
            self.after(0, self._show_document, collection_path, doc_id, data)
        except Exception as exc: 
            self.after(0, self._set_status, f"Hata: {exc}", True)

    #alan ağacı tasarımı
    def _describe(self, v):
        if isinstance(v, bool):
            return ("boolean", "true" if v else "false")
        if isinstance(v, datetime):
            return ("timestamp", v.astimezone(LOCAL_TZ).strftime("%d %b %Y %H:%M:%S"))
        if isinstance(v, bytes):
            return ("bytes", f"<{len(v)} bayt>")
        if isinstance(v, int):
            return ("number", str(v))
        if isinstance(v, float):
            return ("number", repr(v))
        if v is None:
            return ("null", "null")
        if isinstance(v, str):
            return ("string", f'"{v}"')
        tn = type(v).__name__
        if tn == "GeoPoint":
            return ("geopoint", f"[{v.latitude}, {v.longitude}]")
        if tn == "DocumentReference":
            return ("reference", v.path)
        if isinstance(v, dict):
            return ("map", f"{{{len(v)} alan}}")
        if isinstance(v, (list, tuple)):
            return ("array", f"[{len(v)} öğe]")
        return (tn, str(v))

    def _populate_tree(self, parent, data, path):
        items = data.items() if isinstance(data, dict) else list(enumerate(data))
        for key, val in items:
            label = str(key) if isinstance(data, dict) else f"[{key}]"
            tp, disp = self._describe(val)
          
            disp = str(disp).replace("\n", " ").replace("\r", " ").replace("\t", " ")
            if len(disp) > 400:
                disp = disp[:400] + "…"
            try:
                node = self.field_tree.insert(parent, "end", text=str(label), values=(tp, disp), tags=(f"t_{tp}",), open=True)
            except Exception: 
                node = self.field_tree.insert(parent, "end", text=str(label), values=(tp, "<görüntülenemedi>"), tags=(f"t_{tp}",), open=True)
            self._node_path[node] = path + [key]
            if tp in ("map", "array"):
                self._populate_tree(node, val, path + [key])

    def _open_all(self, parent=""):
        for n in self.field_tree.get_children(parent):
            self.field_tree.item(n, open=True)
            self._open_all(n)

    def _show_document(self, collection_path, doc_id, data):
        if collection_path != self.selected_collection or doc_id != self.selected_doc_id:
            return
        self._cancel_cell_edit()
        self._current_data = data
        self.editor_title.configure(text=f"{collection_path}/{doc_id}")
        for row in self.field_tree.get_children():
            self.field_tree.delete(row)
        self._node_path = {}
        try:
            self._populate_tree("", data, [])
        except Exception as exc: 
            self._set_status(f"Bazı alanlar çizilemedi: {exc}", True)
        self._open_all() 
        top_count = len(data) if isinstance(data, dict) else 0
        self.edit_btn.configure(state="normal")
        self.addfield_btn.configure(state="normal")
        self.delete_btn.configure(state="normal")
        self.delfield_btn.configure(state="normal")
        self.subcol_btn.configure(state="normal")
        if self.field_search.get().strip():
            self._field_search()
        self._set_status(f"Doküman yüklendi · {top_count} üst alan")

    def _field_search(self):
        q = self.field_search.get().lower().strip()
        first = [None]
        count = [0]

        def walk(parent=""):
            for n in self.field_tree.get_children(parent):
                tags = [t for t in self.field_tree.item(n, "tags") if t != "match"]
                text = str(self.field_tree.item(n, "text")).lower()
                val = str(self.field_tree.set(n, "value")).lower()
                if q and (q in text or q in val):
                    tags.append("match")
                    count[0] += 1
                    if first[0] is None:
                        first[0] = n
                self.field_tree.item(n, tags=tags)
                walk(n)

        walk()
        if first[0]:
            self.field_tree.see(first[0])
            self.field_tree.selection_set(first[0])
            self._set_status(f"{count[0]} eşleşen alan")
        elif q:
            self.field_tree.selection_remove(self.field_tree.selection())
            self._set_status("Eşleşen alan yok")

    # silme
    def _on_field_right_click(self, event):
        iid = self.field_tree.identify_row(event.y)
        if iid:
            self.field_tree.selection_set(iid)
            try:
                self._ctx_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self._ctx_menu.grab_release()

    def _delete_selected_field(self):
        sel = self.field_tree.selection()
        if not sel or self._current_data is None:
            self._set_status("Silmek için bir alan seçin.", True)
            return
        path = self._node_path.get(sel[0])
        if not path:
            return
        label = " → ".join(str(p) for p in path)
        if not messagebox.askyesno("Alanı Sil", f"'{label}' alanı bu dokümandan silinsin mi?"):
            return
        container = self._current_data
        try:
            for key in path[:-1]:
                container = container[key]
            last = path[-1]
            if isinstance(container, list):
                container.pop(last)
            else:
                del container[last]
        except Exception as exc:
            messagebox.showerror("Hata", f"Alan silinemedi: {exc}")
            return
        col, did, data = self.selected_collection, self.selected_doc_id, self._current_data
        self._set_status("Alan siliniyor...")

        def work():
            try:
                fsvc.set_document(col, did, data)
                self.after(0, self._after_save, col, did, data)
            except Exception as exc:
                self.after(0, self._set_status, f"Hata: {exc}", True)

        threading.Thread(target=work, daemon=True).start()

    # tablo içi düzenleme
    def _value_at(self, path):
        cur = self._current_data
        for k in path:
            cur = cur[k]
        return cur

    def _set_value_at(self, path, newval):
        cur = self._current_data
        for k in path[:-1]:
            cur = cur[k]
        cur[path[-1]] = newval

    def _on_field_double_click(self, event):
        region = self.field_tree.identify("region", event.x, event.y)
        col = self.field_tree.identify_column(event.x)
        iid = self.field_tree.identify_row(event.y)
    
        if region != "cell" or col != "#2" or not iid:
            return None
        if self._begin_cell_edit(iid):
            return "break"  
        return None

    def _edit_selected_cell(self):
        sel = self.field_tree.selection()
        if sel:
            self._begin_cell_edit(sel[0])

    def _begin_cell_edit(self, iid):
        path = self._node_path.get(iid)
        if path is None or self._current_data is None:
            return False
        try:
            val = self._value_at(path)
        except Exception:  
            return False
        if isinstance(val, (dict, list)):
            self._set_status("Map/array alanları JSON modunda düzenlenir.", True)
            return False
        tn = type(val).__name__
        if tn in ("GeoPoint", "DocumentReference") or isinstance(val, bytes):
            self._set_status("Bu tür alanı JSON modunda düzenleyin.", True)
            return False
        bbox = self.field_tree.bbox(iid, "#2")
        if not bbox:
            return False
        x, y, w, h = bbox
        self._cancel_cell_edit()
        if isinstance(val, bool):
            init = "true" if val else "false"
        elif isinstance(val, datetime):
            init = val.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
        elif val is None:
            init = ""
        else:
            init = str(val)
        entry = tkinter.Entry(self.field_tree, font=("Menlo", 12))
        entry.insert(0, init)
        entry.select_range(0, "end")
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus_set()
        entry.bind("<Return>", lambda e: self._commit_cell_edit(path, val))
        entry.bind("<Escape>", lambda e: self._cancel_cell_edit())
        entry.bind("<FocusOut>", lambda e: self._cancel_cell_edit())
        self._edit_entry = entry
        return True

    def _cancel_cell_edit(self):
        if self._edit_entry is not None:
            try:
                self._edit_entry.destroy()
            except Exception:  
                pass
            self._edit_entry = None

    def _parse_like(self, raw, orig):
        raw = raw.strip()
        if isinstance(orig, bool):
            low = raw.lower()
            if low in ("true", "1", "evet", "yes"):
                return True
            if low in ("false", "0", "hayır", "hayir", "no"):
                return False
            raise ValueError("true/false bekleniyor")
        if isinstance(orig, int) and not isinstance(orig, bool):
            return int(raw)
        if isinstance(orig, float):
            return float(raw)
        if isinstance(orig, datetime):
            try:
                dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=LOCAL_TZ)
            return dt
        if orig is None:
            return None if raw == "" else raw
        return raw 

    def _commit_cell_edit(self, path, orig):
        if self._edit_entry is None:
            return
        raw = self._edit_entry.get()
        self._cancel_cell_edit()
        try:
            newval = self._parse_like(raw, orig)
        except Exception as exc: 
            messagebox.showerror("Geçersiz Değer", f"Değer bu türe çevrilemedi: {exc}")
            return
        try:
            self._set_value_at(path, newval)
        except Exception as exc:
            messagebox.showerror("Hata", str(exc))
            return
        col, did, data = self.selected_collection, self.selected_doc_id, self._current_data
        self._set_status("Kaydediliyor...")

        def work():
            try:
                fsvc.set_document(col, did, data)
                self.after(0, self._after_save, col, did, data)
            except Exception as exc:  
                self.after(0, self._set_status, f"Hata: {exc}", True)

        threading.Thread(target=work, daemon=True).start()

    # tablodan ekleme
    @staticmethod
    def _autodetect(raw):
        if raw is None:
            return None
        s = raw.strip()
        if s == "":
            return None
        low = s.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        if low == "null":
            return None
        try:
            if s.isdigit() or (s[0] == "-" and s[1:].isdigit()):
                return int(s)
            return float(s)
        except (ValueError, IndexError):
            pass
        return s

    def _add_field(self):
        if not isinstance(self._current_data, dict):
            self._set_status("Alan yalnızca doküman köküne eklenebilir.", True)
            return
        name = ctk.CTkInputDialog(text="Yeni alan adı:", title="Alan Ekle").get_input()
        if not name or not name.strip():
            return
        name = name.strip()
        raw = ctk.CTkInputDialog(text=f"'{name}' değeri (sayı/true/false/null otomatik algılanır):", title="Alan Değeri").get_input()
        newval = self._autodetect(raw)
        self._current_data[name] = newval
        col, did, data = self.selected_collection, self.selected_doc_id, self._current_data
        self._set_status("Ekleniyor...")

        def work():
            try:
                fsvc.set_document(col, did, data)
                self.after(0, self._after_save, col, did, data)
            except Exception as exc:  # noqa: BLE001
                self.after(0, self._set_status, f"Hata: {exc}", True)

        threading.Thread(target=work, daemon=True).start()

    # hazır kategoriler
    def _open_filter_dialog(self):
        if not self._doc_order:
            self._set_status("Önce bir koleksiyon seçin.", True)
            return
        fields, seen = [], set()
        for did in self._doc_order:
            d = self._doc_map.get(did) or {}
            if isinstance(d, dict):
                for k in d.keys():
                    if k not in seen:
                        seen.add(k)
                        fields.append(k)
        fields.sort()
        if not fields:
            self._set_status("Filtrelenecek alan bulunamadı.", True)
            return

        win = ctk.CTkToplevel(self)
        win.title("Filtrele")
        win.geometry("380x300")
        win.transient(self.winfo_toplevel())
        ctk.CTkLabel(win, text="Alana göre filtrele", font=ctk.CTkFont(size=15, weight="bold")).pack(pady=(16, 8), padx=16, anchor="w")

        ctk.CTkLabel(win, text="Alan:", font=ctk.CTkFont(size=12)).pack(padx=16, anchor="w")
        field_var = ctk.StringVar(value=fields[0])
        field_menu = ctk.CTkOptionMenu(win, variable=field_var, values=fields)
        field_menu.pack(fill="x", padx=16, pady=(0, 8))

        ctk.CTkLabel(win, text="Hazır değerler:", font=ctk.CTkFont(size=12)).pack(padx=16, anchor="w")
        value_var = ctk.StringVar(value="")
        value_menu = ctk.CTkOptionMenu(win, variable=value_var, values=["(tümü)"])
        value_menu.pack(fill="x", padx=16, pady=(0, 8))

        ctk.CTkLabel(win, text="veya değer yaz:", font=ctk.CTkFont(size=12)).pack(padx=16, anchor="w")
        value_entry = ctk.CTkEntry(win, placeholder_text="serbest metin")
        value_entry.pack(fill="x", padx=16, pady=(0, 12))

        def refresh_values(*_):
            field = field_var.get()
            vals, s = [], set()
            for did in self._doc_order:
                d = self._doc_map.get(did) or {}
                v = d.get(field)
                sv = json.dumps(fs_to_jsonable(v), ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else str(v)
                if sv not in s:
                    s.add(sv)
                    vals.append(sv)
            vals = ["(tümü)"] + vals[:50]
            value_menu.configure(values=vals)
            value_var.set("(tümü)")

        field_menu.configure(command=refresh_values)
        refresh_values()

        def apply():
            field = field_var.get()
            typed = value_entry.get().strip()
            chosen = value_var.get()
            val = typed or (chosen if chosen != "(tümü)" else "")
            query = f"{field}:{val}" if val else field
            self.doc_search.delete(0, "end")
            self.doc_search.insert(0, query)
            self._render_documents()
            win.destroy()

        def clear():
            self.doc_search.delete(0, "end")
            self._render_documents()
            win.destroy()

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(fill="x", padx=16, pady=(4, 12))
        ctk.CTkButton(btns, text="Uygula", fg_color=COLORS["accent"], command=apply).pack(side="left")
        ctk.CTkButton(btns, text="Temizle", fg_color=COLORS["muted"], hover_color="#475569", command=clear).pack(side="left", padx=8)
        win.after(120, win.lift)
        win.after(150, win.grab_set)

    # düzenleme modu
    def _enter_edit_mode(self):
        if self._current_data is None:
            return
        self._cancel_cell_edit()
        self._edit_mode = True
        self.viewer_frame.pack_forget()
        self.view_btns.pack_forget()
        self.edit_frame.pack(fill="both", expand=True, padx=14, pady=(0, 8), before=None)
        self.edit_btns.pack(fill="x", padx=14, pady=(0, 12))
        self.editor.configure(state="normal")
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", json.dumps(fs_to_jsonable(self._current_data), ensure_ascii=False, indent=2))
        self._set_status("Düzenleme modu — JSON")

    def _exit_edit_mode(self):
        self._edit_mode = False
        self.edit_frame.pack_forget()
        self.edit_btns.pack_forget()
        self.viewer_frame.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        self.view_btns.pack(fill="x", padx=14, pady=(0, 12))

    def _cancel_edit(self):
        self._exit_edit_mode()
        if self._current_data is not None:
            self._show_document(self.selected_collection, self.selected_doc_id, self._current_data)

    def _parse_editor(self):
        raw = self.editor.get("1.0", "end").strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            messagebox.showerror("JSON Hatası", f"Geçersiz JSON: {exc}")
            return None
        if not isinstance(parsed, dict):
            messagebox.showerror("JSON Hatası", "Doküman içeriği bir JSON nesnesi (obje) olmalı.")
            return None
        return fs_from_jsonable(parsed)

    def _save_document(self):
        if not (self.selected_collection and self.selected_doc_id):
            return
        data = self._parse_editor()
        if data is None:
            return
        col, did = self.selected_collection, self.selected_doc_id
        self._set_status("Kaydediliyor...")

        def work():
            try:
                fsvc.set_document(col, did, data)
                self.after(0, self._after_save, col, did, data)
            except Exception as exc:  # noqa: BLE001
                self.after(0, self._set_status, f"Hata: {exc}", True)

        threading.Thread(target=work, daemon=True).start()

    def _after_save(self, col, did, data):
        self._doc_map[did] = data
        self._exit_edit_mode()
        self._show_document(col, did, data)
        self._set_status("Kaydedildi")

    def _delete_document(self):
        if not (self.selected_collection and self.selected_doc_id):
            return
        col, did = self.selected_collection, self.selected_doc_id
        if not messagebox.askyesno("Doküman Sil", f"'{col}/{did}' dokümanı kalıcı olarak silinsin mi?\nBu işlem geri alınamaz."):
            return
        self._set_status("Siliniyor...")

        def work():
            try:
                fsvc.delete_document(col, did)
                self.after(0, self._after_delete, col)
            except Exception as exc:  
                self.after(0, self._set_status, f"Hata: {exc}", True)

        threading.Thread(target=work, daemon=True).start()

    def _after_delete(self, collection_path):
        self.selected_doc_id = None
        self._clear_editor()
        self._set_status("Silindi")
        threading.Thread(target=self._load_documents, args=(collection_path,), daemon=True).start()

    def _new_document(self):
        if not self.selected_collection:
            return
        dialog = ctk.CTkInputDialog(text="Yeni doküman ID (boş bırakılırsa otomatik atanır):", title="Yeni Doküman")
        doc_id = dialog.get_input()
        if doc_id is None:
            return
        doc_id = doc_id.strip() or None
        col = self.selected_collection
        self._set_status("Oluşturuluyor...")

        def work():
            try:
                new_id = fsvc.add_document(col, {}, doc_id)
                self.after(0, self._after_new_document, col, new_id)
            except Exception as exc: 
                self.after(0, self._set_status, f"Hata: {exc}", True)

        threading.Thread(target=work, daemon=True).start()

    def _after_new_document(self, collection_path, new_id):
        self._set_status(f"'{new_id}' oluşturuldu")
        threading.Thread(target=self._reload_docs_then_select, args=(collection_path, new_id), daemon=True).start()

    def _reload_docs_then_select(self, collection_path, doc_id):
        try:
            docs = fsvc.list_documents_with_data(collection_path)
            self.after(0, self._store_documents, collection_path, docs)
            self.after(0, self._select_document, doc_id)
        except Exception as exc:  # noqa: BLE001
            self.after(0, self._set_status, f"Hata: {exc}", True)

    def _new_collection(self):
        # ilk doküman ekleme
        dialog = ctk.CTkInputDialog(text="Yeni koleksiyon adı:", title="Yeni Koleksiyon")
        name = dialog.get_input()
        if not name or not name.strip():
            return
        name = name.strip()
        base = f"{self.parent_doc_path}/{name}" if self.parent_doc_path else name
        self._set_status("Koleksiyon oluşturuluyor...")

        def work():
            try:
                fsvc.add_document(base, {"_created": datetime.now(timezone.utc).isoformat()})
                self.after(0, self.reload)
                self.after(0, self._set_status, f"'{name}' oluşturuldu")
            except Exception as exc: 
                self.after(0, self._set_status, f"Hata: {exc}", True)

        threading.Thread(target=work, daemon=True).start()

    def _enter_subcollections(self):
        if not (self.selected_collection and self.selected_doc_id):
            return
        self.parent_doc_path = f"{self.selected_collection}/{self.selected_doc_id}"
        self._reset_doc_column()
        self.reload()

    # yardımcı fonk.
    def _render_breadcrumb(self):
        for w in self.breadcrumb.winfo_children():
            w.destroy()
        ctk.CTkButton(
            self.breadcrumb, text="Kök", height=24, width=50,
            fg_color=COLORS["border"] if self.parent_doc_path else COLORS["accent"],
            text_color=COLORS["text"] if self.parent_doc_path else "white",
            hover_color="#cbd5e1", font=ctk.CTkFont(size=11),
            command=self._go_root,
        ).pack(side="left")
        if self.parent_doc_path:
            segments = self.parent_doc_path.split("/")
            acc = []
            for i, seg in enumerate(segments):
                acc.append(seg)
                ctk.CTkLabel(self.breadcrumb, text="  /  ", font=ctk.CTkFont(size=11), text_color=COLORS["muted"]).pack(side="left")
                is_last = i == len(segments) - 1
                path_here = "/".join(acc)
                ctk.CTkButton(
                    self.breadcrumb, text=seg, height=24,
                    fg_color=COLORS["accent"] if is_last else COLORS["border"],
                    text_color="white" if is_last else COLORS["text"],
                    hover_color="#cbd5e1", font=ctk.CTkFont(size=11),
                    command=(lambda p=path_here: self._go_to(p)) if (i % 2 == 1) else (lambda: None),
                ).pack(side="left")

    def _reset_doc_column(self):
        self.selected_collection = None
        self.selected_doc_id = None
        self._doc_map = {}
        self._doc_order = []
        for w in self.doc_list.winfo_children():
            w.destroy()
        self.doc_header.configure(text="Dokümanlar")
        self.new_doc_btn.configure(state="disabled")
        self._clear_editor()

    def _go_root(self):
        self.parent_doc_path = None
        self._reset_doc_column()
        self.reload()

    def _go_to(self, doc_path):
        self.parent_doc_path = doc_path
        self._reset_doc_column()
        self.reload()

    def _show_placeholder(self):
        for row in self.field_tree.get_children():
            self.field_tree.delete(row)
        self.field_tree.insert("", "end", text="Soldan bir doküman seçin", values=("", ""))

    def _clear_editor(self):
        if self._edit_mode:
            self._exit_edit_mode()
        self._current_data = None
        self._node_path = {}
        self.editor_title.configure(text="Doküman")
        self._show_placeholder()
        self.edit_btn.configure(state="disabled")
        self.addfield_btn.configure(state="disabled")
        self.delete_btn.configure(state="disabled")
        self.delfield_btn.configure(state="disabled")
        self.subcol_btn.configure(state="disabled")

    def _set_status(self, text, is_error=False):
        self.status.configure(text=text, text_color=COLORS["red"] if is_error else COLORS["muted"])


# pencere
class App(ctk.CTk):
    NAV_ITEMS = [("dashboard", "Genel Bakış"), ("users", "Kullanıcılar"), ("firestore", "Firestore"), ("chat", "Asistan")]

    def __init__(self):
        super().__init__()
        self.title("Firebase Kurumsal Yönetim Konsolu")
        self.geometry("1280x800")
        self.minsize(1080, 680)
        self.configure(fg_color=COLORS["bg"])
        configure_ttk_style()

        self.chat_history = []  

        #sidebar
        self.sidebar = ctk.CTkFrame(self, width=220, corner_radius=0, fg_color=COLORS["sidebar"])
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        ctk.CTkLabel(self.sidebar, text="Firebase Panel", font=ctk.CTkFont(size=18, weight="bold"), text_color="white").pack(pady=(28, 24), padx=20, anchor="w")

        self.nav_buttons = {}
        for key, label in self.NAV_ITEMS:
            btn = ctk.CTkButton(
                self.sidebar,
                text=label,
                anchor="w",
                fg_color="transparent",
                hover_color=COLORS["sidebar_hover"],
                text_color="#cbd5e1",
                font=ctk.CTkFont(size=13),
                height=40,
                corner_radius=8,
                command=lambda k=key: self.show_view(k),
            )
            btn.pack(fill="x", padx=12, pady=3)
            self.nav_buttons[key] = btn

        self.status_label = ctk.CTkLabel(self.sidebar, text="Bağlanıyor...", font=ctk.CTkFont(size=11), text_color="#94a3b8")
        self.status_label.pack(side="bottom", pady=16, padx=20, anchor="w")

    
        self.content = ctk.CTkFrame(self, fg_color=COLORS["bg"], corner_radius=0)
        self.content.pack(side="right", fill="both", expand=True)
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

        self.views = {
            "dashboard": DashboardView(self.content, self),
            "users": UsersView(self.content, self),
            "firestore": FirestoreView(self.content, self),
            "chat": ChatView(self.content, self),
        }
        for v in self.views.values():
            v.grid(row=0, column=0, sticky="nsew")

        self.show_view("dashboard")
        self.after(150, self._check_connections)

    def show_view(self, key):
        for k, btn in self.nav_buttons.items():
            active = k == key
            btn.configure(fg_color=COLORS["accent_dark"] if active else "transparent", text_color="white" if active else "#cbd5e1")
        self.views[key].tkraise()
        if key == "dashboard":
            self.views["dashboard"].refresh(force=False)
        elif key == "users":
            self.views["users"].refresh(force=False)
        elif key == "firestore":
            self.views["firestore"].ensure_loaded()

    def _check_connections(self):
        ok_fb = FIREBASE_READY
        ok_gemini = client is not None
        if ok_fb and ok_gemini:
            self.status_label.configure(text="Firebase & Gemini Bağlı", text_color="#4ade80")
        else:
            missing = []
            if not ok_fb:
                missing.append("Firebase")
            if not ok_gemini:
                missing.append("Gemini")
            self.status_label.configure(text=f"Eksik: {', '.join(missing)}", text_color="#f87171")
            if not ok_fb and FIREBASE_INIT_ERROR:
                self.after(400, lambda: messagebox.showwarning("Firebase Bağlantı Hatası", FIREBASE_INIT_ERROR))


if __name__ == "__main__":
    app = App()
    app.mainloop()
