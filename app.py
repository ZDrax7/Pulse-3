"""Fintech Pulse Studio: topic -> script -> voice -> 1080x1920 short, in Streamlit.

Free by default: no API keys. Optional secrets: XAI_API_KEY (Grok scripts).
Voice = Edge-TTS (gTTS fallback). Frames are drawn with Pillow and piped to FFmpeg.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ==============================================================================
# DATA (ported from the clip-desk: patterns, CTA modes, lengths, voices, topics)
# ==============================================================================
PATTERNS = {
    "number": ("Number shock", "Open on one figure",
               "First words are a specific dollar, percent, or count. The next sentence names the mechanism. No warm-up."),
    "teardown": ("Tool teardown", "One feature, named",
                 "Name the product category by the second sentence. Isolate the single feature that saves time or basis points. Skip the tour."),
    "contrarian": ("Contrarian correction", "Stop the default",
                   "Call out the common habit, then replace it with the cheaper or faster substitute. The viewer should feel slightly wrong, then relieved."),
    "stack": ("Operator stack", "Three tools, one job",
              "Three beats, one job each, said fast. The payoff is a shortlist, not a philosophy. The CTA offers the list."),
}
CTA_MODES = {
    "keyword": ("Comment keyword", "End by asking viewers to comment one short keyword so a human or ManyChat can reply with the list. Say the keyword clearly."),
    "affiliate": ("Affiliate mention", "One calm line that the link in the bio tracks a bonus or comparison. No hype, no income claim, no 'you will make'."),
    "manychat": ("ManyChat DM", "Ask them to DM one keyword. Say that the automation sends the comparison. Do not promise returns."),
}
LENGTHS = {30: (64, 100), 38: (84, 126), 45: (104, 150)}
VOICES = {  # clip-desk voice name -> free Edge-TTS voice
    "Leo · Authoritative": "en-US-ChristopherNeural",
    "Rex · Clear": "en-US-EricNeural",
    "Altair · Premium": "en-US-AndrewNeural",
    "Perseus · Trust": "en-GB-RyanNeural",
    "Sal · Balanced": "en-US-GuyNeural",
    "Eve · Upbeat": "en-US-JennyNeural",
}
DEFAULT_VOICE = "en-US-ChristopherNeural"
TOPICS = [
    "Top 3 automated budgeting apps worth paying for in 2026",
    "Why CFOs are retiring the spreadsheet treasury",
    "The cash account still beating a checking balance",
    "Three payroll APIs quiet operators already run",
]
THEMES = {  # accent colour; the house look is Neon Cyber
    "Neon Cyber": (0, 240, 255), "Money Green": (0, 255, 150),
    "Sunset Gold": (255, 200, 60), "Royal Violet": (190, 140, 255),
}
BASE = (7, 11, 16)
STYLES = ["Circuit plate (house look)", "Trading-chart motion", "Aurora glow", "My own clips / photos"]
GENERIC_BEATS = ["Start with the one number that actually matters.",
                 "Automate it, so willpower is not part of the plan.",
                 "Check it once a month and ignore the noise."]


# ==============================================================================
# SCRIPT MODEL (plain dicts, same shape as the clip-desk's PulseScript)
# ==============================================================================
def narration_of(sc):
    parts = [sc["hook"], *[b["line"] for b in sc["beats"]], sc["cta"]["spoken"]]
    return " ".join(p.strip() for p in parts if p.strip())


def segments_of(sc):
    rows = [{"kind": "hook", "text": sc["hook"], "onScreen": sc["hook"], "stat": None}]
    rows += [{"kind": "beat", "text": b["line"], "onScreen": b["onScreen"], "stat": b.get("stat")} for b in sc["beats"]]
    rows.append({"kind": "cta", "text": sc["cta"]["spoken"], "onScreen": sc["cta"]["onScreen"], "stat": None})
    return rows


def word_count(text):
    return len(text.split())


HOUSE_SCRIPT = {
    "topic": "The 3-account system operators actually use", "title": "The 3-account split", "viralScore": 0,
    "whyItWorks": ["Opens on a broken habit, not a greeting.", "A hard number lands before the explanation finishes.",
                   "The CTA is a keyword for the list, not a lecture."],
    "hook": "Most people budget in one account. That is why it breaks.",
    "beats": [
        {"line": "Income lands in a bills account the same morning it arrives.", "onScreen": "Bills account, same day",
         "stat": {"label": "accounts", "value": "3"}},
        {"line": "A spend account gets a weekly number, not a vibe.", "onScreen": "One weekly number", "stat": None},
        {"line": "The leftover moves to yield before you can see it.", "onScreen": "Yield, on autopilot", "stat": None},
    ],
    "cta": {"spoken": "Comment STACK and I'll send the exact split.", "onScreen": "Comment STACK", "keyword": "STACK"},
}


# ---- align (cues, segment timing, SRT) ----
def estimate_cues(text, duration):
    words = text.split()
    if not words:
        return []
    weights = [max(1, sum(ch.isalnum() for ch in w)) for w in words]
    total = sum(weights) or 1
    span = max(duration, len(words) * 0.18)
    cur, cues = 0.0, []
    for w, wt in zip(words, weights):
        width = span * wt / total
        cues.append({"text": w, "start": cur, "end": cur + width})
        cur += width
    return cues


def time_segments(cues, segments):
    cursor, out = 0, []
    for seg in segments:
        need = max(1, word_count(seg["text"]))
        si = min(cursor, max(len(cues) - 1, 0))
        cursor = min(len(cues), cursor + need)
        ei = max(si, min(len(cues) - 1, cursor - 1))
        start = cues[si]["start"] if cues else 0.0
        end = cues[ei]["end"] if cues else start
        out.append({**seg, "start": start, "end": max(end, start + 0.2)})
    return out


def srt_ts(sec):
    ms = max(0, round(sec * 1000))
    return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"


def to_srt(cues):
    chunks, cur, start = [], [], 0.0
    for c in cues:
        if not cur:
            start = c["start"]
        cur.append(c)
        if c["end"] - start > 2.2 or len(cur) >= 6:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return "\n".join(f"{i + 1}\n{srt_ts(ch[0]['start'])} --> {srt_ts(ch[-1]['end'])}\n{' '.join(c['text'] for c in ch)}\n"
                     for i, ch in enumerate(chunks))


# ---- viral read (structure hints from a pasted transcript) ----
def score_transcript(text):
    words = re.findall(r"\S+", text)
    if not words:
        return ""
    sents = [s for s in re.split(r"[.!?]+", text) if s.strip()]
    avg = sum(len(s.split()) for s in sents) / max(len(sents), 1)
    hints = []
    if len(re.findall(r"\d", text)) >= 2:
        hints.append("The reference leans on specific figures. Keep a number in the hook or first beat.")
    hints.append("Sentences are short. Match that pace." if avg <= 12 else "Tighten sentences. Shorts die in clauses.")
    if "?" in text:
        hints.append("A question shows up. Use at most one, and not as a greeting.")
    return " ".join(hints)


# ---- key-free drafting ----
def _short(text, limit=42):
    text = " ".join(text.split()).rstrip(".!?")
    return text if len(text) <= limit else (text[:limit].rsplit(" ", 1)[0] or text[:limit])


def _hook(topic, pattern, stat):
    t = topic.strip().rstrip(".?!")
    low = t[:1].lower() + t[1:]
    if pattern == "number":
        return f"{stat['value']} {stat['label']}. Here is what to do about it." if stat else f"One number changes how you think about {low}."
    if pattern == "teardown":
        return f"Before you pay for anything, look at this: {low}."
    if pattern == "contrarian":
        return f"Most people get this backwards: {low}."
    return f"{t}. Here is the short version."


def _cta(mode, keyword):
    kw = (keyword or "GUIDE").strip().upper()[:16]
    if mode == "affiliate":
        return {"spoken": "The link in my bio has the comparison. Not financial advice.", "onScreen": "Link in bio", "keyword": kw}
    if mode == "manychat":
        return {"spoken": f"Message me {kw} and I will send the comparison.", "onScreen": f"DM {kw}", "keyword": kw}
    return {"spoken": f"Comment {kw} and I will send the full list.", "onScreen": f"Comment {kw}", "keyword": kw}


def draft_from_points(topic, points, pattern, cta_mode, keyword, stat_label="", stat_value=""):
    stat = {"label": stat_label.strip()[:28] or "the number", "value": stat_value.strip()[:18]} if stat_value.strip() else None
    lines = [p.strip() for p in points if p.strip()][:3]
    while len(lines) < 3:
        lines.append(GENERIC_BEATS[len(lines)])
    beats = [{"line": l if l.endswith((".", "!", "?")) else l + ".", "onScreen": _short(l), "stat": None} for l in lines]
    if stat:
        beats[0]["stat"] = stat
    return {"topic": topic, "title": _short(topic, 60), "viralScore": 0,
            "whyItWorks": ["Names the topic in the first line.", "One idea per beat keeps the pace up."],
            "hook": _hook(topic, pattern, stat), "beats": beats, "cta": _cta(cta_mode, keyword)}


def parse_pasted(text, topic="Pasted script"):
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", " ".join(text.split())) if s.strip()]
    sents += ["Follow for more."] * max(0, 3 - len(sents))
    hook, cta_line, mid = sents[0], sents[-1], sents[1:-1]
    k = max(1, round(len(mid) / 3))
    groups = [mid[i:i + k] for i in range(0, len(mid), k)]
    while len(groups) > 3:
        last = groups.pop()
        groups[-1] += last
    while len(groups) < 3:
        groups.append([GENERIC_BEATS[len(groups)]])
    return {"topic": topic, "title": _short(hook, 60), "viralScore": 0,
            "whyItWorks": ["Your own words, timed to the voice."], "hook": hook,
            "beats": [{"line": " ".join(g), "onScreen": _short(g[0]), "stat": None} for g in groups],
            "cta": {"spoken": cta_line, "onScreen": _short(cta_line, 28), "keyword": ""}}


# ---- AI drafting (Grok via key, or best-effort free endpoint) ----
SYSTEM_PROMPT = """You write vertical short-form scripts for Fintech Pulse, a Tier-1 channel (US, UK, Canada, Australia) about wealth, fintech, and B2B financial tools.
The cut is 9:16, spoken in a clean professional voice, optimized for retention and a soft affiliate or keyword CTA.
Rules:
- No greetings, no 'hey guys', no hashtags, no emojis, no stage directions.
- Hook is a pattern interrupt. Do not start with the channel name.
- Exactly three beats. One idea each. Named product categories. Use a number only if it is common knowledge or given in the topic.
- Never promise returns, never give personalized financial advice, never invent testimonials, statistics or user counts.
- If reference notes are included, copy their structure only. Do not reuse distinctive phrases.
- onScreen lines are 42 characters or fewer.
- Put a stat object {label, value} on a beat only when a real, given number does the work. Otherwise omit stat.
- whyItWorks explains the retention mechanic in plain language.
Return only JSON with keys title, viralScore, whyItWorks, patternsUsed, hook, beats, cta.
beats is an array of 3 objects: line, onScreen, optional stat.
cta is {spoken, onScreen, keyword}.
viralScore is an integer from 72 to 94."""


def _post_json(url, body, headers, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _extract_json(text):
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    raw = m.group(1) if m else text
    a, b = raw.find("{"), raw.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("The model did not return a script.")
    return json.loads(raw[a:b + 1])


def _bound(v, lo, hi, label):
    v = " ".join(str(v or "").split())
    if not lo <= len(v) <= hi:
        raise ValueError(f"{label} length was {len(v)}.")
    return v


def validate_script(data, topic):
    beats = data.get("beats")
    if not isinstance(beats, list) or len(beats) != 3:
        raise ValueError("Expected exactly three beats.")
    out = []
    for b in beats:
        st_raw, stat = b.get("stat"), None
        if isinstance(st_raw, dict) and st_raw.get("label") and st_raw.get("value"):
            stat = {"label": str(st_raw["label"])[:28], "value": str(st_raw["value"])[:18]}
        out.append({"line": _bound(b.get("line"), 8, 240, "beat line"), "onScreen": _bound(b.get("onScreen"), 2, 72, "onScreen"), "stat": stat})
    cta = data.get("cta") if isinstance(data.get("cta"), dict) else {}
    why = [str(x).strip() for x in (data.get("whyItWorks") or []) if str(x).strip()][:4]
    try:
        score = max(0, min(100, int(float(data.get("viralScore") or 0))))
    except (TypeError, ValueError):
        score = 0
    return {"topic": topic, "title": _bound(data.get("title"), 4, 90, "title"), "viralScore": score, "whyItWorks": why or ["Tight, spoken-first structure."],
            "hook": _bound(data.get("hook"), 8, 200, "hook"), "beats": out,
            "cta": {"spoken": _bound(cta.get("spoken"), 8, 200, "cta"), "onScreen": _bound(cta.get("onScreen"), 2, 48, "cta screen"),
                    "keyword": str(cta.get("keyword") or "")[:24]}}


def ai_script(topic, pattern, cta_mode, seconds, notes, xai_key=None):
    """Grok when a key exists, else a free public endpoint (best effort). Raises on failure."""
    lo, hi = LENGTHS[seconds]
    pname, _, pbrief = PATTERNS[pattern]
    clabel, cinstr = CTA_MODES[cta_mode]
    correction, last, fallback = "", "The script didn't lock.", None
    for _ in range(2):
        user = "\n\n".join(filter(None, [
            f"Topic: {topic}", f"Pattern: {pname}. {pbrief}", f"Length: about {seconds} seconds. Spoken word budget: {lo} to {hi}.",
            f"CTA mode: {clabel}. {cinstr}",
            f"Reference notes (structure only, do not copy wording):\n{notes[:1500]}" if notes.strip() else "No reference notes.",
            f"Rewrite. The previous draft failed this check: {correction}" if correction else ""]))
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
        if xai_key:
            body = {"model": "grok-4.5", "temperature": 0.6, "max_tokens": 1000, "messages": msgs}
            try:
                payload = _post_json("https://api.x.ai/v1/chat/completions", {**body, "response_format": {"type": "json_object"}},
                                     {"Authorization": f"Bearer {xai_key}"})
            except Exception:
                payload = _post_json("https://api.x.ai/v1/chat/completions", body, {"Authorization": f"Bearer {xai_key}"})
        else:
            payload = _post_json("https://text.pollinations.ai/openai", {"model": "openai", "messages": msgs, "jsonMode": True}, {}, 75)
        try:
            sc = validate_script(_extract_json(payload["choices"][0]["message"]["content"]), topic)
        except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            correction = last = str(exc)
            continue
        n = word_count(narration_of(sc))
        if lo <= n <= hi:
            return sc
        correction = last = f"Word count was {n}. Stay inside {lo} to {hi} spoken words."
        fallback = sc
    if fallback:
        return fallback  # word count slightly off, but usable
    raise ValueError(last)


# ==============================================================================
# VOICE (free): Edge-TTS with word timestamps, gTTS fallback
# ==============================================================================
@lru_cache(maxsize=1)
def ffmpeg_path():
    p = shutil.which("ffmpeg")
    if p:
        return p
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def audio_duration(path):
    r = subprocess.run([ffmpeg_path(), "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", r.stderr)
    if not m:
        raise RuntimeError("Could not read audio duration")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])


async def _edge(text, voice, rate, dest):
    import edge_tts
    try:
        comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    except TypeError:
        comm = edge_tts.Communicate(text, voice, rate=rate)
    cues = []
    with open(dest, "wb") as fh:
        async for ch in comm.stream():
            if ch["type"] == "audio":
                fh.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                s = ch["offset"] / 1e7
                cues.append({"text": ch["text"], "start": s, "end": s + ch["duration"] / 1e7})
    if os.path.getsize(dest) == 0:
        raise RuntimeError("empty audio")
    return cues


def synthesize(text, dest, voice, speed_pct):
    import asyncio
    rate, err = f"{speed_pct:+d}%", None
    for v in (voice, voice, DEFAULT_VOICE):
        try:
            cues = asyncio.run(_edge(text, v, rate, dest))
            dur = audio_duration(dest)
            return {"duration": dur, "cues": cues or estimate_cues(text, dur), "aligned": bool(cues), "engine": "Edge-TTS"}
        except Exception as exc:
            err = exc
    try:
        from gtts import gTTS
        gTTS(text, lang="en").save(dest)
        dur = audio_duration(dest)
        return {"duration": dur, "cues": estimate_cues(text, dur), "aligned": False, "engine": "gTTS (fallback)"}
    except Exception as exc:
        raise RuntimeError(f"Both free voice engines failed. Edge-TTS: {err} | gTTS: {exc}")


def silent_audio(dest, duration):
    r = subprocess.run([ffmpeg_path(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=44100",
                        "-t", f"{duration:.3f}", "-c:a", "pcm_s16le", dest], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr[-300:])


# ==============================================================================
# RENDERER: Pillow frames -> FFmpeg (no libass, no keys)
# ==============================================================================
FONT_CANDIDATES = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                   "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf", "/Library/Fonts/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"]


@lru_cache(maxsize=96)
def get_font(size):
    size = max(8, int(size))
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def ease(x):
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) * (1 - x)


def wrap_text(d, text, font, maxw, maxlines):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if cur and d.textlength(trial, font=font) > maxw:
            lines.append(cur)
            cur = w
            if len(lines) == maxlines:
                break
        else:
            cur = trial
    if cur and len(lines) < maxlines:
        lines.append(cur)
    if " ".join(lines) != " ".join(words) and lines:
        lines[-1] = lines[-1][:27].rstrip() + "…"
    return lines


class Scene:
    def __init__(self, W, H, accent, style, cues, segments, title, channel, total, bg_reader=None, seed=7):
        self.W, self.H, self.k = W, H, W / 1080
        self.accent, self.style, self.cues, self.segments = accent, style, cues, segments
        self.title, self.channel, self.total, self.bg_reader = title, channel.upper(), total, bg_reader
        rng = np.random.default_rng(seed)
        sw, sh = max(W // 6, 90), max(H // 6, 160)
        yy, xx = np.mgrid[0:sh, 0:sw].astype(np.float32)
        self.X, self.Y, self.aspect = xx / sh, yy / sh, sw / sh
        self.base = np.array(BASE, np.float32)
        a = np.array(accent, np.float32)
        self.wash_col = a * .13 + np.array((14, 22, 26), np.float32)
        d = np.sqrt((self.X - .5 * self.aspect) ** 2 + (self.Y - .427) ** 2) / .51
        self.wash = np.clip(1 - d, 0, 1)[..., None] ** 1.5
        self.static_bg = self._plate()
        self.blob_cols = [np.array(c, np.float32) for c in ((0, 240, 255), (120, 60, 255), (255, 0, 150))]
        self.blob_cols = [c * .4 + a * .6 for c in self.blob_cols]
        self.blob_ph, self.blob_sp = rng.uniform(0, 6.28, (3, 2)), rng.uniform(.25, .6, (3, 2))
        n = 40
        self.p = dict(x=rng.uniform(0, W, n), y=rng.uniform(0, H, n), s=rng.uniform(20, 90, n) * H / 1920,
                      r=rng.uniform(2, 7, n) * H / 1920, ph=rng.uniform(0, 6.28, n), a=rng.integers(40, 120, n))
        self.SEG, self.candle_sets = 5.0, []
        for _ in range(3):
            o, cs = 0.0, []
            for _ in range(28):
                c = o + rng.normal(.18, 1.0)
                cs.append((o, max(o, c) + abs(rng.normal(0, .5)), min(o, c) - abs(rng.normal(0, .5)), c))
                o = c
            arr = np.array(cs)
            self.candle_sets.append((arr - arr.min()) / (arr.max() - arr.min()))
        self.vol = rng.uniform(.15, 1, (3, 28))

    def _plate(self):
        img = np.clip(self.base + self.wash * (self.wash_col - self.base), 0, 255).astype(np.uint8)
        return Image.fromarray(img).resize((self.W, self.H), Image.BICUBIC)

    def background(self, t):
        if self.style == "footage" and self.bg_reader is not None:
            img = self.bg_reader()
            if img is not None:
                arr = np.asarray(img, np.float32) * .5 + self.base * .3
                return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        if self.style == "aurora":
            img = np.tile(self.base, (*self.X.shape, 1))
            for i, col in enumerate(self.blob_cols):
                cx = (.5 + .45 * math.sin(t * self.blob_sp[i, 0] + self.blob_ph[i, 0])) * self.aspect
                cy = .5 + .55 * math.sin(t * self.blob_sp[i, 1] + self.blob_ph[i, 1])
                img += np.exp(-((self.X - cx) ** 2 + (self.Y - cy) ** 2) / (2 * .16 ** 2))[..., None] * col * .5
            return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).resize((self.W, self.H), Image.BICUBIC)
        return self.static_bg.copy()

    # ---- decorations ----
    def grid(self, d, t):
        W, H, k = self.W, self.H, self.k
        g = 72 * k
        drift = (t * 16 * k) % g
        col = (*self.accent, 18)
        x = -g
        while x <= W + g:
            d.line([(x + drift, 0), (x + drift, H)], fill=col, width=1)
            x += g
        y = -g
        while y <= H + g:
            d.line([(0, y + drift * .35), (W, y + drift * .35)], fill=col, width=1)
            y += g

    def circuit(self, d, t):
        k = self.k
        paths = [[40, 280, 220, 280, 220, 460, 420, 460], [1040, 360, 860, 360, 860, 620, 700, 620],
                 [80, 1500, 260, 1500, 260, 1320, 480, 1320]]
        off = t * 70 * k
        for path in paths:
            pts = [(path[i] * k, path[i + 1] * k) for i in range(0, len(path), 2)]
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                L = math.hypot(x1 - x0, y1 - y0) or 1
                ux, uy = (x1 - x0) / L, (y1 - y0) / L
                pos = -(off % (32 * k))
                while pos < L:
                    a, b = max(pos, 0), min(pos + 14 * k, L)
                    if b > a:
                        d.line([(x0 + ux * a, y0 + uy * a), (x0 + ux * b, y0 + uy * b)], fill=(*self.accent, 96), width=max(2, int(2 * k)))
                    pos += 32 * k
        for i in range(7):
            h = (28 + ((math.sin(t * 1.4 + i) + 1) * .5) * 70) * k
            col = (*self.accent, 140 if i % 2 == 0 else 56)
            d.rectangle([(780 + i * 22) * k, 340 * k - h, (792 + i * 22) * k, 340 * k], fill=col)

    def particles(self, d, t):
        W, H, a, P = self.W, self.H, self.accent, self.p
        for i in range(len(P["x"])):
            y = (P["y"][i] - P["s"][i] * t * 3) % H
            x = P["x"][i] + math.sin(t * .8 + P["ph"][i]) * 22 * self.k
            r = P["r"][i]
            d.ellipse([x - r * 2.2, y - r * 2.2, x + r * 2.2, y + r * 2.2], fill=(*a, int(P["a"][i] * .22)))
            d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, int(P["a"][i])))

    def candles(self, d, t):
        W, H, a = self.W, self.H, self.accent
        x0, x1, y0, y1 = W * .08, W * .92, H * .585, H * .70
        d.rounded_rectangle([x0 - W * .02, y0 - H * .015, x1 + W * .02, y1 + H * .05], radius=W * .03,
                            fill=(0, 0, 0, 95), outline=(*a, 60), width=max(1, W // 540))
        seg, lt = int(t // self.SEG) % 3, (t % self.SEG) / self.SEG
        cs = self.candle_sets[seg]
        n, kk = len(cs), min(len(cs), lt * 1.15 * len(cs))
        slot, bw, ch = (x1 - x0) / n, (x1 - x0) / n * .62, y1 - y0
        closes = []
        for i in range(int(math.ceil(kk))):
            o, h, l, c = cs[i]
            grow = 1.0 if i < int(kk) else (kk - int(kk))
            c = o + (c - o) * grow
            h, l = (max(h, o, c), min(l, o, c)) if grow >= 1 else (max(o, c), min(o, c))
            cx = x0 + slot * (i + .5)
            yo, yc, yh, yl = [y1 - ch * v for v in (o, c, h, l)]
            col = (0, 230, 130, 255) if c >= o else (255, 70, 90, 255)
            d.line([(cx, yh), (cx, yl)], fill=col, width=max(2, W // 300))
            d.rounded_rectangle([cx - bw / 2, min(yo, yc), cx + bw / 2, max(yo, yc) + 2], radius=2, fill=col)
            vh = self.vol[seg][i] * H * .028
            d.rectangle([cx - bw / 2, y1 + H * .04 - vh, cx + bw / 2, y1 + H * .04], fill=(*col[:3], 90))
            closes.append((cx, yc))
        if len(closes) > 5:
            ma = [(closes[i][0], sum(p[1] for p in closes[i - 4:i + 1]) / 5) for i in range(4, len(closes))]
            d.line(ma, fill=(*a, 70), width=max(6, W // 80), joint="curve")
            d.line(ma, fill=(*a, 240), width=max(3, W // 220), joint="curve")
        if closes:
            ex, ey = closes[-1]
            r = W / 80 * (1 + .35 * math.sin(t * 7))
            d.ellipse([ex - r * 2.4, ey - r * 2.4, ex + r * 2.4, ey + r * 2.4], fill=(*a, 60))
            d.ellipse([ex - r, ey - r, ex + r, ey + r], fill=(255, 255, 255, 255))

    # ---- text layers (match the clip-desk stage) ----
    def header(self, d, t):
        W, H, k, a = self.W, self.H, self.k, self.accent
        cx, cy = 92 * k, 118 * k
        d.ellipse([cx - 9 * k, cy - 9 * k, cx + 9 * k, cy + 9 * k], fill=(*a, 255))
        r = (16 + (t % 1.4) * 6) * k
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(*a, 115), width=max(1, int(2 * k)))
        d.text((124 * k, 120 * k), self.channel, font=get_font(34 * k), fill=(*a, 255), anchor="lm")
        title = self.title if len(self.title) <= 36 else self.title[:35].rstrip() + "…"
        d.text((92 * k, 176 * k), title, font=get_font(26 * k), fill=(232, 246, 248, 160), anchor="lm")

    def current_segment(self, t):
        for s in self.segments:
            if s["start"] <= t < s["end"]:
                return s
        return self.segments[-1] if self.segments else None

    def hero(self, d, t):
        W, H, k, a = self.W, self.H, self.k, self.accent
        s = self.current_segment(t)
        if not s:
            return
        local = min(1.0, (t - s["start"]) / .28)
        dy = (1 - ease(local)) * 28 * k
        alpha = int(255 * local)
        x, y, w, h = 90 * k, 430 * k + dy, 900 * k, 620 * k
        d.rounded_rectangle([x, y, x + w, y + h], radius=36 * k, fill=(18, 26, 34, int(210 * local)), outline=(*a, int(97 * local)), width=max(1, int(2 * k)))
        kicker = {"hook": "HOOK", "cta": "CTA"}.get(s["kind"], "BEAT")
        d.text((x + 56 * k, y + 72 * k), kicker, font=get_font(22 * k), fill=(*a, alpha), anchor="lm")
        stat = s.get("stat")
        if stat:
            d.text((x + 56 * k, y + 230 * k), str(stat["value"]), font=get_font(120 * k), fill=(*a, alpha), anchor="lm")
            d.text((x + 60 * k, y + 310 * k), str(stat["label"]).upper(), font=get_font(28 * k), fill=(232, 246, 248, int(alpha * .7)), anchor="lm")
        f = get_font(64 * k)
        start_y = y + (420 if stat else 250) * k
        for i, line in enumerate(wrap_text(d, s["onScreen"], f, 780 * k, 3)):
            d.text((x + 56 * k, start_y + i * 78 * k), line, font=f, fill=(232, 246, 248, alpha), anchor="lm")

    def captions(self, d, t):
        W, H, k, a, cues = self.W, self.H, self.k, self.accent, self.cues
        if not cues:
            return
        idx = next((i for i, c in enumerate(cues) if c["start"] <= t < c["end"] + .02), -1)
        if idx < 0:
            idx = max([i for i, c in enumerate(cues) if c["start"] <= t] or [0])
        start = max(0, idx - 2)
        win = [(c["text"], start + j == idx) for j, c in enumerate(cues[start:start + 7])]
        f = get_font(52 * k)
        sp = d.textlength(" ", font=f)
        lines, cur, cw = [], [], 0.0
        for text, act in win:
            ww = d.textlength(text, font=f)
            if cur and cw + sp + ww > 900 * k:
                lines.append((cur, cw))
                cur, cw = [], 0.0
            cur.append((text, act, ww))
            cw += ww + (sp if len(cur) > 1 else 0)
        lines.append((cur, cw))
        y = 1560 * k
        for ln, lw in lines:
            x = (W - lw) / 2
            for text, act, ww in ln:
                if act:
                    d.rounded_rectangle([x - 12 * k, y - 36 * k, x + ww + 12 * k, y + 32 * k], radius=14 * k, fill=(*a, 41))
                d.text((x, y), text, font=f, fill=(*a, 255) if act else (232, 246, 248, 235), anchor="lm", stroke_width=max(2, int(3 * k)), stroke_fill=(0, 0, 0, 255))
                x += ww + sp
            y += 72 * k

    def footer(self, d, t):
        W, H, k, a = self.W, self.H, self.k, self.accent
        prog = min(1, t / self.total) if self.total > 0 else 0
        d.rounded_rectangle([90 * k, 1768 * k, 990 * k, 1776 * k], radius=4 * k, fill=(*a, 46))
        d.rounded_rectangle([90 * k, 1768 * k, 90 * k + max(8 * k, 900 * k * prog), 1776 * k], radius=4 * k, fill=(*a, 255))
        d.text((W / 2, 1836 * k), "NOT FINANCIAL ADVICE", font=get_font(22 * k), fill=(142, 163, 173, 230), anchor="mm")

    def frame(self, t):
        img = self.background(t)
        d = ImageDraw.Draw(img, "RGBA")
        if self.style != "footage":
            self.grid(d, t)
        if self.style == "circuit":
            self.circuit(d, t)
        if self.style in ("aurora", "candles"):
            self.particles(d, t)
        if self.style == "candles":
            self.candles(d, t)
        self.header(d, t)
        self.hero(d, t)
        self.captions(d, t)
        self.footer(d, t)
        return img


IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")


def build_background(files, workdir, W, H, fps):
    ff, segs = ffmpeg_path(), []
    for i, p in enumerate(files):
        out = os.path.join(workdir, f"seg{i}.mp4")
        if p.lower().endswith(IMG_EXT):
            vf = (f"scale={int(W * 1.3)}:{int(H * 1.3)}:force_original_aspect_ratio=increase,"
                  f"crop={W}:{H}:x='(iw-{W})*t/4':y='(ih-{H})/2',setsar=1,format=yuv420p")
            cmd = [ff, "-y", "-loglevel", "error", "-loop", "1", "-framerate", str(fps), "-t", "4", "-i", p, "-vf", vf,
                   "-r", str(fps), "-c:v", "libx264", "-preset", "ultrafast", "-an", out]
        else:
            vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={fps},setsar=1,format=yuv420p"
            cmd = [ff, "-y", "-loglevel", "error", "-t", "6", "-i", p, "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-an", out]
        if subprocess.run(cmd, capture_output=True, text=True).returncode == 0 and os.path.exists(out):
            segs.append(out)
    if not segs:
        raise RuntimeError("None of the uploaded files could be read. Use MP4/MOV/WEBM or JPG/PNG.")
    lst = os.path.join(workdir, "list.txt")
    with open(lst, "w") as fh:
        fh.write("".join(f"file '{p}'\n" for p in segs))
    bg = os.path.join(workdir, "bg.mp4")
    r = subprocess.run([ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", bg], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("Could not join clips: " + r.stderr[-300:])
    return bg


def open_bg_reader(bg_path, W, H, fps, total):
    proc = subprocess.Popen([ffmpeg_path(), "-loglevel", "error", "-stream_loop", "-1", "-i", bg_path, "-t", f"{total:.2f}", "-r", str(fps),
                             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size, last = W * H * 3, {"img": None}

    def read():
        buf = proc.stdout.read(size)
        if buf and len(buf) == size:
            last["img"] = Image.frombytes("RGB", (W, H), buf)
        return last["img"]
    return read, proc


def render_master(cues, segments, audio, out, accent, style, W, H, fps, title, channel, progress=None, bg_path=None):
    dur = audio_duration(audio)
    total = dur + .35
    reader, bgproc = (None, None)
    if style == "footage" and bg_path:
        reader, bgproc = open_bg_reader(bg_path, W, H, fps, total + 1)
    scene = Scene(W, H, accent, style, cues, segments, title, channel, total, reader)
    n = int(total * fps)
    log = tempfile.NamedTemporaryFile(delete=False, suffix=".log")
    cmd = [ffmpeg_path(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
           "-i", audio, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart", out]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=log)
    try:
        for i in range(n):
            proc.stdin.write(scene.frame(i / fps).tobytes())
            if progress and i % 6 == 0:
                progress(i / n)
        proc.stdin.close()
        proc.wait()
    except BrokenPipeError:
        pass
    finally:
        if bgproc:
            bgproc.kill()
    log.close()
    if proc.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 1000:
        with open(log.name) as fh:
            raise RuntimeError("ffmpeg failed: " + fh.read()[-600:])
    return dur


def preview_stills(script, accent, style, channel):
    """Three quick stills (hook, middle beat, CTA) using estimated timing. No voice, no FFmpeg."""
    narr = narration_of(script)
    dur = max(8.0, word_count(narr) * .38)
    cues = estimate_cues(narr, dur)
    segs = time_segments(cues, segments_of(script))
    W, H = 360, 640
    sc = Scene(W, H, accent, "circuit" if style == "footage" else style, cues, segs, script["title"], channel, dur)
    return [sc.frame(segs[i]["start"] + .45) for i in (0, 2, 4)]


# ==============================================================================
# ===== UI =====================================================================
# ==============================================================================
import streamlit as st  # noqa: E402

st.set_page_config(page_title="Fintech Pulse Studio", page_icon="⚡", layout="wide")
st.markdown("""
<style>
 .block-container { padding-top: 1.4rem; max-width: 1200px; }
 .brand { color:#00F0FF; font-weight:800; font-size:1.7rem; letter-spacing:.04em; margin:0; text-shadow:0 0 12px rgba(0,240,255,.35); }
 .tag { color:#8EA3AD; font-size:.9rem; margin:0 0 .6rem 0; }
 .stButton>button, .stDownloadButton>button { border-radius:12px; font-weight:700; width:100%; }
 .stButton>button[kind="primary"] { background:#00F0FF; color:#070B10; border:none; height:3rem; }
 .why { color:#8EA3AD; font-size:.9rem; margin:.1rem 0; }
 .lbl { color:#00F0FF; font-size:.72rem; letter-spacing:.08em; margin-top:.7rem; }
</style>""", unsafe_allow_html=True)

st.markdown("<p class='brand'>⚡ FINTECH PULSE</p><p class='tag'>Clip desk for Tier-1 finance shorts · 9:16 · YouTube, Reels, TikTok · free, no API keys</p>", unsafe_allow_html=True)


def secret(name):
    try:
        v = st.secrets.get(name)
    except Exception:
        v = None
    return (str(v) if v else os.environ.get(name, "")).strip() or None


XAI = secret("XAI_API_KEY")
ss = st.session_state
ss.setdefault("topic", TOPICS[0])
ss.setdefault("cut", None)
ss.setdefault("edit_loaded", False)


def set_topic(t):
    ss["topic"] = t


left, mid, right = st.columns([1.05, 0.9, 1.05], gap="large")

# ------------------------------- LEFT: controls ------------------------------
with left:
    st.text_area("Topic", key="topic", height=90, max_chars=240)
    chip_cols = st.columns(2)
    for i, t in enumerate(TOPICS):
        chip_cols[i % 2].button(t if len(t) < 34 else t[:32].rstrip() + "…", key=f"chip{i}", on_click=set_topic, args=(t,))

    pattern = st.radio("Why it should travel", list(PATTERNS), index=1, format_func=lambda k: f"{PATTERNS[k][0]} · {PATTERNS[k][1]}")
    notes = st.text_area("Reference transcript (optional). Keeps the rhythm, writes new words.", height=80, max_chars=1500)
    if notes.strip():
        st.caption(score_transcript(notes))

    sources = (["AI script (Grok key found)"] if XAI else ["AI script (free, best effort)"]) + ["Template + my points", "Paste my own script"]
    source = st.radio("Script source", sources)
    keyword = st.text_input("CTA keyword", "GUIDE")
    points, stat_value, stat_label, pasted = ["", "", ""], "", "", ""
    if source.startswith("Template"):
        points = [st.text_input(f"Point {i + 1} (a fact you have checked)", key=f"pt{i}") for i in range(3)]
        s1, s2 = st.columns(2)
        stat_value = s1.text_input("Big number (optional)", placeholder="4.5%")
        stat_label = s2.text_input("Number label", placeholder="savings rate")
    elif source.startswith("Paste"):
        pasted = st.text_area("Your full script (about 60-110 words)", height=150)

    v1, v2 = st.columns(2)
    voice_label = v1.selectbox("Voice", list(VOICES))
    cta_mode = v2.selectbox("Close", list(CTA_MODES), format_func=lambda k: CTA_MODES[k][0])
    seconds = st.radio("Target length", [30, 38, 45], index=1, horizontal=True, format_func=lambda s: f"{s}s")
    speed = st.slider("Voice speed", -20, 30, 8, format="%d%%")

    style_label = st.selectbox("Background", STYLES)
    theme_name = st.selectbox("Colour theme", list(THEMES))
    quality = st.selectbox("Quality", ["Fast (540x960)", "Standard (720x1280)", "Full HD (1080x1920)"], index=1)
    channel = st.text_input("Channel name (header)", "Fintech Pulse")
    uploads = []
    if style_label.startswith("My own"):
        uploads = st.file_uploader("Upload MP4/MOV/WEBM or JPG/PNG (plays in order, loops)", accept_multiple_files=True,
                                   type=["mp4", "mov", "webm", "jpg", "jpeg", "png", "webp"])
        st.caption("Free commercial-use footage: Pexels, Pixabay, Mixkit, Coverr.")

    generate = st.button("Generate cut", type="primary")
    preview = st.button("Quick preview stills (instant)")
    with st.expander("Test the pipeline (house cut, silent)"):
        house = st.button("Render the house cut")

STYLE_KEY = {STYLES[0]: "circuit", STYLES[1]: "candles", STYLES[2]: "aurora", STYLES[3]: "footage"}
style = STYLE_KEY[style_label]
accent = THEMES[theme_name]
W, H = {"Fast": (540, 960), "Standard": (720, 1280), "Full HD": (1080, 1920)}[quality.split(" (")[0]]
FPS = 24
STEPS = ["Viral read", "Script", "Voice", "Graphics", "Master"]


def build_cut(script, status, bar, silent=False):
    """Voice -> segment timing -> graphics -> master. Returns the cut dict."""
    tmp = tempfile.mkdtemp()
    audio = os.path.join(tmp, "voice.wav" if silent else "voice.mp3")
    video = os.path.join(tmp, "master.mp4")
    narr = narration_of(script)
    status.write("🎙️ Voice")
    if silent:
        cues = estimate_cues(narr, 24)
        silent_audio(audio, cues[-1]["end"])
        info = {"duration": cues[-1]["end"], "cues": cues, "aligned": False, "engine": "silent"}
    else:
        info = synthesize(narr, audio, VOICES[voice_label], speed)
        status.write(f"✅ Voice locked via {info['engine']}" + (" (word-timed)" if info["aligned"] else " (estimated timing)"))
    cues = info["cues"]
    segs = time_segments(cues, segments_of(script))

    bg = None
    if style == "footage":
        if not uploads:
            raise RuntimeError("Upload at least one clip or photo, or pick another background.")
        status.write("🎞️ Preparing your footage")
        paths = []
        for k_, up in enumerate(uploads):
            p = os.path.join(tmp, f"up{k_}_{os.path.basename(up.name)}")
            with open(p, "wb") as fh:
                fh.write(up.getbuffer())
            paths.append(p)
        bg = build_background(paths, tmp, W, H, FPS)
    status.write("🎬 Graphics and master")
    render_master(cues, segs, audio, video, accent, style, W, H, FPS, script["title"], channel, lambda p: bar.progress(min(p, 1.0)), bg)
    bar.progress(1.0)
    with open(video, "rb") as fh:
        vbytes = fh.read()
    with open(audio, "rb") as fh:
        abytes = fh.read()
    return {"script": script, "cues": cues, "video": vbytes, "audio": None if silent else abytes, "aligned": info["aligned"],
            "engine": info["engine"], "words": word_count(narr), "duration": info["duration"]}


def load_editor(sc):
    ss["e_title"], ss["e_hook"] = sc["title"], sc["hook"]
    for i, b in enumerate(sc["beats"]):
        ss[f"e_line{i}"], ss[f"e_scr{i}"] = b["line"], b["onScreen"]
        ss[f"e_sv{i}"] = (b.get("stat") or {}).get("value", "")
        ss[f"e_sl{i}"] = (b.get("stat") or {}).get("label", "")
    ss["e_cta"], ss["e_ctascr"] = sc["cta"]["spoken"], sc["cta"]["onScreen"]
    ss["edit_loaded"] = True


def script_from_editor(base):
    return {**base, "title": ss["e_title"], "hook": ss["e_hook"],
            "beats": [{"line": ss[f"e_line{i}"], "onScreen": ss[f"e_scr{i}"] or ss[f"e_line{i}"][:40],
                       "stat": {"label": ss[f"e_sl{i}"] or "", "value": ss[f"e_sv{i}"]} if ss[f"e_sv{i}"].strip() else None} for i in range(3)],
            "cta": {**base["cta"], "spoken": ss["e_cta"], "onScreen": ss["e_ctascr"]}}


def run_cut(script, silent=False, load=True):
    status = st.status("Building the cut…", expanded=True)
    bar = st.progress(0.0)
    try:
        ss["cut"] = build_cut(script, status, bar, silent)
        if load:
            load_editor(script)
        status.update(label="✅ Cut ready", state="complete")
    except Exception as exc:
        status.update(label="❌ Could not build the cut", state="error")
        st.error(str(exc))


with left:
    if generate:
        topic_val = ss["topic"].strip()
        script, err = None, None
        if len(topic_val) < 8 and not source.startswith("Paste"):
            err = "Give the desk a clearer topic."
        else:
            try:
                with st.spinner("Writing the script…"):
                    if source.startswith("AI"):
                        try:
                            script = ai_script(topic_val, pattern, cta_mode, seconds, notes, XAI)
                        except Exception as exc:
                            st.warning(f"AI script unavailable ({str(exc)[:80]}). Used the template instead; add your own points for better results.")
                            script = draft_from_points(topic_val, ["", "", ""], pattern, cta_mode, keyword)
                    elif source.startswith("Template"):
                        script = draft_from_points(topic_val, points, pattern, cta_mode, keyword, stat_label, stat_value)
                    else:
                        if not pasted.strip():
                            err = "Paste a script first."
                        else:
                            script = parse_pasted(pasted, topic_val or "Pasted script")
            except Exception as exc:
                err = str(exc)
        if err:
            st.error(err)
        elif script:
            run_cut(script)
    if house:
        run_cut(HOUSE_SCRIPT, silent=True)
    if preview:
        sc = script_from_editor(ss["cut"]["script"]) if ss["cut"] else HOUSE_SCRIPT
        ss["stills"] = preview_stills(sc, accent, style, channel)

# ------------------------------- MIDDLE: the reel ----------------------------
cut = ss["cut"]
with mid:
    st.markdown(f"**{'Master' if cut and cut['audio'] else ('House cut' if cut else 'Preview')}**" +
                (f" · {cut['duration']:.0f}s · {cut['words']} words" if cut else ""))
    if cut:
        st.video(cut["video"])
        if cut["audio"] is not None and not cut["aligned"]:
            st.caption("Estimated caption timing (the voice engine returned no word timestamps).")
    elif ss.get("stills"):
        for im in ss["stills"]:
            st.image(im)
    else:
        st.info("Pick a topic and press **Generate cut**. Or try *Quick preview stills* for an instant look.")
    if cut and ss.get("stills"):
        with st.expander("Preview stills"):
            for im in ss["stills"]:
                st.image(im, width=220)

# ------------------------------- RIGHT: script + downloads -------------------
with right:
    steps_done = bool(cut)
    st.markdown("".join(f"<div class='why'>{'●' if steps_done else '○'} <b>{n}</b></div>" for n in STEPS), unsafe_allow_html=True)
    if cut:
        sc = cut["script"]
        st.subheader(sc["title"])
        if sc.get("viralScore"):
            st.caption(f"Model's retention estimate: {sc['viralScore']} (a guess, not a measurement)")
        for r in sc.get("whyItWorks", []):
            st.markdown(f"<div class='why'>{r}</div>", unsafe_allow_html=True)
        st.markdown("<div class='lbl'>HOOK</div>" + sc["hook"], unsafe_allow_html=True)
        for i, b in enumerate(sc["beats"]):
            st.markdown(f"<div class='lbl'>BEAT {i + 1}{' · ' + b['stat']['value'] if b.get('stat') else ''}</div>" + b["line"], unsafe_allow_html=True)
        st.markdown("<div class='lbl'>CTA</div>" + sc["cta"]["spoken"], unsafe_allow_html=True)
        st.caption("Drafts are not financial advice. Check every claim, affiliate disclosure and platform rule before you post.")

        d1, d2 = st.columns(2)
        d1.download_button("Master MP4", cut["video"], "fintech_pulse_short.mp4", "video/mp4")
        if cut["audio"] is not None:
            d2.download_button("Voice MP3", cut["audio"], "voice.mp3", "audio/mpeg")
        d3, d4 = st.columns(2)
        pack = {**sc, "narration": narration_of(sc), "words": cut["words"], "aligned": cut["aligned"], "cues": cut["cues"]}
        d3.download_button("Script JSON", json.dumps(pack, indent=2), "script.json", "application/json")
        d4.download_button("Captions SRT", to_srt(cut["cues"]), "captions.srt", "application/x-subrip")

        with st.expander("Edit script and re-render"):
            st.text_input("Title", key="e_title")
            st.text_input("Hook (spoken + on screen)", key="e_hook")
            for i in range(3):
                st.text_area(f"Beat {i + 1} spoken", key=f"e_line{i}", height=68)
                a1, a2, a3 = st.columns([2, 1, 1])
                a1.text_input("On-screen text", key=f"e_scr{i}")
                a2.text_input("Stat value", key=f"e_sv{i}")
                a3.text_input("Stat label", key=f"e_sl{i}")
            st.text_input("CTA spoken", key="e_cta")
            st.text_input("CTA on screen", key="e_ctascr")
            if st.button("Re-render with edits"):
                run_cut(script_from_editor(cut["script"]), load=False)
                st.rerun()
    else:
        st.caption("Your script, the why-it-works notes and downloads appear here.")
