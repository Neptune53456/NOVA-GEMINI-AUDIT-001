from __future__ import annotations

from collections import Counter
from datetime import timedelta
from hashlib import sha256
import re

from ..models import utc_now
from .models import SocialPost, SocialSignal


def _fingerprint(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.casefold())
    return " ".join(words[:30])


class SocialSignalDetector:
    def detect(self, posts: list[SocialPost], history: list[SocialPost] | None = None) -> list[SocialSignal]:
        history = history or []
        unique: dict[str, SocialPost] = {}
        duplicates = Counter()
        for post in sorted(posts, key=lambda p: p.published_at):
            fp = _fingerprint(post.text)
            if fp in unique: duplicates[unique[fp].post_id] += 1
            else: unique[fp] = post
        result = []
        for post in unique.values():
            recent = [p for p in [*history, *posts] if p.post_id != post.post_id
                      and post.published_at - timedelta(hours=1) <= p.published_at <= post.published_at
                      and set(p.instruments) & set(post.instruments)]
            prior = [p for p in history if p.published_at < post.published_at and set(p.instruments) & set(post.instruments)]
            platforms = {post.platform, *(p.platform for p in recent)}
            engagement = sum(max(0, int(v)) for v in post.engagement.values())
            novelty = 1.0 if not any(_fingerprint(p.text) == _fingerprint(post.text) for p in prior) else 0.0
            score = min(1.0, .2 + .25 * bool(post.author.official or post.author.verified) +
                        .1 * min(3, len(recent)) + .1 * min(2, len(platforms) - 1) +
                        .1 * min(2, engagement / 100) + .15 * novelty)
            kind = "burst" if len(recent) >= 2 else ("engagement_anomaly" if engagement >= 100 else "mention")
            sid = sha256(f"{kind}|{post.post_id}".encode()).hexdigest()
            result.append(SocialSignal(sid, kind, post.instruments, post.entities, utc_now(), round(score, 3),
                tuple(sorted(platforms)), (post.post_id,), {"velocity_1h": len(recent) + 1,
                "engagement": engagement, "official_author": post.author.official,
                "duplicate_count": duplicates[post.post_id], "novelty": novelty,
                "cross_source_count": len(platforms)}))
        return result
