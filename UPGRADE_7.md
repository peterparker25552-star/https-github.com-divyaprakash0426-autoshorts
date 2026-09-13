# Qyro Version 7 — YouTube downloads restored

Qyro **v0.7.0**, called **Version 7**, fixes the YouTube download path without
requiring Gemini, Grok, or any AI API key.

## What was wrong

The v0.6.7 YouTube recovery code forced its first request through
`player_client=web`. Newer yt-dlp releases can choose a better client than
that forced value, and YouTube may answer the forced request with a bot check.
That made a build with a newer yt-dlp behave worse than v0.6.2.

## What Version 7 changes

- Restores the v0.6.2 behaviour: the first YouTube request lets the installed
  yt-dlp choose its own current client.
- Rotates through explicit player clients only after an HTTP 429 or a YouTube
  bot-check response.
- Recognises both `HTTP 429` and `Sign in to confirm you're not a bot` errors.
- Keeps the existing transcript cache and cookie-session support.
- Keeps the offline title/highlight engine as the default. Gemini and Grok are
  optional polish providers; they are not involved in downloading videos and
  no API key is required.
- Bumps the app and service-worker shell to `0.7.0`, so an installed Android
  PWA does not keep the old JavaScript shell.

## Upgrade on Android / Termux

From the Qyro checkout:

```bash
cd ~/autoshorts
git pull origin arena/01a099ac-https-github-com-divyaprakash0
pkg update -y
pkg install -y python ffmpeg git
bash install-android.sh
bash run-android.sh
```

If the checkout is on another branch, switch to the merged Version 7 commit or
clone the repository again after the pull request is merged. Then open
`http://localhost:8000` in Chrome.

If an old rate-limit marker exists and YouTube is still shown as paused after
the upgrade, wait for its cooldown or remove only this generated file and
restart Qyro:

```bash
rm -f data/.rate-limit.json
```

That file contains no credentials. If YouTube still returns a bot check, add a
signed-in Netscape-format `cookies.txt` through **Tools → YouTube session**;
never paste cookie contents into chat or commit them to Git.

## Verify Version 7

```bash
python -c "from autoshorts import __version__; print(__version__)"
```

It should print:

```text
0.7.0
```

The health panel should show the current yt-dlp version. Test with one short
video before queuing a whole playlist.
