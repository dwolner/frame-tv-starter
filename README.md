# Frame TV starter kit

Make a Samsung **The Frame** TV less annoying, and keep its Art Mode full of your own photos automatically.

- **Photo sync:** a Mac pulls photos from an **iCloud shared album** or an **iCloud Drive folder** once a day and puts them on the TV. Portrait photos are paired side by side on a paper-colored mat so they fill the 16:9 screen. The TV keeps a rotating set, by default 150 pictures: the 30 newest are always included, and 25 random ones are swapped each day.
- **Slideshow:** the TV cycles through those pictures every 15 minutes. A background check turns the slideshow back on if the TV switches it off.
- **Phone remote:** iPhone Shortcuts (and Siri) for Art Mode, HDMI, YouTube/Netflix/Apple TV/Prime, Shuffle, Brightness and Status. They skip the TV's slow menus.
- **Cleanup:** while the TV is in Art Mode, the background check closes streaming apps left open in the background, which frees the TV's limited memory.
- **Alerts (optional):** an iMessage if Art Mode freezes, the TV disappears from Wi-Fi for a day, or syncing fails for 2 days. You get another when the problem clears.

Everything runs on a Mac on your home network. There's no cloud service, account or subscription.

> Read **[QUIRKS.md](QUIRKS.md)** before you start. The Frame's control interface is unofficial and has a lot of odd behavior, and most of this kit exists to work around it.

## Tested on

| | |
|---|---|
| TV | Samsung The Frame 55" **QN55LS03BAFXZA** (2022, LS03B), Tizen; reports model code `22_PONTUSM_FTV` |
| TV firmware | Late-2026 firmware. The TV now reports its version as `Unknown` (see QUIRKS) |
| Mac | macOS 26 (Apple silicon), Apple's built-in Python 3.9 |
| Library | [`samsungtvws`](https://github.com/xchwarze/samsung-tv-ws-api) 3.0.5 |
| Phone | iPhone, iOS 26 Shortcuts app |

Other Frame years (2020–2024) use the same Art Mode interface and will probably work, but they're untested. Some 2024+ models changed parts of the art interface. Non-Frame Samsung TVs can use the remote-control commands but not Art Mode.

## What you need

- A Samsung **Frame** TV and a **Mac** on the same home network. The Mac does the work, so photo sync and phone control only happen while it's awake.
- Apple's Command Line Tools for its Python. The installer tells you if you need them (`xcode-select --install`).
- For photos, **either** an iCloud shared album (recommended) **or** a folder in iCloud Drive.
- For the phone shortcuts: an iPhone, and **Remote Login** turned on on the Mac. For use away from home, also [Tailscale](https://tailscale.com) (free) on the Mac and the phone.

## Setup (about 15 minutes)

### 1. Install on the Mac

```sh
git clone <this repo> ~/frame-tv-starter
cd ~/frame-tv-starter
./install.sh
```

This creates a Python environment in the folder, puts `frame` and `frame-ssh` in `~/.local/bin`, creates `~/.config/frame-tv/config.json`, and starts the 30-minute background check (`local.frame-tv.check`). Add `~/.local/bin` to your `PATH` if the installer tells you to.

### 2. Pair with the TV

1. On the TV, go to **Settings → General → Network → Network Status → IP Settings** and note its IP address.
2. Also on the TV, go to **Settings → General → External Device Manager → Device Connect Manager** and make sure **Access Notification** is on (it's the setting that shows the Allow prompt).
3. With the TV on, run:

```sh
frame pair 192.168.1.50   # your TV's address
```

Choose **Allow** on the TV. This saves a token and the TV's Wi‑Fi hardware address. If your router later gives the TV a new IP, `frame` finds it again on its own.

The first time, macOS asks whether Terminal (and later Python) may **find devices on your local network**. Choose **Allow**.

Optionally, reserve an IP for the TV in your router's app (often called "DHCP reservation" or "address reservation").

### 3. Choose where photos come from

Edit `~/.config/frame-tv/config.json`.

**Option A: iCloud shared album (recommended)**

1. On iPhone: **Photos → Albums → Shared Albums**, open (or create) the album, then tap the **people icon**.
2. Turn on **Public Website**, tap **Share Link**, and copy it. It looks like `https://www.icloud.com/sharedalbum/#B0aB…`.
3. Put it in the config:

```json
"source": { "type": "icloud_shared_album", "url": "https://www.icloud.com/sharedalbum/#B0aB…" }
```

Anyone who adds photos to the album (family, too) gets them onto the TV. The link is public to anyone who has it, so don't post it. Videos are skipped.

**Option B: iCloud Drive folder**

1. In Finder, make a folder in iCloud Drive, for example `Frame`, and put photos in it. Subfolders, JPEG, PNG and HEIC all work.
2. Right‑click the folder and choose **Keep Downloaded**. Otherwise the Mac keeps only placeholders, and those get skipped.
3. Put it in the config:

```json
"source": { "type": "folder", "path": "~/Library/Mobile Documents/com~apple~CloudDocs/Frame" }
```

4. **Required:** macOS blocks background jobs from reading iCloud Drive. Either give Python Full Disk Access, or use any folder outside iCloud Drive, Documents and Desktop. See [QUIRKS.md → Folder source](QUIRKS.md#folder-source-icloud-drive-is-blocked-for-background-jobs).

### 4. First sync

```sh
frame sync --dry-run   # lists what would be uploaded, touches nothing
frame sync             # uploads ~10 pictures a minute, so the first 150 take ~15 min
frame status
```

Run the first sync with the TV in **Art Mode**. From then on, the background check runs a sync once a day, the first time it sees the TV on. The TV is usually fully off overnight, so a fixed 3 am schedule would miss it. Progress is logged to `~/.config/frame-tv/sync.log`.

### 5. Alerts (optional)

Set `"alert_imessage_to"` to your phone number (`"+15551234567"`) or Apple ID email. The Mac sends these from its own Messages app, so the Mac must be signed into iMessage. The first alert triggers a macOS prompt that lets the background job control Messages. Click **OK** at the Mac. Leave the setting empty to only log alerts.

## iPhone shortcuts

Each shortcut asks the Mac over SSH to run one `frame` command. The phone's key is restricted, so it can run **only** the TV commands and nothing else on the Mac.

**On the Mac (once):** go to **System Settings → General → Sharing** and turn on **Remote Login**. Note your Mac's username (`whoami`) and its local name (shown under **Sharing → Local hostname**, for example `my-mac.local`).

**On the iPhone:**

1. In **Shortcuts**, tap **+**, add the action **Run Script Over SSH**, and expand it:
   - **Script:** `art` (see the table below)
   - **Host:** `my-mac.local`, or the Mac's Tailscale name/IP to use it away from home
   - **Port:** `22` · **User:** your Mac username
   - **Authentication:** **SSH Key**. Tap the key, then **Share Public Key**, and send it to yourself (AirDrop, Notes, …).
2. Add the action **Show Result** (or **Show Notification**) below it. Without this step you get no feedback.
3. Name the shortcut, e.g. **Frame Art**.
4. **On the Mac**, authorize that phone's key:

```sh
~/frame-tv-starter/install.sh --add-phone-key 'ssh-ed25519 AAAA… Shortcuts on iPhone'
```

5. Run the shortcut. The first time, the phone asks you to trust the Mac's host key.
6. Duplicate the shortcut for each command and change only the **Script** field. Every shortcut uses the same key.

| Script | What it does |
|---|---|
| `art` | Art Mode on; closes streaming apps first (`off` does the same) |
| `hdmi` | Switch to your HDMI device (set `hdmi_presses`, see QUIRKS) |
| `youtube` `netflix` `appletv` `prime` | Open that app directly, closing the others |
| `shuffle` | Jump to a random photo now. The slideshow carries on from it. Takes ~30 s |
| `status` | `🖼 Art Mode` / `📺 Watching: netflix`, picture count, last sync |
| `brightness` / `brightness 3` | Show / set Art Mode brightness (0–10) |
| `tidy` | Close background streaming apps |

Ideas: say "Hey Siri, Frame Art". Add the shortcuts to a Home Screen widget or the Action Button. In **Shortcuts → Automation**, try *When I leave home → Frame Art*, or *When Sleep Focus turns on → Frame Art*.

**New phone?** Each iPhone generates its own SSH key, and it is **not** moved to a new phone. Your shortcuts sync over iCloud, but they fail with an authentication error until you repeat step 4 with the new phone's key.

## Command reference (Mac)

```
frame pair [IP]           one-time pairing; saves address + Wi‑Fi MAC
frame art | off | hdmi | youtube | netflix | appletv | prime | tidy | shuffle | status
frame brightness [0-10]
frame sync [--dry-run] [--limit=N]
frame check [--dry-run]   what the 30-minute background job runs
```

Settings live in `~/.config/frame-tv/config.json`. See `config.example.json` for every option. `total_pictures` is limited by the TV's storage. 150 4K JPEGs fits comfortably on the 2022 model.

## Uninstall

```sh
./install.sh --uninstall   # removes the background job and commands
```

Then delete any `frame-ssh` lines from `~/.ssh/authorized_keys`, and remove `~/.config/frame-tv` if you want the cache and settings gone. Photos already on the TV stay under **Art Mode → My Photos**.

## Security notes

- The TV token (`~/.config/frame-tv/token`) lets anyone on your network who has it control the TV. It stays on the Mac.
- The phone key is locked by a `command=` restriction in `authorized_keys` to `frame-ssh`, which allows only the commands in the table above. It refuses extra commands chained on with `;`, `&&` and the like, and port forwarding is disabled.
- A public shared-album link is viewable by anyone who has the link.

## License

MIT
