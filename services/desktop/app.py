import base64
import ctypes
import io
import os
from pathlib import Path
import sys
import threading
import time
import uuid
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import httpx
from PIL import Image, ImageOps, ImageTk

API_BASE_URL = os.getenv("API_URL", "http://localhost:8000").rstrip("/")
FEED_PAGE_SIZE = 4
PREFETCH_THRESHOLD = 2
CARD_IMAGE_SIZE = (540, 390)
PREVIEW_IMAGE_SIZE = (260, 170)
PROFILE_THUMB_SIZE = (280, 195)
SCROLL_COOLDOWN_SEC = 0.40
SCROLL_RESET_SEC = 0.18
BG_MAIN = "#0f172a"
BG_CARD = "#1e293b"
BG_IMAGE = (2, 6, 23)
FG_MAIN = "#f8fafc"
FG_MUTED = "#94a3b8"
FG_LINK = "#38bdf8"

DEFAULT_CATEGORIES = [
    "природа",
    "горы",
    "море",
    "город",
    "архитектура",
    "ночь",
    "закат",
    "осень",
    "снег",
    "космос",
    "кофе",
    "минимализм",
    "спорт",
    "киберпанк",
    "животные",
    "арт",
]

EXT_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def fit_image(pil_img: Image.Image, target_size: tuple[int, int]) -> Image.Image:
    contained = ImageOps.contain(pil_img.convert("RGB"), target_size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", target_size, BG_IMAGE)
    offset_x = (target_size[0] - contained.width) // 2
    offset_y = (target_size[1] - contained.height) // 2
    canvas.paste(contained, (offset_x, offset_y))
    return canvas


class ApiClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.token: str | None = None
        self.user_id: str | None = None
        self._http = httpx.Client(timeout=15.0)

    def _headers(self) -> dict[str, str]:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    def _extract_error(self, resp: httpx.Response) -> str:
        try:
            data = resp.json()
            detail = data.get("detail")
            if isinstance(detail, list):
                return ", ".join(item.get("msg", str(item)) for item in detail)
            if detail:
                return str(detail)
        except Exception:
            pass
        return f"Ошибка сервера ({resp.status_code})"

    def authenticate(self, mode: str, username: str, password: str) -> dict:
        endpoint = "/api/v1/auth/register" if mode == "register" else "/api/v1/auth/login"
        resp = self._http.post(
            f"{self.base_url}{endpoint}",
            json={"username": username, "password": password},
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(self._extract_error(resp))
        data = resp.json()
        self.token = data["access_token"]
        self.user_id = data["userId"]
        return data

    def get_categories(self) -> list[str]:
        try:
            resp = self._http.get(f"{self.base_url}/api/v1/categories")
            if resp.status_code == 200:
                cats = resp.json().get("categories")
                if isinstance(cats, list) and cats:
                    return cats
        except Exception:
            pass
        return list(DEFAULT_CATEGORIES)

    def get_feed(self, cursor: str, limit: int = FEED_PAGE_SIZE) -> dict:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"{self.base_url}/api/v1/feed",
                params={"cursor": cursor, "limit": limit},
                headers=self._headers(),
            )
        if resp.status_code == 401:
            raise PermissionError(self._extract_error(resp))
        if resp.status_code != 200:
            raise RuntimeError(self._extract_error(resp))
        return resp.json()

    def get_my_profile(self) -> dict:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"{self.base_url}/api/v1/users/me",
                headers=self._headers(),
            )
        if resp.status_code == 401:
            raise PermissionError(self._extract_error(resp))
        if resp.status_code != 200:
            raise RuntimeError(self._extract_error(resp))
        return resp.json()

    def get_public_profile(self, user_id: str, limit: int = 20) -> dict:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"{self.base_url}/api/v1/users/{user_id}",
                params={"limit": limit},
                headers=self._headers(),
            )
        if resp.status_code == 401:
            raise PermissionError(self._extract_error(resp))
        if resp.status_code != 200:
            raise RuntimeError(self._extract_error(resp))
        return resp.json()

    def get_user_pins(self, user_id: str, limit: int = 20, cursor: str | None = None) -> dict:
        params: dict[str, str | int] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"{self.base_url}/api/v1/users/{user_id}/pins",
                params=params,
                headers=self._headers(),
            )
        if resp.status_code == 401:
            raise PermissionError(self._extract_error(resp))
        if resp.status_code != 200:
            raise RuntimeError(self._extract_error(resp))
        return resp.json()

    def upload_pin(
        self,
        file_path: str,
        title: str,
        description: str,
        categories: list[str],
        event_id: str,
    ) -> dict:
        path = Path(file_path)
        mime_type = EXT_TO_MIME.get(path.suffix.lower(), "application/octet-stream")
        raw_bytes = path.read_bytes()

        resp = self._http.post(
            f"{self.base_url}/api/v1/pins",
            headers=self._headers(),
            data={
                "title": title,
                "description": description,
                "tags": ",".join(categories),
                "event_id": event_id,
            },
            files={"image": (path.name, raw_bytes, mime_type)},
            timeout=25.0,
        )
        if resp.status_code == 401:
            raise PermissionError(self._extract_error(resp))
        if resp.status_code not in (200, 201):
            raise RuntimeError(self._extract_error(resp))
        return resp.json()

    def like_pin(self, pin_id: str, author_id: str, event_id: str) -> dict:
        resp = self._http.post(
            f"{self.base_url}/api/v1/pins/{pin_id}/like",
            json={"event_id": event_id, "author_id": author_id},
            headers=self._headers(),
        )
        if resp.status_code == 401:
            raise PermissionError(self._extract_error(resp))
        if resp.status_code != 200:
            raise RuntimeError(self._extract_error(resp))
        return resp.json()

    def send_view_async(self, pin_id: str, author_id: str) -> None:
        headers = self._headers()
        url = f"{self.base_url}/api/v1/pins/{pin_id}/view"
        body = {"event_id": str(uuid.uuid4()), "author_id": author_id}

        def worker() -> None:
            try:
                with httpx.Client(timeout=5.0) as client:
                    client.post(url, json=body, headers=headers)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def decode_image(
        self,
        image_url: str,
        target_size: tuple[int, int] = CARD_IMAGE_SIZE,
    ) -> Image.Image | None:
        try:
            if image_url.startswith("data:") and "," in image_url:
                raw_b64 = image_url.split(",", 1)[1]
                raw_bytes = base64.b64decode(raw_b64)
            else:
                with httpx.Client(timeout=15.0) as client:
                    resp = client.get(image_url)
                    resp.raise_for_status()
                    raw_bytes = resp.content
            img = Image.open(io.BytesIO(raw_bytes))
            return fit_image(img, target_size)
        except Exception:
            return None

    def logout(self) -> None:
        self.token = None
        self.user_id = None


class DesktopApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Лента картинок — Apotheosis")
        self.geometry("760x720")
        self.minsize(700, 640)
        self.configure(bg=BG_MAIN)

        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background=BG_MAIN)
        style.configure("Card.TFrame", background=BG_CARD)
        style.configure("TLabel", background=BG_MAIN, foreground=FG_MAIN, font=("Helvetica", 12))
        style.configure("Card.TLabel", background=BG_CARD, foreground=FG_MAIN, font=("Helvetica", 12))
        style.configure("Muted.TLabel", background=BG_CARD, foreground=FG_MUTED, font=("Helvetica", 10))
        style.configure("Title.TLabel", background=BG_MAIN, foreground=FG_LINK, font=("Helvetica", 18, "bold"))
        style.configure("CardTitle.TLabel", background=BG_CARD, foreground=FG_MAIN, font=("Helvetica", 14, "bold"))
        style.configure("TButton", font=("Helvetica", 11), padding=(10, 6))
        style.configure("Primary.TButton", background="#0284c7", foreground="#ffffff", font=("Helvetica", 11, "bold"))
        style.map("Primary.TButton", background=[("active", "#0369a1")])

        self.api = ApiClient(API_BASE_URL)
        self.auth_mode = tk.StringVar(value="login")

        self.pins: list[dict] = []
        self.image_cache: dict[str, ImageTk.PhotoImage] = {}
        self.like_events: dict[str, str] = {}
        self.current_index = 0
        self.next_cursor: str | None = "0"
        self.has_more = True
        self.is_fetching = False
        self.last_scroll_time = 0.0
        self.last_wheel_event_time = 0.0
        self.scroll_accumulator = 0

        self.selected_file_path: str | None = None
        self.pending_upload_event_id: str | None = None
        self.preview_photo: ImageTk.PhotoImage | None = None
        self.category_vars: dict[str, tk.BooleanVar] = {}

        self.profile_image_refs: list[ImageTk.PhotoImage] = []
        self.profile_target_user: str | None = None
        self.profile_next_cursor: str | None = None
        self.profile_card_count = 0
        self.profile_request_id = 0

        self.container = tk.Frame(self, bg=BG_MAIN)
        self.container.pack(fill=tk.BOTH, expand=True)

        self.show_auth_screen()

    def _clear_container(self) -> None:
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>", "<Up>", "<Down>"):
            self.unbind_all(seq)
        try:
            self.unbind_all("<TouchpadScroll>")
        except tk.TclError:
            pass
        for widget in self.container.winfo_children():
            widget.destroy()

    def _extract_touchpad_dy(self, event: tk.Event) -> int:
        raw = int(getattr(event, "delta", 0))
        return ctypes.c_short(raw & 0xFFFF).value

    def show_auth_screen(self) -> None:
        self._clear_container()

        wrapper = ttk.Frame(self.container)
        wrapper.place(relx=0.5, rely=0.5, anchor="center")

        card = ttk.Frame(wrapper, style="Card.TFrame", padding=28)
        card.pack()

        ttk.Label(card, text="Лента картинок", style="Title.TLabel").pack(pady=(0, 14))

        mode_row = ttk.Frame(card, style="Card.TFrame")
        mode_row.pack(fill=tk.X, pady=(0, 16))

        self.login_tab_btn = ttk.Button(mode_row, text="Вход", command=lambda: self._switch_auth_mode("login"))
        self.login_tab_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 6))
        self.reg_tab_btn = ttk.Button(mode_row, text="Регистрация", command=lambda: self._switch_auth_mode("register"))
        self.reg_tab_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(6, 0))

        self.auth_heading = ttk.Label(card, text="Вход в систему", style="CardTitle.TLabel")
        self.auth_heading.pack(anchor="w", pady=(0, 12))

        ttk.Label(card, text="Логин (userId, от 3 символов):", style="Muted.TLabel").pack(anchor="w")
        self.username_entry = ttk.Entry(card, width=34, font=("Helvetica", 12))
        self.username_entry.pack(fill=tk.X, pady=(4, 12))
        self.username_entry.focus_set()

        ttk.Label(card, text="Пароль (от 4 символов):", style="Muted.TLabel").pack(anchor="w")
        self.password_entry = ttk.Entry(card, width=34, show="*", font=("Helvetica", 12))
        self.password_entry.pack(fill=tk.X, pady=(4, 16))
        self.password_entry.bind("<Return>", lambda _e: self._submit_auth())

        self.submit_btn = ttk.Button(card, text="Войти", style="Primary.TButton", command=self._submit_auth)
        self.submit_btn.pack(fill=tk.X)

        self.auth_error_label = ttk.Label(card, text="", style="Card.TLabel", foreground="#fca5a5", wraplength=320)
        self.auth_error_label.pack(pady=(12, 0))

        self._switch_auth_mode("login")

    def _switch_auth_mode(self, mode: str) -> None:
        self.auth_mode.set(mode)
        self.auth_error_label.config(text="")
        if mode == "register":
            self.auth_heading.config(text="Регистрация нового аккаунта")
            self.submit_btn.config(text="Создать аккаунт")
            self.reg_tab_btn.config(style="Primary.TButton")
            self.login_tab_btn.config(style="TButton")
        else:
            self.auth_heading.config(text="Вход в систему")
            self.submit_btn.config(text="Войти")
            self.login_tab_btn.config(style="Primary.TButton")
            self.reg_tab_btn.config(style="TButton")

    def _submit_auth(self) -> None:
        username = self.username_entry.get().strip()
        password = self.password_entry.get()
        if len(username) < 3:
            self.auth_error_label.config(text="Логин должен содержать минимум 3 символа.")
            return
        if len(password) < 4:
            self.auth_error_label.config(text="Пароль должен содержать минимум 4 символа.")
            return

        self.auth_error_label.config(text="")
        try:
            self.api.authenticate(self.auth_mode.get(), username, password)
            self.show_feed_screen()
        except httpx.RequestError:
            self.auth_error_label.config(text=f"Нет связи с API Gateway ({self.api.base_url}). Запущен ли Docker?")
        except Exception as exc:
            self.auth_error_label.config(text=str(exc))

    def _build_header(self, active: str) -> None:
        header = tk.Frame(self.container, bg=BG_MAIN, padx=20, pady=12)
        header.pack(fill=tk.X)

        brand = tk.Label(header, text="Лента картинок", bg=BG_MAIN, fg=FG_LINK, font=("Helvetica", 18, "bold"), cursor="hand2")
        brand.pack(side=tk.LEFT)
        brand.bind("<Button-1>", lambda _e: self.show_feed_screen())

        right_bar = tk.Frame(header, bg=BG_MAIN)
        right_bar.pack(side=tk.RIGHT)

        if active != "feed":
            ttk.Button(right_bar, text="← Лента", command=self.show_feed_screen).pack(side=tk.LEFT, padx=(0, 8))

        if active != "upload":
            ttk.Button(right_bar, text="+ Загрузить пин", style="Primary.TButton", command=self.show_upload_screen).pack(side=tk.LEFT, padx=(0, 8))

        is_own_profile = active == "profile" and self.profile_target_user == self.api.user_id
        if not is_own_profile:
            ttk.Button(
                right_bar,
                text=f"Профиль (@{self.api.user_id})",
                command=lambda: self.show_profile_screen(self.api.user_id or ""),
            ).pack(side=tk.LEFT, padx=(0, 8))

        ttk.Button(right_bar, text="Выйти", command=self._logout).pack(side=tk.LEFT)

    def show_feed_screen(self) -> None:
        self._clear_container()
        self._build_header(active="feed")

        stage = tk.Frame(self.container, bg=BG_MAIN)
        stage.pack(fill=tk.BOTH, expand=True)

        center_row = tk.Frame(stage, bg=BG_MAIN)
        center_row.place(relx=0.5, rely=0.47, anchor="center")

        self.card = tk.Frame(center_row, bg=BG_CARD, padx=18, pady=18)
        self.card.pack(side=tk.LEFT)

        self.image_label = tk.Label(
            self.card,
            bg="#020617",
            fg=FG_MUTED,
            text="Загрузка ленты...",
            width=60,
            height=20,
            font=("Helvetica", 12),
        )
        self.image_label.pack(pady=(0, 14))

        self.title_label = tk.Label(
            self.card,
            text="",
            bg=BG_CARD,
            fg=FG_MAIN,
            font=("Helvetica", 15, "bold"),
            wraplength=520,
            justify=tk.LEFT,
            anchor="w",
        )
        self.title_label.pack(fill=tk.X)

        self.desc_label = tk.Label(
            self.card,
            text="",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=("Helvetica", 11),
            wraplength=520,
            justify=tk.LEFT,
            anchor="w",
        )
        self.desc_label.pack(fill=tk.X, pady=(4, 14))

        footer = tk.Frame(self.card, bg=BG_CARD)
        footer.pack(fill=tk.X)

        author_wrap = tk.Frame(footer, bg=BG_CARD)
        author_wrap.pack(side=tk.LEFT)

        self.author_link_label = tk.Label(
            author_wrap,
            text="",
            bg=BG_CARD,
            fg=FG_LINK,
            font=("Helvetica", 11, "underline"),
            cursor="hand2",
        )
        self.author_link_label.pack(side=tk.LEFT)
        self.author_link_label.bind("<Button-1>", self._open_current_author_profile)

        self.shard_label = tk.Label(author_wrap, text="", bg=BG_CARD, fg=FG_MUTED, font=("Helvetica", 11))
        self.shard_label.pack(side=tk.LEFT)

        self.like_btn = ttk.Button(footer, text="♥ 0", command=self._like_current)
        self.like_btn.pack(side=tk.RIGHT)

        nav_col = tk.Frame(center_row, bg=BG_MAIN, padx=14)
        nav_col.pack(side=tk.LEFT, fill=tk.Y)

        self.prev_btn = ttk.Button(nav_col, text="▲", width=3, command=self._prev_pin)
        self.prev_btn.pack(pady=(120, 12))

        self.counter_label = tk.Label(nav_col, text="0 / 0", bg=BG_MAIN, fg=FG_MUTED, font=("Helvetica", 10))
        self.counter_label.pack(pady=4)

        self.next_btn = ttk.Button(nav_col, text="▼", width=3, command=self._next_pin)
        self.next_btn.pack(pady=(12, 0))

        self.status_bar = tk.Label(
            self.container,
            text="Листайте тачпадом, колесиком мыши или стрелками ↑ / ↓ · нажмите на @автора для профиля",
            bg=BG_MAIN,
            fg=FG_MUTED,
            font=("Helvetica", 11),
            pady=10,
        )
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        self.bind_all("<MouseWheel>", self._on_feed_mousewheel)
        self.bind_all("<Button-4>", lambda _e: self._scroll_step(1))
        self.bind_all("<Button-5>", lambda _e: self._scroll_step(-1))
        try:
            self.bind_all("<TouchpadScroll>", self._on_feed_touchpad)
        except tk.TclError:
            pass
        self.bind_all("<Up>", lambda _e: self._prev_pin())
        self.bind_all("<Down>", lambda _e: self._next_pin())

        self._reset_and_load()

    def _open_current_author_profile(self, _event: tk.Event | None = None) -> None:
        if not self.pins:
            return
        author_id = self.pins[self.current_index].get("authorId")
        if author_id:
            self.show_profile_screen(author_id)

    def show_profile_screen(self, target_user_id: str) -> None:
        target_user_id = target_user_id.strip().lstrip("@")
        if not target_user_id:
            return

        self.profile_target_user = target_user_id
        self.profile_next_cursor = None
        self.profile_card_count = 0
        self.profile_image_refs.clear()
        self.profile_request_id += 1
        req_id = self.profile_request_id

        self._clear_container()
        self._build_header(active="profile")

        is_me = target_user_id == self.api.user_id

        top_box = tk.Frame(self.container, bg=BG_CARD, padx=20, pady=16)
        top_box.pack(fill=tk.X, padx=20, pady=(4, 12))

        info_col = tk.Frame(top_box, bg=BG_CARD)
        info_col.pack(side=tk.LEFT, fill=tk.X, expand=True)

        heading = f"Мой профиль (@{target_user_id})" if is_me else f"Профиль @{target_user_id}"
        self.profile_title_label = tk.Label(info_col, text=heading, bg=BG_CARD, fg=FG_MAIN, font=("Helvetica", 16, "bold"))
        self.profile_title_label.pack(anchor="w")

        self.profile_stats_label = tk.Label(info_col, text="Загрузка профиля...", bg=BG_CARD, fg=FG_MUTED, font=("Helvetica", 11))
        self.profile_stats_label.pack(anchor="w", pady=(4, 0))

        lookup_col = tk.Frame(top_box, bg=BG_CARD)
        lookup_col.pack(side=tk.RIGHT)

        tk.Label(lookup_col, text="Найти профиль по логину:", bg=BG_CARD, fg=FG_MUTED, font=("Helvetica", 10)).pack(anchor="w")
        lookup_row = tk.Frame(lookup_col, bg=BG_CARD)
        lookup_row.pack(pady=(4, 0))

        self.profile_search_entry = ttk.Entry(lookup_row, width=18, font=("Helvetica", 11))
        self.profile_search_entry.pack(side=tk.LEFT, padx=(0, 6))
        self.profile_search_entry.bind("<Return>", lambda _e: self._search_profile())

        ttk.Button(lookup_row, text="Открыть", command=self._search_profile).pack(side=tk.LEFT)

        body = tk.Frame(self.container, bg=BG_MAIN)
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 14))

        self.profile_canvas = tk.Canvas(body, bg=BG_MAIN, highlightthickness=0, yscrollincrement=8)
        scrollbar = ttk.Scrollbar(body, orient=tk.VERTICAL, command=self.profile_canvas.yview)
        self.profile_inner = tk.Frame(self.profile_canvas, bg=BG_MAIN)

        self.profile_inner.bind(
            "<Configure>",
            lambda _e: self.profile_canvas.configure(scrollregion=self.profile_canvas.bbox("all")),
        )
        self.profile_canvas_win = self.profile_canvas.create_window((0, 0), window=self.profile_inner, anchor="nw")
        self.profile_canvas.configure(yscrollcommand=scrollbar.set)

        self.profile_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.profile_canvas.bind(
            "<Configure>",
            lambda e: self.profile_canvas.itemconfigure(self.profile_canvas_win, width=e.width),
        )

        self.profile_grid = tk.Frame(self.profile_inner, bg=BG_MAIN)
        self.profile_grid.pack(fill=tk.BOTH, expand=True)
        self.profile_grid.columnconfigure(0, weight=1)
        self.profile_grid.columnconfigure(1, weight=1)

        self.profile_empty_label = tk.Label(self.profile_inner, text="", bg=BG_MAIN, fg=FG_MUTED, font=("Helvetica", 12))
        self.profile_empty_label.pack(pady=16)

        self.profile_more_btn = ttk.Button(
            self.profile_inner,
            text="Показать ещё публикации",
            command=self._load_more_profile_pins,
        )

        self.bind_all("<MouseWheel>", self._on_profile_mousewheel)
        self.bind_all("<Button-4>", lambda _e: self._scroll_profile_canvas(-3))
        self.bind_all("<Button-5>", lambda _e: self._scroll_profile_canvas(3))
        try:
            self.bind_all("<TouchpadScroll>", self._on_profile_touchpad)
        except tk.TclError:
            pass

        def worker() -> None:
            try:
                if is_me:
                    profile = self.api.get_my_profile()
                    pins_page = self.api.get_user_pins(target_user_id, limit=20)
                    pins = pins_page.get("items", [])
                    next_cursor = pins_page.get("next_cursor")
                else:
                    profile = self.api.get_public_profile(target_user_id, limit=20)
                    pins = profile.get("pins", [])
                    next_cursor = profile.get("next_cursor")

                decoded = [
                    (p, self.api.decode_image(p.get("image_url", ""), PROFILE_THUMB_SIZE))
                    for p in pins
                ]
                self.after(0, lambda: self._on_profile_loaded(req_id, is_me, profile, decoded, next_cursor))
            except PermissionError as exc:
                self.after(0, lambda msg=str(exc): self._handle_auth_expired(msg))
            except Exception as exc:
                self.after(0, lambda msg=str(exc): self._on_profile_error(req_id, msg))

        threading.Thread(target=worker, daemon=True).start()

    def _search_profile(self) -> None:
        query = self.profile_search_entry.get().strip().lstrip("@")
        if query:
            self.show_profile_screen(query)

    def _on_profile_loaded(
        self,
        req_id: int,
        is_me: bool,
        profile: dict,
        decoded: list[tuple[dict, Image.Image | None]],
        next_cursor: str | None,
    ) -> None:
        if req_id != self.profile_request_id:
            return
        if not hasattr(self, "profile_stats_label") or not self.profile_stats_label.winfo_exists():
            return

        uid = profile.get("user_id", self.profile_target_user or "")
        display_name = profile.get("display_name") or uid
        if is_me:
            title_text = f"Мой профиль — {display_name} (@{uid})" if display_name != uid else f"Мой профиль (@{uid})"
            pins_count = profile.get("pins_count", len(decoded))
            likes_count = profile.get("likes_count", 0)
            partial_note = " (частично)" if profile.get("likes_count_partial") else ""
            stats_text = f"Публикаций: {pins_count} · Получено лайков: {likes_count}{partial_note}"
        else:
            title_text = f"{display_name} (@{uid})" if display_name != uid else f"Профиль @{uid}"
            stats_text = f"Загружено публикаций: {len(decoded)}"

        self.profile_title_label.config(text=title_text)
        self.profile_stats_label.config(text=stats_text)
        self.profile_next_cursor = next_cursor

        self._append_profile_cards(decoded)

    def _on_profile_error(self, req_id: int, message: str) -> None:
        if req_id != self.profile_request_id:
            return
        if hasattr(self, "profile_stats_label") and self.profile_stats_label.winfo_exists():
            self.profile_stats_label.config(text=message, fg="#fca5a5")

    def _append_profile_cards(self, decoded: list[tuple[dict, Image.Image | None]]) -> None:
        for pin, pil_img in decoded:
            row = self.profile_card_count // 2
            col = self.profile_card_count % 2
            self.profile_card_count += 1

            card = tk.Frame(self.profile_grid, bg=BG_CARD, padx=12, pady=12)
            card.grid(row=row, column=col, padx=8, pady=8, sticky="nsew")

            if pil_img is not None:
                photo = ImageTk.PhotoImage(pil_img)
                self.profile_image_refs.append(photo)
                tk.Label(card, image=photo, bg="#020617", bd=0).pack(pady=(0, 8))
            else:
                tk.Label(card, text="Нет изображения", bg="#020617", fg=FG_MUTED, width=30, height=10).pack(pady=(0, 8))

            tk.Label(
                card,
                text=pin.get("title", ""),
                bg=BG_CARD,
                fg=FG_MAIN,
                font=("Helvetica", 12, "bold"),
                wraplength=270,
                justify=tk.LEFT,
                anchor="w",
            ).pack(fill=tk.X)

            created_raw = str(pin.get("created_at", ""))[:10]
            if created_raw:
                tk.Label(card, text=created_raw, bg=BG_CARD, fg=FG_MUTED, font=("Helvetica", 10), anchor="w").pack(fill=tk.X, pady=(2, 0))

        if self.profile_card_count == 0:
            self.profile_empty_label.config(text="Публикаций пока нет.")
        else:
            self.profile_empty_label.config(text="")

        if self.profile_next_cursor:
            self.profile_more_btn.pack(pady=(8, 16))
        else:
            self.profile_more_btn.pack_forget()

        self.update_idletasks()
        if hasattr(self, "profile_canvas") and self.profile_canvas.winfo_exists():
            self.profile_canvas.configure(scrollregion=self.profile_canvas.bbox("all"))

    def _load_more_profile_pins(self) -> None:
        if not self.profile_target_user or not self.profile_next_cursor:
            return
        req_id = self.profile_request_id
        uid = self.profile_target_user
        cursor = self.profile_next_cursor
        self.profile_more_btn.config(state=tk.DISABLED)

        def worker() -> None:
            try:
                page = self.api.get_user_pins(uid, limit=20, cursor=cursor)
                pins = page.get("items", [])
                decoded = [
                    (p, self.api.decode_image(p.get("image_url", ""), PROFILE_THUMB_SIZE))
                    for p in pins
                ]
                next_cur = page.get("next_cursor")
                self.after(0, lambda: self._on_more_profile_pins(req_id, decoded, next_cur))
            except PermissionError as exc:
                self.after(0, lambda msg=str(exc): self._handle_auth_expired(msg))
            except Exception as exc:
                self.after(0, lambda msg=str(exc): self._on_profile_error(req_id, msg))

        threading.Thread(target=worker, daemon=True).start()

    def _on_more_profile_pins(
        self,
        req_id: int,
        decoded: list[tuple[dict, Image.Image | None]],
        next_cursor: str | None,
    ) -> None:
        if req_id != self.profile_request_id:
            return
        self.profile_next_cursor = next_cursor
        if hasattr(self, "profile_more_btn") and self.profile_more_btn.winfo_exists():
            self.profile_more_btn.config(state=tk.NORMAL)
        self._append_profile_cards(decoded)

    def _scroll_profile_canvas(self, units: int) -> None:
        if not hasattr(self, "profile_canvas") or not self.profile_canvas.winfo_exists():
            return
        bbox = self.profile_canvas.bbox("all")
        if bbox and (bbox[3] - bbox[1]) > self.profile_canvas.winfo_height():
            self.profile_canvas.yview_scroll(units, "units")

    def _on_profile_mousewheel(self, event: tk.Event) -> None:
        delta = int(getattr(event, "delta", 0))
        if delta == 0:
            return
        if sys.platform == "darwin":
            self._scroll_profile_canvas(-delta * 2)
        else:
            step = -int(delta / 40) if abs(delta) >= 40 else (-1 if delta > 0 else 1)
            self._scroll_profile_canvas(step * 2)

    def _on_profile_touchpad(self, event: tk.Event) -> None:
        dy = self._extract_touchpad_dy(event)
        if dy != 0:
            self._scroll_profile_canvas(-dy)

    def show_upload_screen(self) -> None:
        self._clear_container()
        self._build_header(active="upload")

        self.selected_file_path = None
        self.pending_upload_event_id = None
        self.preview_photo = None
        self.category_vars.clear()

        wrapper = tk.Frame(self.container, bg=BG_MAIN)
        wrapper.pack(fill=tk.BOTH, expand=True, padx=24, pady=(4, 18))

        card = tk.Frame(wrapper, bg=BG_CARD, padx=24, pady=20)
        card.pack(expand=True, fill=tk.BOTH)

        tk.Label(card, text="Загрузить новый пин", bg=BG_CARD, fg=FG_MAIN, font=("Helvetica", 16, "bold")).pack(anchor="w", pady=(0, 12))

        top_row = tk.Frame(card, bg=BG_CARD)
        top_row.pack(fill=tk.X, pady=(0, 14))

        left_col = tk.Frame(top_row, bg=BG_CARD)
        left_col.pack(side=tk.LEFT, padx=(0, 18))

        self.preview_label = tk.Label(
            left_col,
            text="Предпросмотр\n(JPEG, PNG, WEBP)",
            bg="#020617",
            fg=FG_MUTED,
            width=30,
            height=9,
            font=("Helvetica", 10),
        )
        self.preview_label.pack(pady=(0, 8))

        ttk.Button(left_col, text="Выбрать файл...", command=self._choose_image_file).pack(fill=tk.X)

        self.file_name_label = tk.Label(left_col, text="Файл не выбран", bg=BG_CARD, fg=FG_MUTED, font=("Helvetica", 10), wraplength=240)
        self.file_name_label.pack(pady=(6, 0))

        right_col = tk.Frame(top_row, bg=BG_CARD)
        right_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        tk.Label(right_col, text="Название (обязательно):", bg=BG_CARD, fg=FG_MUTED, font=("Helvetica", 11)).pack(anchor="w")
        self.upload_title_entry = ttk.Entry(right_col, font=("Helvetica", 12))
        self.upload_title_entry.pack(fill=tk.X, pady=(4, 12))

        tk.Label(right_col, text="Описание:", bg=BG_CARD, fg=FG_MUTED, font=("Helvetica", 11)).pack(anchor="w")
        self.upload_desc_text = tk.Text(
            right_col,
            height=5,
            font=("Helvetica", 11),
            bg="#0f172a",
            fg=FG_MAIN,
            insertbackground=FG_MAIN,
            relief=tk.FLAT,
            padx=8,
            pady=6,
        )
        self.upload_desc_text.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        tk.Label(
            card,
            text="Категории системы (используются для рекомендаций, выберите минимум 1):",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=("Helvetica", 11),
        ).pack(anchor="w", pady=(6, 8))

        cats_frame = tk.Frame(card, bg=BG_CARD)
        cats_frame.pack(fill=tk.X, pady=(0, 14))

        categories = self.api.get_categories()
        cols = 4
        for idx, cat in enumerate(categories):
            var = tk.BooleanVar(value=False)
            self.category_vars[cat] = var
            cb = tk.Checkbutton(
                cats_frame,
                text=cat,
                variable=var,
                bg=BG_CARD,
                fg=FG_MAIN,
                selectcolor="#0f172a",
                activebackground=BG_CARD,
                activeforeground=FG_MAIN,
                font=("Helvetica", 11),
                anchor="w",
            )
            cb.grid(row=idx // cols, column=idx % cols, sticky="w", padx=8, pady=3)
            cats_frame.columnconfigure(idx % cols, weight=1)

        action_row = tk.Frame(card, bg=BG_CARD)
        action_row.pack(fill=tk.X, pady=(6, 0))

        self.upload_submit_btn = ttk.Button(action_row, text="Опубликовать пин", style="Primary.TButton", command=self._submit_upload)
        self.upload_submit_btn.pack(side=tk.LEFT)

        self.upload_status_label = tk.Label(action_row, text="", bg=BG_CARD, fg="#fca5a5", font=("Helvetica", 11), wraplength=420, justify=tk.LEFT)
        self.upload_status_label.pack(side=tk.LEFT, padx=(14, 0))

    def _choose_image_file(self) -> None:
        file_path = filedialog.askopenfilename(
            title="Выберите изображение",
            filetypes=[
                ("Изображения", ("*.jpg", "*.jpeg", "*.png", "*.webp")),
                ("Все файлы", "*.*"),
            ],
        )
        if not file_path:
            return

        self.selected_file_path = file_path
        self.pending_upload_event_id = str(uuid.uuid4())
        self.file_name_label.config(text=Path(file_path).name)
        self.upload_status_label.config(text="")

        try:
            pil_img = Image.open(file_path)
            fitted = fit_image(pil_img, PREVIEW_IMAGE_SIZE)
            self.preview_photo = ImageTk.PhotoImage(fitted)
            self.preview_label.config(
                image=self.preview_photo,
                text="",
                width=PREVIEW_IMAGE_SIZE[0],
                height=PREVIEW_IMAGE_SIZE[1],
            )
        except Exception:
            self.preview_photo = None
            self.preview_label.config(image="", text="Не удалось открыть предпросмотр", width=30, height=9)

    def _submit_upload(self) -> None:
        if not self.selected_file_path:
            self.upload_status_label.config(text="Выберите файл изображения.", fg="#fca5a5")
            return

        title = self.upload_title_entry.get().strip()
        if not title:
            self.upload_status_label.config(text="Введите название пина.", fg="#fca5a5")
            return

        selected_cats = [cat for cat, var in self.category_vars.items() if var.get()]
        if not selected_cats:
            self.upload_status_label.config(text="Выберите хотя бы одну категорию системы.", fg="#fca5a5")
            return

        description = self.upload_desc_text.get("1.0", tk.END).strip()
        if not self.pending_upload_event_id:
            self.pending_upload_event_id = str(uuid.uuid4())

        self.upload_submit_btn.config(state=tk.DISABLED)
        self.upload_status_label.config(text="Загрузка...", fg=FG_MUTED)
        self.update_idletasks()

        try:
            self.api.upload_pin(
                file_path=self.selected_file_path,
                title=title,
                description=description,
                categories=selected_cats,
                event_id=self.pending_upload_event_id,
            )
            self.pending_upload_event_id = None
            messagebox.showinfo("Готово", "Пин успешно опубликован!")
            self.show_feed_screen()
        except PermissionError as exc:
            self._handle_auth_expired(str(exc))
        except Exception as exc:
            self.upload_submit_btn.config(state=tk.NORMAL)
            self.upload_status_label.config(text=str(exc), fg="#fca5a5")

    def _reset_and_load(self) -> None:
        self.pins.clear()
        self.image_cache.clear()
        self.like_events.clear()
        self.current_index = 0
        self.next_cursor = "0"
        self.has_more = True
        self.is_fetching = False
        self._fetch_next_batch_async(show_first=True)

    def _fetch_next_batch_async(self, show_first: bool = False) -> None:
        if self.is_fetching or not self.has_more or self.next_cursor is None:
            return
        self.is_fetching = True
        cursor = self.next_cursor

        def worker() -> None:
            try:
                data = self.api.get_feed(cursor, FEED_PAGE_SIZE)
                items = data.get("items", [])
                decoded: list[tuple[str, Image.Image | None]] = [
                    (pin["pinId"], self.api.decode_image(pin["image_url"], CARD_IMAGE_SIZE)) for pin in items
                ]
                self.after(0, lambda: self._on_batch_loaded(data, decoded, show_first))
            except PermissionError as exc:
                self.after(0, lambda msg=str(exc): self._handle_auth_expired(msg))
            except Exception as exc:
                self.after(0, lambda msg=str(exc): self._on_fetch_error(msg))

        threading.Thread(target=worker, daemon=True).start()

    def _on_batch_loaded(
        self,
        data: dict,
        decoded: list[tuple[str, Image.Image | None]],
        show_first: bool,
    ) -> None:
        self.is_fetching = False
        items = data.get("items", [])
        for pin_id, pil_img in decoded:
            if pil_img is not None:
                self.image_cache[pin_id] = ImageTk.PhotoImage(pil_img)

        self.pins.extend(items)
        self.next_cursor = data.get("next_cursor")
        self.has_more = bool(data.get("has_more"))

        if not hasattr(self, "image_label") or not self.image_label.winfo_exists():
            return

        if show_first:
            self.current_index = 0
            self._render_current_pin()
        else:
            self._update_nav_controls()

    def _on_fetch_error(self, message: str) -> None:
        self.is_fetching = False
        if hasattr(self, "status_bar") and self.status_bar.winfo_exists():
            self.status_bar.config(text=f"Ошибка загрузки: {message}")

    def _handle_auth_expired(self, message: str) -> None:
        self.is_fetching = False
        messagebox.showwarning("Сессия", message)
        self._logout()

    def _render_current_pin(self) -> None:
        if not self.pins:
            self.image_label.config(
                image="",
                text="В ленте пока нет картинок.\nНажмите «+ Загрузить пин», чтобы добавить первую!",
                width=60,
                height=20,
            )
            self.title_label.config(text="")
            self.desc_label.config(text="")
            self.author_link_label.config(text="")
            self.shard_label.config(text="")
            self.like_btn.config(text="♥ 0", state=tk.DISABLED)
            self._update_nav_controls()
            return

        pin = self.pins[self.current_index]
        photo = self.image_cache.get(pin["pinId"])
        if photo is not None:
            self.image_label.config(image=photo, text="", width=CARD_IMAGE_SIZE[0], height=CARD_IMAGE_SIZE[1])
            self.image_label.image = photo
        else:
            self.image_label.config(image="", text="Не удалось загрузить изображение", width=60, height=20)

        self.title_label.config(text=pin["title"])
        self.desc_label.config(text=pin.get("description", ""))
        self.author_link_label.config(text=f"@{pin['authorId']}")
        shard = pin.get("shard", "")
        self.shard_label.config(text=f" · {shard}" if shard else "")

        self.like_btn.config(
            text=f"♥ {pin['likes_count']}",
            state=tk.NORMAL,
            style="Primary.TButton" if pin.get("liked_by_me") else "TButton",
        )

        self.api.send_view_async(pin["pinId"], pin["authorId"])
        self._update_nav_controls()

        if len(self.pins) - 1 - self.current_index <= PREFETCH_THRESHOLD and self.has_more:
            self._fetch_next_batch_async(show_first=False)

    def _update_nav_controls(self) -> None:
        total = len(self.pins)
        if total == 0:
            self.counter_label.config(text="0 / 0")
            self.prev_btn.config(state=tk.DISABLED)
            self.next_btn.config(state=tk.DISABLED)
            self.status_bar.config(text="Лента пуста · загрузите первый пин")
            return

        suffix = "+" if self.has_more else ""
        self.counter_label.config(text=f"{self.current_index + 1} / {total}{suffix}")
        self.prev_btn.config(state=tk.NORMAL if self.current_index > 0 else tk.DISABLED)
        can_next = self.current_index < total - 1 or self.has_more
        self.next_btn.config(state=tk.NORMAL if can_next else tk.DISABLED)

        if not self.has_more and self.current_index == total - 1:
            self.status_bar.config(text="Вы дошли до конца ленты · листайте вверх или загрузите новый пин")
        else:
            self.status_bar.config(text="Листайте тачпадом, колесиком мыши или стрелками ↑ / ↓ · нажмите на @автора для профиля")

    def _next_pin(self) -> None:
        if self.current_index < len(self.pins) - 1:
            self.current_index += 1
            self._render_current_pin()
        elif self.has_more:
            self.status_bar.config(text="Подгружаем следующие картинки...")
            self._fetch_next_batch_async(show_first=False)

    def _prev_pin(self) -> None:
        if self.current_index > 0:
            self.current_index -= 1
            self._render_current_pin()

    def _scroll_step(self, direction: int) -> None:
        now = time.monotonic()
        if now - self.last_scroll_time < SCROLL_COOLDOWN_SEC:
            return
        self.last_scroll_time = now
        self.scroll_accumulator = 0
        if direction < 0:
            self._next_pin()
        elif direction > 0:
            self._prev_pin()

    def _on_feed_mousewheel(self, event: tk.Event) -> None:
        step = int(getattr(event, "delta", 0))
        if step == 0:
            return

        now = time.monotonic()
        if now - self.last_wheel_event_time > SCROLL_RESET_SEC:
            self.scroll_accumulator = 0
        self.last_wheel_event_time = now

        if now - self.last_scroll_time < SCROLL_COOLDOWN_SEC:
            return

        self.scroll_accumulator += step
        threshold = 1 if sys.platform == "darwin" else 30
        if abs(self.scroll_accumulator) < threshold:
            return

        direction = 1 if self.scroll_accumulator > 0 else -1
        self._scroll_step(direction)

    def _on_feed_touchpad(self, event: tk.Event) -> None:
        dy = self._extract_touchpad_dy(event)
        if dy == 0:
            return
        now = time.monotonic()
        if now - self.last_wheel_event_time > SCROLL_RESET_SEC:
            self.scroll_accumulator = 0
        self.last_wheel_event_time = now

        if now - self.last_scroll_time < SCROLL_COOLDOWN_SEC:
            return

        self.scroll_accumulator += dy
        if abs(self.scroll_accumulator) < 2:
            return

        direction = 1 if self.scroll_accumulator > 0 else -1
        self._scroll_step(direction)

    def _like_current(self) -> None:
        if not self.pins:
            return
        pin = self.pins[self.current_index]
        pin_id = pin["pinId"]
        event_id = self.like_events.setdefault(pin_id, str(uuid.uuid4()))
        try:
            res = self.api.like_pin(pin_id, pin["authorId"], event_id)
            pin["likes_count"] = res["likes_count"]
            pin["liked_by_me"] = True
            self.like_btn.config(text=f"♥ {pin['likes_count']}", style="Primary.TButton")
        except PermissionError as exc:
            self._handle_auth_expired(str(exc))
        except Exception as exc:
            messagebox.showerror("Ошибка", str(exc))

    def _logout(self) -> None:
        self.api.logout()
        self.show_auth_screen()


if __name__ == "__main__":
    app = DesktopApp()
    app.mainloop()
