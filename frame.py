#!/usr/bin/env python3
"""frame — control a Samsung Frame TV and keep its Art Mode filled with your photos.

  frame pair               one-time: approve this Mac on the TV, save its address
  frame art | off          Art Mode (closes background apps)
  frame hdmi               switch to the HDMI input (see "hdmi_presses" in config)
  frame youtube | netflix | appletv | prime
  frame tidy               close background streaming apps
  frame shuffle            jump to a random photo now (slideshow carries on from it)
  frame status
  frame brightness [0-10]  show or set Art Mode brightness
  frame check [--dry-run]  heads-up iMessage if something's wrong (runs every 30 min)
  frame sync [--dry-run]   refresh Art Mode photos from your iCloud album or folder

Settings: ~/.config/frame-tv/config.json (see config.example.json).
"""
from __future__ import annotations

import io
import json
import os
import random
import sys
import time
import warnings
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

import requests
from PIL import Image, ImageOps
from samsungtvws import SamsungTVWS

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass

from samsungtvws import exceptions as _tvexc
from samsungtvws.art import art as _artmod


def _patient_open(self):
    """Library's open() fails if any event precedes ms.channel.ready
    (e.g. another client connecting). Skip those instead."""
    _artmod.SamsungTVWSConnection.open(self)
    for _ in range(10):
        event, frame = self._recv_frame()
        if event == _artmod.MS_CHANNEL_READY_EVENT:
            return self.connection
        if event not in ("ms.channel.clientConnect", "ms.channel.clientDisconnect"):
            break
    self.close()
    raise _tvexc.ConnectionFailure(frame)


_artmod.SamsungTVArt.open = _patient_open

CONF = Path(os.environ.get("FRAME_CONF", Path.home() / ".config/frame-tv")).expanduser()
CONFIG_FILE = CONF / "config.json"
HOST_FILE = CONF / "host"   # last known TV address; re-found by Wi-Fi MAC if the router changes it
TOKEN = CONF / "token"
STATE = CONF / "state.json"
CACHE = CONF / "cache"
LOG = CONF / "sync.log"

DEFAULTS = {
    "tv_host": "",            # TV's IP address (Settings > General > Network > Network Status)
    "tv_mac": "",             # filled in by `frame pair`; used to find the TV if its IP changes
    "source": {"type": "icloud_shared_album", "url": ""},
    "alert_imessage_to": "",  # phone number or Apple ID email for alerts; empty = log only
    "total_pictures": 150,    # pictures kept on the TV (the TV's storage is limited)
    "recent_pictures": 30,    # newest pictures always included
    "rotate_per_day": 25,     # random pictures swapped out each daily sync
    "slideshow_minutes": 15,
    "hdmi_presses": 2,        # KEY_HDMI presses from Art Mode/Home to reach your input
    "mat_color": [242, 239, 233],
}
CFG = dict(DEFAULTS)
if CONFIG_FILE.exists():
    CFG.update(json.loads(CONFIG_FILE.read_text()))

TV_MAC = (CFG["tv_mac"] or "").lower()
HOST = os.environ.get("FRAME_HOST") or (HOST_FILE.read_text().strip() if HOST_FILE.exists() else CFG["tv_host"])

APPS = {
    "youtube": "111299001912",
    "netflix": "3201907018807",
    "appletv": "3201807016597",
    "prime": "3201910019365",
}

# Art Mode library shape
TOTAL_FRAMES = int(CFG["total_pictures"])
RECENT_FRAMES = int(CFG["recent_pictures"])
ROTATE_PER_NIGHT = int(CFG["rotate_per_day"])
SLIDESHOW_MINUTES = int(CFG["slideshow_minutes"])
W, H = 3840, 2160
PAPER = tuple(CFG["mat_color"])
MARGIN, GAP = 70, 50


def log(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    CONF.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def tv(timeout=15) -> SamsungTVWS:
    return SamsungTVWS(host=HOST, port=8002, token_file=str(TOKEN), name="FrameControl", timeout=timeout)


def with_timeout(fn, seconds=12, default=None):
    """Run fn in a thread; give up if the TV's art service is wedged."""
    import threading
    box = {}
    th = threading.Thread(target=lambda: box.setdefault("v", fn()), daemon=True)
    th.start()
    th.join(seconds)
    return box.get("v", default)


def artmode(t: SamsungTVWS) -> str | None:
    return with_timeout(lambda: t.art().get_artmode())


def leave_art(t: SamsungTVWS):
    if artmode(t) == "on":
        with_timeout(lambda: t.art().set_artmode("off"))
        time.sleep(2)


def ensure_slideshow(art) -> str:
    """Make sure My Photos is cycling; returns 'on', 'restarted', or 'failed'.

    Uses in-order mode: this TV's shuffle mode snaps back to one fixed photo whenever
    a remote connection closes, so it effectively stops rotating. Variety comes from
    the daily sync choosing a random set instead.
    """
    cur = with_timeout(lambda: art.get_slideshow_status(), 20)
    if cur and cur.get("value") not in (None, "", "off") and cur.get("category_id") == "MY-C0002" \
            and cur.get("type") == "slideshow":
        return "on"
    with_timeout(lambda: art.set_slideshow_status(duration=SLIDESHOW_MINUTES, type=False, category=2), 20)
    time.sleep(3)
    cur = with_timeout(lambda: art.get_slideshow_status(), 20)
    return "restarted" if cur and cur.get("value") not in (None, "", "off") else "failed"


def _device(host: str, timeout=3) -> dict | None:
    try:
        d = requests.get(f"http://{host}:8001/api/v2/", timeout=timeout).json()["device"]
    except Exception:
        return None
    if TV_MAC:
        return d if (d.get("wifiMac") or "").lower() == TV_MAC else None
    return d if d.get("FrameTVSupport") == "true" else None


def rediscover() -> str | None:
    """Scan the Mac's local subnet for the TV (by MAC) and remember its address."""
    global HOST
    import ipaddress
    import socket
    import subprocess
    from concurrent.futures import ThreadPoolExecutor
    try:
        cfg = subprocess.run(["ifconfig", CFG.get("network_interface", "en0")], capture_output=True, text=True).stdout
        parts = next(l.split() for l in cfg.splitlines() if l.strip().startswith("inet "))
        net = ipaddress.ip_network(f"{parts[1]}/{bin(int(parts[3], 16)).count('1')}", strict=False)
    except Exception:
        return None
    if net.num_addresses > 4096:
        return None

    def probe(ip):
        try:
            with socket.create_connection((ip, 8001), timeout=0.7):
                pass
        except OSError:
            return None
        return ip if _device(ip) else None

    with ThreadPoolExecutor(128) as ex:
        found = next((ip for ip in ex.map(probe, (str(h) for h in net.hosts())) if ip), None)
    if found and found != HOST:
        log(f"tv: address changed {HOST} -> {found}")
        HOST = found
        HOST_FILE.write_text(found + "\n")
    return found


def is_on() -> bool:
    d = _device(HOST)
    if d is None and TV_MAC and not os.environ.get("FRAME_HOST") and rediscover():
        d = _device(HOST)
    if d is None:
        return False
    ps = d.get("PowerState")
    # Since 2026-11-09 the TV reports PowerState "" while on (firmware shows "Unknown").
    # It's off the network when truly off, so only an explicit standby means off.
    return ps in ("on", "", None)


# ---------- remote control ----------

def tidy(t: SamsungTVWS, keep: str | None = None) -> list[str]:
    closed = []
    for name, app_id in APPS.items():
        if app_id == keep:
            continue
        try:
            if t.rest_app_status(app_id).get("running"):
                t.rest_app_close(app_id)
                closed.append(name)
        except Exception:
            pass
    return closed


def cmd_app(name: str):
    t = tv()
    app_id = APPS[name]
    tidy(t, keep=app_id)
    leave_art(t)
    t.rest_app_run(app_id)
    for _ in range(10):
        time.sleep(1)
        if t.rest_app_status(app_id).get("visible"):
            print(f"{name}: on screen")
            return
    sys.exit(f"{name}: launch sent but TV doesn't report it visible")


def cmd_art():
    t = tv()
    closed = tidy(t)
    if artmode(t) == "on":  # TV never answers "on" when already on — skip the wait
        print("art mode: on (already)" + (f" (closed {', '.join(closed)})" if closed else ""))
        return
    with_timeout(lambda: t.art().set_artmode("on"))
    time.sleep(2)
    state = artmode(t)
    if state is None:
        sys.exit("art mode: TV's art service isn't responding — restart the TV (hold power ~5s)")
    print(f"art mode: {state}" + (f" (closed {', '.join(closed)})" if closed else ""))
    if state != "on":
        sys.exit(1)


def cmd_hdmi():
    t = tv()
    tidy(t)
    leave_art(t)
    # KEY_HDMI1/KEY_HDMI2 are ignored on the tested model; KEY_HDMI steps through
    # sources instead. Count the presses your TV needs and set "hdmi_presses".
    for i in range(int(CFG["hdmi_presses"])):
        if i:
            time.sleep(2.5)
        t.send_key("KEY_HDMI")
    print("📺 Switched to HDMI")


def cmd_pair(host: str | None):
    host = host or CFG["tv_host"]
    if not host:
        sys.exit("usage: frame pair <TV IP address>   (TV: Settings > General > Network > Network Status)")
    try:
        d = requests.get(f"http://{host}:8001/api/v2/", timeout=5).json()["device"]
    except Exception as e:
        sys.exit(f"Can't reach a Samsung TV at {host}: {e!r}\n"
                 "Check the address, that the TV is on, and (macOS) that Terminal has Local Network access.")
    if d.get("FrameTVSupport") != "true":
        print(f"warning: {d.get('modelName')} doesn't report Frame/Art Mode support")
    CONF.mkdir(parents=True, exist_ok=True)
    print(f"Found {d.get('modelName')} at {host}. Look at the TV and choose Allow…")
    t = SamsungTVWS(host=host, port=8002, token_file=str(TOKEN), name="FrameControl", timeout=60)
    t.rest_device_info()
    apps = t.app_list() or []  # opens the authenticated channel, which triggers the Allow prompt
    if apps:
        print("Installed apps (add any to APPS in frame.py to control it):")
        for a in sorted(apps, key=lambda a: a["name"].lower()):
            print(f"  {a['name']}: {a['appId']}")
    if not TOKEN.exists():
        sys.exit("No token saved — the TV didn't approve. Run `frame pair` again and choose Allow.")
    cfg = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else dict(DEFAULTS)
    cfg["tv_host"], cfg["tv_mac"] = host, (d.get("wifiMac") or "").lower()
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n")
    HOST_FILE.write_text(host + "\n")
    print(f"Paired. Saved address and Wi-Fi MAC to {CONFIG_FILE}")


def cmd_shuffle():
    st = load_state()
    ids = [f["content_id"] for f in st["frames"].values() if f.get("content_id")]
    if not ids:
        sys.exit("shuffle: no photos on the TV yet")
    if not is_on():
        sys.exit("📴 TV is off or not on Wi-Fi")
    art = tv(timeout=30).art()
    cur = (with_timeout(lambda: art.get_slideshow_status(), 20) or {}).get("current_content_id")
    pick = random.choice([i for i in ids if i != cur] or ids)
    if with_timeout(lambda: art.select_image(pick, category="MY-C0002"), 20, "TIMEOUT") == "TIMEOUT":
        sys.exit("shuffle: TV didn't answer — try again, or restart it (hold power ~5s)")
    time.sleep(3)
    ss = ensure_slideshow(art)  # picking a photo stops the slideshow
    shown = (with_timeout(lambda: art.get_current(), 20) or {}).get("content_id")
    if shown != pick:
        sys.exit("shuffle: TV didn't switch photos — try again")
    print("🔀 New photo" + ("" if ss != "failed" else " (⚠️ slideshow didn't restart)"))


def cmd_status():
    st = load_state()
    last = st.get("last_ok") or st.get("last_sync")
    if last:
        dt = datetime.fromisoformat(last).astimezone()
        when = dt.strftime("%a %-I:%M %p" if (datetime.now().astimezone() - dt).days < 1 else "%a %b %-d")
    else:
        when = "never"
    photos = f"{len(st['frames'])} pictures · last sync {when}"
    if not is_on():
        print(f"📴 TV is off or not on Wi-Fi\n🖼 {photos}")
        return
    t = tv()
    mode = artmode(t)
    on_screen, background = None, []
    for name, app_id in APPS.items():
        s = t.rest_app_status(app_id)
        if s.get("visible"):
            on_screen = name
        elif s.get("running"):
            background.append(name)
    if mode is None:
        print("⚠️ Art Mode not responding — hold remote power ~5s to restart TV")
    elif mode == "on":
        print("🖼 Art Mode")
    else:
        print(f"📺 Watching: {on_screen or 'HDMI / TV input'}")
    print(f"🖼 {photos}")
    if background:
        print(f"💤 Open in background: {', '.join(background)}")


# ---------- photo sources ----------
# Each source lists photos as dicts: guid, date (ISO), checksum (stable id), portrait (bool).

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif"}


def album_token() -> str:
    """Token from a public iCloud shared-album link: https://www.icloud.com/sharedalbum/#B0aB..."""
    url = (CFG["source"].get("url") or "").strip()
    tok = url.split("#", 1)[-1].split(";")[0]
    if not tok or "/" in tok:
        sys.exit("config: source.url must be a public iCloud shared-album link "
                 "(https://www.icloud.com/sharedalbum/#...)")
    return tok


def album_host() -> str:
    # Apple answers 330 with the partition host that actually serves this album.
    r = requests.post(f"https://p23-sharedstreams.icloud.com/{album_token()}/sharedstreams/webstream",
                      json={"streamCtag": None}, timeout=30)
    return r.json().get("X-Apple-MMe-Host", "p23-sharedstreams.icloud.com") if r.status_code == 330 else "p23-sharedstreams.icloud.com"


def fetch_album(host: str) -> list[dict]:
    r = requests.post(f"https://{host}/{album_token()}/sharedstreams/webstream",
                      json={"streamCtag": None}, timeout=180)
    r.raise_for_status()
    photos = []
    for p in r.json()["photos"]:
        if (p.get("mediaAssetType") or "photo") != "photo":
            continue
        best = max(p["derivatives"].values(), key=lambda d: int(d.get("fileSize", 0)))
        w, h = int(best["width"]), int(best["height"])
        photos.append({"guid": p["photoGuid"], "date": p["dateCreated"], "checksum": best["checksum"],
                       "portrait": h > w})
    return photos


def asset_urls(host: str, guids: list[str]) -> dict[str, str]:
    out = {}
    for i in range(0, len(guids), 25):
        r = requests.post(f"https://{host}/{album_token()}/sharedstreams/webasseturls",
                          json={"photoGuids": guids[i:i + 25]}, timeout=60)
        r.raise_for_status()
        for checksum, item in r.json()["items"].items():
            out[checksum] = f"https://{item['url_location']}{item['url_path']}"
    return out


def folder_path() -> Path:
    p = Path(os.path.expanduser(CFG["source"].get("path") or ""))
    if not p.is_dir():
        sys.exit(f"config: source.path is not a folder: {p}")
    return p


def fetch_folder() -> list[dict]:
    """Photos in a local folder (e.g. an iCloud Drive folder synced to this Mac), including subfolders."""
    import hashlib
    photos = []
    try:
        os.listdir(folder_path())  # rglob silently skips folders it can't read; this raises instead
        files = sorted(folder_path().rglob("*"))
    except PermissionError:
        sys.exit(f"sync: macOS blocked reading {folder_path()}. Background jobs can't read iCloud Drive "
                 "or Documents/Desktop without Full Disk Access — see QUIRKS.md (\"Folder source\").")
    for f in files:
        if f.suffix.lower() not in IMAGE_EXTS or f.name.startswith("."):
            continue
        try:
            st = f.stat()
            with Image.open(f) as im:
                w, h = im.size
                exif = im.getexif()
                if exif.get(0x0112) in (5, 6, 7, 8):  # rotated 90°: swap
                    w, h = h, w
                taken = exif.get(0x0132) or exif.get_ifd(0x8769).get(0x9003)
        except Exception as e:  # iCloud placeholder not downloaded yet, or not an image
            log(f"sync: skipping {f.name}: {e}")
            continue
        try:
            dt = datetime.strptime(str(taken), "%Y:%m:%d %H:%M:%S").isoformat() if taken else None
        except ValueError:
            dt = None
        key = hashlib.sha1(f"{f}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()
        photos.append({"guid": key, "checksum": key, "path": str(f), "portrait": h > w,
                       "date": dt or datetime.fromtimestamp(st.st_mtime).isoformat()})
    return photos


def source_type() -> str:
    t = CFG["source"].get("type")
    if t not in ("icloud_shared_album", "folder"):
        sys.exit('config: source.type must be "icloud_shared_album" or "folder"')
    return t


def build_frames(photos: list[dict]) -> list[dict]:
    """Landscape photos stand alone; portraits pair up, same day first."""
    photos = sorted(photos, key=lambda p: p["date"])
    frames, leftovers = [], []
    by_day = defaultdict(list)
    for p in photos:
        if p["portrait"]:
            by_day[p["date"][:10]].append(p)
        else:
            frames.append([p])
    for day in sorted(by_day):
        ps = by_day[day]
        for i in range(0, len(ps) - 1, 2):
            frames.append([ps[i], ps[i + 1]])
        if len(ps) % 2:
            leftovers.append(ps[-1])
    for i in range(0, len(leftovers) - 1, 2):
        frames.append([leftovers[i], leftovers[i + 1]])
    if len(leftovers) % 2:
        frames.append([leftovers[-1]])
    return [{"key": "+".join(p["guid"] for p in f), "photos": f, "date": max(p["date"] for p in f)} for f in frames]


def choose(frames: list[dict], current: dict) -> list[dict]:
    by_key = {f["key"]: f for f in frames}
    recent = sorted(frames, key=lambda f: f["date"], reverse=True)[:RECENT_FRAMES]
    chosen = {f["key"] for f in recent}
    # keep yesterday's random picks (still in album), minus the oldest uploads
    kept = [k for k, v in sorted(current.items(), key=lambda kv: kv[1]["uploaded"])
            if k in by_key and k not in chosen]
    kept = kept[min(ROTATE_PER_NIGHT, len(kept)):] if len(kept) + len(chosen) >= TOTAL_FRAMES else kept
    chosen.update(kept)
    pool = [k for k in by_key if k not in chosen]
    random.Random(date.today().isoformat()).shuffle(pool)
    chosen.update(pool[:max(0, TOTAL_FRAMES - len(chosen))])
    return [by_key[k] for k in chosen]


# ---------- images ----------

def load_image(url: str, checksum: str) -> Image.Image:
    if not url.startswith("http"):  # folder source: url is a local path
        return ImageOps.exif_transpose(Image.open(url)).convert("RGB")
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / checksum
    if not path.exists():
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        path.write_bytes(r.content)
    img = Image.open(path)
    return ImageOps.exif_transpose(img).convert("RGB")


def fit(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    s = min(box_w / img.width, box_h / img.height)
    return img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))), Image.LANCZOS)


def compose(images: list[Image.Image]) -> bytes:
    canvas = Image.new("RGB", (W, H), PAPER)
    if len(images) == 1:
        im = fit(images[0], W - 2 * MARGIN, H - 2 * MARGIN)
        canvas.paste(im, ((W - im.width) // 2, (H - im.height) // 2))
    else:
        slot_w = (W - 2 * MARGIN - GAP) // 2
        ims = [fit(i, slot_w, H - 2 * MARGIN) for i in images]
        h = min(i.height for i in ims)  # equalize heights
        ims = [i.resize((round(i.width * h / i.height), h), Image.LANCZOS) if i.height != h else i for i in ims]
        total = ims[0].width + GAP + ims[1].width
        x = (W - total) // 2
        y = (H - h) // 2
        canvas.paste(ims[0], (x, y))
        canvas.paste(ims[1], (x + ims[0].width + GAP, y))
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=90, optimize=True)
    return buf.getvalue()


# ---------- sync ----------

def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"frames": {}, "last_sync": None}


def save_state(st: dict):
    STATE.write_text(json.dumps(st, indent=1))


def cmd_sync(dry_run=False, limit: int | None = None):
    st = load_state()
    kind = source_type()
    log("sync: listing photos…")
    if kind == "icloud_shared_album":
        host = album_host()
        photos = fetch_album(host)
    else:
        photos = fetch_folder()
    if not photos:
        log("sync: no photos found in the source — nothing to do")
        return
    frames = build_frames(photos)
    wanted = choose(frames, st["frames"])
    if limit:
        wanted = sorted(wanted, key=lambda f: f["date"], reverse=True)[:limit]
    wanted_keys = {f["key"] for f in wanted}
    to_add = [f for f in wanted if f["key"] not in st["frames"]]
    to_remove = [k for k in st["frames"] if k not in wanted_keys]
    pairs = sum(len(f["photos"]) == 2 for f in frames)
    log(f"sync: {len(photos)} photos -> {len(frames)} pictures ({pairs} pairs); "
        f"keep {len(wanted) - len(to_add)}, add {len(to_add)}, remove {len(to_remove)}")
    if dry_run:
        return

    if not is_on():
        log("sync: TV is off/unreachable — skipping (will retry next run)")
        return
    # resolve download links before opening the art channel: the TV drops it when idle
    if kind == "icloud_shared_album":
        urls = asset_urls(host, [p["guid"] for f in to_add for p in f["photos"]]) if to_add else {}
    else:
        urls = {p["checksum"]: p["path"] for f in to_add for p in f["photos"]}
    art = tv(timeout=60).art()
    if with_timeout(art.get_api_version, 20) is None:
        log("sync: TV art service not responding — skipping (restart the TV)")
        return

    if to_remove:
        ids = [st["frames"][k]["content_id"] for k in to_remove]
        res = with_timeout(lambda: art.delete_list(ids), 60, "TIMEOUT")
        if res == "TIMEOUT":  # without a limit, a hung delete blocked this sync and every check after it
            log("sync: deleting old pictures got no answer in 60s — stopping; next check retries")
            return
        for k in to_remove:
            st["frames"].pop(k, None)
        save_state(st)

    ok = streak = 0
    for n, f in enumerate(to_add, 1):
        try:
            imgs = [load_image(urls[p["checksum"]], p["checksum"]) for p in f["photos"]]
            data = compose(imgs)
            art.get_api_version()  # handshake; uploads on a cold channel get dropped
            cid = with_timeout(lambda: art.upload(data, file_type="JPEG", matte="none", portrait_matte="none"), 90, "TIMEOUT")
            if cid in ("TIMEOUT", None):  # a hung upload used to block every later check
                raise TimeoutError("upload got no answer in 90s")
            st["frames"][f["key"]] = {"content_id": cid, "uploaded": time.time(), "date": f["date"]}
            save_state(st)
            ok += 1
            streak = 0
            if n % 10 == 0:
                log(f"sync: uploaded {n}/{len(to_add)}")
        except Exception as e:
            streak += 1
            log(f"sync: failed on {f['key'][:20]}…: {e!r}")
            if streak >= 3:
                log("sync: 3 failures in a row — stopping so the TV can recover; next run resumes")
                break
            try:
                art.close()
            except Exception:
                pass
            time.sleep(20)
            if not is_on():
                log("sync: TV went offline — stopping")
                break
            art = tv(timeout=60).art()
    log("sync: slideshow ->", ensure_slideshow(art))
    st["last_sync"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if ok >= len(to_add) - 2:  # a stray failure is retried next night
        st["last_ok"] = st["last_sync"]
    save_state(st)
    # trim cache to what's on the TV
    keep = {p["checksum"] for f in wanted for p in f["photos"]}
    for p in CACHE.glob("*"):
        if p.name not in keep:
            p.unlink()
    log(f"sync: done — uploaded {ok}/{len(to_add)}, {len(st['frames'])} pictures on TV")


# ---------- heads-up alerts ----------

ALERT_TO = (CFG["alert_imessage_to"] or "").strip()
ALERTS = CONF / "alerts.json"
RESOLVED = {"art": "Art Mode is responding again.", "offline": "TV is back on Wi-Fi.",
            "sync": "Photo sync is working again."}


def imessage(text: str) -> bool:
    import subprocess
    if not ALERT_TO:
        log("alert:", text)
        return True
    script = ('on run argv\n tell application "Messages"\n'
              '  send (item 1 of argv) to participant (item 2 of argv) of (1st account whose service type = iMessage)\n'
              ' end tell\nend run')
    try:
        r = subprocess.run(["osascript", "-e", script, text, ALERT_TO], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:  # Messages hung — retry on the next check instead of crashing it
        log("alert: iMessage timed out")
        return False
    if r.returncode:
        log("alert: iMessage failed:", r.stderr.strip())
    return r.returncode == 0


def problems(now: float, al: dict) -> dict[str, str]:
    """Current problems keyed by id. Only conditions that persist count."""
    out = {}
    if is_on():
        al["last_seen"] = now
        t = tv()
        mode = artmode(t)
        if mode == "on":  # nobody's watching — free the TV's memory
            closed = tidy(t)
            if closed:
                log("check: closed background apps:", ", ".join(closed))
            ss = ensure_slideshow(t.art())
            if ss != "on":  # something switched it off — log so we can see when
                log("check: slideshow was off ->", ss)
        if mode is None:
            al["art_bad"] = al.get("art_bad", 0) + 1
            if al["art_bad"] >= 2:  # two checks in a row, ~1h apart
                out["art"] = "Art Mode isn't responding. Fix: hold the remote's power button ~5s to restart the TV."
        else:
            al["art_bad"] = 0
    elif now - al.setdefault("last_seen", now) > 24 * 3600:
        out["offline"] = "TV hasn't been reachable on Wi-Fi for over a day."
    st = load_state()
    last_ok = st.get("last_ok") or st.get("last_sync")
    if last_ok and now - datetime.fromisoformat(last_ok).timestamp() > 2 * 24 * 3600:
        out["sync"] = f"Photo sync hasn't succeeded since {last_ok[:10]}. See {LOG}"
    return out


def sync_due(now: float) -> bool:
    st = load_state()
    last = st.get("last_ok") or st.get("last_sync")
    # once per calendar day (local time), whenever the TV is first seen on
    return not last or datetime.fromisoformat(last).astimezone().date() < datetime.fromtimestamp(now).date()


def single_instance():
    """Only one check/sync at a time; a slow sync must not overlap the next 30-min check."""
    import fcntl
    CONF.mkdir(parents=True, exist_ok=True)
    fh = open(CONF / "run.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another check/sync is still running — skipping")
        sys.exit(0)
    return fh  # keep open for the life of the process


def cmd_check(dry_run=False):
    al = json.loads(ALERTS.read_text()) if ALERTS.exists() else {"open": {}}
    now = time.time()
    # The TV is often fully off overnight, so sync whenever it's first seen on each day.
    if not dry_run and sync_due(now) and is_on():
        log("check: TV is on and sync is due — syncing")
        try:
            cmd_sync()
        except (Exception, SystemExit) as e:  # config/permission errors exit; still run the alert checks
            log("check: sync failed:", e if isinstance(e, SystemExit) else repr(e))
    cur = problems(now, al)
    new = {k: v for k, v in cur.items() if k not in al["open"]}
    fixed = [k for k in al["open"] if k not in cur]
    msgs = [f"Frame TV: {v}" for v in new.values()] + \
           [f"Frame TV: ✓ {RESOLVED.get(k, k + ' resolved')}" for k in fixed]
    sent = True
    for m in msgs:
        print("ALERT:", m)
        if not dry_run:
            sent = imessage(m) and sent
    if sent and not dry_run:  # if sending failed, retry next check
        al["open"] = cur
    if not dry_run:
        ALERTS.write_text(json.dumps(al, indent=1))
    if not msgs:
        print("ok" if not cur else f"ongoing (already notified): {', '.join(cur)}")


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    c = argv[0]
    if c == "pair":
        return cmd_pair(argv[1] if len(argv) > 1 else None)
    if not HOST:
        sys.exit("Not set up yet: run `frame pair <TV IP address>` first.")
    if c in APPS:
        cmd_app(c)
    elif c in ("art", "off"):
        cmd_art()
    elif c in ("hdmi", "dvd"):
        cmd_hdmi()
    elif c == "tidy":
        print("closed:", ", ".join(tidy(tv())) or "nothing")
    elif c == "brightness":
        a = tv(timeout=20).art()
        if len(argv) > 1:
            n = max(0, min(10, int(argv[1])))
            with_timeout(lambda: a.set_brightness(n), 15)
        print(f"Art Mode brightness: {with_timeout(a.get_brightness, 15)}/10")
    elif c == "check":
        _lock = single_instance()
        cmd_check(dry_run="--dry-run" in argv)
    elif c == "status":
        cmd_status()
    elif c == "shuffle":
        cmd_shuffle()
    elif c == "sync":
        _lock = single_instance()
        lim = next((int(a.split("=")[1]) for a in argv if a.startswith("--limit=")), None)
        cmd_sync(dry_run="--dry-run" in argv, limit=lim)
    else:
        sys.exit(f"unknown command: {c}\n{__doc__}")


if __name__ == "__main__":
    main(sys.argv[1:])
