# media — public videos and images, mapped to place
Write ingest/domains/media.py. YouTube: use yt-dlp search (installed at ~/.local/bin/yt-dlp) with
`ytsearch50:<query>` for a fixed query list (per city: "homeless encampment <city>", "skid row documentary",
"tent city <city>", "homeless sweep <city>") — take title, id, uploader, duration, upload date,
view count; store in `media` (kind='video'). Images: Wikimedia Commons API search per city/topic
(kind='image', license from imageinfo extmetadata). Store every row with place/state and license.
accept: >=500 videos, >=300 images, all with license or documented 'unknown'
