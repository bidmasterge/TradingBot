"""Desktop window for the Binance order bot.

Double-click "Trading Bot.bat", or run:

  .\\.venv\\Scripts\\python.exe desktop.py
"""

from __future__ import annotations

import asyncio
import ctypes
import io
import json
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import zipfile

import orjson
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from dotenv import dotenv_values
from websockets.asyncio.client import connect

from engine.client import BinanceError, OrderClient, unwrap
from engine.signer import HmacSigner

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "profiles.json"
UPDATE_CONFIG_PATH = ROOT / "update.json"
SANS = "Segoe UI"
MONO = "Consolas"
BG = "#1b1d21"
CARD = "#24262c"
FG = "#e8e9ec"
MUTED = "#2e3138"
MUTED_FG = "#9aa0a8"
PRIMARY = "#e2b34a"
PRIMARY_FG = "#2c2618"
SECONDARY = "#34373e"
BUY = "#3dcb92"
SELL = "#e15d56"
BORDER = "#3e424a"
INPUT = "#1e2025"
DECIMAL = re.compile(r"^\d+(\.\d+)?$")
SYMBOL = re.compile(r"^[A-Z0-9]{5,20}$")


def enable_dpi() -> None:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TradingBot.Desktop")
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def ms(ns: int) -> str:
    return f"{ns / 1_000_000:.2f} ms"


def price_step(text: str) -> Decimal:
    """One step is the last digit of the price the user typed. 0.40 -> 0.01."""
    if "." in text:
        places = len(text.split(".", 1)[1])
        if places:
            return Decimal(10) ** -places
    return Decimal(1)


def choose_step(user_step: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0 or user_step % tick == 0:
        return user_step
    return tick


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _price_away(order: dict, bid: Decimal, ask: Decimal) -> bool:
    band = order["tick"] * order["steps"]
    if order["side"] == "BUY":
        return bid > order["price"] + band
    if order["side"] == "SELL":
        return ask < order["price"] - band
    return False


def resting_qty(row: dict, lot: Decimal) -> Decimal:
    orig = Decimal(str(row.get("origQty") or "0"))
    executed = Decimal(str(row.get("executedQty") or "0"))
    left = orig - executed
    if lot > 0:
        left = floor_step(left, lot)
    return left if left > 0 else Decimal(0)


def moved_order_id(result: object, old_id: int) -> int:
    if not isinstance(result, dict):
        return old_id
    outcome = result.get("newOrderResult")
    if outcome not in (None, "SUCCESS"):
        raise ValueError(str(result.get("newOrderResponse") or outcome or "Move failed"))
    fresh = result.get("newOrderResponse") or {}
    if isinstance(fresh, dict) and fresh.get("orderId"):
        return int(fresh["orderId"])
    if result.get("orderId"):
        return int(result["orderId"])
    return old_id


def _remaining_qty(result: object, order: dict) -> Decimal:
    qty = order["qty"]
    if not isinstance(result, dict) or result.get("origQty") is None:
        return qty
    left = Decimal(str(result["origQty"])) - Decimal(str(result.get("executedQty") or "0"))
    lot = order.get("lot") or Decimal(0)
    if lot > 0:
        left = floor_step(left, lot)
    return left if left > 0 else Decimal(0)


def _split_symbol(symbol: str) -> tuple[str, str]:
    for quote in ("USDT", "USDC", "FDUSD", "BUSD", "TUSD", "BTC", "ETH", "BNB", "EUR", "TRY"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote
    return symbol, "USDT"


def _out_of_funds(exc: Exception) -> bool:
    if isinstance(exc, BinanceError) and exc.code in {-2010, -2018, -2019}:
        return True
    return "insufficient" in str(exc).lower()


def dec_str(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def quote_to_base(usdt: Decimal, price: Decimal, lot: Decimal) -> Decimal:
    if price <= 0:
        return Decimal(0)
    return floor_step(usdt / price, lot)


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}
        values = dotenv_values(ROOT / ".env")
        key = (values.get("BINANCE_API_KEY") or "").strip()
        secret = (values.get("BINANCE_API_SECRET") or "").strip()
        if key and secret:
            data = {"apis": [{"name": "Main", "api_key": key, "secret": secret}], "symbols": []}
            save_config(data)
    data.setdefault("apis", [])
    data.setdefault("symbols", [])
    return data


def save_config(data: dict) -> None:
    temporary = CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(CONFIG_PATH)


def load_update_config() -> dict:
    data = {"repo": "bidmasterge/TradingBot", "branch": "main", "version": "1.0.0", "token": ""}
    if UPDATE_CONFIG_PATH.exists():
        try:
            loaded = json.loads(UPDATE_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update({k: str(v).strip() for k, v in loaded.items() if k in data and v is not None})
        except Exception:
            pass
    return data


def save_update_config(data: dict) -> None:
    UPDATE_CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _ver_tuple(ver: str) -> tuple[int, ...]:
    nums = [int(x) for x in re.findall(r"\d+", ver or "0")]
    return tuple(nums) if nums else (0,)


def check_remote_version() -> str | None:
    cfg = load_update_config()
    repo = cfg.get("repo") or "bidmasterge/TradingBot"
    branch = cfg.get("branch") or "main"
    token = cfg.get("token") or ""
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/update.json?t={int(time.time())}"
    headers = {"User-Agent": "TradingBot-Updater", "Cache-Control": "no-cache"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            remote = json.loads(resp.read().decode("utf-8"))
        remote_ver = str(remote.get("version") or "").strip()
        local_ver = str(cfg.get("version") or "1.0.0").strip()
        if remote_ver and _ver_tuple(remote_ver) > _ver_tuple(local_ver):
            return remote_ver
    except Exception:
        return None
    return None


def pull_latest_from_github() -> int:
    cfg = load_update_config()
    repo = cfg.get("repo") or "bidmasterge/TradingBot"
    branch = cfg.get("branch") or "main"
    token = cfg.get("token") or ""
    url = f"https://codeload.github.com/{repo}/zip/refs/heads/{branch}"
    headers = {"User-Agent": "TradingBot-Updater"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw_zip = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(
                f"GitHub repo '{repo}' ({branch}) not found or is private. Right-click Update to set repo/token."
            ) from exc
        raise RuntimeError(f"Update download failed: HTTP {exc.code}") from exc
    skip_exact = {"profiles.json", ".env"}
    skip_Top = {".git", ".venv", "runtime", "Design", "__pycache__"}
    updated = 0
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            parts = Path(info.filename).parts
            if len(parts) < 2:
                continue
            rel_parts = parts[1:]
            if rel_parts[0] in skip_Top:
                continue
            rel_path = Path(*rel_parts)
            if rel_path.name in skip_exact or rel_path.suffix in {".pyc", ".pem", ".tmp"}:
                continue
            dest = ROOT / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                dest.write_bytes(zf.read(info.filename))
                updated += 1
            except PermissionError:
                continue
    return updated


def fetch_rules(market: str, symbol: str) -> tuple[Decimal, Decimal]:
    if market == "futures":
        url = f"https://fapi.binance.com/fapi/v1/exchangeInfo?symbol={symbol}"
    else:
        url = f"https://api.binance.com/api/v3/exchangeInfo?symbol={symbol}"
    with urllib.request.urlopen(url, timeout=10) as response:
        payload = json.loads(response.read().decode())
    symbols = payload.get("symbols") or []
    match = next((item for item in symbols if item.get("symbol") == symbol), None)
    if match is None and symbols:
        match = symbols[0]
    tick = Decimal(0)
    lot = Decimal(0)
    for item in (match or {}).get("filters", []):
        if item.get("filterType") == "PRICE_FILTER":
            tick = Decimal(item["tickSize"])
        elif item.get("filterType") == "LOT_SIZE":
            lot = Decimal(item["stepSize"])
    if tick <= 0 or lot <= 0:
        raise ValueError(f"No tick size for {symbol}")
    return tick, lot


class SymbolBox(ttk.Combobox):
    ROW_H = 38
    MAX_VISIBLE = 8

    def __init__(self, master, **kwargs) -> None:
        super().__init__(master, **kwargs)
        self._all: list[str] = []
        self._matches: list[str] = []
        self._active = 0
        self._popup: tk.Toplevel | None = None
        self._canvas: tk.Canvas | None = None
        self._scroll: ttk.Scrollbar | None = None
        self.bind("<KeyRelease>", self._filter)
        self.bind("<ButtonPress-1>", self._on_click)
        self.bind("<Double-Button-1>", self._on_double_click)
        self.bind("<Down>", lambda _event: self._move(1))
        self.bind("<Up>", lambda _event: self._move(-1))
        self.bind("<Return>", self._choose_active)
        self.bind("<Escape>", self._hide_event)
        self.bind("<FocusOut>", self._on_focus_out)

    def set_symbols(self, symbols: list[str]) -> None:
        self._all = list(symbols)
        self["values"] = self._all

    def _filter(self, event: tk.Event) -> None:
        if event.keysym in {"Up", "Down", "Return", "Escape", "Tab", "Left", "Right", "Shift_L", "Shift_R"}:
            return
        query = self.get().strip().upper()
        if not query:
            self._matches = list(self._all)
        else:
            self._matches = [symbol for symbol in self._all if symbol.startswith(query)]
        self["values"] = self._matches
        self._active = 0
        if self._matches:
            self._show()
        else:
            self._hide()
        self.focus_set()

    def _ensure_popup(self) -> None:
        if self._popup is not None:
            return
        popup = tk.Toplevel(self)
        popup.withdraw()
        popup.overrideredirect(True)
        popup.configure(bg=PRIMARY)
        inner = tk.Frame(popup, bg=INPUT)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        canvas = tk.Canvas(
            inner,
            bg=INPUT,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        scroll = ttk.Scrollbar(inner, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        canvas.bind("<Motion>", self._on_hover_motion)
        canvas.bind("<ButtonPress-1>", self._click_list)
        canvas.bind("<MouseWheel>", self._on_wheel)
        self._popup = popup
        self._canvas = canvas
        self._scroll = scroll

    def _draw_items(self) -> None:
        canvas = self._canvas
        if canvas is None:
            return
        canvas.delete("all")
        width = max(canvas.winfo_width(), self.winfo_width(), 250)
        total_h = len(self._matches) * self.ROW_H
        canvas.configure(scrollregion=(0, 0, width, total_h))
        current_val = self.get().strip().upper()
        for idx, symbol in enumerate(self._matches):
            y0 = idx * self.ROW_H
            y1 = y0 + self.ROW_H
            is_active = idx == self._active
            is_current = symbol == current_val
            row_bg = "#263d35" if is_active else ("#252932" if idx % 2 else INPUT)
            canvas.create_rectangle(0, y0, width, y1, fill=row_bg, outline="")
            if is_active:
                canvas.create_rectangle(0, y0, 4, y1, fill=BUY, outline="")
            canvas.create_line(0, y1 - 1, width, y1 - 1, fill="#30343d")
            base, quote = _split_symbol(symbol)
            mid_y = y0 + self.ROW_H // 2
            base_color = "#ffffff" if is_active else (PRIMARY if is_current else FG)
            quote_color = BUY if is_active else MUTED_FG
            base_item = canvas.create_text(
                14,
                mid_y,
                text=base,
                anchor="w",
                fill=base_color,
                font=(MONO, 14, "bold"),
            )
            bbox = canvas.bbox(base_item)
            qx = (bbox[2] + 2) if bbox else 80
            canvas.create_text(
                qx,
                mid_y,
                text=f"/{quote}",
                anchor="w",
                fill=quote_color,
                font=(MONO, 11),
            )

    def _ensure_visible(self) -> None:
        canvas = self._canvas
        if canvas is None or not self._matches:
            return
        total = len(self._matches)
        if total <= self.MAX_VISIBLE:
            canvas.yview_moveto(0)
            return
        top_idx = int(canvas.yview()[0] * total)
        bot_idx = top_idx + self.MAX_VISIBLE - 1
        if self._active < top_idx:
            canvas.yview_moveto(self._active / total)
        elif self._active > bot_idx:
            canvas.yview_moveto((self._active - self.MAX_VISIBLE + 1) / total)

    def _show(self) -> None:
        self._ensure_popup()
        canvas = self._canvas
        popup = self._popup
        scroll = self._scroll
        if canvas is None or popup is None or scroll is None:
            return
        if not self._matches:
            self._hide()
            return
        self._active = min(self._active, len(self._matches) - 1)
        self.update_idletasks()
        width = max(self.winfo_width(), 260)
        visible_rows = min(self.MAX_VISIBLE, len(self._matches))
        height = visible_rows * self.ROW_H + 2
        if len(self._matches) > self.MAX_VISIBLE:
            scroll.pack(side="right", fill="y")
        else:
            scroll.pack_forget()
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height() + 2
        popup.geometry(f"{width}x{height}+{x}+{y}")
        popup.deiconify()
        popup.lift()
        popup.update_idletasks()
        self._draw_items()
        self._ensure_visible()
        self.focus_set()

    def _hide(self) -> None:
        if self._popup is not None:
            self._popup.withdraw()

    def _hide_event(self, _event: object = None):
        self._hide()
        return "break"

    def _move(self, delta: int):
        if not self._matches:
            return "break"
        if self._popup is None or not self._popup.winfo_viewable():
            self._show()
        self._active = max(0, min(len(self._matches) - 1, self._active + delta))
        self._ensure_visible()
        self._draw_items()
        self.focus_set()
        return "break"

    def _on_hover_motion(self, event: tk.Event) -> None:
        if self._canvas is None or not self._matches:
            return
        cy = self._canvas.canvasy(event.y)
        idx = int(cy // self.ROW_H)
        if 0 <= idx < len(self._matches) and idx != self._active:
            self._active = idx
            self._draw_items()

    def _on_wheel(self, event: tk.Event):
        if self._canvas is not None and len(self._matches) > self.MAX_VISIBLE:
            self._canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _choose(self, symbol: str) -> None:
        self.set(symbol)
        self.icursor("end")
        self.selection_clear()
        self._hide()
        self.focus_set()

    def _choose_active(self, _event: object = None):
        if self._popup is None or not self._popup.winfo_viewable() or not self._matches:
            return None
        self._choose(self._matches[self._active])
        return "break"

    def _click_list(self, event: tk.Event):
        if self._canvas is None or not self._matches:
            return "break"
        cy = self._canvas.canvasy(event.y)
        index = int(cy // self.ROW_H)
        if 0 <= index < len(self._matches):
            self._choose(self._matches[index])
        return "break"

    def _on_click(self, event: tk.Event):
        self.focus_set()
        arrow = 24
        if event.x >= self.winfo_width() - arrow:
            query = self.get().strip().upper()
            self._matches = self._all if not query else [symbol for symbol in self._all if symbol.startswith(query)]
            self._active = 0
            if self._popup is not None and self._popup.winfo_viewable():
                self._hide()
            elif self._matches:
                self._show()
        else:
            self.selection_clear()
            self.icursor(f"@{event.x}")
        return "break"

    def _on_double_click(self, event: tk.Event):
        self.focus_set()
        arrow = 24
        if event.x < self.winfo_width() - arrow:
            self.selection_range(0, "end")
            self.icursor("end")
        return "break"

    def _on_focus_out(self, _event: object) -> None:
        self.after(120, self._hide_if_outside)

    def _hide_if_outside(self) -> None:
        if self._popup is None or not self._popup.winfo_viewable():
            return
        x, y = self.winfo_pointerxy()
        widget = self.winfo_containing(x, y)
        if widget in {self, self._canvas, self._scroll, self._popup}:
            self.focus_set()
            return
        self._hide()


class DataTable(tk.Frame):
    HEAD_H = 34
    ROW_H = 44

    def __init__(
        self,
        master: tk.Misc,
        columns: tuple[tuple[str, str, int, str], ...],
        *,
        on_row_click=None,
        on_cell_click=None,
        action_cols: set[int] | None = None,
    ) -> None:
        super().__init__(master, bg=INPUT, highlightthickness=1, highlightbackground=BORDER)
        self._cols = columns
        self._on_row_click = on_row_click
        self._on_cell_click = on_cell_click
        self._action_cols = action_cols or set()
        self._rows: list[dict] = []
        self._selected: int | None = None
        self._hover: tuple[int | None, int | None] = (None, None)

        left = tk.Frame(self, bg=INPUT)
        left.pack(side="left", fill="both", expand=True)
        self._head = tk.Canvas(left, bg=MUTED, height=self.HEAD_H, highlightthickness=0, bd=0)
        self._head.pack(fill="x")
        tk.Frame(left, bg=BORDER, height=1).pack(fill="x")
        self._body = tk.Canvas(left, bg=INPUT, highlightthickness=0, bd=0)
        self._body.pack(fill="both", expand=True)
        self._scroll = ttk.Scrollbar(self, orient="vertical", command=self._body.yview)
        self._body.configure(yscrollcommand=self._scroll.set)
        self._scroll.pack(side="right", fill="y")

        self._head.bind("<Configure>", lambda _e: self._draw())
        self._body.bind("<Configure>", lambda _e: self._draw())
        self._body.bind("<ButtonPress-1>", self._handle_click)
        self._body.bind("<Motion>", self._handle_motion)
        self._body.bind("<Leave>", self._handle_leave)
        self._body.bind("<MouseWheel>", self._handle_wheel)

    def _spans(self, total_w: int) -> list[tuple[int, int]]:
        total_w = max(total_w, 300)
        weights = sum(col[2] for col in self._cols) or 1
        spans: list[tuple[int, int]] = []
        x = 0
        for idx, (_cid, _title, weight, _align) in enumerate(self._cols):
            if idx == len(self._cols) - 1:
                x1 = total_w
            else:
                x1 = x + int(total_w * weight / weights)
            spans.append((x, x1))
            x = x1
        return spans

    def set_rows(self, rows: list[dict]) -> None:
        self._rows = list(rows)
        if self._selected is not None and self._selected >= len(self._rows):
            self._selected = None
        self._draw()

    def _draw(self) -> None:
        w = max(self._body.winfo_width(), self._head.winfo_width(), 300)
        spans = self._spans(w)
        self._head.delete("all")
        self._head.create_rectangle(0, 0, w, self.HEAD_H, fill=MUTED, outline="")
        mid_h = self.HEAD_H // 2
        for idx, (_cid, title, _weight, align) in enumerate(self._cols):
            x0, x1 = spans[idx]
            tx = (x0 + 12) if align == "w" else ((x1 - 12) if align == "e" else (x0 + x1) // 2)
            self._head.create_text(tx, mid_h, text=title, anchor=align, fill=MUTED_FG, font=(SANS, 10, "bold"))

        self._body.delete("all")
        total_h = max(len(self._rows) * self.ROW_H, 1)
        self._body.configure(scrollregion=(0, 0, w, total_h))
        h_row, h_col = self._hover
        for r_idx, row in enumerate(self._rows):
            y0 = r_idx * self.ROW_H
            y1 = y0 + self.ROW_H
            is_sel = r_idx == self._selected
            is_hov = r_idx == h_row
            bg = "#353122" if is_sel else ("#262a33" if is_hov else ("#21242b" if r_idx % 2 else INPUT))
            self._body.create_rectangle(0, y0, w, y1, fill=bg, outline="")
            self._body.create_line(0, y1 - 1, w, y1 - 1, fill="#2e323a")
            values = row.get("values") or ()
            colors: dict[int, str] = row.get("colors") or {}
            buttons: dict[int, str] = row.get("buttons") or {}
            mid_y = y0 + self.ROW_H // 2
            for c_idx, (_cid, _title, _weight, align) in enumerate(self._cols):
                if c_idx >= len(values):
                    continue
                x0, x1 = spans[c_idx]
                text = str(values[c_idx])
                if c_idx in buttons:
                    kind = buttons[c_idx]
                    btn_Hover = is_hov and h_col == c_idx
                    if kind == "danger":
                        b_bg = "#522a2d" if btn_Hover else "#3a2426"
                        b_fg = SELL
                        b_bd = "#a14e48"
                    else:
                        b_bg = "#3e434d" if btn_Hover else SECONDARY
                        b_fg = FG
                        b_bd = BORDER
                    pad_x = 8
                    pad_y = 7
                    self._body.create_rectangle(
                        x0 + pad_x,
                        y0 + pad_y,
                        x1 - pad_x,
                        y1 - pad_y,
                        fill=b_bg,
                        outline=b_bd,
                    )
                    self._body.create_text(
                        (x0 + x1) // 2,
                        mid_y,
                        text=text,
                        anchor="center",
                        fill=b_fg,
                        font=(SANS, 12, "bold"),
                    )
                else:
                    fg = colors.get(c_idx, FG)
                    tx = (x0 + 12) if align == "w" else ((x1 - 12) if align == "e" else (x0 + x1) // 2)
                    self._body.create_text(tx, mid_y, text=text, anchor=align, fill=fg, font=(MONO, 14, "bold"))

    def _hit(self, event: tk.Event) -> tuple[int | None, int | None]:
        if not self._rows:
            return None, None
        cy = self._body.canvasy(event.y)
        r_idx = int(cy // self.ROW_H)
        if r_idx < 0 or r_idx >= len(self._rows):
            return None, None
        w = max(self._body.winfo_width(), 300)
        spans = self._spans(w)
        c_idx = None
        for idx, (x0, x1) in enumerate(spans):
            if x0 <= event.x <= x1:
                c_idx = idx
                break
        return r_idx, c_idx

    def _handle_motion(self, event: tk.Event) -> None:
        r_idx, c_idx = self._hit(event)
        new_hover = (r_idx, c_idx)
        if new_hover != self._hover:
            self._hover = new_hover
            self._draw()
        self._body.configure(cursor="hand2" if r_idx is not None and (c_idx in self._action_cols or self._on_row_click) else "")

    def _handle_leave(self, _event: tk.Event) -> None:
        if self._hover != (None, None):
            self._hover = (None, None)
            self._draw()
        self._body.configure(cursor="")

    def _handle_click(self, event: tk.Event) -> None:
        r_idx, c_idx = self._hit(event)
        if r_idx is None:
            return
        row = self._rows[r_idx]
        if c_idx in self._action_cols and self._on_cell_click is not None:
            self._on_cell_click(r_idx, c_idx, row)
            return
        self._selected = r_idx
        self._draw()
        if self._on_row_click is not None:
            self._on_row_click(r_idx, row)

    def _handle_wheel(self, event: tk.Event):
        if len(self._rows) * self.ROW_H > self._body.winfo_height():
            self._body.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"


class TradingApp:
    def __init__(self) -> None:
        enable_dpi()
        self.root = tk.Tk()
        self.root.title("Trading Bot")
        icon = ROOT / "icon.ico"
        if icon.exists():
            try:
                self.root.iconbitmap(str(icon))
            except tk.TclError:
                pass
        self.root.geometry("980x820")
        self.root.minsize(900, 700)

        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, name="binance", daemon=True)
        self.client: OrderClient | None = None
        self.connected = False
        self.busy = False
        self._multi_running = False
        self._stop_multi = False
        self._closing = False
        self._guard_task: asyncio.Task | None = None
        self._account_task: asyncio.Task | None = None
        self._book_task: asyncio.Task | None = None
        self._update_check_task: asyncio.Task | None = None
        self._update_available: str | None = None
        self._protected: dict[int, dict] = {}
        self._kept: list[dict] = []
        self._positions_by_item: dict[str, dict] = {}
        self._rules: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
        self.config = load_config()

        names = [item["name"] for item in self.config["apis"]]
        self.api_name = tk.StringVar(value=names[0] if names else "")
        self.market = tk.StringVar(value="Futures")
        self.position_side = tk.StringVar(value="BOTH")
        self.leverage = tk.StringVar(value="20")
        symbols = self.config["symbols"]
        saved_sym = str(self.config.get("last_symbol") or "").strip().upper()
        initial_sym = saved_sym if saved_sym in symbols else (symbols[0] if symbols else "")
        self.symbol = tk.StringVar(value=initial_sym)
        self.side = tk.StringVar(value="BUY")
        self.usdt = tk.StringVar(value="100")
        self.price = tk.StringVar(value="0.40")
        self.order_id = tk.StringVar()
        self.last_price = tk.StringVar(value="—")
        self.balance_text = tk.StringVar(value="—")
        self._balance_val: Decimal | None = None
        self.max_pos_text = tk.StringVar(value="Max: —")
        self.qty_text = tk.StringVar(value="0")
        self.base_text = tk.StringVar(value="")
        self.distance_text = tk.StringVar(value="")
        self._last_px: Decimal | None = None
        self.detail = tk.StringVar(value="Not connected")
        self.interval = tk.StringVar(value="1 sec")
        self.order_count = tk.StringVar(value="0")
        self.move_up_steps = tk.StringVar(value="1")
        self.move_down_steps = tk.StringVar(value="1")
        self.protection = tk.BooleanVar(value=True)
        self.protection_keep = tk.BooleanVar(value=False)
        self.protect_steps = tk.StringVar(value="3")

        self._build()
        self._reload_choices()
        self._refresh_buttons()
        self.thread.start()
        self._book_task = asyncio.run_coroutine_threadsafe(self._depth_loop(), self.loop)
        self._update_check_task = asyncio.run_coroutine_threadsafe(self._update_check_loop(), self.loop)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.log("Save an API, add coin pairs, choose a market, then Connect.")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _build(self) -> None:
        self._apply_theme()
        self.root.configure(bg=BG)
        self.root.geometry("1280x900")
        self.root.minsize(1160, 820)
        try:
            self.root.state("zoomed")
        except tk.TclError:
            pass

        shell = tk.Frame(self.root, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        shell.pack(fill="both", expand=True)

        header = tk.Frame(shell, bg=CARD)
        header.pack(fill="x")
        titles = tk.Frame(header, bg=CARD)
        titles.pack(side="left", padx=18, pady=12)
        tk.Label(titles, text="Trading Bot", bg=CARD, fg=FG, font=(SANS, 16, "bold")).pack(anchor="w")
        tk.Label(titles, textvariable=self.detail, bg=CARD, fg=MUTED_FG, font=(SANS, 10)).pack(anchor="w")
        live = tk.Frame(header, bg="#3a2428", highlightthickness=1, highlightbackground="#8a4540")
        live.pack(side="right", padx=(10, 18))
        tk.Label(live, text="●", bg="#3a2428", fg=SELL, font=(SANS, 8)).pack(side="left", padx=(8, 2), pady=4)
        self.mode_label = tk.Label(live, text="LIVE", bg="#3a2428", fg=SELL, font=(SANS, 9, "bold"))
        self.mode_label.pack(side="left", padx=(0, 8), pady=4)
        self.update_btn = self._btn(header, "Update", self.on_update)
        self.update_btn.pack(side="right")
        self.update_btn.bind("<ButtonPress-3>", lambda _e: self.open_update_window())
        ver_str = load_update_config().get("version") or "1.0.0"
        self.version_label = tk.Label(
            header, text=f"v{ver_str}", bg=CARD, fg=MUTED_FG, font=(MONO, 10, "bold")
        )
        self.version_label.pack(side="right", padx=(0, 8))
        tk.Frame(shell, bg=BORDER, height=1).pack(fill="x")

        account = tk.Frame(shell, bg=MUTED)
        account.pack(fill="x")
        account_row = tk.Frame(account, bg=MUTED)
        account_row.pack(fill="x", padx=18, pady=8)
        self._btn(account_row, "API", self.open_api_window).pack(side="left")
        self.api_box = ttk.Combobox(account_row, textvariable=self.api_name, state="readonly", width=18)
        self.api_box.pack(side="left", padx=(8, 12))
        tk.Label(account_row, text="Market", bg=MUTED, fg=MUTED_FG, font=(SANS, 10)).pack(side="left")
        self.market_box = ttk.Combobox(
            account_row, textvariable=self.market, values=("Spot", "Futures"), width=10, state="readonly"
        )
        self.market_box.pack(side="left", padx=(6, 12))
        self.market_box.bind("<<ComboboxSelected>>", lambda _event: self._sync_market_fields())
        self.connect_btn = self._btn(account_row, "Connect", self.on_connect_toggle, "primary")
        self.connect_btn.pack(side="left")
        self.badge = tk.Label(
            account_row,
            text="●  Disconnected",
            bg=SECONDARY,
            fg=MUTED_FG,
            font=(SANS, 10),
            padx=10,
            pady=4,
        )
        self.badge.pack(side="left", padx=(10, 0))
        balance = tk.Frame(account_row, bg=MUTED)
        balance.pack(side="right")
        tk.Label(balance, text="AVAILABLE", bg=MUTED, fg=MUTED_FG, font=(SANS, 8)).pack(anchor="e")
        balance_line = tk.Frame(balance, bg=MUTED)
        balance_line.pack(anchor="e")
        tk.Label(balance_line, textvariable=self.balance_text, bg=MUTED, fg=FG, font=(MONO, 11)).pack(side="left")
        tk.Label(balance_line, text=" USDT", bg=MUTED, fg=MUTED_FG, font=(SANS, 10)).pack(side="left")
        tk.Frame(shell, bg=BORDER, height=1).pack(fill="x")

        body = tk.Frame(shell, bg=BG)
        body.pack(fill="both", expand=True)
        left = tk.Frame(body, bg=CARD, width=520)
        left.pack(side="right", fill="y")
        left.pack_propagate(False)
        tk.Frame(body, bg=BORDER, width=1).pack(side="right", fill="y")
        book_panel = tk.Frame(body, bg=CARD, width=280)
        book_panel.pack(side="right", fill="y")
        book_panel.pack_propagate(False)
        tk.Frame(body, bg=BORDER, width=1).pack(side="right", fill="y")
        self._build_order_book(book_panel)
        right = tk.Frame(body, bg=CARD)
        right.pack(side="left", fill="both", expand=True)

        instrument = self._section(left, "Instrument")
        symbol_row = tk.Frame(instrument, bg=CARD)
        symbol_row.pack(fill="x")
        self._btn(symbol_row, "Coins", self.open_coins_window, big=True).pack(side="left")
        self.symbol_box = SymbolBox(symbol_row, textvariable=self.symbol, width=14, font=(MONO, 14, "bold"))
        self.symbol_box.pack(side="left", fill="x", expand=True, padx=8, ipady=3)
        self.side_box = ttk.Combobox(
            symbol_row, textvariable=self.side, values=("BUY", "SELL"), width=6, state="readonly", font=(SANS, 13, "bold")
        )
        self.side_box.pack(side="right", ipady=3)
        self.position_row = tk.Frame(instrument, bg=CARD)
        tk.Label(self.position_row, text="Position", bg=CARD, fg=MUTED_FG, font=(SANS, 13), width=8, anchor="w").pack(
            side="left", pady=(8, 0)
        )
        ttk.Combobox(
            self.position_row,
            textvariable=self.position_side,
            values=("BOTH", "LONG", "SHORT"),
            width=12,
            state="readonly",
            font=(SANS, 13),
        ).pack(side="left", fill="x", expand=True, pady=(8, 0), ipady=2)
        self.leverage_row = tk.Frame(instrument, bg=CARD)
        tk.Label(self.leverage_row, text="Leverage", bg=CARD, fg=MUTED_FG, font=(SANS, 13), width=8, anchor="w").pack(
            side="left", pady=(8, 0)
        )
        ttk.Combobox(
            self.leverage_row,
            textvariable=self.leverage,
            values=tuple(str(n) for n in range(1, 126)),
            width=5,
            state="readonly",
            font=(SANS, 13),
        ).pack(side="left", pady=(8, 0), ipady=2)
        tk.Label(self.leverage_row, text="x", bg=CARD, fg=MUTED_FG, font=(SANS, 13, "bold")).pack(
            side="left", padx=(6, 0), pady=(8, 0)
        )
        self.max_pos_label = tk.Label(
            self.leverage_row, textvariable=self.max_pos_text, bg=CARD, fg=MUTED_FG, font=(MONO, 12, "bold")
        )
        self.max_pos_label.pack(side="right", pady=(8, 0))

        size = self._section(left, "Size & price")
        presets = tk.Frame(size, bg=CARD)
        presets.pack(fill="x", pady=(0, 8))
        for idx, val in enumerate(("50000", "100000", "500000")):
            btn = tk.Button(
                presets,
                text=val,
                command=lambda v=val: self.usdt.set(v),
                relief="flat",
                bd=0,
                padx=12,
                pady=4,
                font=(MONO, 12, "bold"),
                cursor="hand2",
                bg=SECONDARY,
                fg=FG,
                activebackground="#3c4048",
                activeforeground=FG,
                highlightthickness=1,
                highlightbackground=BORDER,
                highlightcolor=BORDER,
            )
            btn.pack(side="left", padx=(0 if idx == 0 else 8, 0))
        amounts = tk.Frame(size, bg=CARD)
        amounts.pack(fill="x")
        amount_col = tk.Frame(amounts, bg=CARD)
        amount_col.pack(side="left", fill="x", expand=True, padx=(0, 6))
        price_col = tk.Frame(amounts, bg=CARD)
        price_col.pack(side="left", fill="x", expand=True, padx=(6, 0))
        tk.Label(amount_col, text="Amount", bg=CARD, fg=MUTED_FG, font=(SANS, 13)).pack(anchor="w")
        self.usdt_entry = self._entry(amount_col, self.usdt, big=True)
        self.usdt_entry.pack(fill="x", pady=(3, 0), ipady=4)
        tk.Label(price_col, text="Price", bg=CARD, fg=MUTED_FG, font=(SANS, 13)).pack(anchor="w")
        self._entry(price_col, self.price, big=True).pack(fill="x", pady=(3, 0), ipady=4)

        execution = self._section(left, "Execution")
        timing = tk.Frame(execution, bg=CARD)
        timing.pack(fill="x")
        time_col = tk.Frame(timing, bg=CARD)
        time_col.pack(side="left", fill="x", expand=True, padx=(0, 6))
        count_col = tk.Frame(timing, bg=CARD)
        count_col.pack(side="left", fill="x", expand=True, padx=(6, 0))
        tk.Label(time_col, text="Time", bg=CARD, fg=MUTED_FG, font=(SANS, 13)).pack(side="left")
        self.interval_box = ttk.Combobox(
            time_col,
            textvariable=self.interval,
            values=tuple(f"{n} sec" for n in range(1, 11)),
            width=7,
            state="readonly",
            font=(SANS, 13),
        )
        self.interval_box.pack(side="left", fill="x", expand=True, padx=(6, 0), ipady=2)
        tk.Label(count_col, text="Orders", bg=CARD, fg=MUTED_FG, font=(SANS, 13)).pack(side="left")
        self.count_box = ttk.Combobox(
            count_col,
            textvariable=self.order_count,
            values=("0",) + tuple(str(n) for n in range(2, 31)),
            width=5,
            state="readonly",
            font=(SANS, 13),
        )
        self.count_box.pack(side="left", fill="x", expand=True, padx=(6, 0), ipady=2)
        tk.Label(execution, text="0 = until balance runs out", bg=CARD, fg=MUTED_FG, font=(SANS, 11)).pack(
            anchor="w", pady=(6, 0)
        )
        actions = tk.Frame(execution, bg=CARD)
        actions.pack(fill="x", pady=(8, 0))
        self.place_btn = self._btn(actions, "Place limit", self.on_place, "buy", big=True)
        self.place_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.multi_btn = self._btn(actions, "Multi limit", self.on_multi, big=True)
        self.multi_btn.pack(side="left", fill="x", expand=True, padx=(6, 0))
        move_up = tk.Frame(execution, bg=CARD)
        move_up.pack(fill="x", pady=(8, 0))
        self.move_up_btn = self._btn(move_up, "Move up", lambda: self.on_move(1), big=True)
        self.move_up_btn.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.move_up_box = ttk.Combobox(
            move_up,
            textvariable=self.move_up_steps,
            values=tuple(str(n) for n in range(1, 51)),
            width=5,
            state="readonly",
            font=(SANS, 13),
        )
        self.move_up_box.pack(side="left", ipady=2)
        move_down = tk.Frame(execution, bg=CARD)
        move_down.pack(fill="x", pady=(8, 0))
        self.move_down_btn = self._btn(move_down, "Move down", lambda: self.on_move(-1), big=True)
        self.move_down_btn.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.move_down_box = ttk.Combobox(
            move_down,
            textvariable=self.move_down_steps,
            values=tuple(str(n) for n in range(1, 51)),
            width=5,
            state="readonly",
            font=(SANS, 13),
        )
        self.move_down_box.pack(side="left", ipady=2)

        protection = self._section(left, "Protection")
        checks = tk.Frame(protection, bg=CARD)
        checks.pack(fill="x")
        ttk.Checkbutton(
            checks, text="Protection", variable=self.protection, command=self._on_protection, style="Big.TCheckbutton"
        ).pack(side="left")
        ttk.Checkbutton(
            checks,
            text="Protection and keep",
            variable=self.protection_keep,
            command=self._on_protection_keep,
            style="Big.TCheckbutton",
        ).pack(side="left", padx=(14, 0))
        steps = tk.Frame(protection, bg=CARD)
        steps.pack(fill="x", pady=(8, 0))
        self.steps_box = ttk.Combobox(
            steps,
            textvariable=self.protect_steps,
            values=tuple(str(n) for n in range(1, 51)),
            width=5,
            state="disabled",
            font=(SANS, 13),
        )
        self.steps_box.pack(side="left", ipady=2)
        tk.Label(steps, text="steps from the market", bg=CARD, fg=MUTED_FG, font=(SANS, 13)).pack(
            side="left", padx=(10, 0)
        )

        cancel = self._section(left, "Cancel", rule=False)
        cancel_row = tk.Frame(cancel, bg=CARD)
        cancel_row.pack(fill="x")
        self.cancel_all_btn = self._btn(cancel_row, "Cancel all", self.on_cancel_all, "danger", big=True)
        self.cancel_all_btn.configure(font=(SANS, 15, "bold"), pady=8)
        self.cancel_all_btn.pack(fill="x", expand=True)

        orders_pane = tk.Frame(right, bg=CARD)
        orders_pane.pack(fill="both", expand=True)
        orders_head = tk.Frame(orders_pane, bg=CARD)
        orders_head.pack(fill="x", padx=16, pady=(10, 4))
        tk.Label(orders_head, text="Open orders", bg=CARD, fg=FG, font=(SANS, 14, "bold")).pack(side="left")
        self.order_count_badge = tk.Label(orders_head, text="0", bg=SECONDARY, fg=MUTED_FG, font=(MONO, 12, "bold"), padx=8)
        self.order_count_badge.pack(side="left", padx=(8, 0))
        self.refresh_btn = self._btn(orders_head, "Refresh", self.on_refresh, "ghost")
        self.refresh_btn.pack(side="right")
        self.tree = DataTable(
            orders_pane,
            (
                ("id", "ORDER ID", 150, "w"),
                ("symbol", "SYMBOL", 115, "w"),
                ("side", "SIDE", 75, "w"),
                ("position", "POSITION", 95, "w"),
                ("price", "PRICE", 115, "e"),
                ("qty", "QUANTITY", 115, "e"),
                ("status", "STATUS", 95, "w"),
                ("action", "CANCEL", 110, "center"),
            ),
            on_row_click=self._on_order_row_select,
            on_cell_click=self._on_order_cell_action,
            action_cols={7},
        )
        self.tree.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        self.empty_orders = tk.Label(self.tree, text="Connect to load open orders", bg=INPUT, fg=MUTED_FG, font=(SANS, 13))
        self.empty_orders.place(relx=0.5, rely=0.58, anchor="center")
        self.empty_orders.lift()

        tk.Frame(right, bg=BORDER, height=1).pack(fill="x")
        pos_pane = tk.Frame(right, bg=CARD)
        pos_pane.pack(fill="both", expand=True)
        pos_head = tk.Frame(pos_pane, bg=CARD)
        pos_head.pack(fill="x", padx=16, pady=(8, 4))
        tk.Label(pos_head, text="Open positions", bg=CARD, fg=FG, font=(SANS, 14, "bold")).pack(side="left")
        self.pos_count_badge = tk.Label(pos_head, text="0", bg=SECONDARY, fg=MUTED_FG, font=(MONO, 12, "bold"), padx=8)
        self.pos_count_badge.pack(side="left", padx=(8, 0))
        self.pos_refresh_btn = self._btn(pos_head, "Refresh", self.on_refresh, "ghost")
        self.pos_refresh_btn.pack(side="right")
        self.pos_tree = DataTable(
            pos_pane,
            (
                ("symbol", "SYMBOL", 115, "w"),
                ("side", "SIDE", 85, "w"),
                ("size", "SIZE (USDT)", 130, "e"),
                ("entry", "ENTRY", 115, "e"),
                ("mark", "MARK", 115, "e"),
                ("pnl", "PNL (USDT)", 125, "e"),
                ("limit_close", "LIMIT", 100, "center"),
                ("market_close", "MARKET", 105, "center"),
            ),
            on_cell_click=self._on_position_cell_action,
            action_cols={6, 7},
        )
        self.pos_tree.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        self.empty_positions = tk.Label(
            self.pos_tree, text="Connect to load open positions", bg=INPUT, fg=MUTED_FG, font=(SANS, 13)
        )
        self.empty_positions.place(relx=0.5, rely=0.58, anchor="center")
        self.empty_positions.lift()

        tk.Frame(right, bg=BORDER, height=1).pack(fill="x")
        activity_head = tk.Frame(right, bg=CARD)
        activity_head.pack(fill="x", padx=16, pady=(6, 4))
        tk.Label(activity_head, text="Activity", bg=CARD, fg=FG, font=(SANS, 11, "bold")).pack(side="left")
        self._btn(activity_head, "Clear", self._clear_log, "ghost").pack(side="right")
        log_wrap = tk.Frame(right, bg=INPUT, highlightthickness=1, highlightbackground=BORDER)
        log_wrap.pack(fill="x", padx=16, pady=(0, 10))
        self.output = ScrolledText(
            log_wrap,
            height=7,
            font=(MONO, 10),
            state="disabled",
            wrap="word",
            bg=INPUT,
            fg=FG,
            insertbackground=FG,
            relief="flat",
            borderwidth=0,
        )
        self.output.pack(fill="both", expand=True, padx=4, pady=4)
        self.output.tag_configure("time", foreground=MUTED_FG)
        self.output.tag_configure("info", foreground=FG)
        self.output.tag_configure("ok", foreground=BUY)
        self.output.tag_configure("warn", foreground=PRIMARY)
        self.output.tag_configure("err", foreground=SELL)
        self._sync_market_fields()
        self._sync_quote()
        self.usdt.trace_add("write", self._sync_quote)
        self.price.trace_add("write", self._sync_quote)
        self.symbol.trace_add("write", self._sync_quote)
        self.leverage.trace_add("write", self._sync_quote)
        self.order_id.trace_add("write", lambda *_args: self._refresh_buttons())
        self.side.trace_add("write", lambda *_args: self._refresh_buttons())

    def _build_order_book(self, parent: tk.Frame) -> None:
        head = tk.Frame(parent, bg=CARD)
        head.pack(fill="x", padx=12, pady=(12, 6))
        tk.Label(head, text="ORDER BOOK", bg=CARD, fg=MUTED_FG, font=(SANS, 10, "bold")).pack(side="left")
        self.book_sym_label = tk.Label(head, textvariable=self.symbol, bg=CARD, fg=FG, font=(MONO, 10, "bold"))
        self.book_sym_label.pack(side="right")

        cols = tk.Frame(parent, bg=MUTED)
        cols.pack(fill="x", padx=12)
        tk.Label(cols, text="PRICE", bg=MUTED, fg=MUTED_FG, font=(SANS, 8, "bold"), anchor="w").pack(
            side="left", padx=6, pady=4
        )
        tk.Label(cols, text="USDT", bg=MUTED, fg=MUTED_FG, font=(SANS, 8, "bold"), anchor="e").pack(
            side="right", padx=6, pady=4
        )

        self.ask_rows: list[tuple[tk.Frame, tk.Label, tk.Label]] = []
        asks_wrap = tk.Frame(parent, bg=INPUT, highlightthickness=1, highlightbackground=BORDER)
        asks_wrap.pack(fill="x", padx=12, pady=(4, 0))
        for _ in range(12):
            row = tk.Frame(asks_wrap, bg=INPUT, cursor="hand2")
            row.pack(fill="x", padx=6, pady=2)
            px = tk.Label(row, text="—", bg=INPUT, fg=SELL, font=(MONO, 11, "bold"), anchor="w", cursor="hand2")
            px.pack(side="left")
            tot = tk.Label(row, text="", bg=INPUT, fg=FG, font=(MONO, 10), anchor="e", width=13, cursor="hand2")
            tot.pack(side="right")
            for w in (row, px, tot):
                w.bind("<ButtonPress-1>", lambda _e, p=px: self._pick_book_price(p))
                w.bind("<Enter>", lambda _e, r=row, ws=(row, px, tot): [x.configure(bg=MUTED) for x in ws])
                w.bind("<Leave>", lambda _e, r=row, ws=(row, px, tot): [x.configure(bg=INPUT) for x in ws])
            self.ask_rows.append((row, px, tot))

        mid_bar = tk.Frame(parent, bg=SECONDARY, highlightthickness=1, highlightbackground=BORDER, cursor="hand2")
        mid_bar.pack(fill="x", padx=12, pady=6)
        self.book_mid_label = tk.Label(
            mid_bar, text="—", bg=SECONDARY, fg=FG, font=(MONO, 13, "bold"), cursor="hand2"
        )
        self.book_mid_label.pack(side="left", padx=8, pady=5)
        self.book_spread_label = tk.Label(
            mid_bar, text="", bg=SECONDARY, fg=MUTED_FG, font=(MONO, 9), cursor="hand2"
        )
        self.book_spread_label.pack(side="right", padx=8, pady=5)
        for w in (mid_bar, self.book_mid_label, self.book_spread_label):
            w.bind("<ButtonPress-1>", lambda _e: self._pick_book_price(self.book_mid_label))

        self.bid_rows: list[tuple[tk.Frame, tk.Label, tk.Label]] = []
        bids_wrap = tk.Frame(parent, bg=INPUT, highlightthickness=1, highlightbackground=BORDER)
        bids_wrap.pack(fill="x", padx=12, pady=(0, 12))
        for _ in range(12):
            row = tk.Frame(bids_wrap, bg=INPUT, cursor="hand2")
            row.pack(fill="x", padx=6, pady=2)
            px = tk.Label(row, text="—", bg=INPUT, fg=BUY, font=(MONO, 11, "bold"), anchor="w", cursor="hand2")
            px.pack(side="left")
            tot = tk.Label(row, text="", bg=INPUT, fg=FG, font=(MONO, 10), anchor="e", width=13, cursor="hand2")
            tot.pack(side="right")
            for w in (row, px, tot):
                w.bind("<ButtonPress-1>", lambda _e, p=px: self._pick_book_price(p))
                w.bind("<Enter>", lambda _e, r=row, ws=(row, px, tot): [x.configure(bg=MUTED) for x in ws])
                w.bind("<Leave>", lambda _e, r=row, ws=(row, px, tot): [x.configure(bg=INPUT) for x in ws])
            self.bid_rows.append((row, px, tot))

    def _pick_book_price(self, label: tk.Label) -> None:
        val = label.cget("text").strip().replace(",", "")
        if val and val != "—" and DECIMAL.fullmatch(val):
            self.price.set(dec_str(Decimal(val)))

    def _apply_theme(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", background=CARD, foreground=FG)
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED_FG)
        style.configure("TFrame", background=CARD)
        style.configure("TButton", background=SECONDARY, foreground=FG, bordercolor=BORDER, focuscolor=CARD)
        style.map("TButton", background=[("active", MUTED), ("disabled", "#2a2d33")], foreground=[("disabled", MUTED_FG)])
        style.configure("TEntry", fieldbackground=INPUT, foreground=FG, bordercolor=BORDER, insertcolor=FG)
        style.configure(
            "TCombobox",
            fieldbackground=INPUT,
            background=SECONDARY,
            foreground=FG,
            arrowcolor=MUTED_FG,
            bordercolor=BORDER,
            lightcolor=INPUT,
            darkcolor=INPUT,
            padding=4,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", INPUT), ("disabled", "#26282d")],
            foreground=[("disabled", MUTED_FG), ("readonly", FG)],
            background=[("readonly", SECONDARY), ("disabled", "#26282d")],
        )
        style.configure(
            "Treeview",
            background=INPUT,
            fieldbackground=INPUT,
            foreground=FG,
            borderwidth=0,
            rowheight=46,
            font=(MONO, 15, "bold"),
        )
        style.configure(
            "Treeview.Heading",
            background=MUTED,
            foreground=MUTED_FG,
            relief="flat",
            padding=(6, 8),
            font=(SANS, 11, "bold"),
        )
        style.map("Treeview", background=[("selected", "#3d3824")], foreground=[("selected", FG)])
        style.configure("TCheckbutton", background=CARD, foreground=FG)
        style.map("TCheckbutton", background=[("active", CARD)])
        style.configure("Big.TCheckbutton", background=CARD, foreground=FG, font=(SANS, 13))
        style.map("Big.TCheckbutton", background=[("active", CARD)])
        self.root.option_add("*TCombobox*Listbox.font", (SANS, 12))
        style.configure(
            "Vertical.TScrollbar",
            background=SECONDARY,
            troughcolor=INPUT,
            bordercolor=INPUT,
            arrowcolor=MUTED_FG,
        )

    def _section(self, parent: tk.Frame, title: str, rule: bool = True) -> tk.Frame:
        wrap = tk.Frame(parent, bg=CARD)
        wrap.pack(fill="x")
        tk.Label(wrap, text=title.upper(), bg=CARD, fg=MUTED_FG, font=(SANS, 10, "bold")).pack(
            anchor="w", padx=16, pady=(6, 2)
        )
        body = tk.Frame(wrap, bg=CARD)
        body.pack(fill="x", padx=16, pady=(0, 8))
        if rule:
            tk.Frame(parent, bg=BORDER, height=1).pack(fill="x")
        return body

    def _btn(self, parent: tk.Misc, text: str, command, kind: str = "default", big: bool = False) -> tk.Button:
        button = tk.Button(
            parent,
            text=text,
            command=command,
            relief="flat",
            bd=0,
            padx=12 if big else 10,
            pady=4 if big else 4,
            font=(SANS, 13, "bold") if big else (SANS, 10),
            cursor="hand2",
            highlightthickness=1,
        )
        button._kind = kind  # type: ignore[attr-defined]
        self._paint(button, kind)
        return button

    def _paint(self, button: tk.Button, kind: str | None = None) -> None:
        kind = kind or getattr(button, "_kind", "default")
        colors = {
            "default": (SECONDARY, FG, "#3c4048", BORDER),
            "primary": (PRIMARY, PRIMARY_FG, "#c9a24a", PRIMARY),
            "buy": (BUY, "#10241c", "#34b87e", BUY),
            "sell": (SELL, "#fff7f6", "#c94e49", SELL),
            "danger": ("#3a2426", SELL, "#4a2c2e", "#8a4540"),
            "ghost": (CARD, MUTED_FG, MUTED, CARD),
        }
        bg, fg, active, edge = colors[kind]
        disabled = str(button.cget("state")) == "disabled"
        if disabled:
            bg, fg, active, edge = "#2a2d33", "#6b7078", "#2a2d33", "#2a2d33"
        button.configure(
            bg=bg,
            fg=fg,
            activebackground=active,
            activeforeground=fg,
            disabledforeground="#6b7078",
            highlightbackground=edge,
            highlightcolor=edge,
        )

    def _entry(self, parent: tk.Misc, variable: tk.StringVar, width: int = 12, big: bool = False) -> tk.Entry:
        return tk.Entry(
            parent,
            textvariable=variable,
            width=width,
            bg=INPUT,
            fg=FG,
            insertbackground=FG,
            relief="flat",
            font=(MONO, 14, "bold") if big else (MONO, 10),
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=PRIMARY,
        )

    def _sync_quote(self, *_args: object) -> None:
        max_pos: Decimal | None = None
        if self._balance_val is not None:
            try:
                lev = Decimal(self.leverage.get().strip())
                max_pos = self._balance_val * lev
                self.max_pos_text.set(f"Max: {max_pos:,.0f} USDT")
            except Exception:
                self.max_pos_text.set("Max: —")
        else:
            self.max_pos_text.set("Max: —")
        over_max = False
        try:
            amt = Decimal(self.usdt.get().strip())
            if self.market.get() == "Futures" and max_pos is not None and amt > max_pos:
                over_max = True
            elif self.market.get() == "Spot" and self._balance_val is not None and amt > self._balance_val:
                over_max = True
        except Exception:
            pass
        self.max_pos_label.configure(fg=SELL if over_max else MUTED_FG)
        self.usdt_entry.configure(
            fg=SELL if over_max else FG,
            highlightbackground=SELL if over_max else BORDER,
            highlightcolor=SELL if over_max else PRIMARY,
        )
        symbol = self._symbol()
        if symbol in self.config.get("symbols", []) and self.config.get("last_symbol") != symbol:
            self.config["last_symbol"] = symbol
            try:
                save_config(self.config)
            except Exception:
                pass
        base, _quote = _split_symbol(symbol) if symbol else ("", "")
        self.base_text.set(f" {base}" if base else "")
        try:
            usdt = Decimal(self.usdt.get().strip())
            price = Decimal(self.price.get().strip())
        except Exception:
            self.qty_text.set("0")
            self.distance_text.set("")
            return
        market = "futures" if self.market.get() == "Futures" else "spot"
        rules = self._rules.get((market, symbol))
        if price > 0 and usdt > 0 and rules:
            qty = quote_to_base(usdt, price, rules[1])
        elif price > 0 and usdt > 0:
            qty = floor_step(usdt / price, Decimal("0.0001"))
        else:
            qty = Decimal(0)
        self.qty_text.set(dec_str(qty))

    def _clear_log(self) -> None:
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")

    def _on_protection(self) -> None:
        if self.protection.get():
            self.protection_keep.set(False)
        self._refresh_buttons()

    def _on_protection_keep(self) -> None:
        if self.protection_keep.get():
            self.protection.set(False)
        self._refresh_buttons()

    def _sync_market_fields(self) -> None:
        if self.market.get() == "Futures":
            self.position_row.pack(fill="x")
            self.leverage_row.pack(fill="x")
        else:
            self.position_row.pack_forget()
            self.leverage_row.pack_forget()
        self._sync_quote()

    def _reload_choices(self) -> None:
        names = [item["name"] for item in self.config["apis"]]
        self.api_box["values"] = names
        if names and self.api_name.get() not in names:
            self.api_name.set(names[0])
        if not names:
            self.api_name.set("")
        self.symbol_box.set_symbols(self.config["symbols"])
        if self.config["symbols"] and self.symbol.get().strip().upper() not in self.config["symbols"]:
            self.symbol.set(self.config["symbols"][0])

    def log(self, text: str, level: str = "info") -> None:
        stamp = time.strftime("%H:%M:%S")
        tag = {"success": "ok", "warn": "warn", "error": "err"}.get(level, "info")
        self.output.configure(state="normal")
        self.output.insert("end", f"{stamp}  ", "time")
        self.output.insert("end", f"{text}\n", tag)
        self.output.see("end")
        self.output.configure(state="disabled")

    def _set_badge(self, connected: bool) -> None:
        if connected:
            self.badge.configure(text="●  Connected", bg="#17352c", fg=BUY)
        else:
            self.badge.configure(text="●  Disconnected", bg=SECONDARY, fg=MUTED_FG)

    def _refresh_buttons(self) -> None:
        trade = "normal" if self.connected and not self.busy else "disabled"
        for button in (self.place_btn, self.cancel_all_btn, self.refresh_btn, self.pos_refresh_btn):
            button.configure(state=trade)
        move_state = "normal" if self.connected and not self.busy and not self._multi_running else "disabled"
        self.move_up_btn.configure(state=move_state)
        self.move_down_btn.configure(state=move_state)
        if self._multi_running and self.connected and not self.busy:
            self.multi_btn.configure(state="normal", text="Stop")
            self.multi_btn._kind = "danger"  # type: ignore[attr-defined]
        else:
            self.multi_btn.configure(
                state="normal" if self.connected and not self.busy else "disabled",
                text="Multi limit",
            )
            self.multi_btn._kind = "default"  # type: ignore[attr-defined]
        side_kind = "buy" if self.side.get() == "BUY" else "sell"
        self.place_btn._kind = side_kind  # type: ignore[attr-defined]
        for button in (
            self.place_btn,
            self.multi_btn,
            self.move_up_btn,
            self.move_down_btn,
            self.cancel_all_btn,
            self.refresh_btn,
            self.pos_refresh_btn,
            self.connect_btn,
        ):
            self._paint(button)
        self.connect_btn.configure(
            state="disabled" if self.busy else "normal",
            text="Disconnect" if self.connected else "Connect",
        )
        self.update_btn.configure(
            state="disabled" if self.busy else "normal",
            text=f"●  Update (v{self._update_available})" if self._update_available else "Update",
        )
        self._paint(self.update_btn)
        if self._update_available and not self.busy:
            self.update_btn.configure(
                bg="#17352c",
                fg=BUY,
                activebackground="#1f463a",
                activeforeground=BUY,
                highlightbackground=BUY,
                highlightcolor=BUY,
            )
        self.connect_btn._kind = "default" if self.connected else "primary"  # type: ignore[attr-defined]
        self._paint(self.connect_btn)
        locked = "disabled" if self.connected or self.busy else "readonly"
        self.api_box.configure(state=locked)
        self.market_box.configure(state=locked)
        running = "disabled" if self._multi_running else "readonly"
        self.interval_box.configure(state=running)
        self.count_box.configure(state=running)
        protecting = bool(self.protection.get() or self.protection_keep.get())
        self.steps_box.configure(state="readonly" if protecting else "disabled")

    def spawn(self, coro, ok) -> None:
        if self.busy:
            return
        self.busy = True
        self._refresh_buttons()
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def done(pending: asyncio.Future) -> None:
            try:
                value = pending.result()
            except Exception as exc:
                self.root.after(0, lambda exc=exc: self._failed(exc))
            else:
                self.root.after(0, lambda value=value: self._finished(value, ok))

        future.add_done_callback(done)

    def _finished(self, value, ok) -> None:
        self.busy = False
        ok(value)

    def _failed(self, exc: Exception) -> None:
        self.busy = False
        self.log(str(exc))
        self._refresh_buttons()

    def _selected_api(self) -> dict | None:
        name = self.api_name.get().strip()
        for item in self.config["apis"]:
            if item["name"] == name:
                return item
        return None

    def _symbol(self) -> str:
        return self.symbol.get().strip().upper()

    def _trade_spec(self) -> dict | None:
        symbol = self._symbol()
        usdt = self.usdt.get().strip()
        price = self.price.get().strip()
        if symbol not in self.config["symbols"]:
            self.log("Choose a coin pair. Use Coins to add one.")
            return None
        if not DECIMAL.fullmatch(usdt):
            self.log("USDT must look like 100 or 100.50")
            return None
        if not DECIMAL.fullmatch(price) or Decimal(price) <= 0:
            self.log("Price must look like 0.40")
            return None
        return {
            "symbol": symbol,
            "side": self.side.get().upper(),
            "usdt": Decimal(usdt),
            "price": Decimal(price),
            "price_text": price,
            "position": self.position_side.get() if self.market.get() == "Futures" else None,
            "leverage": int(self.leverage.get()) if self.market.get() == "Futures" else None,
            "market": "futures" if self.market.get() == "Futures" else "spot",
        }

    def open_api_window(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("API")
        window.transient(self.root)
        window.resizable(False, False)
        window.configure(bg=CARD)
        frame = tk.Frame(window, bg=CARD, padx=16, pady=16)
        frame.pack(fill="both", expand=True)
        name = tk.StringVar()
        key = tk.StringVar()
        secret = tk.StringVar()
        notice = tk.StringVar()
        editing = {"name": ""}

        ttk.Label(frame, text="Saved APIs").grid(row=0, column=0, columnspan=2, sticky="w")
        listed = tk.Listbox(
            frame,
            height=6,
            width=42,
            bg=INPUT,
            fg=FG,
            selectbackground="#3d3824",
            selectforeground=FG,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            font=(MONO, 10),
        )
        listed.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        def fill_list() -> None:
            listed.delete(0, "end")
            for item in self.config["apis"]:
                listed.insert("end", item["name"])

        fill_list()

        def load_selected() -> None:
            selected = listed.curselection()
            if not selected:
                notice.set("Choose an API.")
                return
            label = listed.get(selected[0])
            profile = next(item for item in self.config["apis"] if item["name"] == label)
            editing["name"] = label
            name.set(profile["name"])
            key.set(profile["api_key"])
            secret.set(profile["secret"])
            notice.set(f"Editing {label}.")

        def delete_selected() -> None:
            selected = listed.curselection()
            if not selected:
                notice.set("Choose an API.")
                return
            label = listed.get(selected[0])
            self.config["apis"] = [item for item in self.config["apis"] if item["name"] != label]
            save_config(self.config)
            if editing["name"] == label:
                editing["name"] = ""
                name.set("")
                key.set("")
                secret.set("")
            if self.api_name.get() == label:
                self.api_name.set("")
            self._reload_choices()
            fill_list()
            self.log(f"Deleted API {label}.")
            notice.set(f"Deleted {label}.")

        for row, (label, variable, hidden) in enumerate(
            (("Name", name, False), ("API key", key, False), ("Security", secret, True))
        ):
            ttk.Label(frame, text=label).grid(row=row + 3, column=0, sticky="w", pady=4, padx=(0, 8))
            ttk.Entry(frame, textvariable=variable, width=42, show="*" if hidden else "").grid(
                row=row + 3, column=1, sticky="ew", pady=4
            )

        def save() -> None:
            label = name.get().strip()
            api_key = key.get().strip()
            api_secret = secret.get().strip()
            if not label or not api_key or not api_secret:
                notice.set("Enter a name, API key, and security.")
                return
            previous = editing["name"]
            apis = [
                item
                for item in self.config["apis"]
                if item["name"] != label and item["name"] != previous
            ]
            apis.append({"name": label, "api_key": api_key, "secret": api_secret})
            self.config["apis"] = apis
            save_config(self.config)
            self.api_name.set(label)
            self._reload_choices()
            self.log(f"Saved API {label}.")
            window.destroy()

        actions = ttk.Frame(frame)
        actions.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 8))
        ttk.Button(actions, text="Edit", command=load_selected).pack(side="left")
        ttk.Button(actions, text="Delete", command=delete_selected).pack(side="left", padx=(8, 0))
        listed.bind("<Double-Button-1>", lambda _event: load_selected())
        ttk.Button(frame, text="Save", command=save).grid(row=6, column=1, sticky="w", pady=(8, 0))
        ttk.Label(frame, textvariable=notice, style="Muted.TLabel").grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

    def open_coins_window(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Coin pairs")
        window.transient(self.root)
        window.resizable(False, False)
        window.configure(bg=CARD)
        frame = tk.Frame(window, bg=CARD, padx=16, pady=16)
        frame.pack(fill="both", expand=True)
        symbol = tk.StringVar()
        notice = tk.StringVar()
        ttk.Label(frame, text="Pair").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(frame, textvariable=symbol, width=20)
        entry.grid(row=0, column=1, sticky="w", padx=(8, 8))
        listed = tk.Listbox(
            frame,
            height=8,
            width=24,
            bg=INPUT,
            fg=FG,
            selectbackground="#3d3824",
            selectforeground=FG,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            font=(MONO, 10),
        )
        listed.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        for item in self.config["symbols"]:
            listed.insert("end", item)

        def add(_event: object = None) -> None:
            pair = symbol.get().strip().upper()
            if not SYMBOL.fullmatch(pair):
                notice.set("Use a pair like BTCUSDT.")
                return
            current = list(listed.get(0, "end"))
            if pair not in current:
                listed.insert("end", pair)
            symbol.set("")
            notice.set("")

        def remove() -> None:
            selected = list(listed.curselection())
            for index in reversed(selected):
                listed.delete(index)

        def save() -> None:
            self.config["symbols"] = list(listed.get(0, "end"))
            save_config(self.config)
            self._reload_choices()
            self.log(f"Saved {len(self.config['symbols'])} coin pair(s).")
            window.destroy()

        entry.bind("<Return>", add)
        ttk.Button(frame, text="Add", command=add).grid(row=0, column=2, sticky="w")
        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(buttons, text="Remove", command=remove).pack(side="left")
        ttk.Button(buttons, text="Save", command=save).pack(side="left", padx=(8, 0))
        ttk.Label(frame, textvariable=notice, style="Muted.TLabel").grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def open_update_window(self) -> None:
        cfg = load_update_config()
        window = tk.Toplevel(self.root)
        window.title("Update source")
        window.transient(self.root)
        window.resizable(False, False)
        window.configure(bg=CARD)
        frame = tk.Frame(window, bg=CARD, padx=16, pady=16)
        frame.pack(fill="both", expand=True)
        repo_var = tk.StringVar(value=cfg.get("repo", "bidmasterge/TradingBot"))
        branch_var = tk.StringVar(value=cfg.get("branch", "main"))
        token_var = tk.StringVar(value=cfg.get("token", ""))
        for row, (label, var, hidden) in enumerate(
            (
                ("GitHub repo (owner/name)", repo_var, False),
                ("Branch", branch_var, False),
                ("Token (optional, if private)", token_var, True),
            )
        ):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 8))
            ttk.Entry(frame, textvariable=var, width=36, show="*" if hidden else "").grid(
                row=row, column=1, sticky="ew", pady=4
            )

        def save() -> None:
            save_update_config(
                {
                    "repo": repo_var.get().strip() or "bidmasterge/TradingBot",
                    "branch": branch_var.get().strip() or "main",
                    "version": cfg.get("version") or "1.0.0",
                    "token": token_var.get().strip(),
                }
            )
            self.log(f"Update source saved: {repo_var.get().strip()}", "success")
            window.destroy()

        ttk.Button(frame, text="Save", command=save).grid(row=3, column=1, sticky="e", pady=(8, 0))

    def on_update(self) -> None:
        cfg = load_update_config()
        self.log(f"Checking for updates from {cfg.get('repo')}...")
        self.spawn(asyncio.to_thread(pull_latest_from_github), self._on_updated)

    def _on_updated(self, count: int) -> None:
        self.log(f"Updated {count} file(s). Restarting app...", "success")
        self._refresh_buttons()
        self.root.after(400, self._restart_app)

    def _restart_app(self) -> None:
        try:
            subprocess.Popen(
                [sys.executable, str(ROOT / "desktop.py")],
                cwd=str(ROOT),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            self.log(f"Restart failed: {exc}", "error")
            return
        self.on_close()

    def on_connect_toggle(self) -> None:
        if self.connected:
            self.spawn(self._disconnect_ws(), self._on_disconnected)
            return
        profile = self._selected_api()
        if profile is None:
            self.log("Choose an API. Use the API button to save one.")
            return
        market = "futures" if self.market.get() == "Futures" else "spot"
        self.log(f"Connecting to live {self.market.get()} with {profile['name']}...")
        self.spawn(self._connect_ws(profile["api_key"], profile["secret"], market), self._on_connected)

    def _on_connected(self, info: dict) -> None:
        self.connected = True
        self._set_badge(True)
        market_name = "Futures" if info["market"] == "futures" else "Spot"
        self.detail.set(f"Connected to Binance {market_name} as {self.api_name.get()}")
        self.log(f"Connected to live {info['market']}. Clock offset {info['offset']} ms.", "success")
        if info["market"] == "futures":
            self.log("This API key must have Futures enabled. Use BOTH unless the account is in hedge mode.")
        self._refresh_buttons()
        self.spawn(self._open(), self._show_orders)

    def _on_disconnected(self, _value: object) -> None:
        self.connected = False
        self._set_badge(False)
        self.detail.set("Not connected")
        self.balance_text.set("—")
        self._balance_val = None
        self._last_px = None
        self.last_price.set("—")
        self.distance_text.set("")
        self._show_positions([])
        self._render_order_rows([])
        self._sync_quote()
        self.log("Disconnected.", "warn")
        self._refresh_buttons()

    def on_price(self) -> None:
        symbol = self._symbol()
        if not symbol:
            self.log("Choose a symbol.")
            return
        self.spawn(self._price(symbol), self._show_price)

    def _show_price(self, price: str) -> None:
        self._last_px = Decimal(price)
        self.last_price.set(price)
        self.price.set(dec_str(Decimal(price)))
        self._sync_quote()
        self.log(f"{self._symbol()} last price {price}", "success")
        self._refresh_buttons()

    def on_place(self) -> None:
        spec = self._trade_spec()
        if spec is None:
            return
        keep = bool(self.protection_keep.get())
        protect = bool(self.protection.get()) or keep
        steps = int(self.protect_steps.get())
        self.spawn(self._place_one(spec, protect, steps, keep), self._on_placed)

    def on_move(self, direction: int) -> None:
        symbol = self._symbol()
        if symbol not in self.config["symbols"]:
            self.log("Choose a coin pair. Use Coins to add one.")
            return
        steps = int(self.move_up_steps.get() if direction > 0 else self.move_down_steps.get())
        word = "up" if direction > 0 else "down"
        self.log(f"Moving open {symbol} orders {word} by {steps} steps.")
        self.spawn(self._move_orders(symbol, direction, steps), self._on_moved)

    def _on_moved(self, info: dict) -> None:
        word = "up" if info["direction"] > 0 else "down"
        if info["empty"]:
            self.log(f"No open {info['symbol']} orders to move.")
        else:
            self.log(
                f"Moved {info['moved']} order(s) {word} by {info['steps']} steps ({dec_str(info['tick'])})."
            )
            if info["failed"]:
                self.log(f"{info['failed']} order(s) stayed at the old price.", "warn")
        self._show_orders(info["rows"], announce=False)
        self.root.after(0, self._request_balance)
        self._refresh_buttons()

    def _on_placed(self, payload: tuple) -> None:
        result, local_ns, net_ns, price, qty, usdt = payload
        order_id = result.get("orderId", "")
        self.order_id.set(str(order_id))
        self.log(
            f"Placed {order_id}  {qty} at {price}  ({dec_str(usdt)} USDT)  "
            f"local {ms(local_ns)}  network {ms(net_ns)}"
        )
        self._refresh_buttons()
        self.spawn(self._open(), self._show_orders)

    def on_multi(self) -> None:
        if self._multi_running:
            self._stop_multi = True
            self.log("Stopping multi limit.", "warn")
            return
        spec = self._trade_spec()
        if spec is None:
            return
        count = int(self.order_count.get())
        interval = int(self.interval.get().split()[0])
        keep = bool(self.protection_keep.get())
        protect = bool(self.protection.get()) or keep
        steps = int(self.protect_steps.get())
        self._multi_running = True
        self._stop_multi = False
        self._refresh_buttons()
        if count == 0:
            self.log(
                f"Placing {spec['side']} orders every {interval}s until the balance runs out, "
                f"{dec_str(spec['usdt'])} USDT, starting at {spec['price_text']}."
            )
        else:
            self.log(
                f"Placing {count} {spec['side']} orders every {interval}s, "
                f"{dec_str(spec['usdt'])} USDT, starting at {spec['price_text']}."
            )
        if keep:
            self.log(
                f"Protection and keep is on. Cancel within {steps} steps, "
                "then put the order back when the price moves away."
            )
        elif protect:
            self.log(f"Protection is on. Cancel within {steps} steps.")
        asyncio.run_coroutine_threadsafe(
            self._multi_run(spec, interval, count, protect, steps, keep), self.loop
        )

    def on_cancel(self) -> None:
        raw = self.order_id.get().strip()
        if not raw.isdigit():
            self.log("Enter an order id, or click an open order.")
            return
        symbol = self._symbol()
        self.spawn(self._cancel(symbol, int(raw)), self._on_canceled)

    def _on_canceled(self, timing: tuple) -> None:
        result, local_ns, net_ns = timing
        self.log(
            f"Canceled {result.get('orderId', '')}  status {result.get('status', '')}  "
            f"local {ms(local_ns)}  network {ms(net_ns)}"
        )
        self._refresh_buttons()
        self.spawn(self._open(), self._show_orders)

    def on_cancel_all(self) -> None:
        symbol = self._symbol()
        if not symbol:
            self.log("Choose a symbol.")
            return
        self.spawn(self._cancel_all(symbol), self._on_canceled_all)

    def _on_canceled_all(self, rows: list) -> None:
        if isinstance(rows, list) and rows:
            self.log(f"Canceled {len(rows)} order(s).")
        else:
            self.log("Canceled all open orders.")
        self._refresh_buttons()
        self.spawn(self._open(), self._show_orders)

    def on_refresh(self) -> None:
        self.spawn(self._open(), self._show_orders)

    def _show_orders(self, rows: object, announce: bool = True) -> None:
        if not isinstance(rows, list):
            self.log("Unexpected open-order reply.")
            self._refresh_buttons()
            return
        self._render_order_rows(rows)
        if announce:
            self.log(f"Open orders: {len(rows)}")
        self.root.after(0, self._request_balance)
        self._refresh_buttons()

    def _on_order_row_select(self, _r_idx: int, row: dict) -> None:
        values = row.get("values") or ()
        if not values:
            return
        self.order_id.set(str(values[0]))
        if len(values) > 4:
            px_val = str(values[4]).strip()
            if DECIMAL.fullmatch(px_val) and Decimal(px_val) > 0:
                self.price.set(dec_str(Decimal(px_val)))

    def _on_order_cell_action(self, _r_idx: int, c_idx: int, row: dict) -> None:
        if c_idx != 7:
            return
        values = row.get("values") or ()
        if not values:
            return
        raw_id = str(values[0]).strip()
        row_symbol = str(values[1]).strip() or self._symbol()
        if raw_id.isdigit() and row_symbol and self.connected and not self.busy:
            self.order_id.set(raw_id)
            self.spawn(self._cancel(row_symbol, int(raw_id)), self._on_canceled)

    def _show_positions(self, rows: object) -> None:
        self._positions_by_item.clear()
        if not isinstance(rows, list):
            rows = []
        table_rows: list[dict] = []
        for row in rows:
            amt = Decimal(str(row.get("positionAmt") or "0"))
            if amt == 0:
                continue
            pos_side = str(row.get("positionSide") or "BOTH")
            is_long = amt > 0
            side_label = pos_side if pos_side in {"LONG", "SHORT"} else ("LONG" if is_long else "SHORT")
            size_abs = abs(amt)
            entry_px = Decimal(str(row.get("entryPrice") or "0"))
            mark_px = Decimal(str(row.get("markPrice") or entry_px))
            notional_raw = Decimal(str(row.get("notional") or "0"))
            size_usdt = abs(notional_raw) if notional_raw != 0 else size_abs * (mark_px if mark_px > 0 else entry_px)
            pnl = Decimal(str(row.get("unRealizedProfit") or row.get("unrealizedProfit") or "0"))
            info = {
                "symbol": str(row.get("symbol") or ""),
                "amt": amt,
                "qty": size_abs,
                "positionSide": pos_side,
                "markPrice": mark_px,
            }
            table_rows.append(
                {
                    "values": (
                        row.get("symbol", ""),
                        side_label,
                        f"{size_usdt:,.2f}",
                        dec_str(entry_px),
                        dec_str(mark_px),
                        f"{pnl:+,.2f}",
                        "Limit",
                        "Market",
                    ),
                    "colors": {5: BUY if pnl >= 0 else SELL},
                    "buttons": {6: "default", 7: "danger"},
                    "meta": info,
                }
            )
        self.pos_tree.set_rows(table_rows)
        count = len(table_rows)
        self.pos_count_badge.configure(text=str(count))
        if count:
            self.empty_positions.place_forget()
        else:
            if not self.connected:
                msg = "Connect to load open positions"
            elif self.market.get() != "Futures":
                msg = "Positions apply to Futures mode"
            else:
                msg = "No open positions"
            self.empty_positions.configure(text=msg)
            self.empty_positions.place(relx=0.5, rely=0.58, anchor="center")
            self.empty_positions.lift()

    def _on_position_cell_action(self, _r_idx: int, c_idx: int, row: dict) -> None:
        if c_idx not in {6, 7}:
            return
        info = row.get("meta")
        if not info or not self.connected or self.busy:
            return
        mode = "LIMIT" if c_idx == 6 else "MARKET"
        self.spawn(self._close_position(info, mode), self._on_position_closed)

    async def _close_position(self, info: dict, mode: str) -> tuple:
        symbol = info["symbol"]
        amt: Decimal = info["amt"]
        qty: Decimal = info["qty"]
        pos_side = info["positionSide"]
        close_side = "SELL" if amt > 0 else "BUY"
        params: dict = {
            "symbol": symbol,
            "side": close_side,
            "type": mode,
            "quantity": dec_str(qty),
            "newOrderRespType": "ACK",
        }
        if pos_side in {"LONG", "SHORT"}:
            params["positionSide"] = pos_side
        else:
            params["positionSide"] = "BOTH"
            params["reduceOnly"] = "true"
        if mode == "LIMIT":
            raw_px = self.price.get().strip()
            if symbol == self._symbol() and DECIMAL.fullmatch(raw_px) and Decimal(raw_px) > 0:
                px = Decimal(raw_px)
            elif info["markPrice"] > 0:
                px = info["markPrice"]
            else:
                px = Decimal(await self._price(symbol))
            rules = await self._rules_for("futures", symbol)
            if rules and rules[0] > 0:
                px = floor_step(px, rules[0])
            params["price"] = dec_str(px)
            params["timeInForce"] = "GTC"
        result, local_ns, net_ns = await self._timed("order.place", params)
        return result, local_ns, net_ns, mode, symbol, close_side, dec_str(qty), params.get("price")

    def _on_position_closed(self, payload: tuple) -> None:
        result, local_ns, net_ns, mode, symbol, close_side, qty, px = payload
        oid = result.get("orderId", "")
        if mode == "MARKET":
            self.log(
                f"Market close {symbol} ({close_side} {qty})  order {oid}  "
                f"local {ms(local_ns)}  network {ms(net_ns)}",
                "success",
            )
        else:
            self.log(
                f"Limit close {symbol} ({close_side} {qty} at {px})  order {oid}  "
                f"local {ms(local_ns)}  network {ms(net_ns)}",
                "success",
            )
        self._refresh_buttons()
        self.spawn(self._open(), self._show_orders)

    async def _connect_ws(self, api_key: str, secret: str, market: str) -> dict:
        await self._disconnect_ws()
        client = OrderClient(api_key, HmacSigner(secret), testnet=False, market=market, recv_window=5000)
        await client.connect()
        try:
            await client.call("account.status", signed=True)
        except Exception:
            await client.close()
            raise
        self.client = client
        self._stop_multi = False
        self._guard_task = asyncio.create_task(self._guard_loop(market))
        self._account_task = asyncio.create_task(self._account_sync_loop())
        return {"offset": client.clock_offset_ms, "market": client.market}

    async def _disconnect_ws(self) -> None:
        self._stop_multi = True
        for task in (self._guard_task, self._account_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._guard_task = None
        self._account_task = None
        if self._protected or self._kept:
            self._protected.clear()
            self._kept.clear()
            self.root.after(
                0,
                lambda: self.log(
                    "Protection stopped. Orders already sent stay on the exchange. Canceled orders are not put back."
                ),
            )
        client = self.client
        self.client = None
        if client is not None:
            await client.close()

    async def _rules_for(self, market: str, symbol: str) -> tuple[Decimal, Decimal] | None:
        key = (market, symbol)
        if key in self._rules:
            return self._rules[key]
        try:
            rules = await asyncio.to_thread(fetch_rules, market, symbol)
        except Exception as exc:
            self.root.after(0, lambda exc=exc: self.log(f"Exchange info unavailable, using the typed price step. {exc}"))
            return None
        self._rules[key] = rules
        return rules

    async def _apply_leverage(self, spec: dict) -> None:
        if spec.get("market") != "futures":
            return
        client = self._need_client()
        pos = spec.get("position")
        if pos in {"LONG", "SHORT"}:
            await client.ensure_position_mode(True)
        elif pos == "BOTH":
            await client.ensure_position_mode(False)
        leverage = spec.get("leverage")
        if not leverage:
            return
        result = await client.set_leverage(spec["symbol"], int(leverage))
        applied = result.get("leverage", leverage)
        symbol = spec["symbol"]
        self.root.after(
            0,
            lambda applied=applied, symbol=symbol: self.log(f"Leverage set to {applied}x for {symbol}."),
        )

    async def _order_at(self, spec: dict, price: Decimal, lot: Decimal) -> dict:
        qty = quote_to_base(spec["usdt"], price, lot)
        if qty <= 0:
            raise ValueError("Quantity rounds to zero at this price. Raise the USDT amount.")
        params = {
            "symbol": spec["symbol"],
            "side": spec["side"],
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": dec_str(qty),
            "price": dec_str(price),
            "newOrderRespType": "ACK",
        }
        if spec["position"]:
            params["positionSide"] = spec["position"]
        result, local_ns, net_ns = await self._timed("order.place", params)
        return {
            "result": result,
            "local_ns": local_ns,
            "net_ns": net_ns,
            "price": dec_str(price),
            "qty": dec_str(qty),
            "raw_price": price,
        }

    def _retarget_protection(self, old_id: int, new_id: int, price: Decimal, qty: Decimal) -> None:
        armed = self._protected.pop(old_id, None)
        if armed is None:
            return
        armed["order_id"] = new_id
        armed["price"] = price
        armed["qty"] = qty
        self._protected[new_id] = armed

    async def _move_orders(self, symbol: str, direction: int, steps: int) -> dict:
        market = "futures" if self.market.get() == "Futures" else "spot"
        rows = await self._open(symbol)
        if not isinstance(rows, list) or not rows:
            return {
                "symbol": symbol,
                "direction": direction,
                "steps": steps,
                "tick": Decimal(0),
                "moved": 0,
                "failed": 0,
                "empty": True,
                "rows": rows if isinstance(rows, list) else [],
            }
        rules = await self._rules_for(market, symbol)
        price_text = self.price.get().strip()
        if DECIMAL.fullmatch(price_text) and Decimal(price_text) > 0:
            user_step = price_step(price_text)
        else:
            user_step = rules[0] if rules else Decimal("0.00000001")
        tick = choose_step(user_step, rules[0] if rules else user_step)
        lot = rules[1] if rules else Decimal("0.00000001")
        ordered = sorted(rows, key=lambda row: Decimal(str(row.get("price") or "0")), reverse=direction > 0)
        moved = 0
        failed = 0
        for row in ordered:
            order_id = int(row.get("orderId") or 0)
            if str(row.get("type") or "LIMIT") not in {"LIMIT", "LIMIT_MAKER"}:
                failed += 1
                self.root.after(0, lambda order_id=order_id: self.log(f"Order {order_id} is not a limit order."))
                continue
            old = Decimal(str(row.get("price") or "0"))
            new_price = floor_step(old + tick * steps * direction, tick)
            if new_price <= 0:
                failed += 1
                self.root.after(
                    0,
                    lambda order_id=order_id: self.log(f"Order {order_id} would move to zero. It stays."),
                )
                continue
            try:
                result, _local_ns, _net_ns = await self._shift_order(market, row, new_price, lot)
                new_id = moved_order_id(result, order_id)
            except Exception as exc:
                failed += 1
                self.root.after(
                    0, lambda order_id=order_id, exc=exc: self.log(f"Move {order_id} failed: {exc}", "error")
                )
                if isinstance(exc, BinanceError) and exc.status in {418, 429}:
                    break
                continue
            qty = resting_qty(row, lot)
            self._retarget_protection(order_id, new_id, new_price, qty)
            moved += 1
            self.root.after(
                0,
                lambda order_id=order_id, new_id=new_id, new_price=new_price: self.log(
                    f"Moved {order_id} to {dec_str(new_price)}"
                    + (f" as {new_id}" if new_id != order_id else "")
                    + "."
                ),
            )
        try:
            fresh = await self._open()
        except Exception:
            fresh = []
        return {
            "symbol": symbol,
            "direction": direction,
            "steps": steps,
            "tick": tick,
            "moved": moved,
            "failed": failed,
            "empty": False,
            "rows": fresh if isinstance(fresh, list) else [],
        }

    async def _shift_order(self, market: str, row: dict, new_price: Decimal, lot: Decimal) -> tuple:
        order_id = int(row["orderId"])
        if market == "futures":
            return await self._timed(
                "order.modify",
                {
                    "symbol": row["symbol"],
                    "orderId": order_id,
                    "side": row["side"],
                    "price": dec_str(new_price),
                },
            )
        qty = resting_qty(row, lot)
        if qty <= 0:
            raise ValueError("Nothing left on this order to move.")
        return await self._timed(
            "order.cancelReplace",
            {
                "symbol": row["symbol"],
                "cancelReplaceMode": "STOP_ON_FAILURE",
                "cancelOrderId": order_id,
                "side": row["side"],
                "type": "LIMIT",
                "timeInForce": "GTC",
                "quantity": dec_str(qty),
                "price": dec_str(new_price),
                "newOrderRespType": "ACK",
            },
        )

    def _arm_protection(
        self,
        spec: dict,
        order_id: int,
        price: Decimal,
        qty: Decimal,
        tick: Decimal,
        lot: Decimal,
        steps: int,
        keep: bool,
    ) -> None:
        self._protected[order_id] = {
            "order_id": order_id,
            "symbol": spec["symbol"],
            "side": spec["side"],
            "price": price,
            "qty": qty,
            "tick": tick,
            "lot": lot,
            "steps": steps,
            "position": spec.get("position"),
            "leverage": spec.get("leverage"),
            "keep": keep,
        }

    async def _place_one(self, spec: dict, protect: bool, steps: int, keep: bool = False) -> tuple:
        rules = await self._rules_for(spec["market"], spec["symbol"])
        user_step = price_step(spec["price_text"])
        tick = choose_step(user_step, rules[0] if rules else user_step)
        lot = rules[1] if rules else Decimal("0.00000001")
        price = floor_step(spec["price"], tick)
        await self._apply_leverage(spec)
        placed = await self._order_at(spec, price, lot)
        order_id = int(placed["result"]["orderId"])
        if protect or keep:
            self._arm_protection(spec, order_id, price, Decimal(placed["qty"]), tick, lot, steps, keep)
            if keep:
                self.root.after(
                    0,
                    lambda order_id=order_id, steps=steps: self.log(
                        f"Protection and keep on for {order_id}. "
                        f"Cancel within {steps} steps, then put it back when the price moves away."
                    ),
                )
            else:
                self.root.after(
                    0,
                    lambda order_id=order_id, steps=steps: self.log(
                        f"Protection on for {order_id}. Cancel if the market comes within {steps} steps."
                    ),
                )
        return placed["result"], placed["local_ns"], placed["net_ns"], placed["price"], placed["qty"], spec["usdt"]

    async def _multi_run(
        self, spec: dict, interval: int, count: int, protect: bool, steps: int, keep: bool = False
    ) -> None:
        try:
            rules = await self._rules_for(spec["market"], spec["symbol"])
            user_step = price_step(spec["price_text"])
            tick = choose_step(user_step, rules[0] if rules else user_step)
            lot = rules[1] if rules else Decimal("0.00000001")
            await self._apply_leverage(spec)
            self.root.after(0, lambda tick=tick: self.log(f"Each next price is {dec_str(tick)} higher."))
            index = 0
            while count == 0 or index < count:
                if index and not await self._pause(interval):
                    self.root.after(0, lambda: self.log("Multi order stopped."))
                    return
                if self._stop_multi or self.client is None:
                    self.root.after(0, lambda: self.log("Multi order stopped."))
                    return
                price = floor_step(spec["price"] + tick * index, tick)
                if not await self._can_afford(spec, price, lot):
                    self._notify_no_amount()
                    return
                try:
                    placed = await self._order_at(spec, price, lot)
                except Exception as exc:
                    if _out_of_funds(exc):
                        self._notify_no_amount()
                        return
                    label = f"{index + 1}" if count == 0 else f"{index + 1}/{count}"
                    self.root.after(0, lambda exc=exc, label=label: self.log(f"Order {label} failed: {exc}"))
                    if isinstance(exc, BinanceError) and exc.status in {418, 429}:
                        return
                    index += 1
                    continue
                order_id = int(placed["result"]["orderId"])
                if protect or keep:
                    self._arm_protection(spec, order_id, price, Decimal(placed["qty"]), tick, lot, steps, keep)
                label = f"{index + 1}" if count == 0 else f"{index + 1}/{count}"
                self.root.after(
                    0,
                    lambda label=label, placed=placed, order_id=order_id: self.log(
                        f"{label}  order {order_id}  {placed['qty']} at {placed['price']}  "
                        f"network {ms(placed['net_ns'])}"
                    ),
                )
                index += 1
                try:
                    rows = await self._open()
                    positions = await self._need_client().open_positions() if spec["market"] == "futures" else []
                except Exception:
                    rows = None
                    positions = []
                if rows is not None:
                    self.root.after(0, lambda rows=rows, positions=positions: (self._show_orders(rows, announce=False), self._show_positions(positions)))
        finally:
            self.root.after(0, self._multi_finished)

    def _multi_finished(self) -> None:
        self._multi_running = False
        self._refresh_buttons()

    def _notify_no_amount(self) -> None:
        self.root.after(0, self._show_no_amount)

    def _show_no_amount(self) -> None:
        self.log("no amount")
        messagebox.showinfo("Trading Bot", "no amount", parent=self.root)

    async def _can_afford(self, spec: dict, price: Decimal, lot: Decimal) -> bool:
        base, quote = _split_symbol(spec["symbol"])
        if spec["market"] == "futures":
            free = await self._spendable("futures", quote)
            return free is None or free > 0
        if spec["side"] == "BUY":
            free = await self._spendable("spot", quote)
            return free is None or free >= spec["usdt"]
        free = await self._spendable("spot", base)
        return free is None or free >= quote_to_base(spec["usdt"], price, lot)

    async def _spendable(self, market: str, asset: str) -> Decimal | None:
        try:
            result = await self._need_client().call("account.status", signed=True)
        except Exception:
            return None
        if market == "futures":
            for row in result.get("assets") or []:
                if row.get("asset") == asset:
                    return Decimal(str(row.get("availableBalance") or "0"))
            if result.get("availableBalance") is not None:
                return Decimal(str(result["availableBalance"]))
            return None
        for row in result.get("balances") or []:
            if row.get("asset") == asset:
                return Decimal(str(row.get("free") or "0"))
        return Decimal(0)

    def _request_balance(self) -> None:
        if not self.connected or self.client is None:
            return
        asyncio.run_coroutine_threadsafe(self._sync_account_state(include_orders=False, delay=0.0), self.loop)
        asyncio.run_coroutine_threadsafe(self._sync_account_state(include_orders=True, delay=0.35), self.loop)

    async def _sync_account_state(self, include_orders: bool = False, delay: float = 0.0) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        if not self.connected or self.client is None:
            return
        market = "futures" if self.market.get() == "Futures" else "spot"
        try:
            free = await self._spendable(market, "USDT")
            positions = await self._need_client().open_positions() if market == "futures" else []
            rows = await self._open() if include_orders else None
        except Exception:
            return
        if not self.connected or self.client is None:
            return
        self.root.after(0, lambda f=free, p=positions, r=rows: self._apply_account_state(f, p, r))

    def _apply_account_state(self, free: Decimal | None, positions: list, rows: list | None) -> None:
        if not self.connected:
            return
        if free is not None:
            self._balance_val = free
            self.balance_text.set(f"{free:,.2f}")
        self._show_positions(positions)
        if rows is not None:
            self._render_order_rows(rows)
        self._sync_quote()
        self._refresh_buttons()

    def _render_order_rows(self, rows: list) -> None:
        table_rows: list[dict] = []
        for row in rows:
            side = str(row.get("side", ""))
            table_rows.append(
                {
                    "values": (
                        row.get("orderId", ""),
                        row.get("symbol", ""),
                        side,
                        row.get("positionSide") or "—",
                        row.get("price", ""),
                        row.get("origQty", ""),
                        row.get("status", ""),
                        "✕ Cancel",
                    ),
                    "colors": {2: BUY if side == "BUY" else SELL},
                    "buttons": {7: "danger"},
                }
            )
        self.tree.set_rows(table_rows)
        count = len(table_rows)
        self.order_count_badge.configure(text=str(count))
        if count:
            self.empty_orders.place_forget()
        else:
            self.empty_orders.configure(text="No open orders" if self.connected else "Connect to load open orders")
            self.empty_orders.place(relx=0.5, rely=0.58, anchor="center")
            self.empty_orders.lift()

    async def _account_sync_loop(self) -> None:
        try:
            while self.client is not None and not self._closing:
                await asyncio.sleep(2.5)
                if self.client is None or self._closing:
                    break
                await self._sync_account_state(include_orders=True, delay=0.0)
        except asyncio.CancelledError:
            raise

    async def _pause(self, seconds: int) -> bool:
        remaining = float(seconds)
        while remaining > 0:
            if self._stop_multi or self.client is None:
                return False
            chunk = min(0.2, remaining)
            await asyncio.sleep(chunk)
            remaining -= chunk
        return self.client is not None and not self._stop_multi

    async def _guard_loop(self, market: str) -> None:
        ws = None
        current = ""
        try:
            while self.client is not None:
                try:
                    symbols = sorted(
                        {item["symbol"].lower() for item in self._protected.values()}
                        | {item["symbol"].lower() for item in self._kept}
                    )
                    if not symbols:
                        if ws is not None:
                            await ws.close()
                            ws = None
                            current = ""
                        await asyncio.sleep(0.2)
                        continue
                    streams = "/".join(f"{symbol}@bookTicker" for symbol in symbols)
                    if streams != current:
                        if ws is not None:
                            await ws.close()
                        host = "fstream.binance.com" if market == "futures" else "stream.binance.com:9443"
                        ws = await connect(
                            f"wss://{host}/stream?streams={streams}",
                            compression=None,
                            ping_interval=20,
                            max_size=1_048_576,
                        )
                        current = streams
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1)
                    except TimeoutError:
                        continue
                    payload = orjson.loads(raw)
                    data = payload.get("data", payload) if isinstance(payload, dict) else {}
                    if "s" in data and "b" in data and "a" in data:
                        await self._on_book(data)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.root.after(0, lambda exc=exc: self.log(f"Protection stream: {exc}"))
                    if ws is not None:
                        await ws.close()
                    ws = None
                    current = ""
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            if ws is not None:
                await ws.close()
            raise

    async def _on_book(self, data: dict) -> None:
        symbol = str(data["s"])
        bid = Decimal(data["b"])
        ask = Decimal(data["a"])
        for order in list(self._protected.values()):
            if order["symbol"] != symbol:
                continue
            band = order["tick"] * order["steps"]
            near = False
            if order["side"] == "BUY" and bid <= order["price"] + band:
                near = True
            elif order["side"] == "SELL" and ask >= order["price"] - band:
                near = True
            if near:
                await self._cancel_protected(order)
        now = time.monotonic()
        for order in list(self._kept):
            if order["symbol"] != symbol or order.get("retry_after", 0) > now:
                continue
            if _price_away(order, bid, ask):
                await self._restore_kept(order)

    async def _cancel_protected(self, order: dict) -> None:
        order_id = order["order_id"]
        self._protected.pop(order_id, None)
        try:
            result = await self._need_client().cancel_order(order["symbol"], order_id=order_id)
        except BinanceError as exc:
            if exc.code != -2011:
                self._protected[order_id] = order
            self.root.after(0, lambda exc=exc, order_id=order_id: self.log(f"Protection cancel {order_id}: {exc}"))
            return
        if order.get("keep"):
            left = _remaining_qty(result, order)
            if left > 0:
                parked = {key: value for key, value in order.items() if key != "order_id"}
                parked["qty"] = left
                parked["keep"] = True
                parked.pop("retry_after", None)
                parked.pop("error_logged", None)
                self._kept.append(parked)
                self.root.after(
                    0,
                    lambda order_id=order_id, steps=order["steps"]: self.log(
                        f"Protection and keep canceled {order_id}. "
                        f"It goes back when the price moves {steps} steps away."
                    ),
                )
            else:
                self.root.after(
                    0,
                    lambda order_id=order_id: self.log(f"Protection and keep: {order_id} was filled."),
                )
        else:
            self.root.after(
                0,
                lambda order_id=order_id, steps=order["steps"]: self.log(
                    f"Protection canceled {order_id}. Market was within {steps} steps."
                ),
            )
        await self._refresh_open(order["symbol"])

    async def _restore_kept(self, order: dict) -> None:
        if order in self._kept:
            self._kept.remove(order)
        params = {
            "symbol": order["symbol"],
            "side": order["side"],
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": dec_str(order["qty"]),
            "price": dec_str(order["price"]),
            "newOrderRespType": "ACK",
        }
        if order.get("position"):
            params["positionSide"] = order["position"]
        try:
            if order.get("leverage"):
                await self._need_client().set_leverage(order["symbol"], int(order["leverage"]))
            result, _local_ns, _net_ns = await self._timed("order.place", params)
        except Exception as exc:
            order["retry_after"] = time.monotonic() + 2
            if not order.get("error_logged"):
                order["error_logged"] = True
                self.root.after(
                    0,
                    lambda exc=exc, price=order["price"]: self.log(
                        f"Could not put the order back at {dec_str(price)}: {exc}"
                    ),
                )
            self._kept.append(order)
            return
        order_id = int(result["orderId"])
        restored = {key: value for key, value in order.items() if key not in {"retry_after", "error_logged"}}
        restored["order_id"] = order_id
        restored["keep"] = True
        self._protected[order_id] = restored
        self.root.after(
            0,
            lambda order_id=order_id, order=order: self.log(
                f"Protection and keep put back {order_id}: {order['side']} {dec_str(order['qty'])} at {dec_str(order['price'])}."
            ),
        )
        await self._refresh_open(order["symbol"])

    async def _refresh_open(self, symbol: str = "") -> None:
        await self._sync_account_state(include_orders=True, delay=0.0)
        await self._sync_account_state(include_orders=True, delay=0.35)

    async def _price(self, symbol: str) -> str:
        return await self._need_client().last_price(symbol)

    async def _open(self, symbol: str | None = None) -> list:
        return await self._need_client().open_orders(symbol)

    async def _cancel_all(self, symbol: str) -> list:
        result = await self._need_client().cancel_all(symbol)
        self._protected = {key: value for key, value in self._protected.items() if value["symbol"] != symbol}
        self._kept = [item for item in self._kept if item["symbol"] != symbol]
        return result

    async def _timed(self, method: str, params: dict) -> tuple:
        client = self._need_client()
        pending = await client.submit(method, params, signed=True)
        result, local_ns, net_ns = await _wait(client, pending)
        return result, local_ns, net_ns

    async def _cancel(self, symbol: str, order_id: int) -> tuple:
        client = self._need_client()
        pending = await client.submit(
            "order.cancel",
            {"symbol": symbol, "orderId": order_id},
            signed=True,
        )
        self._protected.pop(order_id, None)
        return await _wait(client, pending)

    def _need_client(self) -> OrderClient:
        if self.client is None:
            raise RuntimeError("Not connected")
        return self.client

    def on_close(self) -> None:
        self._closing = True
        symbol = self._symbol()
        if symbol in self.config.get("symbols", []):
            self.config["last_symbol"] = symbol
            try:
                save_config(self.config)
            except Exception:
                pass
        if self._book_task is not None:
            self._book_task.cancel()
        if self._update_check_task is not None:
            self._update_check_task.cancel()
        try:
            future = asyncio.run_coroutine_threadsafe(self._disconnect_ws(), self.loop)
            future.result(timeout=3)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.root.destroy()

    async def _update_check_loop(self) -> None:
        await asyncio.sleep(2.0)
        while not self._closing:
            try:
                remote_ver = await asyncio.to_thread(check_remote_version)
                if remote_ver and remote_ver != self._update_available:
                    self._update_available = remote_ver
                    self.root.after(0, lambda v=remote_ver: self._flag_update(v))
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(30.0)

    def _flag_update(self, remote_ver: str) -> None:
        if self._closing:
            return
        self._refresh_buttons()
        self.log(f"New update available: v{remote_ver}. Click Update to install.", "success")

    async def _depth_loop(self) -> None:
        ws = None
        current = ("", "")
        try:
            while not self._closing:
                try:
                    symbol = self._symbol().lower()
                    market = "futures" if self.market.get() == "Futures" else "spot"
                    if not symbol or not SYMBOL.fullmatch(symbol.upper()):
                        if ws is not None:
                            await ws.close()
                            ws = None
                            current = ("", "")
                        await asyncio.sleep(0.3)
                        continue
                    target = (market, symbol)
                    if target != current or ws is None:
                        if ws is not None:
                            await ws.close()
                        host = "fstream.binance.com" if market == "futures" else "stream.binance.com:9443"
                        ws = await connect(
                            f"wss://{host}/ws/{symbol}@depth20@100ms",
                            compression=None,
                            ping_interval=20,
                            max_size=1_048_576,
                        )
                        current = target
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    except TimeoutError:
                        continue
                    payload = orjson.loads(raw)
                    asks = payload.get("asks") or payload.get("a") or []
                    bids = payload.get("bids") or payload.get("b") or []
                    if asks or bids:
                        self.root.after(0, lambda a=asks, b=bids: self._render_depth(a, b))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if ws is not None:
                        try:
                            await ws.close()
                        except Exception:
                            pass
                    ws = None
                    current = ("", "")
                    await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            raise

    def _render_depth(self, asks: list, bids: list) -> None:
        if self._closing:
            return
        top_asks = asks[:12]
        ask_cum: list[tuple[Decimal, Decimal]] = []
        running_ask = Decimal(0)
        for item in top_asks:
            p = Decimal(str(item[0]))
            q = Decimal(str(item[1]))
            running_ask += p * q
            ask_cum.append((p, running_ask))
        padded_asks = [None] * (12 - len(ask_cum)) + list(reversed(ask_cum))
        for idx, item in enumerate(padded_asks):
            _row, px_lbl, tot_lbl = self.ask_rows[idx]
            if item is None:
                px_lbl.configure(text="—")
                tot_lbl.configure(text="")
            else:
                p, cum_usdt = item
                px_lbl.configure(text=dec_str(p))
                tot_lbl.configure(text=f"{cum_usdt:,.0f}")
        running_bid = Decimal(0)
        for idx in range(12):
            _row, px_lbl, tot_lbl = self.bid_rows[idx]
            if idx >= len(bids):
                px_lbl.configure(text="—")
                tot_lbl.configure(text="")
            else:
                p = Decimal(str(bids[idx][0]))
                q = Decimal(str(bids[idx][1]))
                running_bid += p * q
                px_lbl.configure(text=dec_str(p))
                tot_lbl.configure(text=f"{running_bid:,.0f}")
        if asks and bids:
            best_ask = Decimal(str(asks[0][0]))
            best_bid = Decimal(str(bids[0][0]))
            self.book_mid_label.configure(text=dec_str(best_bid))
            self.book_spread_label.configure(text=f"Spread {dec_str(best_ask - best_bid)}")

    def run(self) -> None:
        self.root.mainloop()


async def _wait(client: OrderClient, pending) -> tuple:
    try:
        msg = await asyncio.wait_for(asyncio.shield(pending.future), client.timeout)
    except TimeoutError:
        client._pending.pop(pending.req_id, None)
        if not pending.future.done():
            pending.future.cancel()
        raise TimeoutError("Binance did not answer in time") from None
    ack = time.perf_counter_ns()
    return unwrap(msg), pending.sent_ns - pending.started_ns, ack - pending.sent_ns


def main() -> None:
    app = TradingApp()
    if "--smoke" in sys.argv:
        app.root.after(400, app.root.destroy)
    app.run()


if __name__ == "__main__":
    main()
