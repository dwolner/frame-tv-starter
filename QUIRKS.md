# Known quirks, blockers and workarounds

All of these were found the hard way on a 2022 Frame (QN55LS03B) during late 2026. The code already works around each one. This page explains why the code looks the way it does, and what to try when something breaks.

## Setup and the Mac

### "No route to host" even though the TV answers ping: use Apple's Python
macOS **Local Network privacy** blocks Python builds that weren't installed by Apple (uv, pyenv, Homebrew, python.org) from reaching devices on your LAN. The symptom is that every TV call fails with `No route to host`.
**Workaround:** `install.sh` builds the environment from `/usr/bin/python3` (Apple's), which macOS lets you approve. If you see this error, re-run `./install.sh`. Also check **System Settings → Privacy & Security → Local Network**.

### Folder source: iCloud Drive is blocked for background jobs
A job started by launchd can't read `~/Library/Mobile Documents` (iCloud Drive), `~/Documents` or `~/Desktop`. Python's folder search doesn't raise an error in that case. It silently finds **zero** files. The kit checks for this explicitly, and `sync.log` shows *"macOS blocked reading …"*. Running `frame sync` by hand in Terminal works, because Terminal has access.
**Workarounds, pick one:**
1. Use an **iCloud shared album** instead (recommended, no permissions needed).
2. Give Full Disk Access to the Python app that the venv uses: **System Settings → Privacy & Security → Full Disk Access → +**, press ⌘⇧G and add `/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app`. Your version folder may differ; `ls` that path to check. The catch is that every script run with that Python gets full disk access.
3. Point `source.path` at a folder outside iCloud Drive, Documents and Desktop, for example `~/FramePhotos`, and copy photos there yourself.

Also turn on **Keep Downloaded** for the iCloud folder. Otherwise most photos are placeholders, and those get skipped and logged.

### The Mac must be awake
Phone shortcuts and the daily sync run on the Mac. If it's asleep, shortcuts time out and the sync waits until the next check that finds both the Mac and the TV awake. A desktop Mac that stays on, or **Prevent automatic sleeping when the display is off** (Battery/Energy settings), fixes this.

### Alerts stop when Messages hangs
Alerts are sent by scripting the Mac's Messages app. If Messages is stuck, for example on a pop-up or a sign-in prompt, the send times out after 60 s. The kit logs the failure and retries on the next check. If alerts stop arriving, open Messages on the Mac and look for a dialog.

## Network

### The TV's IP changes (new router, DHCP lease) and it looks "off"
Everything addresses the TV by IP. After a router change the old address is dead, so `status` says *"TV is off or not on Wi‑Fi"* even while you're watching it.
**Workaround:** `frame pair` saves the TV's Wi‑Fi MAC address. When the saved IP stops answering, `frame` scans the Mac's subnet for that MAC, saves the new IP, and logs `tv: address changed`. Reserving an address for the TV in your router also prevents it.

### "Off" really means off the network
The TV only shows up on the network while it's on, or in Art Mode. When fully off overnight it's unreachable, so the daily sync runs **the first time each day the TV is seen on**, not at a fixed time.

## TV interface (unofficial, changes with firmware)

### PowerState went blank after a firmware update
The TV's info page (`http://TV:8001/api/v2/`) used to report `PowerState: "on"`. After an automatic update it reports `""` while on, and `firmwareVersion` became `"Unknown"`. Code that required `"on"` decided the TV was off and skipped syncs.
**Workaround:** a blank or missing PowerState counts as on, and only `"standby"` counts as off.
**Tip:** consider turning off **Settings → Support → Software Update → Auto Update**. Samsung updates have broken third‑party Art Mode control before. The trade-off is that you install security fixes yourself.

### Art Mode service freezes ("wedges")
Sometimes the TV's art service stops answering. Connections open but never send their ready message, and every art command times out. Remote buttons still work. It also happens mid‑upload.
**Workaround:** every art call has a time limit: 90 s per upload, 60 s for deleting old pictures. Only one check or sync runs at a time (a lock file), so a slow sync can't pile up behind the next 30‑minute check. After three failures in a row the sync stops and resumes at the next check. `status` shows *"Art Mode not responding"*, and after two checks in a row you get an alert.
**Fix: restart, *then* enter Art Mode once.** Hold the remote's power button until the screen goes black and the Samsung logo appears (~5 s), or unplug the TV for a minute. **Restarting alone isn't enough:** after a cold boot the art service stays frozen until the TV enters Art Mode. Press power briefly once it's back on, or send `KEY_POWER` from the Mac; the remote-control channel keeps working while the art channel is frozen.

### Connecting fails if another app connects at the same moment
The `samsungtvws` library gives up if any event arrives before "channel ready", for example the SmartThings app connecting. The kit patches `open()` to skip those events instead (`_patient_open` in `frame.py`).

### The art connection drops when idle
The TV closes an idle art connection quickly. The sync gets every photo's download link *before* opening the connection, and sends a quick handshake before each upload. Uploads on a "cold" connection were silently dropped.

### Apps only launch over REST
The websocket "run app" command is ignored on this model. Launch apps with the REST endpoint (`rest_app_run`) instead. App IDs are in `APPS` in `frame.py`. Get others with `frame pair`, which lists the installed apps, or `samsungtvws`'s `app_list()`.

### There's no "go to HDMI 1" key
`KEY_HDMI1` and `KEY_HDMI2` are ignored. `KEY_HDMI` steps to the next source. Inputs are also ignored while in Art Mode, so the kit leaves Art Mode first.
**Workaround:** set `"hdmi_presses"` to however many presses take you from the TV's home/Art screen to your device. It was 2 on the tested TV, for HDMI 1.

### Shuffle mode snaps back to one photo
The TV's own **shuffle** slideshow has a bug. Every time a remote connection closes (the 30‑minute check, a shortcut, a sync), it jumps back to one fixed photo, so the picture never seems to change.
**Workaround:** the kit uses the **in‑order** slideshow, which keeps going correctly. Variety comes from the daily sync picking a random set. If the check finds the slideshow off or in shuffle mode, it switches it back to in‑order.

### Choosing a picture turns the slideshow off
Selecting a specific picture, with `select_image` or by hand on the TV, silently stops the slideshow. The TV still answers "OK" when you turn the slideshow on, so the kit reads the setting back to confirm (`ensure_slideshow`). `frame shuffle` selects a random photo, then restarts the in‑order slideshow from it.

### "Already in Art Mode" never answers
Asking for Art Mode while the TV is already in Art Mode gets no reply, which looked like a hang. `frame art` checks the current mode first.

### Smaller annoyances the kit handles
- Streaming apps stay open in the background and slow the TV down. The background check closes them whenever the TV is in Art Mode.
- The TV's own "My Photos" upload from the SmartThings app is slow and manual. The kit replaces it.
- The TV's pairing token is per client name (`FrameControl`). If you deny the prompt once, run `frame pair` again, or remove the device under **Device Connect Manager → Device List**.

## Not possible / not worth it
- **Removing Samsung's built‑in apps or ads:** not possible without developer tools, and even those can't remove the built‑in apps. Network-level blocking (NextDNS, Pi-hole) removes many ads, but aggressive block lists can break Art Mode and the app store.
- **Replacing the TV's operating system:** not possible on this TV.
- **Turning off Samsung TV Plus or Multi View auto-launch:** partly possible in the TV's settings (look under **General → Smart Features** and **Broadcasting**), but firmware updates tend to switch them back on. Using `art` and `hdmi` from the phone gets around the menus.
