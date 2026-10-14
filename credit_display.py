#!/usr/bin/env python3
"""
Bitcoin Ekasi Arcade - credit display (Raspberry Pi 3 + Pygame)

Shows a "scan to pay" QR code and the credit balance, full screen.
Credits arrive from:
  - the ESP32 over USB serial  (lines like "CREDIT 2 <invoiceId>")
  - a GPIO pin wired to the relay contact (optional)
  - the keyboard, for testing

Keys:  C = add a test credit    T = ask the ESP32 to click the relay (full test)
       1 / Enter = spend a credit    Esc = quit

Needs: python3-pygame, python3-qrcode, python3-serial (all in Add/Remove Software).
"""

import argparse
import glob
import json
import math
import os
import queue
import sys
import threading
import time
from collections import deque
from datetime import datetime

import pygame

try:
    import qrcode
except ImportError:
    qrcode = None

try:
    import serial
except ImportError:
    serial = None


# ================================ SETTINGS ===================================
CONFIG = {
    # What players scan: your BTCPay Point of Sale link (or an LNURL from it).
    "pay_url": "https://YOUR-BTCPAY-SERVER/apps/YOUR_APP_ID/pos",
    "title": "BITCOIN EKASI ARCADE",
    "price_text": "R5 = 1 CREDIT",

    # ESP32 on USB. "auto" uses the first /dev/ttyUSB* or /dev/ttyACM*. None = off.
    "serial_port": "auto",
    "baud": 115200,

    # Optional GPIO inputs (BCM pin numbers), switch to GND. None = not used.
    # Use credit_gpio only if you are NOT using serial, or credits count twice.
    "credit_gpio": None,   # relay contact: each click adds 1 credit
    "start_gpio": None,    # start button: each press spends 1 credit

    # Keyboard keys (pygame key names). A USB arcade encoder that types keys works too.
    "add_keys": ["c"],
    "spend_keys": ["1", "return"],
    "test_keys": ["t"],

    "credits_file": "credits.json",   # saved next to this script
    "sound_file": "coin.wav",         # optional, next to this script
    "fullscreen": True,
    "fps": 30,
}
# =============================================================================

HERE = os.path.dirname(os.path.abspath(__file__))

ORANGE = (247, 147, 26)
BG = (10, 10, 16)
PANEL = (22, 22, 32)
WHITE = (245, 245, 245)
BLACK = (0, 0, 0)
GREY = (140, 140, 155)
DIM = (70, 70, 85)
GREEN = (60, 210, 110)
YELLOW = (240, 200, 60)
RED = (230, 70, 70)


# ----------------------------------------------------------------- credits --

class CreditStore:
    """Credit balance saved to a JSON file, so a power cut never loses paid credits."""

    def __init__(self, path):
        self.path = path
        self.credits = 0
        self.total_sold = 0
        self.last_payment = None
        try:
            with open(path) as f:
                data = json.load(f)
            self.credits = max(0, int(data.get("credits", 0)))
            self.total_sold = int(data.get("total_credits_sold", 0))
            self.last_payment = data.get("last_payment")
        except FileNotFoundError:
            pass
        except (ValueError, OSError) as e:
            print("Could not read credits file:", e, file=sys.stderr)

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"credits": self.credits,
                           "total_credits_sold": self.total_sold,
                           "last_payment": self.last_payment}, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            print("Could not save credits:", e, file=sys.stderr)

    def add(self, n):
        self.credits += n
        self.total_sold += n
        self.last_payment = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._save()

    def spend(self):
        if self.credits <= 0:
            return False
        self.credits -= 1
        self._save()
        return True


# ------------------------------------------------------------------ inputs --

class SerialLink(threading.Thread):
    """Reads the ESP32's messages in the background and reconnects if unplugged."""

    def __init__(self, port, baud, events):
        super().__init__(daemon=True)
        self.port = port
        self.baud = baud
        self.events = events
        self.ser = None
        self.connected = False
        self.port_name = None
        self.last_rx = 0.0
        self.esp_state = None          # OK, NOWIFI, NOTIME, APIERR
        self.lock = threading.Lock()

    def _candidates(self):
        if self.port != "auto":
            return [self.port]
        return sorted(glob.glob("/dev/ttyUSB*")) + sorted(glob.glob("/dev/ttyACM*"))

    def run(self):
        while True:
            ports = self._candidates()
            if not ports:
                time.sleep(2)
                continue
            try:
                ser = serial.Serial()
                ser.port = ports[0]
                ser.baudrate = self.baud
                ser.timeout = 1
                ser.dtr = False        # don't reset the ESP32 when the port opens
                ser.rts = False
                ser.open()
            except (serial.SerialException, OSError):
                time.sleep(2)
                continue

            with self.lock:
                self.ser = ser
            self.connected, self.port_name = True, ports[0]
            print("ESP32 connected on", ports[0])
            try:
                while True:
                    raw = ser.readline()
                    if raw:
                        self._parse_line(raw.decode("utf-8", "replace").strip())
            except (serial.SerialException, OSError):
                print("ESP32 disconnected")
            finally:
                with self.lock:
                    self.ser = None
                self.connected = False
                try:
                    ser.close()
                except Exception:
                    pass
                time.sleep(1)

    def _parse_line(self, line):
        self.last_rx = time.time()
        parts = line.split()
        if not parts:
            return
        cmd = parts[0].upper()
        if cmd == "CREDIT" and len(parts) >= 2:
            try:
                n = int(parts[1])
            except ValueError:
                return
            if 0 < n <= 100:
                self.events.put(("credit", n, parts[2] if len(parts) > 2 else ""))
        elif cmd == "HB" and len(parts) >= 2:
            self.esp_state = parts[1].upper()
        elif cmd in ("BOOT", "WIFI", "INFO", "ERR"):
            print("ESP32:", line)

    def send(self, text):
        with self.lock:
            if self.ser:
                try:
                    self.ser.write((text + "\n").encode())
                except (serial.SerialException, OSError):
                    pass


def setup_gpio(cfg, events):
    """Optional GPIO buttons. Returns the objects so they stay alive."""
    if cfg["credit_gpio"] is None and cfg["start_gpio"] is None:
        return []
    try:
        from gpiozero import Button
    except ImportError:
        print("gpiozero not installed - GPIO inputs are off", file=sys.stderr)
        return []
    keep = []
    if cfg["credit_gpio"] is not None:
        b = Button(cfg["credit_gpio"], pull_up=True, bounce_time=0.03)
        b.when_pressed = lambda: events.put(("credit", 1, "gpio"))
        keep.append(b)
    if cfg["start_gpio"] is not None:
        b = Button(cfg["start_gpio"], pull_up=True, bounce_time=0.05)
        b.when_pressed = lambda: events.put(("spend",))
        keep.append(b)
    return keep


# ----------------------------------------------------------------- drawing --

def load_font(size, bold=True):
    for name in ("dejavusansmono", "liberationmono", "freemono", "couriernew"):
        path = pygame.font.match_font(name, bold=bold)
        if path:
            return pygame.font.Font(path, size)
    return pygame.font.Font(None, int(size * 1.3))


def make_qr(data, max_size):
    """QR code as a pygame Surface, sized to fit max_size. Needs no Pillow."""
    qr = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    box = max(1, max_size // n)
    surf = pygame.Surface((n * box, n * box))
    surf.fill(WHITE)
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                surf.fill(BLACK, (x * box, y * box, box, box))
    return surf


def blit_center(target, surf, center):
    target.blit(surf, surf.get_rect(center=center))


class CreditDisplay:
    def __init__(self, cfg, screen):
        self.cfg = cfg
        self.screen = screen
        self.W, self.H = screen.get_size()
        self.u = self.H / 720.0                       # layout unit
        self.events = queue.Queue()
        self.store = CreditStore(os.path.join(HERE, cfg["credits_file"]))
        self.recent_ids = deque(maxlen=200)
        self.started = time.time()
        self.pop_at = -10.0
        self.banner_at = -10.0
        self.banner_n = 0
        self.spend_at = -10.0

        self.link = None
        if cfg["serial_port"]:
            if serial is None:
                print("pyserial not installed - serial input is off", file=sys.stderr)
            else:
                self.link = SerialLink(cfg["serial_port"], cfg["baud"], self.events)
                self.link.start()
        self.gpio = setup_gpio(cfg, self.events)

        self.sound = None
        sound_path = os.path.join(HERE, cfg["sound_file"]) if cfg["sound_file"] else ""
        if sound_path and os.path.exists(sound_path):
            try:
                pygame.mixer.init()
                self.sound = pygame.mixer.Sound(sound_path)
            except pygame.error as e:
                print("Sound off:", e, file=sys.stderr)

        self.add_keys = {pygame.key.key_code(k) for k in cfg["add_keys"]}
        self.spend_keys = {pygame.key.key_code(k) for k in cfg["spend_keys"]}
        self.test_keys = {pygame.key.key_code(k) for k in cfg["test_keys"]}

        u = self.u
        self.f_title = load_font(int(44 * u))
        self.f_label = load_font(int(34 * u))
        self.f_big = load_font(int(230 * u))
        self.f_mid = load_font(int(38 * u))
        self.f_small = load_font(int(22 * u), bold=False)
        self._build_static()
        self._render_number()

    # Everything that never changes is drawn once, here.
    def _build_static(self):
        W, H, u = self.W, self.H, self.u
        s = pygame.Surface((W, H))
        s.fill(BG)

        title = self.f_title.render(self.cfg["title"], True, ORANGE)
        blit_center(s, title, (W // 2, int(48 * u)))
        pygame.draw.line(s, ORANGE, (int(W * 0.08), int(84 * u)), (int(W * 0.92), int(84 * u)), max(2, int(3 * u)))

        # left: credits panel
        self.left_c = (int(W * 0.29), int(H * 0.52))
        lp = pygame.Rect(0, 0, int(W * 0.40), int(H * 0.62))
        lp.center = self.left_c
        pygame.draw.rect(s, PANEL, lp, border_radius=int(24 * u))
        blit_center(s, self.f_label.render("CREDITS", True, GREY), (self.left_c[0], lp.top + int(48 * u)))
        self.left_panel = lp

        # CRT scanlines, baked in so they cost nothing per frame (kept off the QR code)
        lines = pygame.Surface((W, H), pygame.SRCALPHA)
        for y in range(0, H, 3):
            pygame.draw.line(lines, (0, 0, 0, 55), (0, y), (W, y))
        s.blit(lines, (0, 0))

        # right: QR panel
        size = int(min(H * 0.50, W * 0.34))
        self.qr_panel = pygame.Rect(0, 0, size, size)
        self.qr_panel.center = (int(W * 0.71), int(H * 0.50))
        pygame.draw.rect(s, WHITE, self.qr_panel, border_radius=int(18 * u))
        pad = int(size * 0.07)
        url = self.cfg["pay_url"]
        if qrcode and url:
            qr = make_qr(url, size - 2 * pad)
            blit_center(s, qr, self.qr_panel.center)
        else:
            msg = "pip: qrcode missing" if not qrcode else "set pay_url"
            blit_center(s, self.f_small.render(msg, True, BLACK), self.qr_panel.center)
        blit_center(s, self.f_mid.render("SCAN TO PAY", True, WHITE),
                    (self.qr_panel.centerx, self.qr_panel.top - int(34 * u)))
        blit_center(s, self.f_mid.render(self.cfg["price_text"], True, ORANGE),
                    (self.qr_panel.centerx, self.qr_panel.bottom + int(36 * u)))
        blit_center(s, self.f_small.render("Lightning accepted", True, GREY),
                    (self.qr_panel.centerx, self.qr_panel.bottom + int(72 * u)))
        self.static = s.convert()

        self.offline_overlay = pygame.Surface(self.qr_panel.size, pygame.SRCALPHA)
        self.offline_overlay.fill((10, 10, 16, 215))

    def _render_number(self):
        color = ORANGE if self.store.credits > 0 else DIM
        self.number = self.f_big.render(str(self.store.credits), True, color)

    # ------------------------------------------------------------- events --
    def on_credit(self, n, ident):
        if ident and ident not in ("gpio", "TEST", "key"):
            if ident in self.recent_ids:
                return                                   # same invoice twice: ignore
            self.recent_ids.append(ident)
        self.store.add(n)
        self._render_number()
        now = time.time()
        self.pop_at = now
        self.banner_at = now
        self.banner_n = n
        if self.sound:
            self.sound.play()
        print(f"+{n} credit(s) [{ident}] balance={self.store.credits}")

    def on_spend(self):
        if self.store.spend():
            self._render_number()
            self.spend_at = time.time()

    def link_status(self):
        """(color, text, payments_offline)"""
        if self.link is None:
            if self.gpio:
                return GREEN, "Credits via GPIO", False
            return GREY, "No payment input configured", False
        grace = time.time() - self.started < 20
        if not self.link.connected:
            return RED, "ESP32 not connected", not grace
        if time.time() - self.link.last_rx > 15:
            return RED, "ESP32 not responding", not grace
        st = self.link.esp_state
        if st == "OK":
            return GREEN, "Payments online", False
        if st == "NOWIFI":
            return RED, "ESP32 has no Wi-Fi", not grace
        if st == "APIERR":
            return YELLOW, "ESP32 can't reach BTCPay", not grace
        if st == "NOTIME":
            return YELLOW, "ESP32 syncing clock", False
        return YELLOW, "ESP32 starting", False

    # --------------------------------------------------------------- frame --
    def draw(self):
        W, H, u = self.W, self.H, self.u
        now = time.time()
        sc = self.screen
        sc.blit(self.static, (0, 0))

        # credit number with a little "pop" when credits arrive
        t = now - self.pop_at
        scale = 1 + 0.35 * math.sin(math.pi * t / 0.45) if t < 0.45 else 1.0
        num = self.number
        if scale != 1.0:
            num = pygame.transform.smoothscale(
                num, (int(num.get_width() * scale), int(num.get_height() * scale)))
        if now - self.spend_at < 0.25:
            num = num.copy()
            num.fill((255, 255, 255, 0), special_flags=pygame.BLEND_RGBA_MAX)
        blit_center(sc, num, (self.left_c[0], self.left_c[1] + int(10 * u)))

        # prompt under the number, blinking
        blink = int(now * 2) % 2 == 0
        if self.store.credits > 0:
            prompt, col = "PRESS START", WHITE
        else:
            prompt, col = "SCAN THE QR TO PLAY", GREY
        if blink or self.store.credits == 0:
            blit_center(sc, self.f_mid.render(prompt, True, col),
                        (self.left_c[0], self.left_panel.bottom - int(52 * u)))

        # glowing border round the QR while waiting for a player
        color, status_text, offline = self.link_status()
        if self.store.credits == 0 and not offline:
            glow = 0.5 + 0.5 * math.sin(now * 3)
            c = tuple(int(BG[i] + (ORANGE[i] - BG[i]) * glow) for i in range(3))
            pygame.draw.rect(sc, c, self.qr_panel.inflate(int(18 * u), int(18 * u)),
                             max(3, int(5 * u)), border_radius=int(24 * u))
        if offline:
            sc.blit(self.offline_overlay, self.qr_panel.topleft)
            blit_center(sc, self.f_mid.render("PAYMENTS", True, RED),
                        (self.qr_panel.centerx, self.qr_panel.centery - int(24 * u)))
            blit_center(sc, self.f_mid.render("OFFLINE", True, RED),
                        (self.qr_panel.centerx, self.qr_panel.centery + int(24 * u)))

        # payment banner
        t = now - self.banner_at
        if t < 3.0:
            slide = min(1.0, t / 0.25)
            alpha = 255 if t < 2.4 else int(255 * (3.0 - t) / 0.6)
            n = self.banner_n
            text = f"+{n} CREDIT{'S' if n != 1 else ''}  -  THANK YOU!"
            band = pygame.Surface((W, int(96 * u)), pygame.SRCALPHA)
            band.fill(ORANGE + (alpha,))
            label = self.f_mid.render(text, True, BLACK)
            label.set_alpha(alpha)
            blit_center(band, label, (W // 2, band.get_height() // 2))
            y = int(H * 0.5 - band.get_height() / 2)
            sc.blit(band, (int(-W * (1 - slide)), y))

        # status bar
        y = H - int(26 * u)
        pygame.draw.circle(sc, color, (int(30 * u), y), max(4, int(8 * u)))
        sc.blit(self.f_small.render(status_text, True, GREY),
                (int(48 * u), y - self.f_small.get_height() // 2))
        if self.store.last_payment:
            right = self.f_small.render(f"Last payment {self.store.last_payment}", True, GREY)
            sc.blit(right, (W - right.get_width() - int(24 * u), y - right.get_height() // 2))

    # ----------------------------------------------------------------- loop --
    def handle_pygame_event(self, e):
        if e.type == pygame.QUIT:
            return False
        if e.type == pygame.KEYDOWN:
            if e.key == pygame.K_ESCAPE:
                return False
            if e.key in self.add_keys:
                self.events.put(("credit", 1, "key"))
            elif e.key in self.spend_keys:
                self.events.put(("spend",))
            elif e.key in self.test_keys and self.link:
                self.link.send("TEST")
        return True

    def process_queue(self):
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return
            if ev[0] == "credit":
                self.on_credit(ev[1], ev[2])
            elif ev[0] == "spend":
                self.on_spend()

    def run(self):
        clock = pygame.time.Clock()
        running = True
        while running:
            for e in pygame.event.get():
                running = self.handle_pygame_event(e) and running
            self.process_queue()
            self.draw()
            pygame.display.flip()
            clock.tick(self.cfg["fps"])


def main():
    ap = argparse.ArgumentParser(description="Bitcoin Ekasi arcade credit display")
    ap.add_argument("--windowed", action="store_true", help="run in a 1280x720 window")
    args = ap.parse_args()

    cfg = dict(CONFIG)
    if args.windowed:
        cfg["fullscreen"] = False

    pygame.init()
    pygame.display.set_caption("Bitcoin Ekasi Arcade")
    if cfg["fullscreen"]:
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        pygame.mouse.set_visible(False)
    else:
        screen = pygame.display.set_mode((1280, 720))
    try:
        CreditDisplay(cfg, screen).run()
    finally:
        pygame.quit()


if __name__ == "__main__":
    main()
