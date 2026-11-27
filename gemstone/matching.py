"""Loose names for the assistant's Gemstone tools: zones (controllers), patterns, colours.

Requests come from speech, so names are matched by their words (lower case,
singular, no fillers), with near misses ("hot tube") swapped for the nearest
known word. Modelled on device_control's and speakers' matching.
"""
import difflib
import math
import re

FUZZY_CUTOFF = 0.75
_SPLIT = re.compile(r",|;|&|\+|/|\band\b|\bplus\b|\bwith\b", re.I)


class NoMatch(LookupError):
    """Nothing matches; the message says what there is."""


class Ambiguous(LookupError):
    """The request could mean more than one zone; ask which."""


def join(parts):
    """"a", "a and b", "a, b and c"."""
    parts = list(parts)
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _stem(word):
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _raw(text):
    text = str(text or "").casefold().replace("'", "").replace("\u2019", "").replace("&", " and ")
    return [w for w in re.split(r"[^0-9a-z]+", text) if w]


def words(text, filler=frozenset()):
    return tuple(w for w in (_stem(w) for w in _raw(text)) if w not in filler)


def _fix(found, vocab):
    """``found`` with each unknown word swapped for the nearest word in ``vocab``; None if one has none."""
    out, changed = [], False
    for w in found:
        if w in vocab:
            out.append(w)
            continue
        close = difflib.get_close_matches(w, sorted(vocab), n=1, cutoff=FUZZY_CUTOFF)
        if not close:
            return None
        out.append(close[0])
        changed = True
    return out if changed else None


# ---------------------------------------------------------------- zones

ZONE_FILLER = frozenset({
    "the", "a", "an", "and", "in", "on", "of", "at", "to", "for", "my", "our", "this", "that", "these",
    "those", "please", "light", "lighting", "led", "string", "strip", "permanent", "zone", "controller",
    "around", "up", "it", "them", "here", "there",
})
#: Words that mean every zone: they are all outside, and "gemstone" is the product.
ALL_WORDS = frozenset(words(
    "all every everything both each whole entire everywhere outside outdoor outdoors exterior external "
    "gemstone gemstones christmas holiday"))
#: Words that mean the main (largest) controller, the one on the building itself.
HOUSE_WORDS = frozenset(words("house lodge home roof roofline eave eaves main building cabin chalet"))


class Zones:
    """The controllers: dicts with id, name and label. ``primary`` (an id) also answers to "house"."""

    def __init__(self, zones, primary=None):
        self.zones = list(zones)
        for z in self.zones:
            z["_words"] = set(words(z["name"], ZONE_FILLER)) | set(words(z["label"], ZONE_FILLER))
            z["_words"] -= ALL_WORDS
            if z["id"] == primary:
                z["_words"] |= HOUSE_WORDS

    def labels(self):
        return [z["label"] for z in self.zones]

    def _vocab(self):
        vocab = set(ALL_WORDS)
        for z in self.zones:
            vocab |= z["_words"]
        return vocab

    def _one(self, found, plural):
        k = set(found) - ALL_WORDS
        if not k:
            return list(self.zones)
        matches = [z for z in self.zones if k <= z["_words"]]
        if len(matches) > 1:
            exact = [z for z in matches if k == set(words(z["name"], ZONE_FILLER)) - ALL_WORDS]
            if len(exact) == 1:
                return exact
            if not plural:
                raise Ambiguous(f"That could be the {join([z['label'] for z in matches])} lights. "
                                "Ask which, or say all of them.")
        return matches or None

    def _resolve(self, text):
        found = words(text, ZONE_FILLER)
        plural = any(_stem(w) != w for w in _raw(text) if _stem(w) not in ZONE_FILLER)
        got = self._one(found, plural)
        if got is None:
            fixed = _fix(found, self._vocab())
            if fixed:
                got = self._one(fixed, plural)
        return got

    def resolve(self, text):
        """The zones ``text`` names (all of them for none, "outside" or "all"), in order."""
        if not self.zones:
            raise NoMatch("There are no Gemstone lights set up.")
        if not str(text or "").strip():
            return list(self.zones)
        got = self._resolve(text)
        if got is None:
            parts = [p for p in _SPLIT.split(str(text)) if p.strip()]
            if len(parts) < 2:
                raise NoMatch(self.no_match(text))
            got = []
            for part in parts:
                one = self._resolve(part)
                if one is None:
                    raise NoMatch(self.no_match(part))
                got += one
        ids = {z["id"] for z in got}
        return [z for z in self.zones if z["id"] in ids]

    def no_match(self, text):
        return (f"There are no Gemstone lights called {str(text).strip()!r}. "
                f"The lights are: {join(self.labels())}.")


# ---------------------------------------------------------------- patterns

PATTERN_FILLER = frozenset({
    "the", "a", "an", "to", "on", "in", "of", "for", "my", "our", "some", "please", "pattern", "design",
    "theme", "scene", "show", "mode", "light", "lighting", "one", "c", "it", "them",
})


def _norm(name):
    return " ".join(_raw(re.sub(r"^\s*\(c\)\s*", "", str(name or ""), flags=re.I)))


def _tier(q, p):
    """How well query words ``q`` name pattern ``p``: (tier, coverage) or None."""
    name_w = p["_words"]
    if not name_w and not p["_folder"]:
        return None
    if q == name_w:
        return 4, 1.0
    if q <= name_w:
        return 3, len(q) / len(name_w)
    if q <= name_w | p["_folder"]:
        return 2, len(q & name_w) / max(1, len(name_w))
    return None


def _prepare(patterns):
    out = []
    for p in patterns:
        if not p.get("name"):
            continue
        p = dict(p)
        p["_words"] = set(words(_norm(p["name"]), PATTERN_FILLER))
        p["_folder"] = set(words(p.get("folder_name") or "", PATTERN_FILLER))
        out.append(p)
    return out


def _bonus(p, plays):
    n = int((plays.get(p.get("id")) or {}).get("n", 0) or 0)
    return (1 if p.get("fav") else 0, min(n, 50), -len(p["name"]))


def find_pattern(query, patterns, plays=None):
    """The best pattern for ``query`` and up to three runners-up (distinct names).

    ``patterns`` are dicts with id and name (and optionally fav and
    folder_name); ``plays`` maps ids to ``{"n": count}``. Exact names win,
    then names holding every word asked for (favourites, then the most played,
    then the shortest), then patterns whose name and folder hold them, then the
    same with near-miss words corrected. Returns None when nothing matches.
    """
    plays = plays or {}
    pats = _prepare(patterns)
    q = set(words(_norm(query), PATTERN_FILLER))
    if not q:
        return None
    vocab = set()
    for p in pats:
        vocab |= p["_words"] | p["_folder"]
    for attempt in (q, _fix(sorted(q), vocab)):
        if not attempt:
            continue
        attempt = set(attempt)
        scored = []
        for p in pats:
            t = _tier(attempt, p)
            if t:
                scored.append(((t[0], round(t[1], 2)) + _bonus(p, plays), p))
        if not scored:
            continue
        scored.sort(key=lambda s: s[0], reverse=True)
        best = scored[0][1]
        top = scored[0][0][0]
        others, seen = [], {best["name"].casefold()}
        for score, p in scored[1:]:
            if score[0] < top - 1 or p["name"].casefold() in seen:
                continue
            seen.add(p["name"].casefold())
            others.append(p)
            if len(others) == 3:
                break
        return best, others
    return None


def suggest(query, patterns, n=3, plays=None):
    """Up to ``n`` pattern names that look like ``query`` (for "there's no pattern called …")."""
    plays = plays or {}
    by_norm = {}
    for p in patterns:
        if p.get("name"):
            by_norm.setdefault(_norm(p["name"]), p["name"])
    target = _norm(query)
    close = difflib.get_close_matches(target, list(by_norm), n=n, cutoff=0.5)
    if len(close) < n:
        q = set(words(target, PATTERN_FILLER))
        for key in by_norm:
            if key in close:
                continue
            kw = set(words(key, PATTERN_FILLER))
            if any(difflib.SequenceMatcher(None, a, b).ratio() >= 0.8 for a in q for b in kw):
                close.append(key)
                if len(close) == n:
                    break
    return [by_norm[k] for k in close[:n]]


def popular(patterns, plays=None, n=3):
    """Favourites and the most played names, for when a request matches nothing at all."""
    plays = plays or {}

    def rank(p):
        return (1 if p.get("fav") else 0, int((plays.get(p.get("id")) or {}).get("n", 0) or 0))
    out = []
    for p in sorted((p for p in patterns if p.get("name") and not p.get("guest")), key=rank, reverse=True):
        if p["name"] not in out:
            out.append(p["name"])
        if len(out) == n:
            break
    return out


# ---------------------------------------------------------------- colours

#: The Tonight page's solid colour swatches (same hex, so the swatch shows as playing).
SOLIDS = (("Warm white", "#fff1e0"), ("White", "#ffffff"), ("Red", "#e3262b"), ("Orange", "#ff7a1a"),
          ("Gold", "#ffb800"), ("Green", "#1fa64a"), ("Teal", "#14b8a6"), ("Blue", "#2f6bff"),
          ("Purple", "#8b3dff"), ("Pink", "#ff4fa3"))
EXTRA = (("Yellow", "#ffd400"), ("Aqua", "#00d5ff"), ("Magenta", "#ff1fd2"))
COLOR_ALIASES = {
    "warm": "Warm white", "soft white": "Warm white", "warm white": "Warm white",
    "cool white": "White", "bright white": "White", "daylight": "White",
    "amber": "Gold", "turquoise": "Teal", "cyan": "Aqua", "light blue": "Aqua", "sky blue": "Aqua",
    "violet": "Purple", "lavender": "Purple", "rose": "Pink", "hot pink": "Pink", "fuchsia": "Magenta",
    "crimson": "Red", "scarlet": "Red", "navy": "Blue", "royal blue": "Blue", "emerald": "Green",
    "lime": "Green",
}
_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")
_COLOR_FILLER = frozenset({"the", "a", "colour", "color", "solid", "all", "just", "plain", "please", "light"})


def colours():
    return dict(SOLIDS + EXTRA)


def find_colour(text):
    """``(name, "#rrggbb")`` for a colour name or hex, or None."""
    text = str(text or "").strip()
    m = _HEX.match(text)
    if m:
        return text if text.startswith("#") else "#" + m[1], "#" + m[1].lower()
    key = " ".join(w for w in _raw(text) if w not in _COLOR_FILLER)
    table = colours()
    by_key = {name.casefold(): name for name in table}
    name = by_key.get(key) or COLOR_ALIASES.get(key)
    if name is None:
        close = difflib.get_close_matches(key, list(by_key) + list(COLOR_ALIASES), n=1, cutoff=0.8)
        if close:
            name = by_key.get(close[0]) or COLOR_ALIASES.get(close[0])
    if name is None:
        return None
    return name, table[name]


def colour_names():
    return [name.lower() for name, _ in SOLIDS + EXTRA]


def pct(level):
    """0-255 device brightness as a whole percent."""
    return max(0, min(100, int(math.floor(int(level) * 100 / 255 + 0.5))))
