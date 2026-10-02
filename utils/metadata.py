"""
metadata.py

Turns messy YouTube titles into (artist, title) pairs and stable canonical keys,
so plays of the same song from different uploads can be grouped together.
"""

import re
import unicodedata

# Uploads from these channels use "Song | Movie | Actors | Composer" style titles.
# Compared after normalize_channel(), so keep entries lowercase without punctuation.
INDIAN_LABEL_CHANNELS = {
    "t series", "tseries", "t series bhakti sagar", "t series apna punjab",
    "sony music india", "sony music south", "zee music company", "zee music classic",
    "saregama music", "saregama", "saregama tamil", "saregama telugu",
    "tips official", "tips music", "tips films", "speed records", "yrf", "yash raj films",
    "venus", "venus movies", "shemaroo", "shemaroo filmi gaane", "eros now music",
    "times music", "aditya music", "lahari music", "think music india",
    "desi music factory", "white hill music", "geet mp3", "sun music",
}

# Noise that never changes which recording it is. (Remix)/(Acoustic)/(Live) are kept on purpose.
_NESTED = r"(?:[^()\[\]]|[\(\[][^()\[\]]*[\)\]])*"   # bracket contents, one level of nesting allowed
_NOISE_PATTERNS = [
    rf"[\(\[]{_NESTED}\bofficial\b{_NESTED}[\)\]]",     # (Official Video), (Official Video (Short Version))
    r"[\(\[][^\)\]]*\bofficial\b[^\)\]]*[\)\]]",       # (Official Video), [Official Audio], ...
    r"[\(\[][^\)\]]*\blyrics?\b[^\)\]]*[\)\]]",         # (Lyric Video), [Lyrics]
    r"[\(\[]\s*audio\s*[\)\]]",
    r"[\(\[]\s*full\s+(?:video|song|audio)[^\)\]]*[\)\]]",
    r"[\(\[][^\)\]]*\bremaster(?:ed)?\b[^\)\]]*[\)\]]",
    r"[\(\[][^\)\]]*\b(?:hd|hq|4k|8k|1080p|720p)\b[^\)\]]*[\)\]]",
    r"\bfull\s+video\s+song\b",
    r"\bfull\s+(?:video|song|audio)\b",
    r"\bofficial\s+(?:music\s+)?video\b",
    r"\blyrical\s+video\b",
    r"\b(?:hd|hq|4k|8k)\b",
    r"[\(\[]\s*[\)\]]",                                 # empty brackets left behind
]
_NOISE_RE = [re.compile(p, re.IGNORECASE) for p in _NOISE_PATTERNS]
_PREFIX_RE = re.compile(r"^\s*(?:lyrical|lyrics|full\s+video|full\s+song|video|audio)\s*:\s*", re.IGNORECASE)
_LABEL_MARKER_RE = re.compile(r"^(.*?)\s+(?:full\s+(?:video\s+)?song|full\s+video|title\s+song)\b", re.IGNORECASE)
_HASHTAG_RE = re.compile(r"(?:\s*#\w+)+\s*$", re.UNICODE)
_FEAT_RE = re.compile(
    r"[\(\[]?\s*\b(?:ft|feat|featuring)\b\.?\s+([^\)\]\|\-]+?)\s*(?:[\)\]]|(?=\s[\-\|])|$)",
    re.IGNORECASE,
)
# Emoji / pictographs / dingbats / variation selectors.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF️‍]+"
)
_DASH_RE = re.compile(r"\s+[–—]\s+")
_TOPIC_RE = re.compile(r"^(.*?)\s*-\s*topic$", re.IGNORECASE)
_VEVO_RE = re.compile(r"^(.*?)\s*vevo$", re.IGNORECASE)
_OFFICIAL_SUFFIX_RE = re.compile(r"\s*\b(?:official|officiel)\s*$", re.IGNORECASE)
# Channel names containing these words are lyric/compilation/fan channels, not the artist.
_NON_ARTIST_CHANNEL_WORDS = {
    "lyrics", "lyric", "fan", "fans", "music", "records", "recordings", "beats", "radio",
    "tv", "channel", "hits", "mix", "mixes", "songs", "vibes", "entertainment", "studio",
    "studios", "media", "network", "nation", "topic", "playlist", "playlists", "sounds",
}


def _artist_from_channel(channel: str) -> str:
    """An official artist channel ("Kendrick Lamar", "Major Lazer Official") -> the artist name."""
    name = _OFFICIAL_SUFFIX_RE.sub("", channel).strip()
    words = set(normalize_channel(name).split())
    if not words or words & _NON_ARTIST_CHANNEL_WORDS:
        return ""
    return name


def _collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" -|–—:").strip()


def clean_title(raw: str) -> str:
    s = _EMOJI_RE.sub("", raw)
    s = _HASHTAG_RE.sub("", s)
    s = _PREFIX_RE.sub("", s)
    for rx in _NOISE_RE:
        s = rx.sub("", s)
    return _collapse(s)


def _extract_featured(s: str) -> tuple[str, list[str]]:
    featured: list[str] = []

    def _grab(m: re.Match) -> str:
        for name in re.split(r",|&|\band\b", m.group(1)):
            name = name.strip()
            if name:
                featured.append(name)
        return " "

    return _collapse(_FEAT_RE.sub(_grab, s)), featured


def normalize_channel(channel: str) -> str:
    return _collapse(re.sub(r"[^a-z0-9 ]+", " ", _strip_accents(channel).lower()))


def parse_artist_title_full(raw_title: str, channel: str = "") -> tuple[str, str, float, list[str]]:
    """Like parse_artist_title, but also returns the featured artists."""
    channel = (channel or "").strip()
    cleaned, featured = _extract_featured(clean_title(raw_title or ""))
    cleaned = _DASH_RE.sub(" - ", cleaned)

    # Indian labels: "Song | Movie | Actors | Composer" - don't guess the artist from actor names.
    if normalize_channel(channel) in INDIAN_LABEL_CHANNELS:
        # "Song Movie Full Video Song | ..." -> the song name is whatever precedes the marker.
        m = _LABEL_MARKER_RE.match(raw_title or "")
        if m and m.group(1).strip():
            cleaned = _extract_featured(clean_title(m.group(1)))[0]
        first = _collapse(re.split(r"\s*[\|｜]\s*", cleaned)[0])
        if " - " in first:  # some label uploads still use "Song - Movie"
            first = _collapse(first.split(" - ")[0])
        return "", first or cleaned, 0.3, featured

    if " - " in cleaned:
        left, right = cleaned.split(" - ", 1)
        left, right = _collapse(left), _collapse(re.split(r"\s*\|\s*| - ", right)[0])
        if left and right:
            return left, right, 0.8, featured

    m = _TOPIC_RE.match(channel)
    if m and m.group(1):
        return m.group(1).strip(), cleaned, 0.9, featured

    m = _VEVO_RE.match(channel)
    if m and m.group(1):
        return m.group(1).strip(), cleaned, 0.8, featured

    # Plain title on what looks like the artist's own channel ("Money Trees" on "Kendrick Lamar").
    artist = _artist_from_channel(channel)
    if artist:
        return artist, cleaned, 0.5, featured

    return "", cleaned, 0.2, featured


def parse_artist_title(raw_title: str, channel: str = "") -> tuple[str, str, float]:
    """Best-effort (artist, title, confidence 0..1) from a YouTube title + channel name."""
    artist, title, conf, _ = parse_artist_title_full(raw_title, channel)
    return artist, title, conf


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _norm_part(s: str) -> str:
    s = _strip_accents(s).lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE).replace("_", " ")
    return re.sub(r"\s+", " ", s).strip()


def normalize_key(artist: str, title: str) -> str:
    """Canonical "artist|title" key: lowercased, accents/punctuation stripped, whitespace collapsed."""
    return f"{_norm_part(artist or '')}|{_norm_part(title or '')}"


def video_alias_key(video_id: str) -> str:
    return f"yt:{video_id}" if video_id else ""
