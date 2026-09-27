"""Structured public social providers; no HTML scraping."""
from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
from urllib.parse import quote

from ..models import MarketInstrument, as_utc, utc_now
from ..network import SafeHttpClient
from .models import SocialAuthor, SocialPost


def _match(text: str, instruments: list[MarketInstrument]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    folded = text.casefold(); symbols, entities = set(), set()
    for item in instruments:
        if any(alias.casefold() in folded for alias in (item.symbol, item.name, *item.entities) if len(alias) > 1):
            symbols.add(item.symbol); entities.update(item.entities or (item.name,))
    return tuple(sorted(entities)), tuple(sorted(symbols))


class BlueskyProvider:
    name = "bluesky"
    def __init__(self, queries: tuple[str, ...] = ("bitcoin", "markets"), client: SafeHttpClient | None = None) -> None:
        self.queries = queries
        self.client = client or SafeHttpClient({"public.api.bsky.app"})

    def fetch(self, instruments: list[MarketInstrument], limit: int = 50) -> list[SocialPost]:
        posts: dict[str, SocialPost] = {}
        for query in self.queries:
            url = f"https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?q={quote(query)}&limit={min(100, limit)}"
            data, _ = self.client.get_json(url)
            if not isinstance(data, dict) or not isinstance(data.get("posts"), list):
                raise ValueError("malformed_response")
            for raw in data["posts"]:
                record, author = raw.get("record", {}), raw.get("author", {})
                text = " ".join(str(record.get("text", "")).split())[:1000]
                uri = str(raw.get("uri", "")); cid = str(raw.get("cid", ""))
                if not text or not uri: continue
                did = str(author.get("did", "")); handle = str(author.get("handle", did))
                ref = f"https://bsky.app/profile/{handle}/post/{uri.rsplit('/', 1)[-1]}"
                entities, symbols = _match(text, instruments)
                post = SocialPost(SocialPost.stable_id(self.name, cid or uri, text), self.name,
                    SocialAuthor(did, str(author.get("displayName") or handle), bool(author.get("verification")), False),
                    as_utc(record.get("createdAt") or utc_now()), text, ref, entities, symbols,
                    {"likes": int(raw.get("likeCount", 0)), "reposts": int(raw.get("repostCount", 0)),
                     "replies": int(raw.get("replyCount", 0))}, .65)
                posts[post.post_id] = post
        return list(posts.values())[:limit]


class YouTubeFeedProvider:
    name = "youtube"
    def __init__(self, channel_ids: tuple[str, ...], client: SafeHttpClient | None = None) -> None:
        self.channel_ids = channel_ids
        self.client = client or SafeHttpClient({"www.youtube.com"})

    def fetch(self, instruments: list[MarketInstrument], limit: int = 50) -> list[SocialPost]:
        result = []
        for channel in self.channel_ids:
            url = f"https://www.youtube.com/feeds/videos.xml?channel_id={quote(channel)}"
            body, _, _ = self.client.get_bytes(url, accept="application/atom+xml, application/xml")
            root = ET.fromstring(body)
            for entry in root.findall(".//{*}entry"):
                title = (entry.findtext("{*}title") or "").strip(); video = entry.findtext("{*}videoId") or ""
                published = entry.findtext("{*}published") or utc_now().isoformat()
                author = entry.findtext("{*}author/{*}name") or channel
                if not title or not video: continue
                entities, symbols = _match(title, instruments)
                result.append(SocialPost(SocialPost.stable_id(self.name, video, title), self.name,
                    SocialAuthor(channel, author, official=True), as_utc(published), title[:1000],
                    f"https://www.youtube.com/watch?v={video}", entities, symbols, {}, .75))
        return result[:limit]
