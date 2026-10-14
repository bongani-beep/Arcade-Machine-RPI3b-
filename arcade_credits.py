#!/usr/bin/env python3
"""
arcade_credits.py — insert arcade coins when the ESP32 signals a paid credit.

The ESP32 (BTCPay payment trigger) drives a relay whose dry contacts are wired
between CREDIT_GPIO and GND. Each brief closure = one settled Lightning payment.
For every closure this script "presses" COIN_KEY a few times on a virtual
keyboard; lr-mame2003 / lr-fbneo read that as Player 1 "Select" = Insert Coin.

Wiring:
    relay COM  -> Pi GND               (e.g. physical pin 9)
    relay NO   -> Pi GPIO17 / pin 11   (internal pull-up; a closure pulls it LOW)

Timed systems (SNES): a payment banks play time (set-credit-time <minutes>), shown
on screen via RetroArch network commands; the game pauses at zero until the next
payment. Arcade cores keep the coin-key behaviour above.

Test without paying:  sudo systemctl kill -s SIGUSR1 arcade-credits
Logs:                 journalctl -u arcade-credits -f
"""

import fcntl
import json
import math
import os
import signal
import socket
import struct
import sys
import threading
import time

import RPi.GPIO as GPIO

# ============================== CONFIG ==============================
CREDIT_GPIO       = 17     # BCM numbering (physical pin 11)
COINS_PER_PAYMENT = 2      # coin presses emitted per payment pulse  <-- change me
ACTIVE_LOW        = True   # relay closure pulls the pin to GND (LOW)

COIN_KEY          = 54     # KEY_RIGHTSHIFT — matches input_player1_select in retroarch.cfg
COIN_HOLD_MS      = 70     # how long each coin key is held
COIN_GAP_MS       = 140    # pause between consecutive coin presses

POLL_MS           = 5      # input polling interval

# --- false-credit guard ---
# A real payment is ONE clean relay pulse of RELAY_PULSE_MS (300 ms in the ESP32
# sketch), on a line that was quiet before and stays quiet after. An ESP32 reset or a
# floating relay input produces short, repeated or long pulses instead.
MIN_PULSE_MS      = 200    # shorter than this = glitch (a payment pulse is ~300 ms)
MAX_PULSE_MS      = 700    # longer than this = line stuck/held, not a payment
WIDTH_CAP_MS      = 5000   # stop measuring a pulse after this long (for logging only)
CONFIRM_S         = 1.5    # after a valid pulse the line must stay quiet this long
NOISE_LOCKOUT_S   = 5      # after any rejected pulse, refuse payments for this long
MIN_PAYMENT_GAP_S = 8      # real payments can't arrive faster than the ESP32 makes invoices
STARTUP_GRACE_S   = 5      # ignore the line for this long after the service starts

# --- timed play (SNES etc.) ---
# Cores whose name contains one of these (case-insensitive) are "timed": a payment
# banks play time instead of pressing the coin key, the time counts down while the
# game runs, and at zero the game is paused with INSERT COIN until the next payment.
ALL_TIMED         = True   # True: every system is paid by time. False: only TIMED_CORES.
TIMED_CORES       = ("snes", "mame 2003")   # used only when ALL_TIMED is False
# Arcade cores also get the coin key pressed after each payment (their games need a coin
# to start; console cores must NOT get it - Select does things in some games). Names are
# matched, case-insensitively, against RetroArch's core name.
COIN_CORES        = ("mame", "finalburn", "fb alpha")
CONF_PATH         = "/home/pi/arcade_credits.conf"       # change with: set-credit-time <minutes>
STATE_PATH        = "/var/lib/arcade_credits/state.json"
DEFAULT_MINUTES   = 10     # minutes per credit if the conf file is missing
RA_ADDR           = ("127.0.0.1", 55355)   # RetroArch network_cmd_port
SAVE_EVERY_S      = 10
# ==================================================================

# --- /dev/uinput minimal ABI (armhf / 32-bit) ---
UI_SET_EVBIT  = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
EV_SYN, EV_KEY = 0x00, 0x01
SYN_REPORT = 0x00
BUS_USB = 0x03


def log(msg):
    print("[arcade_credits] " + msg, flush=True)


class VirtualKeyboard:
    def __init__(self, name="arcade-credits-coin", keys=(COIN_KEY,)):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_SYN)
        for k in keys:
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, k)
        # struct uinput_user_dev: name[80], input_id(4*u16), ff_effects_max(u32),
        # absmax/absmin/absfuzz/absflat [ABS_CNT=64] each (s32)
        uidev = struct.pack(
            "80sHHHHi" + "256i",
            name.encode(), BUS_USB, 0x1, 0x1, 1, 0, *([0] * 256)
        )
        os.write(self.fd, uidev)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        time.sleep(0.3)  # let udev create the node so RetroArch picks it up

    def _emit(self, etype, code, value):
        # struct input_event: timeval(2*long), u16 type, u16 code, s32 value
        os.write(self.fd, struct.pack("llHHi", 0, 0, etype, code, value))

    def tap(self, code, hold_ms):
        self._emit(EV_KEY, code, 1)
        self._emit(EV_SYN, SYN_REPORT, 0)
        time.sleep(hold_ms / 1000.0)
        self._emit(EV_KEY, code, 0)
        self._emit(EV_SYN, SYN_REPORT, 0)

    def close(self):
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self.fd)


def insert_coins(kbd, n):
    for i in range(n):
        kbd.tap(COIN_KEY, COIN_HOLD_MS)
        if i != n - 1:
            time.sleep(COIN_GAP_MS / 1000.0)
    log("inserted {} coin(s)".format(n))


def read_conf():
    """key = value lines from CONF_PATH. Read live so set-credit-time and
    set-free-play take effect without a restart."""
    conf = {}
    try:
        with open(CONF_PATH) as f:
            for line in f:
                key, sep, val = line.partition("=")
                if sep:
                    conf[key.strip()] = val.strip()
    except OSError:
        pass
    return conf


def minutes_per_credit():
    try:
        m = float(read_conf().get("minutes_per_credit", DEFAULT_MINUTES))
        return m if m > 0 else float(DEFAULT_MINUTES)
    except ValueError:
        return float(DEFAULT_MINUTES)


def free_play():
    """Free play: timed games run without credits and the timer doesn't count down."""
    return read_conf().get("free_play", "off").lower() in ("on", "1", "true", "yes")


def fmt_time(secs):
    m, s = divmod(int(secs), 60)
    return "{:02d}:{:02d}".format(m, s)


class RetroArch:
    """Talks to RetroArch's UDP network command port (network_cmd_enable)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.5)

    def _send(self, cmd):
        self.sock.sendto(cmd.encode(), RA_ADDR)

    def status(self):
        """Returns (state, core_name) e.g. ("PLAYING", "Snes9x 2010"), or None."""
        with self.lock:
            self.sock.setblocking(False)
            try:
                while True:
                    self.sock.recv(4096)        # drop stale replies
            except (BlockingIOError, OSError):
                pass
            self.sock.settimeout(0.5)
            for _ in range(2):
                try:
                    self._send("GET_STATUS")
                    reply = self.sock.recv(4096).decode(errors="replace").strip()
                except (socket.timeout, OSError):
                    continue
                parts = reply.split(" ", 2)
                if len(parts) < 2 or parts[0] != "GET_STATUS":
                    continue
                core = parts[2].split(",")[0] if len(parts) > 2 else ""
                return parts[1], core
        return None

    def command(self, cmd):
        with self.lock:
            try:
                self._send(cmd)
            except OSError:
                pass

    def show_msg(self, text):
        self.command("SHOW_MSG " + text)


def is_timed(core):
    """True if a game on this core is paid by time. `core` is "" when no game is loaded."""
    if not core:
        return False
    return ALL_TIMED or any(t in core.lower() for t in TIMED_CORES)


def wants_coin(core):
    return any(t in core.lower() for t in COIN_CORES)


class Credits:
    """Banked play time (seconds), saved so it survives a reboot."""

    def __init__(self):
        self.lock = threading.Lock()
        self.balance = 0.0
        try:
            with open(STATE_PATH) as f:
                self.balance = float(json.load(f).get("balance_s", 0))
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"balance_s": self.balance}, f)
            os.chmod(tmp, 0o644)
            os.replace(tmp, STATE_PATH)
        except OSError as e:
            log("could not save state: {}".format(e))

    def add_payment(self):
        with self.lock:
            self.balance += minutes_per_credit() * 60
            self.save()
            return self.balance

    def spend(self, secs):
        with self.lock:
            self.balance = max(0.0, self.balance - secs)
            return self.balance

    def message(self):
        with self.lock:
            bal = self.balance
        n = int(math.ceil(bal / (minutes_per_credit() * 60))) if bal > 0 else 0
        return "CREDITS {}   TIME {}".format(n, fmt_time(bal))


def timer_loop(ra, credits):
    """Once a second: count down SNES play time, pause at zero, resume on payment."""
    self_paused = False       # True only if WE paused the game (never undo a manual pause)
    last_save = last_msg = 0.0
    was_free = False
    st0 = ra.status()
    if st0 and is_timed(st0[1]) and st0[0] == "PAUSED":
        # A previous run of this service paused it (we were restarted); keep managing it
        # or it would stay stuck paused forever.
        self_paused = True
        log("took over a paused game left by an earlier run")
    while True:
        time.sleep(1)
        now = time.monotonic()
        free = free_play()
        if free != was_free:
            was_free = free
            log("free play {}".format("ON" if free else "OFF"))
            last_msg = 0.0
        st = ra.status()
        if st is None or not is_timed(st[1]):
            self_paused = False          # no game, menu, or a non-timed (arcade) core
            continue
        state = st[0]
        if free:                         # no countdown, no lock; release a lock we set
            if state == "PAUSED" and self_paused:
                ra.command("PAUSE_TOGGLE")
                self_paused = False
            if state == "PLAYING" and now - last_msg >= 60:
                last_msg = now
                ra.show_msg("FREE PLAY")
            continue
        if state == "PLAYING":
            left = credits.spend(1)
            if left <= 0:
                ra.command("PAUSE_TOGGLE")
                self_paused = True
                ra.show_msg("TIME UP - INSERT COIN")
                credits.save()
                log("time up, game paused")
            elif now - last_msg >= (5 if left < 60 else 30):
                last_msg = now
                ra.show_msg(credits.message())
        elif state == "PAUSED" and self_paused:
            if credits.balance > 0:
                ra.command("PAUSE_TOGGLE")
                self_paused = False
                log("credit received, game resumed")
            elif now - last_msg >= 3:
                last_msg = now
                ra.show_msg("INSERT COIN - " + credits.message())
        if now - last_save >= SAVE_EVERY_S:
            last_save = now
            credits.save()


def coins_when_playing(kbd, ra):
    """Press the coin key once the game is actually running (a locked/paused game ignores
    it, and a payment that unlocks the game resumes it within about a second)."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        st = ra.status()
        if st and st[0] == "PLAYING":
            time.sleep(0.5)
            insert_coins(kbd, COINS_PER_PAYMENT)
            return
        time.sleep(0.5)
    log("game was not running; coin key not pressed")


def handle_payment(kbd, ra, credits):
    """A payment banks play time for whatever is running (or will run). Arcade cores also
    get the coin key, since their games need a coin to start."""
    st = ra.status()
    credits.add_payment()
    log("banked {:g} min ({})".format(minutes_per_credit(), credits.message()))
    ra.show_msg("CREDIT ADDED   " + credits.message())
    if st and st[1] and wants_coin(st[1]):
        threading.Thread(target=coins_when_playing, args=(kbd, ra), daemon=True).start()


def pin_active():
    lvl = GPIO.input(CREDIT_GPIO)
    return lvl == GPIO.LOW if ACTIVE_LOW else lvl == GPIO.HIGH


def pulse_width_ms():
    """Call right after an idle->active edge; returns how long the line stayed active
    (measured up to WIDTH_CAP_MS so a too-long pulse still logs its real length)."""
    t0 = time.monotonic()
    while pin_active() and (time.monotonic() - t0) * 1000.0 <= WIDTH_CAP_MS:
        time.sleep(POLL_MS / 1000.0)
    return (time.monotonic() - t0) * 1000.0


def wait_idle():
    while pin_active():
        time.sleep(POLL_MS / 1000.0)


def active_within(seconds):
    """True if the line goes active again within `seconds`."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if pin_active():
            return True
        time.sleep(POLL_MS / 1000.0)
    return False


def watch_credit_line(on_payment, should_stop=lambda: False):
    """Calls on_payment() once per genuine payment pulse; rejects noise (see CONFIG)."""
    started = time.monotonic()
    last_accepted = last_noise = float("-inf")
    prev_active = pin_active()
    while not should_stop():
        time.sleep(POLL_MS / 1000.0)
        active = pin_active()
        edge = active and not prev_active
        prev_active = active
        if not edge:
            continue

        t0 = time.monotonic()
        width = pulse_width_ms()
        if width > MAX_PULSE_MS:
            wait_idle()                        # held line: wait for it to release
        now = time.monotonic()
        prev_active = pin_active()

        if width < MIN_PULSE_MS or width > MAX_PULSE_MS:
            last_noise = now
            log("ignored noise: pulse of {:.0f} ms (a payment is ~300 ms)".format(width))
        elif now - started < STARTUP_GRACE_S:
            log("ignored pulse during startup grace period")
        elif now - last_noise < NOISE_LOCKOUT_S:
            last_noise = now
            log("ignored pulse: line was noisy in the last {} s".format(NOISE_LOCKOUT_S))
        elif t0 - last_accepted < MIN_PAYMENT_GAP_S:
            last_noise = now
            log("ignored pulse: within {} s of the last payment".format(MIN_PAYMENT_GAP_S))
        elif active_within(CONFIRM_S):
            wait_idle()                        # a burst is noise, not a payment
            prev_active = False
            last_noise = time.monotonic()
            log("ignored burst: line pulsed again within {} s".format(CONFIRM_S))
        else:
            last_accepted = t0
            log("payment pulse detected on BCM{} ({:.0f} ms)".format(CREDIT_GPIO, width))
            on_payment()
            prev_active = pin_active()


def main():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(CREDIT_GPIO, GPIO.IN,
               pull_up_down=GPIO.PUD_UP if ACTIVE_LOW else GPIO.PUD_DOWN)

    kbd = VirtualKeyboard()
    log("virtual keyboard ready; watching BCM{} ({}), {} coin(s)/payment, "
        "initial state = {}".format(
            CREDIT_GPIO, "active-low" if ACTIVE_LOW else "active-high",
            COINS_PER_PAYMENT, "ACTIVE" if pin_active() else "idle"))

    ra = RetroArch()
    credits = Credits()
    log("timed credits: {:g} min per credit, balance {}".format(
        minutes_per_credit(), credits.message()))
    threading.Thread(target=timer_loop, args=(ra, credits), daemon=True).start()

    signal.signal(signal.SIGUSR1,
                  lambda *_: (log("SIGUSR1: simulating a payment"),
                              handle_payment(kbd, ra, credits)))

    def shutdown(*_):
        log("shutting down")
        try:
            credits.save()
            GPIO.cleanup()
        finally:
            kbd.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    watch_credit_line(lambda: handle_payment(kbd, ra, credits))


if __name__ == "__main__":
    main()
