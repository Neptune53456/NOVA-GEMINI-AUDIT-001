from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256

from ..models import utc_now


@dataclass(frozen=True)
class SocialSource:
    source_id: str
    platform: str
    quality: float = .5
    official: bool = False
    active: bool = True


@dataclass(frozen=True)
class SocialAuthor:
    author_id: str
    name: str
    verified: bool = False
    official: bool = False


@dataclass(frozen=True)
class SocialPost:
    post_id: str
    platform: str
    author: SocialAuthor
    published_at: datetime
    text: str
    url: str
    entities: tuple[str, ...] = ()
    instruments: tuple[str, ...] = ()
    engagement: dict[str, int] = field(default_factory=dict)
    source_quality: float = .5
    ingested_at: datetime = field(default_factory=utc_now)

    @staticmethod
    def stable_id(platform: str, ref: str, text: str) -> str:
        return sha256(f"{platform}|{ref or ' '.join(text.casefold().split())}".encode()).hexdigest()


@dataclass(frozen=True)
class SocialSignal:
    signal_id: str
    signal_type: str
    instruments: tuple[str, ...]
    entities: tuple[str, ...]
    detected_at: datetime
    score: float
    source_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    metrics: dict[str, float | int | bool | str] = field(default_factory=dict)
    recommendation: None = None
