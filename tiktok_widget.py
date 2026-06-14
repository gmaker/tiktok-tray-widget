#!/usr/bin/env python3
"""TikTok Stats — system tray widget: followers, likes, and total video views."""

import hashlib
import json
import os
import random
import string
import struct
import tempfile
import threading
import time
import urllib.parse
import wave
import webbrowser
import winsound
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

import tkinter as tk

import requests
from PIL import Image, ImageDraw, ImageFont
import pystray

# ── Settings ──────────────────────────────────────────────────────────────────
_DIR           = os.path.dirname(os.path.abspath(__file__))
_SETTINGS_FILE = os.path.join(_DIR, "settings.json")
TOKEN_FILE     = os.path.join(_DIR, "token.json")

_DEFAULTS: dict = {
    "client_key":       "",
    "client_secret":    "",
    "redirect_uri":     "http://localhost:8080/callback",
    "scopes":           "user.info.basic,user.info.profile,user.info.stats,video.list",
    "poll_interval":    60,
    "color_followers":  [254, 44,  85],
    "color_likes":      [105, 201, 208],
    "color_views":      [100, 210, 130],
    "views_enabled":    False,
    "sound_likes":      "snd/1.wav",
    "sound_followers":  "snd/2.wav",
    "sound_volume":     1.0,
}

def _load_settings() -> dict:
    s = dict(_DEFAULTS)
    if os.path.exists(_SETTINGS_FILE):
        try:
            with open(_SETTINGS_FILE) as f:
                s.update(json.load(f))
        except Exception:
            pass
    return s

def _save_setting(key: str, value) -> None:
    try:
        data = dict(_DEFAULTS)
        if os.path.exists(_SETTINGS_FILE):
            with open(_SETTINGS_FILE) as f:
                data.update(json.load(f))
        data[key] = value
        with open(_SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

SETTINGS      = _load_settings()
CLIENT_KEY    = SETTINGS["client_key"]
CLIENT_SECRET = SETTINGS["client_secret"]
REDIRECT_URI  = SETTINGS["redirect_uri"]
SCOPES        = SETTINGS["scopes"]
POLL_INTERVAL = int(SETTINGS["poll_interval"])

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\ariblk.ttf",
    r"C:\Windows\Fonts\impact.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
]


# ─────────────────────────────────────────────────────────────────────────────
# Sound playback with volume scaling
# ─────────────────────────────────────────────────────────────────────────────
def _play_sound(rel_path: str, volume: float) -> None:
    path = rel_path if os.path.isabs(rel_path) else os.path.join(_DIR, rel_path)
    if not os.path.exists(path):
        return
    try:
        if volume >= 0.99:
            winsound.PlaySound(path, winsound.SND_FILENAME)
            return
        with wave.open(path) as wf:
            params = wf.getparams()
            raw    = wf.readframes(wf.getnframes())
        if params.sampwidth == 2:
            fmt     = f"<{len(raw) // 2}h"
            samples = list(struct.unpack(fmt, raw))
            samples = [max(-32768, min(32767, int(s * volume))) for s in samples]
            raw     = struct.pack(fmt, *samples)
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_name = tmp.name
        tmp.close()
        with wave.open(tmp_name, "w") as wf:
            wf.setparams(params)
            wf.writeframes(raw)
        try:
            winsound.PlaySound(tmp_name, winsound.SND_FILENAME)
        finally:
            try:
                os.unlink(tmp_name)
            except Exception:
                pass
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Token storage / refresh
# ─────────────────────────────────────────────────────────────────────────────
class TokenManager:
    def __init__(self):
        self.access_token:  Optional[str] = None
        self.refresh_token: Optional[str] = None
        self._expiry: float = 0
        self._load()

    def _load(self):
        if not os.path.exists(TOKEN_FILE):
            return
        try:
            d = json.loads(open(TOKEN_FILE).read())
            self.access_token  = d.get("access_token")
            self.refresh_token = d.get("refresh_token")
            self._expiry       = d.get("expiry", 0)
        except Exception:
            pass

    def save(self, access_token: str, refresh_token: str, expires_in: int):
        self.access_token  = access_token
        self.refresh_token = refresh_token
        self._expiry       = time.time() + expires_in - 120
        with open(TOKEN_FILE, "w") as f:
            json.dump({"access_token": access_token,
                       "refresh_token": refresh_token,
                       "expiry": self._expiry}, f)

    def is_valid(self) -> bool:
        return bool(self.access_token) and time.time() < self._expiry

    def try_refresh(self) -> bool:
        if not self.refresh_token:
            return False
        try:
            r = requests.post(
                "https://open.tiktokapis.com/v2/oauth/token/",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={"client_key": CLIENT_KEY, "client_secret": CLIENT_SECRET,
                      "grant_type": "refresh_token",
                      "refresh_token": self.refresh_token},
                timeout=20,
            )
            if r.ok:
                d = r.json()
                self.save(d["access_token"],
                          d.get("refresh_token", self.refresh_token),
                          d.get("expires_in", 86400))
                return True
        except Exception:
            pass
        return False


# ─────────────────────────────────────────────────────────────────────────────
# OAuth PKCE flow
# ─────────────────────────────────────────────────────────────────────────────
class OAuthFlow:
    def __init__(self):
        chars           = string.ascii_letters + string.digits + "-._~"
        self._verifier  = "".join(random.choice(chars) for _ in range(64))
        self._challenge = hashlib.sha256(self._verifier.encode()).hexdigest()
        self._state     = "".join(random.choice(chars) for _ in range(32))
        self.result:    Optional[dict] = None
        self._srv:      Optional[HTTPServer] = None

    def auth_url(self) -> str:
        return "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode({
            "client_key": CLIENT_KEY, "scope": SCOPES, "response_type": "code",
            "redirect_uri": REDIRECT_URI, "state": self._state,
            "code_challenge": self._challenge, "code_challenge_method": "S256",
        })

    def run(self) -> bool:
        flow = self

        class _H(BaseHTTPRequestHandler):
            def do_GET(self):
                q    = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                code = q.get("code", [None])[0]
                ok   = q.get("state", [None])[0] == flow._state and code
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                if not ok:
                    self.wfile.write(b"<h1>Auth failed - close tab and restart.</h1>")
                else:
                    self.wfile.write(b"<h1>Authorised! You can close this tab.</h1>")
                    try:
                        r = requests.post(
                            "https://open.tiktokapis.com/v2/oauth/token/",
                            headers={"Content-Type": "application/x-www-form-urlencoded"},
                            data={"client_key": CLIENT_KEY, "client_secret": CLIENT_SECRET,
                                  "code": code, "grant_type": "authorization_code",
                                  "redirect_uri": REDIRECT_URI,
                                  "code_verifier": flow._verifier},
                            timeout=20,
                        )
                        if r.ok:
                            flow.result = r.json()
                    except Exception:
                        pass
                threading.Thread(target=flow._srv.shutdown, daemon=True).start()

            def log_message(self, *_):
                pass

        parsed    = urllib.parse.urlparse(REDIRECT_URI)
        port      = parsed.port or 8080
        flow._srv = HTTPServer(("localhost", port), _H)
        webbrowser.open(self.auth_url())
        flow._srv.serve_forever()
        return flow.result is not None


# ─────────────────────────────────────────────────────────────────────────────
# Split-flap animated value widget
# ─────────────────────────────────────────────────────────────────────────────
class _FlipValue(tk.Frame):
    """Each digit shuffles through random values before landing — like a Solari board."""

    _SHUFFLES = 7   # random frames before the real digit
    _FRAME_MS = 50  # ms per frame
    _STAGGER  = 35  # ms delay per changed position (left → right)

    def __init__(self, parent, color: str, bg: str = "#141414"):
        super().__init__(parent, bg=bg)
        self._color = color
        self._bg    = bg
        self._lbls: list = []
        self._text  = ""

    def set_value(self, text: str, animate: bool = True):
        if text == self._text:
            return
        old, self._text = self._text, text
        if not animate or not old or len(old) != len(text):
            self._rebuild(text)
            return
        self._animate(old, text)

    def _rebuild(self, text: str):
        for lbl in self._lbls:
            lbl.destroy()
        self._lbls = []
        for ch in text:
            lbl = tk.Label(self, text=ch, fg=self._color, bg=self._bg,
                           font=("Segoe UI", 14, "bold"))
            lbl.pack(side="left")
            self._lbls.append(lbl)

    def _animate(self, old: str, new: str):
        if len(self._lbls) != len(new):
            self._rebuild(old)
        for i, (lbl, oc, nc) in enumerate(zip(self._lbls, old, new)):
            if oc != nc:
                self._flip(lbl, nc, delay=i * self._STAGGER, shuffle=nc.isdigit())

    def _flip(self, lbl, target: str, delay: int, shuffle: bool):
        frames = ([random.choice("0123456789") for _ in range(self._SHUFFLES)]
                  if shuffle else [])
        frames.append(target)

        def _step(i: int):
            if i >= len(frames):
                return
            try:
                lbl.configure(text=frames[i])
                lbl.after(self._FRAME_MS, lambda: _step(i + 1))
            except tk.TclError:
                pass

        try:
            lbl.after(delay, lambda: _step(0))
        except tk.TclError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Main app
# ─────────────────────────────────────────────────────────────────────────────
class TikTokTray:
    def __init__(self):
        self._tokens        = TokenManager()
        self._followers     = 0
        self._likes         = 0
        self._views         = 0
        self._muted         = False
        self._muted_likes   = False
        self._views_enabled = bool(SETTINGS["views_enabled"])
        self._running       = True
        self._font          = self._load_font()

        self._tray_f: Optional[pystray.Icon] = None  # followers
        self._tray_l: Optional[pystray.Icon] = None  # likes
        self._tray_v: Optional[pystray.Icon] = None  # views
        self._popup_win:  Optional[tk.Tk] = None
        self._popup_rows: dict             = {}

        self._COLOR_F = tuple(SETTINGS["color_followers"])
        self._COLOR_L = tuple(SETTINGS["color_likes"])
        self._COLOR_V = tuple(SETTINGS["color_views"])

    @staticmethod
    def _load_font() -> Optional[ImageFont.FreeTypeFont]:
        for path in _FONT_CANDIDATES:
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, 1)
                except Exception:
                    pass
        return None

    def _fmt(self, n: int) -> str:
        if n < 1000:
            return str(n)
        if n < 10_000:
            return f"{n / 1000:.1f}K"
        if n < 1_000_000:
            return f"{n // 1000}K"
        if n < 10_000_000:
            return f"{n / 1_000_000:.1f}M"
        return f"{n // 1_000_000}M"

    def _make_icon(self, text: str, color: tuple, highlight: bool = False) -> Image.Image:
        sz  = 64
        img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
        d   = ImageDraw.Draw(img)

        bg = (35, 35, 35, 240) if not highlight else (50, 30, 35, 240)
        d.rounded_rectangle([0, 0, sz - 1, sz - 1], radius=10, fill=bg)

        font_size = {1: 56, 2: 50, 3: 38, 4: 30}.get(len(text), 22)
        font      = (self._font.font_variant(size=font_size)
                     if self._font else ImageFont.load_default())

        bb       = d.textbbox((0, 0), text, font=font)
        tw, th   = bb[2] - bb[0], bb[3] - bb[1]
        x        = (sz - tw) // 2 - bb[0]
        y        = (sz - th) // 2 - bb[1]
        d.text((x, y), text, fill=color + (255,), font=font)

        return img

    def _update_views_icon(self):
        if not self._tray_v:
            return
        if self._views_enabled:
            self._tray_v.icon  = self._make_icon(self._fmt(self._views), self._COLOR_V)
            self._tray_v.title = f"Views: {self._views:,}"
        else:
            self._tray_v.icon  = self._make_icon("--", self._COLOR_V)
            self._tray_v.title = "Views: disabled"

    def _set_icons(self, gained_f: int = 0, gained_l: int = 0):
        if self._tray_f:
            self._tray_f.icon  = self._make_icon(self._fmt(self._followers), self._COLOR_F, gained_f > 0)
            self._tray_f.title = (f"Followers: {self._followers:,}"
                                  + (f" (+{gained_f})" if gained_f else ""))
        if self._tray_l:
            self._tray_l.icon  = self._make_icon(self._fmt(self._likes), self._COLOR_L, gained_l > 0)
            self._tray_l.title = f"Likes: {self._likes:,}"
        self._update_views_icon()
        self._update_popup()

    def _set_status(self, text: str):
        if self._tray_f:
            self._tray_f.title = text

    # ── Polling ───────────────────────────────────────────────────────────────
    def _poll_loop(self):
        if not self._tokens.is_valid():
            if not (self._tokens.refresh_token and self._tokens.try_refresh()):
                self._set_status("opening browser for auth...")
                self._do_auth()

        while self._running:
            self._fetch()
            for _ in range(POLL_INTERVAL):
                if not self._running:
                    return
                time.sleep(1)

    def _do_auth(self):
        flow = OAuthFlow()
        if flow.run() and flow.result:
            d = flow.result
            self._tokens.save(d["access_token"],
                               d.get("refresh_token", ""),
                               d.get("expires_in", 86400))

    def _fetch_views(self) -> int:
        total    = 0
        cursor   = 0
        has_more = True
        while has_more:
            r = requests.post(
                "https://open.tiktokapis.com/v2/video/list/",
                params={"fields": "id,view_count"},
                headers={
                    "Authorization":  f"Bearer {self._tokens.access_token}",
                    "Content-Type":   "application/json",
                },
                json={"max_count": 20, "cursor": cursor},
                timeout=20,
            )
            r.raise_for_status()
            data     = r.json().get("data", {})
            for v in data.get("videos", []):
                total += int(v.get("view_count", 0))
            has_more = bool(data.get("has_more", False))
            cursor   = int(data.get("cursor", 0))
        return total

    def _fetch(self):
        if not self._tokens.is_valid() and not self._tokens.try_refresh():
            self._set_status("token expired — restart app to re-authorise")
            return
        try:
            r = requests.get(
                "https://open.tiktokapis.com/v2/user/info/",
                params={"fields": "display_name,follower_count,likes_count"},
                headers={"Authorization": f"Bearer {self._tokens.access_token}"},
                timeout=20,
            )
            r.raise_for_status()
            user = r.json().get("data", {}).get("user", {})

            new_f    = int(user.get("follower_count", self._followers))
            new_l    = int(user.get("likes_count",    self._likes))
            gained_f = new_f - self._followers
            gained_l = new_l - self._likes

            self._followers = new_f
            self._likes     = new_l

        except Exception as exc:
            self._set_status(f"error: {str(exc)[:60]}")
            return

        if self._views_enabled:
            try:
                self._views = self._fetch_views()
            except requests.HTTPError as e:
                if self._tray_v:
                    self._tray_v.title = ("Views: re-auth needed (video.list scope missing)"
                                          if e.response.status_code == 403
                                          else f"Views error: {e.response.status_code}")
            except Exception:
                pass  # keep previous value

        self._set_icons(gained_f, gained_l)

        vol = float(SETTINGS.get("sound_volume", 1.0))
        if gained_f > 0 and not self._muted:
            snd = str(SETTINGS.get("sound_followers", "snd/2.wav"))
            threading.Thread(target=_play_sound, args=(snd, vol), daemon=True).start()
        if gained_l > 0 and not self._muted_likes:
            snd = str(SETTINGS.get("sound_likes", "snd/1.wav"))
            threading.Thread(target=_play_sound, args=(snd, vol), daemon=True).start()

        if gained_f > 0 or gained_l > 0:
            def _reset():
                time.sleep(3)
                self._set_icons()
            threading.Thread(target=_reset, daemon=True).start()

    def _all_trays(self):
        return [t for t in (self._tray_f, self._tray_l, self._tray_v) if t]

    # ── Detail popup ──────────────────────────────────────────────────────────
    def _show_detail_popup(self):
        if self._popup_win is not None:
            try:
                self._popup_win.after(0, self._popup_win.destroy)
            except Exception:
                pass
            self._popup_win  = None
            self._popup_rows = {}
            return

        def _run():
            win = tk.Tk()
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.configure(bg="#141414")

            border = tk.Frame(win, bg="#383838", padx=1, pady=1)
            border.pack(fill="both", expand=True)
            body = tk.Frame(border, bg="#141414")
            body.pack(fill="both", expand=True)

            def _rgb(c):
                return "#{:02x}{:02x}{:02x}".format(*c)

            row_defs = [
                ("f", "Followers", self._followers,                               self._COLOR_F),
                ("l", "Likes",     self._likes,                                   self._COLOR_L),
                ("v", "Views",     self._views if self._views_enabled else None,  self._COLOR_V),
            ]
            popup_rows = {}

            for i, (key, label, value, color) in enumerate(row_defs):
                row = tk.Frame(body, bg="#141414")
                row.pack(fill="x")
                tk.Frame(row, bg=_rgb(color), width=3).pack(side="left", fill="y")
                inner = tk.Frame(row, bg="#141414")
                inner.pack(side="left", fill="x", expand=True,
                           padx=(10, 16),
                           pady=(6 if i == 0 else 4, 6 if i == len(row_defs) - 1 else 4))
                tk.Label(inner, text=label, fg="#5a5a5a", bg="#141414",
                         font=("Segoe UI", 9), anchor="w").pack(side="left")
                flip = _FlipValue(inner, _rgb(color))
                flip.pack(side="right")
                flip.set_value(f"{value:,}" if value is not None else "—", animate=False)
                popup_rows[key] = flip

            win.update_idletasks()
            w = max(win.winfo_reqwidth(), 240)
            h = win.winfo_reqheight()
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
            win.geometry(f"{w}x{h}+{sw - w - 10}+{sh - h - 52}")

            win.bind("<Escape>", lambda e: win.destroy())

            self._popup_win  = win
            self._popup_rows = popup_rows
            try:
                win.mainloop()
            finally:
                self._popup_win  = None
                self._popup_rows = {}

        threading.Thread(target=_run, daemon=True).start()

    def _update_popup(self):
        win  = self._popup_win
        rows = self._popup_rows
        if not win or not rows:
            return
        fmt = lambda v: f"{v:,}" if v is not None else "—"
        data = {
            "f": fmt(self._followers),
            "l": fmt(self._likes),
            "v": fmt(self._views if self._views_enabled else None),
        }
        for key, text in data.items():
            flip = rows.get(key)
            if flip:
                try:
                    win.after(0, lambda f=flip, t=text: f.set_value(t))
                except Exception:
                    pass

    # ── Tray entry point ──────────────────────────────────────────────────────
    def run(self):
        def on_mute(icon, _):
            self._muted = not self._muted
            for t in self._all_trays():
                t.update_menu()

        def on_mute_likes(icon, _):
            self._muted_likes = not self._muted_likes
            for t in self._all_trays():
                t.update_menu()

        def on_toggle_views(icon, _):
            self._views_enabled = not self._views_enabled
            _save_setting("views_enabled", self._views_enabled)
            for t in self._all_trays():
                t.update_menu()
            if self._views_enabled:
                threading.Thread(target=self._fetch, daemon=True).start()
            else:
                self._update_views_icon()

        def on_refresh(icon, _):
            threading.Thread(target=self._fetch, daemon=True).start()

        def on_exit(icon, _):
            self._running = False
            for t in self._all_trays():
                t.stop()

        menu = pystray.Menu(
            pystray.MenuItem(
                "Details",
                lambda icon, item: self._show_detail_popup(),
                default=True,
                visible=False,
            ),
            pystray.MenuItem(
                lambda _: "Sound: OFF" if self._muted else "Sound: ON",
                on_mute,
                checked=lambda _: not self._muted,
            ),
            pystray.MenuItem(
                lambda _: "Likes sound: OFF" if self._muted_likes else "Likes sound: ON",
                on_mute_likes,
                checked=lambda _: not self._muted_likes,
            ),
            pystray.MenuItem(
                lambda _: "Views: ON" if self._views_enabled else "Views: OFF",
                on_toggle_views,
                checked=lambda _: self._views_enabled,
            ),
            pystray.MenuItem("Refresh now", on_refresh),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", on_exit),
        )

        self._tray_f = pystray.Icon("tiktok_followers",
                                    self._make_icon("...", self._COLOR_F),
                                    "Followers: loading...", menu)
        self._tray_l = pystray.Icon("tiktok_likes",
                                    self._make_icon("...", self._COLOR_L),
                                    "Likes: loading...", menu)
        self._tray_v = pystray.Icon("tiktok_views",
                                    self._make_icon("--", self._COLOR_V),
                                    "Views: disabled", menu)

        threading.Thread(target=self._poll_loop, daemon=True).start()

        self._tray_l.run_detached()
        self._tray_v.run_detached()
        try:
            self._tray_f.run()
        finally:
            self._running = False
            self._tray_l.stop()
            self._tray_v.stop()


if __name__ == "__main__":
    TikTokTray().run()
