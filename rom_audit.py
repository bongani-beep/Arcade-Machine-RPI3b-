#!/usr/bin/env python3
"""
rom_audit.py - launch every ROM in the real emulator for a moment and record which ones load.

    python3 rom_audit.py --test      # try a small sample (good + deliberately bad files)
    python3 rom_audit.py --run       # test every ROM (resumable), then move definite failures
    python3 rom_audit.py --report    # print the summary of results so far

Each ROM is opened with `retroarch --max-frames N` using the system's normal core, with audio
off, RetroArch's network commands off (so the credit service never sees the test game), and
saves/history redirected away from the games. Nothing is deleted: definite load failures are
MOVED to NOT_PLAYING_DIR with a report. Only games are ever touched: BIOS sets are skipped, and
a system whose failure rate is suspiciously high is left alone and flagged instead.
"""

import os
import re
import shutil
import subprocess
import sys
import time

ROOT = "/home/pi/RetroPie/roms"
NOT_PLAYING_DIR = "/home/pi/RetroPie/_not_playing"      # outside roms/, so EmulationStation does not list it
WORK = "/home/pi/rom_audit"
RESULTS = os.path.join(WORK, "results.tsv")
RA = "/opt/retropie/emulators/retroarch/bin/retroarch"
CORES = "/opt/retropie/libretrocores/"
FRAMES = 45
TIMEOUT_S = 60
MAX_FAIL_RATE = 0.30          # if more than this share of a system fails, do not move anything

SYSTEMS = {
    #  system          core                                   extensions
    "snes":          ("lr-snes9x2010/snes9x2010_libretro.so",      {".smc", ".sfc", ".fig", ".swc"}),
    "megadrive":     ("lr-genesis-plus-gx/genesis_plus_gx_libretro.so", {".gen", ".md", ".smd", ".bin"}),
    "mastersystem":  ("lr-genesis-plus-gx/genesis_plus_gx_libretro.so", {".sms"}),
    "nes":           ("lr-fceumm/fceumm_libretro.so",              {".nes"}),
    "mame-libretro": ("lr-mame2003/mame2003_libretro.so",          {".zip"}),
    "neogeo":        ("lr-fbneo/fbneo_libretro.so",                {".zip"}),
}
SKIP_DIRS = {"mame2003", "neogeo", "cfg", "ctrlr", "diff", "hi", "memcard", "nvram", "snap", "sta", "artwork"}
# BIOS / device sets that other games depend on. They are never games, never tested, never moved.
BIOS_SETS = {"neogeo", "pgm", "playch10", "decocass", "skns", "stvbios", "konamigx", "namcond1",
             "cpzn1", "cpzn2", "taitofx1", "megaplay", "megatech", "nmk004", "isgsm", "ym2608",
             "aes", "hng64", "coh1000c", "coh3002c", "coh1002m", "cpsb"}
# The ONLY definite failure marker: RetroArch's own "the core refused this content" line.
# (Lines such as "ROM ... is required" also appear for games that load fine, because the
# emulator accepts alternative BIOS files, and audio/GameMode warnings are harmless.)
FAIL_PATTERNS = re.compile(r"\[ERROR\] \[Content\]: Failed to load content")

APPEND_CFG = """video_fullscreen = "true"
audio_enable = "false"
audio_driver = "null"
network_cmd_enable = "false"
config_save_on_exit = "false"
history_list_enable = "false"
savefile_directory = "/tmp/audit_saves"
savestate_directory = "/tmp/audit_saves"
savestate_auto_save = "false"
savestate_auto_load = "false"
"""


def setup():
    os.makedirs(WORK, exist_ok=True)
    os.makedirs("/tmp/audit_saves", exist_ok=True)
    with open(os.path.join(WORK, "audit.cfg"), "w") as f:
        f.write(APPEND_CFG)


def run_one(system, path):
    """Returns (status, detail): OK / FAIL (definite load failure) / CRASH / TIMEOUT."""
    core = SYSTEMS[system][0]
    log = os.path.join(WORK, "one.log")
    if os.path.exists(log):
        os.remove(log)
    cmd = [RA, "-L", CORES + core, "--config", "/opt/retropie/configs/%s/retroarch.cfg" % system,
           "--appendconfig", os.path.join(WORK, "audit.cfg"), "--max-frames", str(FRAMES),
           "--verbose", "--log-file", log, path]
    try:
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, timeout=TIMEOUT_S)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        subprocess.run(["pkill", "-9", "-x", "retroarch"])
        return "TIMEOUT", "no exit after %ds" % TIMEOUT_S
    text = ""
    try:
        with open(log, errors="replace") as f:
            text = f.read()
    except OSError:
        pass
    if FAIL_PATTERNS.search(text):
        # The reason is the last core-specific error line before the failure, e.g. the missing ROM.
        reasons = [re.sub(r"\s+", " ", l.strip()) for l in text.splitlines()
                   if "[libretro ERROR]" in l or "Failed to process content" in l]
        return "FAIL", "; ".join(reasons[:2])[:200] or "core refused the file"
    if rc != 0:
        return "CRASH", "exit code %d" % rc
    if "Frames pushed" not in text and "Content ran for" not in text:
        return "CRASH", "never started running"
    return "OK", ""


def list_roms():
    roms = []
    for system, (_, exts) in SYSTEMS.items():
        base = os.path.join(ROOT, system)
        for dirpath, dirs, files in os.walk(base):
            if system == "mame-libretro":
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in sorted(files):
                ext = os.path.splitext(name)[1].lower()
                if ext not in exts:
                    continue
                if system in ("mame-libretro", "neogeo") and os.path.splitext(name)[0].lower() in BIOS_SETS:
                    continue
                roms.append((system, os.path.join(dirpath, name)))
    return sorted(roms)


def load_results():
    done = {}
    if os.path.exists(RESULTS):
        with open(RESULTS) as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 3:
                    done[parts[2]] = parts
    return done


CANARY = ("mame-libretro", os.path.join(ROOT, "mame-libretro", "snowbro2.zip"))   # known to play
CANARY_EVERY = 40


def canary_ok():
    """A game known to work. If it stops loading, the display or emulator is the problem, not
    the ROMs, so the run must stop instead of blaming the games."""
    status, detail = run_one(*CANARY)
    return status == "OK", "%s %s" % (status, detail)


def confirm_failures():
    """Re-test every FAIL once; only a ROM that fails twice counts. Re-checks the canary first."""
    ok, why = canary_ok()
    if not ok:
        print("canary failed (%s); not confirming or moving anything" % why, flush=True)
        return False
    res = load_results()
    fails = [r for r in res.values() if r[0] == "FAIL"]
    print("re-testing %d failed ROM(s)" % len(fails), flush=True)
    with open(RESULTS, "a") as out:
        for status, system, path, *_ in fails:
            if not os.path.exists(path):
                continue
            status2, detail2 = run_one(system, path)
            out.write("\t".join(["FAIL" if status2 == "FAIL" else ("OK" if status2 == "OK" else status2),
                                 system, path, detail2 or "passed on retry"]) + "\n")
            out.flush()
    ok, why = canary_ok()
    if not ok:
        print("canary failed after re-testing (%s); nothing moved" % why, flush=True)
        return False
    return True


def summarize():
    res = load_results()
    by = {}
    for status, system, path, *_ in res.values():
        by.setdefault(system, {}).setdefault(status, 0)
        by[system][status] += 1
    for system in sorted(by):
        print("%-14s %s" % (system, "  ".join("%s=%d" % kv for kv in sorted(by[system].items()))))
    return by


def move_failures():
    res = load_results()
    by = {}
    for status, system, path, *_ in res.values():
        by.setdefault(system, []).append(status)
    report = []
    moved = 0
    for system, sts in by.items():
        fails = sts.count("FAIL")
        rate = fails / max(1, len(sts))
        if rate > MAX_FAIL_RATE:
            report.append("SKIPPED %s: %d of %d failed (%.0f%%), too many to trust, nothing moved"
                          % (system, fails, len(sts), rate * 100))
            continue
    keep = {s for s, sts in by.items() if sts.count("FAIL") / max(1, len(sts)) > MAX_FAIL_RATE}
    for status, system, path, *rest in sorted(res.values(), key=lambda r: r[2]):
        if status != "FAIL" or system in keep or not os.path.exists(path):
            continue
        rel = os.path.relpath(path, os.path.join(ROOT, system))
        dst = os.path.join(NOT_PLAYING_DIR, system, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(path, dst)
        moved += 1
        report.append("MOVED  %-13s %s   [%s]" % (system, rel, rest[0] if rest else ""))
    os.makedirs(NOT_PLAYING_DIR, exist_ok=True)
    with open(os.path.join(NOT_PLAYING_DIR, "REPORT.txt"), "w") as f:
        f.write("ROMs that failed to load in the emulator, moved here by rom_audit.py.\n"
                "To restore one, move it back to /home/pi/RetroPie/roms/<system>/<same path>.\n\n")
        f.write("\n".join(report) + "\n")
    print("moved %d ROM(s); report: %s/REPORT.txt" % (moved, NOT_PLAYING_DIR))


def main():
    setup()
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--test":
        sample = []
        roms = list_roms()
        for system in SYSTEMS:
            sample += [r for r in roms if r[0] == system][:2]
        bad = os.path.join(WORK, "bad")
        os.makedirs(bad, exist_ok=True)
        for name, data in (("garbage.zip", b"this is not a zip file" * 50), ("empty.smc", b""), ("junk.nes", b"\x00" * 200)):
            with open(os.path.join(bad, name), "wb") as f:
                f.write(data)
        sample += [("mame-libretro", os.path.join(bad, "garbage.zip")), ("snes", os.path.join(bad, "empty.smc")),
                   ("nes", os.path.join(bad, "junk.nes"))]
        for system, path in sample:
            t = time.time()
            status, detail = run_one(system, path)
            print("%-8s %-13s %5.1fs  %s  %s" % (status, system, time.time() - t, os.path.basename(path), detail))
    elif mode == "--report":
        summarize()
    elif mode == "--run":
        done = load_results()
        roms = list_roms()
        print("%d ROMs to test, %d already done" % (len(roms), len(done)), flush=True)
        start = time.time()
        ok, why = canary_ok()
        if not ok:
            print("canary game does not load (%s): display or emulator problem, not starting" % why, flush=True)
            return
        n = 0
        with open(RESULTS, "a") as out:
            for i, (system, path) in enumerate(roms, 1):
                if path in done:
                    continue
                status, detail = run_one(system, path)
                out.write("\t".join([status, system, path, detail]) + "\n")
                out.flush()
                n += 1
                if n % CANARY_EVERY == 0:
                    ok, why = canary_ok()
                    if not ok:
                        # the last batch cannot be trusted: forget it so it is re-tested next time
                        print("canary failed at ROM %d (%s): STOPPING. Fix the display, then re-run --run." % (i, why), flush=True)
                        keep = [l for l in open(RESULTS).read().splitlines()][:-CANARY_EVERY]
                        open(RESULTS, "w").write("\n".join(keep) + ("\n" if keep else ""))
                        return
                    print("%d/%d  %.0f min elapsed" % (i, len(roms), (time.time() - start) / 60), flush=True)
        print("testing finished", flush=True)
        summarize()
        if confirm_failures():
            summarize()
            move_failures()
        else:
            print("NOTHING WAS MOVED. Check the display, then run --run again.", flush=True)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
