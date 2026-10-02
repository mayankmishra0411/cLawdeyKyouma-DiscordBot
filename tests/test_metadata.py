import pytest

from utils.metadata import (
    clean_title, normalize_key, parse_artist_title, parse_artist_title_full,
)
from utils.track_resolver import Track

# (raw title, channel, expected artist, expected title, expected confidence)
CASES = [
    # Western "Artist - Title" with noise
    ("Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)", "Rick Astley",
     "Rick Astley", "Never Gonna Give You Up", 0.8),
    ("Queen – Bohemian Rhapsody (Official Video Remastered)", "Queen Official",
     "Queen", "Bohemian Rhapsody", 0.8),  # en dash treated like " - "
    ("Coldplay - Viva La Vida (Official Video)", "Coldplay", "Coldplay", "Viva La Vida", 0.8),
    ("The Weeknd - Blinding Lights (Official Audio)", "TheWeekndVEVO",
     "The Weeknd", "Blinding Lights", 0.8),
    ("Imagine Dragons - Believer (Lyrics)", "7clouds", "Imagine Dragons", "Believer", 0.8),
    ("Eminem - Lose Yourself [HD]", "msvogue23", "Eminem", "Lose Yourself", 0.8),
    ("Billie Eilish - bad guy (Audio)", "Billie Eilish", "Billie Eilish", "bad guy", 0.8),
    ("Adele - Hello (Lyric Video)", "Adele", "Adele", "Hello", 0.8),
    ("Linkin Park - Numb [Official Music Video] [4K UPGRADE] – Linkin Park", "Linkin Park",
     "Linkin Park", "Numb", 0.8),
    # Keep (Remix) / (Live) / (Acoustic)
    ("Daft Punk - One More Time (Live)", "Daft Punk", "Daft Punk", "One More Time (Live)", 0.8),
    ("Dua Lipa - Levitating (Remix) (Official Video)", "Dua Lipa",
     "Dua Lipa", "Levitating (Remix)", 0.8),
    ("Ed Sheeran - Perfect (Acoustic)", "Ed Sheeran", "Ed Sheeran", "Perfect (Acoustic)", 0.8),
    # feat. handling
    ("Calvin Harris - This Is What You Came For (Official Video) ft. Rihanna", "CalvinHarrisVEVO",
     "Calvin Harris", "This Is What You Came For", 0.8),
    ("Post Malone - Sunflower (feat. Swae Lee) [Official Audio]", "Post Malone",
     "Post Malone", "Sunflower", 0.8),
    ("DJ Snake ft. Justin Bieber - Let Me Love You", "DJSnakeVEVO",
     "DJ Snake", "Let Me Love You", 0.8),
    ("Mark Ronson - Uptown Funk (Official Video) featuring Bruno Mars", "Mark Ronson",
     "Mark Ronson", "Uptown Funk", 0.8),
    # Nested brackets inside a noise group
    ("Justin Bieber - Intentions (Official Video (Short Version)) ft. Quavo", "Justin Bieber",
     "Justin Bieber", "Intentions", 0.8),
    ("Popular (From The Idol Vol. 1 (Music from the HBO Original Series))", "The Weeknd - Topic",
     "The Weeknd", "Popular (From The Idol Vol. 1 (Music from the HBO Original Series))", 0.9),
    # "- Topic" channels
    ("Blinding Lights", "The Weeknd - Topic", "The Weeknd", "Blinding Lights", 0.9),
    ("Tum Hi Ho", "Arijit Singh - Topic", "Arijit Singh", "Tum Hi Ho", 0.9),
    ("Kesariya (From \"Brahmastra\")", "Pritam - Topic", "Pritam", "Kesariya (From \"Brahmastra\")", 0.9),
    # VEVO channel, no dash in title
    ("Shape of You", "EdSheeranVEVO", "EdSheeran", "Shape of You", 0.8),
    ("Hello", "AdeleVEVO", "Adele", "Hello", 0.8),
    # Indian label pipe format
    ("Kesariya - Brahmastra | Ranbir Kapoor | Alia Bhatt | Pritam | Arijit Singh | Amitabh",
     "Sony Music India", "", "Kesariya", 0.3),
    ("Full Video: Tum Hi Ho | Aashiqui 2 | Aditya Roy Kapur, Shraddha Kapoor | Mithoon",
     "T-Series", "", "Tum Hi Ho", 0.3),
    ("Tum Hi Ho Full Video Song | Aashiqui 2 | Aditya Roy Kapur, Shraddha Kapoor", "T-Series",
     "", "Tum Hi Ho", 0.3),
    ("Apna Bana Le - Bhediya | Varun Dhawan, Kriti Sanon | Sachin-Jigar | Arijit Singh",
     "Zee Music Company", "", "Apna Bana Le", 0.3),
    ("Raataan Lambiyan – Official Video | Shershaah | Sidharth – Kiara | Tanishk B| Jubin Nautiyal",
     "Sony Music India", "", "Raataan Lambiyan", 0.3),
    ("Chaiyya Chaiyya | Dil Se | Shah Rukh Khan | Malaika Arora | A.R. Rahman", "Saregama Music",
     "", "Chaiyya Chaiyya", 0.3),
    ("Tip Tip Barsa Paani | Mohra | Akshay Kumar | Raveena Tandon | Alka Yagnik", "Tips Official",
     "", "Tip Tip Barsa Paani", 0.3),
    ("Lehanga : Jass Manak (Official Video) Satti Dhillon | Punjabi Songs 2019", "Geet MP3",
     "", "Lehanga : Jass Manak Satti Dhillon", 0.3),
    ("Tujhe Dekha To | DDLJ | Shah Rukh Khan | Kajol", "YRF", "", "Tujhe Dekha To", 0.3),
    # Real YouTube Mix entries from a T-Series seed
    ("Chahun Main Ya Naa Full Video Song Aashiqui 2 | Aditya Roy Kapur, Shraddha Kapoor", "T-Series",
     "", "Chahun Main Ya Naa", 0.3),
    ("SANAM RE Title  Song FULL VIDEO | Pulkit Samrat, Yami Gautam, Urvashi Rautela", "T-Series",
     "", "SANAM RE", 0.3),
    ("Lyrical: Tum Hi Aana | Marjaavaan | Riteish D, Sidharth M, Tara S |Jubin Nautiyal", "T-Series",
     "", "Tum Hi Aana", 0.3),
    # Plain title on the artist's own channel
    ("Money Trees", "Kendrick Lamar", "Kendrick Lamar", "Money Trees", 0.5),
    ("m.A.A.d city", "Kendrick Lamar", "Kendrick Lamar", "m.A.A.d city", 0.5),
    ("Cold Water (feat. Justin Bieber & MØ)", "Major Lazer Official", "Major Lazer", "Cold Water", 0.5),
    ("Believer", "7clouds Lyrics", "", "Believer", 0.2),
    ("Believer", "Best Music Mix", "", "Believer", 0.2),
    # Fallback: no dash, unknown channel
    ("Lofi hip hop radio 📚 beats to relax/study to", "Lofi Records", "",
     "Lofi hip hop radio beats to relax/study to", 0.2),
    ("tum hi ho arijit singh #shorts #bollywood", "random fan", "", "tum hi ho arijit singh", 0.2),
]


@pytest.mark.parametrize("raw,channel,artist,title,conf", CASES)
def test_parse_artist_title(raw, channel, artist, title, conf):
    assert parse_artist_title(raw, channel) == (artist, title, conf)


def test_case_count():
    assert len(CASES) >= 30


def test_featured_artists_extracted():
    _, _, _, feat = parse_artist_title_full("Post Malone - Sunflower (feat. Swae Lee & Nicki Minaj)", "")
    assert feat == ["Swae Lee", "Nicki Minaj"]


def test_clean_title_strips_emoji_and_hashtags():
    assert clean_title("Song Name 🔥🔥 #music #viral") == "Song Name"


def test_normalize_key():
    assert normalize_key("Beyoncé", "Halo!!  (Live)") == "beyonce|halo live"
    assert normalize_key("", "Tum Hi Ho") == "|tum hi ho"
    # Two uploads of the same song collapse to the same key
    a = parse_artist_title("Coldplay - Viva La Vida (Official Video)", "Coldplay")
    b = parse_artist_title("Viva La Vida", "Coldplay - Topic")
    assert normalize_key(*a[:2]) == normalize_key(*b[:2])


def test_track_apply_metadata_youtube():
    t = Track(title="Adele - Hello (Official Music Video)", channel="AdeleVEVO", video_id="YQHsXMglC9A")
    t.apply_metadata()
    assert (t.canonical_artist, t.canonical_title, t.meta_confidence) == ("Adele", "Hello", 0.8)
    assert t.canonical_key == "adele|hello"


def test_track_apply_metadata_spotify_is_trusted():
    t = Track(title="Tum Hi Ho", artist="Arijit Singh", source="Spotify -> YouTube").apply_metadata()
    assert t.meta_confidence == 1.0
    assert t.canonical_key == "arijit singh|tum hi ho"
