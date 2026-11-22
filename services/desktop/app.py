import base64
import io
import os
import sys
import threading
import time
import uuid
import tkinter as tk
from tkinter import messagebox, ttk
import httpx
from PIL import Image, ImageTk

API_BASE_URL = os.getenv("API_URL", "http://localhost:8000").rstrip("/")
FEED_PAGE_SIZE = 4
PREFETCH_THRESHOLD = 2
CARD_IMAGE_SIZE = (540, 390)
SCROLL_COOLDOWN_SEC = 0.32
BG_MAIN = "#0f172a"
BG_CARD = "#1e293b"
FG_MAIN = "#f8fafc"
FG_MUTED = "#94a3b8"


class ApiClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.token: str | None = None
        self.user_id: str | None = None
        self._http = httpx.Client(timeout=10.0)

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

    def decode_image(self, image_url: str) -> Image.Image | None:
        try:
            if image_url.startswith("data:") and "," in image_url:
                raw_b64 = image_url.split(",", 1)[1]
                raw_bytes = base64.b64decode(raw_b64)
            else:
                with httpx.Client(timeout=10.0) as client:
                    resp = client.get(image_url)
                    resp.raise_for_status()
                    raw_bytes = resp.content
            img = Image.open(io.BytesIO(raw_bytes))
            return img.resize(CARD_IMAGE_SIZE, Image.Resampling.LANCZOS)
        except Exception:
            return None

    def logout(self) -> None:
        self.token = None
        self.user_id = None


class DesktopApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Лента картинок — Apotheosis")
        self.geometry("720x680")
        self.minsize(660, 620)
        self.configure(bg=BG_MAIN)

        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background=BG_MAIN)
        style.configure("Card.TFrame", background=BG_CARD)
        style.configure("TLabel", background=BG_MAIN, foreground=FG_MAIN, font=("Helvetica", 12))
        style.configure("Card.TLabel", background=BG_CARD, foreground=FG_MAIN, font=("Helvetica", 12))
        style.configure("Muted.TLabel", background=BG_CARD, foreground=FG_MUTED, font=("Helvetica", 10))
        style.configure("Title.TLabel", background=BG_MAIN, foreground="#38bdf8", font=("Helvetica", 18, "bold"))
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
        self.scroll_accumulator = 0

        self.container = tk.Frame(self, bg=BG_MAIN)
        self.container.pack(fill=tk.BOTH, expand=True)

        self.show_auth_screen()

    def _clear_container(self) -> None:
        self.unbind_all("<MouseWheel>")
        self.unbind_all("<Up>")
        self.unbind_all("<Down>")
        for widget in self.container.winfo_children():
            widget.destroy()

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

    def show_feed_screen(self) -> None:
        self._clear_container()

        header = tk.Frame(self.container, bg=BG_MAIN, padx=20, pady=12)
        header.pack(fill=tk.X)

        tk.Label(header, text="Лента картинок", bg=BG_MAIN, fg="#38bdf8", font=("Helvetica", 18, "bold")).pack(side=tk.LEFT)

        right_bar = tk.Frame(header, bg=BG_MAIN)
        right_bar.pack(side=tk.RIGHT)

        tk.Label(right_bar, text=f"@{self.api.user_id}", bg=BG_MAIN, fg=FG_MUTED, font=("Helvetica", 12)).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Button(right_bar, text="Обновить", command=self._reset_and_load).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(right_bar, text="Выйти", command=self._logout).pack(side=tk.LEFT)

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

        self.meta_label = tk.Label(footer, text="", bg=BG_CARD, fg=FG_MUTED, font=("Helvetica", 11))
        self.meta_label.pack(side=tk.LEFT)

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
            text="Листайте колесиком, тачпадом или стрелками ↑ / ↓",
            bg=BG_MAIN,
            fg=FG_MUTED,
            font=("Helvetica", 11),
            pady=10,
        )
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        self.bind_all("<MouseWheel>", self._on_mousewheel)
        self.bind_all("<Up>", lambda _e: self._prev_pin())
        self.bind_all("<Down>", lambda _e: self._next_pin())

        self._reset_and_load()

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
                    (pin["pinId"], self.api.decode_image(pin["image_url"])) for pin in items
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
            self.image_label.config(image="", text="В ленте пока нет картинок", width=60, height=20)
            self.title_label.config(text="")
            self.desc_label.config(text="")
            self.meta_label.config(text="")
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
        self.meta_label.config(text=f"@{pin['authorId']} · {pin.get('shard', '')}")

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
            return

        suffix = "+" if self.has_more else ""
        self.counter_label.config(text=f"{self.current_index + 1} / {total}{suffix}")
        self.prev_btn.config(state=tk.NORMAL if self.current_index > 0 else tk.DISABLED)
        can_next = self.current_index < total - 1 or self.has_more
        self.next_btn.config(state=tk.NORMAL if can_next else tk.DISABLED)

        if not self.has_more and self.current_index == total - 1:
            self.status_bar.config(text="Вы дошли до конца ленты · нажмите «Обновить» для перезагрузки")
        else:
            self.status_bar.config(text="Листайте колесиком, тачпадом или стрелками ↑ / ↓")

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

    def _on_mousewheel(self, event: tk.Event) -> None:
        now = time.monotonic()
        if now - self.last_scroll_time < SCROLL_COOLDOWN_SEC:
            return

        step = int(event.delta)
        if step == 0:
            return

        if sys.platform == "darwin":
            self.scroll_accumulator += step
            if abs(self.scroll_accumulator) < 2:
                return
            direction = self.scroll_accumulator
            self.scroll_accumulator = 0
        else:
            direction = step

        self.last_scroll_time = now
        if direction < 0:
            self._next_pin()
        else:
            self._prev_pin()

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
