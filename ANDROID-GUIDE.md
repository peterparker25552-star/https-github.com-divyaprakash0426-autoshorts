# Install Qyro on your Android phone — complete beginner guide

This guide assumes you have **never** used a terminal, a command line, or Python
before. That is fine. Follow the steps in order, type exactly what you see in
the grey boxes, and you will have the app running.

**What you end up with:** Qyro runs *on your phone*. It opens as a normal
web page in Chrome, and if you like, you can put its icon on your home screen
so it looks and behaves like a real app. Nothing is uploaded anywhere — your
videos are cut on your own device.

**Time:** about 15–25 minutes (most of it is waiting for downloads).
**Cost:** free.
**You need:** an Android phone (Android 7 or newer), and an internet
connection for the install only.

---

## Step 1 — Install Termux

Termux is the small app that lets a phone run the tools Qyro needs. Qyro
itself is not on the Play Store, so this is the only app you install by hand.

1. Open **F-Droid** in your phone's browser: <https://f-droid.org>
   - If you don't have F-Droid, tap **Download F-Droid**, install it, and open
     it. (Android will ask you to allow apps from this source — tap *Allow*.)
2. In F-Droid, tap the search icon and type `Termux`.
3. Tap **Termux** (publisher: Fredrik Fornwall) and tap **Install**.

> **Do not install "Termux" from the Google Play Store.** That copy is old and
> no longer updated; it fails with download errors. Use the F-Droid one.

Open Termux. You will see a black screen with a cursor and a prompt that looks
like this:

```
~ $
```

That is the terminal. Everything you type goes on the line after `$`, and you
run it by pressing **Enter** (the ↵ key). If the keyboard does not show `~ $`,
you are in the wrong app.

**Tip for typing:** long-press anywhere in the Termux window and tap **More**
to get a row of extra keys (`|`, `/`, `-`, `_`, `Ctrl`, arrows).

---

## Step 2 — Let Termux reach your files

Qyro saves finished videos into your phone's storage so you can share them.
Termux needs permission for that. Type this line and press Enter:

```
termux-setup-storage
```

Android will pop up a permission request — tap **Allow**.

---

## Step 3 — Install everything with one command

Copy this **single line**, paste it into Termux (long-press → **Paste**), and
press Enter:

> **Important:** use the repository URL below exactly. The separate
> `divyaprakash0426/autoshorts` repository does not contain this Android
> installer. A raw URL for that repository returns GitHub's `404: Not Found`
> text, which Bash then tries to execute.

```
pkg update -y && pkg install -y curl && curl -fsSL https://raw.githubusercontent.com/peterparker25552-star/https-github.com-divyaprakash0426-autoshorts/main/android-install.sh -o "$HOME/autoshorts-android-install.sh" && bash "$HOME/autoshorts-android-install.sh"
```

The `-f` option makes curl stop on HTTP errors, and the `&&` chain prevents
Bash from running a failed download. It will ask `Do you want to continue?
[Y/n]` a few times — just press **Enter** each time (the default is yes).

This downloads Python, ffmpeg (the video engine), git, then Qyro itself, then
the Hindi caption font. It takes **5–15 minutes** depending on your connection.
You will see a lot of scrolling text; that is normal.

When it finishes you will see:

```
============================================================
  ✅ Qyro v0.6.2 is installed!

  TO START IT (any time):
     open Termux and type:  bash ~/start-autoshorts.sh

  then open Chrome at:      http://localhost:8000
============================================================
```

### If that one command fails

Sometimes a phone blocks the download. Do it in pieces instead — type each
line, press Enter, wait for it to finish, then type the next:

```
pkg update -y
```
```
pkg install -y python ffmpeg git curl
```
```
git clone https://github.com/peterparker25552-star/https-github.com-divyaprakash0426-autoshorts.git ~/autoshorts
```
```
cd ~/autoshorts
```
```
bash install-android.sh
```

---

## Step 4 — Start the app

In Termux, type:

```
bash ~/start-autoshorts.sh
```

You will see:

```
  Qyro is starting... keep Termux OPEN and the phone AWAKE.

  On this phone : http://localhost:8000
  On other devices on the same Wi-Fi: http://192.168.1.42:8000
```

**Leave Termux open.** The app lives inside it. If you close Termux, the app
stops.

---

## Step 5 — Open it in Chrome

1. Open **Chrome** (or any browser) on the same phone.
2. In the address bar at the top, type exactly:

```
http://localhost:8000
```

3. Press Go.

Qyro's dark interface appears. That's it — it is running.

---

## Step 6 (optional) — Put it on your home screen

This makes Qyro open full-screen with its own icon, like a real app.

1. With the app open in Chrome, tap the **⋮** menu (top right).
2. Tap **Add to Home screen**.
3. Tap **Add** (or **Install**).

Now you can launch Qyro from the icon. You still need Termux running in the
background — start it first (Step 4), then tap the icon.

---

## Using it

1. **Add a video.** Paste a YouTube video or playlist link in the box at the
   top and tap **Add**. Qyro downloads it and reads the captions.
2. **Pick your settings.** Tap **Fine-tune** on an episode to change how the
   short is made.
3. **Make shorts.** Tap **Generate**. Wait while it works.
4. **Download.** Tap the download arrow on a finished clip. Files also land in
   `~/autoshorts/data/clips/`.

### The v0.6.0 settings, in plain words

| Setting | What it does |
|---|---|
| **Follow person** | The camera *tracks the speaker* instead of cropping the middle of a wide shot. Keep it on **Track the person** — this is the fix for people being cut out of frame. |
| **Framing** | How much room to leave around the person. **Auto** picks for you; **Tight** zooms in, **Wide** shows more. |
| **Caption motion** | How the words animate in: fade, pop, zoom, bounce, glow, blur in, **karaoke** (words light up as they're spoken), drop. |
| **Caption font** | Ten fonts. Pick **Devanagari** or **Devanagari serif** for Hindi. |
| **Language** | Set **Hindi** for a Hindi episode — it stops the captions being turned into CAPITAL LETTERS and gives Hindi more words per line. |
| **Clip transition** | A fade, dip, flash or slide at the cut points. **None** is a hard cut, which is safest. |
| **Best parts only** | Skips the boring stretches (long pauses, thin rambling) and keeps the tight, quotable ones. Leave it on. |

---

## Keeping the phone awake

Rendering a video is real work. Android likes to freeze background apps, which
stops the render.

- **Turn off battery optimisation for Termux:** Settings → Apps → Termux →
  Battery → choose **Unrestricted** / **No restrictions**.
- **Plug the phone in** for long jobs.
- **Leave the screen on** while rendering (Settings → Display → Screen timeout
  → 30 minutes).
- If a render seems stuck, pull down Termux's notification and tap **Acquire
  wakelock**, then try again.

---

## Troubleshooting

**"This must run inside Termux on Android."**
You ran an install script outside Termux (for example in a file manager). Open
the Termux app and run it there.

**`command not found: python` or `ffmpeg`**
Step 3 did not finish. Run these, one at a time:
```
pkg update -y
pkg install -y python ffmpeg
```

**"Address already in use"**
Qyro is already running. Either use it at `http://localhost:8000`, or stop the
old one first: in Termux press **Ctrl+C** (tap the *Volume Down* button and the
letter **C** at the same time), then start it again.

**Hindi captions show empty boxes (□□□)**
The Hindi font is missing. Easiest fix: in the app, choose Hindi in
**Language** — a yellow **Install fonts** button appears next to it. Tap it.
From the terminal instead:
```
pip install devanagari-fonts
```
Then copy the font where the system can find it:
```
mkdir -p ~/.fonts
cp $(python -c "import pathlib,importlib.util; s=importlib.util.find_spec('devanagari_fonts'); print(list(s.submodule_search_locations)[0])")/fonts/Shobhika-*/Shobhika-*.otf ~/.fonts/
```

**"pip: command not found"**
```
pkg install -y python-pip
```

**Downloads from YouTube fail**
```
pip install -U yt-dlp
```
YouTube changes often; keeping `yt-dlp` current is what fixes most download
errors. Demo mode works with no internet at all, so you can still try the app.

**The page won't load at localhost:8000**
- Is Termux still open with the "Qyro is starting…" message showing?
- Did you type `http://` and not `https://`?
- Try `http://127.0.0.1:8000`.

**Rendering is very slow**
Use **720p · quick** in the Quality box. 1440p on a phone can take many
minutes per short.

**I want to start over**
```
rm -rf ~/autoshorts
```
Then do Step 3 again. Your downloaded videos live in `~/autoshorts/data/`.

---

## Starting it next time

Every time you want to use Qyro:

1. Open **Termux**.
2. Type `bash ~/start-autoshorts.sh` and press Enter.
3. Open Chrome → `http://localhost:8000` (or tap the home-screen icon).

To stop it, go back to Termux and press **Ctrl+C**.

---

## Useful commands

| You want to… | Type this |
|---|---|
| Start Qyro | `bash ~/start-autoshorts.sh` |
| Stop Qyro | press **Ctrl+C** in Termux |
| Update to the newest version | `cd ~/autoshorts && git pull` |
| See where your clips are saved | `cd ~/autoshorts/data/clips && ls` |
| Copy clips to your phone's Movies folder | `cp ~/autoshorts/data/clips/*.mp4 /sdcard/Movies/` |
| See how much space is used | `du -sh ~/autoshorts/data` |
| Delete everything and start again | `rm -rf ~/autoshorts` |

---

## What Qyro does and does not do

**It runs entirely on your phone.** No account, no subscription, no server.
The only thing that leaves your phone is the YouTube link you paste in.

**It needs internet** to download a YouTube video, and nothing else.

**Honest limits:**
- Face tracking uses a lightweight built-in detector. It tracks people very
  well, but for a video where someone walks fully out of frame and back, the
  camera can take a moment to catch up.
- It works best on **talking-head and podcast** footage. Sports or fast action
  with no clear single subject is harder to follow.
- Rendering is the slow part. A one-minute short takes roughly a minute on a
  mid-range phone at 720p.
- It reads the captions YouTube already has. If a video has no captions at
  all, there is nothing to cut on, and Qyro will say so instead of guessing.
