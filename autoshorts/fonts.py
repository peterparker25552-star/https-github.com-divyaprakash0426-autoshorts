"""Caption font resolution — including Devanagari (Hindi) rescue.

A caption font is chosen by *id* (``bold``, ``rounded``, ``devanagari``, …)
rather than by name, because the fonts that exist on an Android phone, a Mac
and a Windows box are completely different. This module walks a stack of
candidate families and returns the first one the machine can actually draw,
falling back to :data:`config.CAPTION_FONT` so a render never dies on a font.

Two extras matter for Hindi:

* ``fc-list :lang=hi`` is the only reliable way to ask "which installed font
  has Devanagari glyphs". When it is available the Devanagari stacks are
  resolved through it.
* ``fontsdir`` — libass can load fonts straight out of a folder, so dropping a
  ``.ttf`` into ``data/fonts`` is enough. No ``sudo``, no system install, which
  is exactly what a Termux user needs.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from . import config

_CACHE: dict[str, object] = {}


def _fc_list() -> str:
    if "fc" not in _CACHE:
        _CACHE["fc"] = shutil.which("fc-list") or ""
    return str(_CACHE["fc"] or "")


def _run_fc(args: list[str]) -> str:
    binary = _fc_list()
    if not binary:
        return ""
    try:
        proc = subprocess.run(
            [binary, *args], capture_output=True, text=True, timeout=20
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout or ""


def installed_families() -> set[str]:
    """Every font family fontconfig knows about (cached; empty without fc-list).

    Includes the families of any ``.ttf``/``.otf`` dropped into
    :data:`config.FONTS_DIR`, matched by filename, so user fonts resolve even
    before fontconfig has indexed them.
    """
    if "families" in _CACHE:
        return set(_CACHE["families"])  # type: ignore[arg-type]
    families: set[str] = set()
    raw = _run_fc(["--format", "%{family[0]}\\n"])
    for line in raw.splitlines():
        name = line.strip()
        if name:
            families.add(name)
            families.add(name.split(",")[0].strip())
    families |= user_font_families()
    _CACHE["families"] = families
    return families


_STYLE_SUFFIXES = (
    "regular", "bold", "italic", "light", "medium", "semibold", "extrabold",
    "black", "thin", "heavy", "book", "demi", "condensed", "expanded",
    "narrow", "wide", "variable",
)


def _families_from_filename(stem: str) -> set[str]:
    """Guess the family names inside a font file's name.

    ``NotoSansDevanagari-Bold`` is the family ``NotoSansDevanagari``; without
    fontconfig (or fontTools) the filename is the only clue libass gives us,
    so both the stem and every style-stripped prefix are offered as candidates
    and matched loosely by :func:`_pick`.
    """
    out = {stem}
    parts = re.split(r"[-_ ]+", stem)
    while parts and parts[-1].lower() in _STYLE_SUFFIXES:
        parts.pop()
        if parts:
            out.add("".join(parts))
            out.add(" ".join(parts))
    if parts:
        out.add("".join(parts))
    return {name for name in out if name}


def user_font_families() -> set[str]:
    """Families of every font file dropped into :data:`config.FONTS_DIR`."""
    families: set[str] = set()
    try:
        entries = list(config.FONTS_DIR.iterdir())
    except Exception:
        return families
    for path in entries:
        if path.suffix.lower() not in config.FONT_EXTENSIONS:
            continue
        families.add(path.stem)
        families |= _families_from_filename(path.stem)
        families |= _families_from_fonttools(path)
    return families


def _families_from_fonttools(path) -> set[str]:
    """Exact family names via fontTools, when that optional dep is present."""
    try:
        from fontTools.ttLib import TTFont  # type: ignore

        font = TTFont(str(path), fontNumber=0, lazy=True)
        names = set()
        for record in font["name"].names:
            if record.nameID in (1, 16):
                text = str(record).strip()
                if text:
                    names.add(text)
        font.close()
        return names
    except Exception:
        return set()


def families_for_lang(lang: str) -> set[str]:
    """Families that can render ``lang`` (``hi`` = Devanagari)."""
    key = f"lang:{lang}"
    if key in _CACHE:
        return set(_CACHE[key])  # type: ignore[arg-type]
    raw = _run_fc([f":lang={lang}", "family"])
    out = {
        line.strip() for line in raw.replace(",", "\n").splitlines() if line.strip()
    }
    _CACHE[key] = out
    return out


def has_devanagari_font() -> bool:
    """True when some installed font can draw Devanagari."""
    known = families_for_lang("hi")
    if known:
        return True
    families = installed_families()
    return any(
        any(marker.lower() in name.lower() for marker in config.DEVANAGARI_FONTS)
        for name in families
    )


def is_devanagari_font(name: str) -> bool:
    """True when ``name`` is one of the known Devanagari families."""
    low = str(name or "").lower()
    return any(marker.lower() in low for marker in config.DEVANAGARI_FONTS)


def _pick(stack: list[str], pool: set[str] | None = None) -> str:
    pool = installed_families() if pool is None else pool
    for name in stack:
        if not pool:            # no fontconfig: trust the first candidate
            return name
        if name in pool:
            return name
        # fontconfig reports "Noto Sans Devanagari" but the file may say
        # "NotoSansDevanagari-Regular" — accept a de-spaced match too
        squashed = name.replace(" ", "").lower()
        for candidate in pool:
            if candidate.replace(" ", "").lower() == squashed:
                return candidate
    return ""


def resolve(
    font_id: str | None,
    text: str = "",
    language: str = "auto",
) -> tuple[str, str]:
    """Resolve a font id to a real family name.

    Returns ``(family, note)``. ``text``/``language`` let the resolver rescue
    Devanagari: if the caption text is Devanagari (or the language is ``hi``)
    and the chosen family cannot draw it, a Devanagari family is substituted
    instead — otherwise every Hindi word renders as an empty box.
    """
    font_id = str(font_id or config.DEFAULT_CAPTION_FONT).strip().lower()
    stack = list(config.FONT_STACKS.get(font_id) or [])
    needs_deva = wants_devanagari(text, language)
    if needs_deva and not (stack and is_devanagari_font(stack[0])):
        stack = list(config.FONT_STACKS["devanagari"]) + stack
    pool = installed_families()
    if needs_deva:
        lang_pool = families_for_lang("hi")
        if lang_pool:
            picked = _pick(stack, lang_pool) or _pick(
                list(config.FONT_STACKS["devanagari"]), lang_pool
            )
            if picked:
                return picked, f"font={font_id} (Devanagari)"
    picked = _pick(stack, pool)
    if not picked:
        base = config.CAPTION_FONT
        return base, f"font={font_id} (fallback to {base})"
    return picked, f"font={font_id}"


_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")


def wants_devanagari(text: str = "", language: str = "auto") -> bool:
    """True when the captions need Devanagari glyphs."""
    lang = str(language or "auto").strip().lower()
    if lang == "hi":
        return True
    if lang == "en":
        return False
    return bool(_DEVANAGARI_RE.search(str(text or "")))


def devanagari_share(text: str) -> float:
    """Fraction of letters in ``text`` that are Devanagari (0.0 for none)."""
    letters = [ch for ch in str(text or "") if ch.isalpha()]
    if not letters:
        return 0.0
    hits = sum(1 for ch in letters if _DEVANAGARI_RE.match(ch))
    return hits / len(letters)


def available_fonts() -> list[dict]:
    """The font picker payload for the UI."""
    pool = installed_families()
    out = []
    for font_id in config.CAPTION_FONTS:
        stack = config.FONT_STACKS.get(font_id) or []
        found = _pick(stack, pool) if stack else ""
        out.append({
            "id": font_id,
            "label": font_id.replace("-", " ").title(),
            "candidates": list(stack),
            "resolved": found or (config.CAPTION_FONT if not stack else ""),
            "installed": bool(found),
        })
    return out


def fontsdir() -> str:
    """The folder libass should also search for fonts (created on demand).

    Also makes sure anything the user dropped in there is *installed* where
    fontconfig can see it — see :func:`ensure_installed`.
    """
    ensure_installed()
    try:
        config.FONTS_DIR.mkdir(parents=True, exist_ok=True)
        return str(config.FONTS_DIR)
    except Exception:
        return ""


# Where fontconfig looks for per-user fonts. ``~/.fonts`` is the classic path
# and the one Termux actually scans; the XDG path covers newer desktops.
def user_font_roots() -> list:
    import os
    from pathlib import Path

    roots = [Path.home() / ".fonts"]
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    roots.append(Path(xdg) / "fonts")
    return roots


def ensure_installed(force: bool = False) -> dict:
    """Copy ``data/fonts`` into a fontconfig-scanned dir and refresh the cache.

    This is not optional polish: libass asks *fontconfig* to resolve a family
    name, so a font that only exists in ``fontsdir`` is loaded but never
    selected — the render silently falls back to a Latin font and every Hindi
    word becomes a box. Installing into ``~/.fonts`` (which fontconfig scans,
    Termux included) is what makes the family resolvable.

    Idempotent and never raises; returns a small status dict for the UI.
    """
    if _CACHE.get("installed") and not force:
        return dict(_CACHE["installed_status"] or {})  # type: ignore[arg-type]
    status = {"copied": 0, "skipped": 0, "cache_refreshed": False, "root": "",
              "downloaded": 0, "detail": ""}
    sources = _bundled_fonts()
    if not sources and not has_devanagari_font():
        # Nothing bundled and nothing installed: try the one-command download.
        fetched = download_devanagari()
        status["downloaded"] = len(fetched.get("installed") or [])
        status["detail"] = fetched.get("detail") or ""
        sources = _bundled_fonts()
    target_root = None
    for root in user_font_roots():
        try:
            root.mkdir(parents=True, exist_ok=True)
            target_root = root
            break
        except Exception:
            continue
    if target_root is not None:
        import shutil

        for source in sources:
            target = target_root / source.name
            try:
                if target.exists() and target.stat().st_size == source.stat().st_size:
                    status["skipped"] += 1
                    continue
                shutil.copy2(source, target)
                status["copied"] += 1
            except Exception:
                continue
        status["root"] = str(target_root)
    if status["copied"] or force:
        status["cache_refreshed"] = _refresh_font_cache()
    _CACHE["installed"] = True
    _CACHE["installed_status"] = status
    clear_family_cache()
    return status


# --------------------------------------------------------------------------
# Obtaining a Devanagari font when the machine has none
# --------------------------------------------------------------------------
# A fresh clone ships no fonts (``data/`` is user state, not source), so on a
# new phone Hindi captions would render as boxes until the user hunted down a
# .otf themselves. The ``devanagari-fonts`` PyPI package ships the OFL-licensed
# Shobhika family as a pure-data sdist, and pip is already present on Termux —
# so "install Hindi support" is one command with no sudo, no apt and no GitHub
# raw fetch (which Termux cannot always reach).
DEVANAGARI_PYPI_PACKAGE = "devanagari-fonts"


def _bundled_fonts() -> list[Path]:
    try:
        return sorted(
            path for path in config.FONTS_DIR.iterdir()
            if path.suffix.lower() in config.FONT_EXTENSIONS
        )
    except Exception:
        return []


def download_devanagari(timeout: float = 180.0) -> dict:
    """Fetch an OFL Devanagari font into ``config.FONTS_DIR`` via pip.

    Returns ``{"ok": bool, "installed": [...], "detail": str}``. Offline, no
    pip, or a failed download is a normal outcome, not an exception: the
    caller falls back to whatever is already installed.
    """
    import sys
    import tarfile
    import tempfile

    status: dict = {"ok": False, "installed": [], "detail": ""}
    python = sys.executable or "python3"
    with tempfile.TemporaryDirectory(prefix="qyro-fonts-") as tmp:
        try:
            proc = subprocess.run(
                [python, "-m", "pip", "download", "--no-deps",
                 "--no-binary", ":all:", "-d", tmp, DEVANAGARI_PYPI_PACKAGE],
                capture_output=True, timeout=timeout,
            )
        except Exception as exc:
            status["detail"] = f"pip download failed: {exc}"
            return status
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace")
            status["detail"] = "pip could not fetch the font package: " + (
                tail.strip().splitlines()[-1] if tail.strip() else "unknown error"
            )
            return status

        archives = [
            path for path in Path(tmp).iterdir()
            if path.name.endswith((".tar.gz", ".tgz", ".zip"))
        ]
        found: list[Path] = []
        for archive in archives:
            try:
                with tarfile.open(archive, "r:*") as tar:
                    for member in tar.getmembers():
                        if not member.isfile():
                            continue
                        name = Path(member.name).name
                        if not name.lower().endswith(config.FONT_EXTENSIONS):
                            continue
                        if "shobhika" not in name.lower():
                            continue
                        handle = tar.extractfile(member)
                        if handle is None:
                            continue
                        config.FONTS_DIR.mkdir(parents=True, exist_ok=True)
                        target = config.FONTS_DIR / name
                        target.write_bytes(handle.read())
                        found.append(target)
            except Exception:
                continue
        status["installed"] = [path.name for path in found]
        status["ok"] = bool(found)
        status["detail"] = (
            f"installed {len(found)} font file(s) from PyPI"
            if found else "the font package contained no usable font file"
        )
    return status


def _refresh_font_cache() -> bool:
    """Run ``fc-cache -f`` when fontconfig's CLI is installed."""
    binary = shutil.which("fc-cache")
    if not binary:
        return False
    try:
        proc = subprocess.run(
            [binary, "-f"], capture_output=True, text=True, timeout=120
        )
        return proc.returncode == 0
    except Exception:
        return False


def clear_family_cache() -> None:
    """Drop only the cached family lists (keeps the install marker)."""
    for key in [k for k in _CACHE if k == "families" or k.startswith("lang:")]:
        _CACHE.pop(key, None)


def clear_cache() -> None:
    """Drop every cached font fact (used after a font is uploaded)."""
    _CACHE.clear()
