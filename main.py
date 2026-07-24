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

import csv
import os
import sys
import threading
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
from firebase_admin import auth, credentials
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
    except Exception:  # noqa: BLE001
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
    COLUMNS = ("email", "created", "last_signin", "provider", "verified", "status")
    HEADERS = {
        "email": "E-posta",
        "created": "Kayıt Tarihi (TR)",
        "last_signin": "Son Giriş (TR)",
        "provider": "Yöntem",
        "verified": "Doğrulandı",
        "status": "Durum",
    }
    WIDTHS = {"email": 220, "created": 160, "last_signin": 160, "provider": 130, "verified": 100, "status": 120}

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
        search_row.pack(fill="x", padx=28, pady=(0, 8))
        self.search_entry = ctk.CTkEntry(search_row, placeholder_text="E-posta ile ara...")
        self.search_entry.pack(fill="x")
        self.search_entry.bind("<KeyRelease>", lambda e: self._filter())

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
            # Varsayılan sıralama: en yeni kayıt en üstte (eskiden Firebase'in
            # döndürdüğü rastgele sırayla listeleniyordu).
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
        self._render(self._all_rows if not q else [r for r in self._all_rows if q in r["email"].lower()])

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
                        except Exception as exc:  # noqa: BLE001
                            result = {"type": "text", "text": f"'{fc.name}' çalıştırılırken hata oluştu: {exc}"}
                    self.after(0, self.add_result_bubble, result)
                    summaries.append(self._summarize(result))
                history.append(types.Content(role="model", parts=[types.Part(text=" ".join(summaries))]))
            else:
                reply = response.text or "Üzgünüm, bir yanıt oluşturamadım."
                self.after(0, self.add_bubble, "bot", reply)
                history.append(types.Content(role="model", parts=[types.Part(text=reply)]))
            del history[:-20]
        except Exception as exc:  # noqa: BLE001
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

    # ---- Balon çizimi ----
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


# pencere
class App(ctk.CTk):
    NAV_ITEMS = [("dashboard", "Genel Bakış"), ("users", "Kullanıcılar"), ("chat", "Asistan")]

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
