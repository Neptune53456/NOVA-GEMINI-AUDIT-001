"""Outils web publics en lecture seule avec protections SSRF."""

from html import unescape
from html.parser import HTMLParser
import ipaddress
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from difflib import SequenceMatcher
import re
import socket
import unicodedata
from urllib.parse import quote_plus, urljoin, urlsplit
import xml.etree.ElementTree as ET

import httpx


USER_AGENT = "LocalAssistantWebReader/1.0 (+public read-only web access)"
REQUEST_TIMEOUT = 10.0
MAX_DOWNLOAD_BYTES = 1_000_000
MAX_TEXT_CHARS = 20_000
MAX_REDIRECTS = 5
SEARCH_ENDPOINT = "https://www.bing.com/search?format=rss&q={query}"

FRESHNESS_TERMS = (
    "dernière version",
    "derniere version",
    "version actuelle",
    "dernière release",
    "derniere release",
    "latest",
    "récent",
    "recent",
    "récente",
    "recente",
    "dernières actualités",
    "dernieres actualites",
    "aujourd'hui",
    "aujourdhui",
    "actuellement",
    "nouveauté",
    "nouveaute",
    "mise à jour",
    "mise a jour",
)

OFFICIAL_SOURCE_HINTS = {
    "python": {
        "domain": "python.org",
        "preferred_path": "/downloads",
        "official_url": "https://www.python.org/downloads/",
    },
    "openai": {
        "domain": "openai.com",
        "preferred_path": "/news",
        "official_url": "https://openai.com/news/",
    },
    "microsoft": {
        "domain": "microsoft.com",
        "preferred_path": "",
        "official_url": "https://www.microsoft.com/",
    },
    "nvidia": {
        "domain": "nvidia.com",
        "preferred_path": "",
        "official_url": "https://www.nvidia.com/",
    },
    "github": {
        "domain": "github.com",
        "preferred_path": "",
        "official_url": "https://github.com/",
    },
}

FRENCH_RSS_DATE_PARTS = {
    "lun.": "Mon", "mar.": "Tue", "mer.": "Wed", "jeu.": "Thu",
    "ven.": "Fri", "sam.": "Sat", "dim.": "Sun",
    "janv.": "Jan", "févr.": "Feb", "mars": "Mar", "avr.": "Apr",
    "mai": "May", "juin": "Jun", "juil.": "Jul", "août": "Aug",
    "sept.": "Sep", "oct.": "Oct", "nov.": "Nov", "déc.": "Dec",
}

SEARCH_STOPWORDS = {
    "a", "au", "aux", "de", "des", "du", "en", "et", "est", "la", "le",
    "les", "ma", "mon", "pour", "que", "quel", "quelle", "sur", "un", "une",
    "and", "current", "for", "is", "latest", "of", "on", "the", "what",
    "actuellement", "derniere", "dernieres", "stable", "version",
}

VERSION_INTENT_TERMS = (
    "dernière version", "derniere version", "version actuelle",
    "latest stable version", "current version", "latest release",
    "dernière release", "derniere release",
)

BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata.azure.internal",
}


def _error(message):
    return {"success": False, "error": message}


def _validate_public_url(url):
    if not isinstance(url, str) or not url.strip():
        return False, "URL invalide.", None
    try:
        parsed = urlsplit(url.strip())
    except ValueError as error:
        return False, f"URL invalide : {error}", None

    if parsed.scheme.lower() not in {"http", "https"}:
        return False, "Seules les URL HTTP et HTTPS sont autorisées.", None
    if not parsed.hostname:
        return False, "L'URL ne contient aucun hôte valide.", None
    if parsed.username or parsed.password:
        return False, "Les identifiants intégrés dans une URL sont refusés.", None
    try:
        port = parsed.port
    except ValueError as error:
        return False, f"Port invalide : {error}", None
    if port not in {None, 80, 443}:
        return False, "Seuls les ports HTTP/HTTPS standards sont autorisés.", None

    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname in BLOCKED_HOSTS or hostname.endswith(".localhost"):
        return False, f"Hôte local ou sensible refusé : {hostname}", None

    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                hostname,
                port or (443 if parsed.scheme.lower() == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as error:
        return False, f"Résolution DNS impossible : {error}", None

    if not addresses:
        return False, "La résolution DNS n'a retourné aucune adresse.", None
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False, f"Adresse IP invalide retournée par DNS : {address}", None
        if not ip.is_global:
            return False, f"Adresse réseau privée ou sensible refusée : {ip}", None

    return True, None, parsed.geturl()


def _download_limited(url):
    current_url = url
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html, application/xhtml+xml, text/plain, application/rss+xml",
    }

    with httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=False,
        headers=headers,
    ) as client:
        for redirect_count in range(MAX_REDIRECTS + 1):
            valid, error, normalized_url = _validate_public_url(current_url)
            if not valid:
                raise ValueError(error)

            with client.stream("GET", normalized_url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Redirection sans destination.")
                    if redirect_count >= MAX_REDIRECTS:
                        raise ValueError("Trop de redirections HTTP.")
                    current_url = urljoin(normalized_url, location)
                    continue

                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                allowed_content = (
                    content_type.startswith("text/")
                    or "html" in content_type
                    or "xml" in content_type
                    or "rss" in content_type
                )
                if content_type and not allowed_content:
                    raise ValueError(f"Type de contenu non textuel refusé : {content_type}")

                data = bytearray()
                truncated = False
                for chunk in response.iter_bytes():
                    remaining = MAX_DOWNLOAD_BYTES + 1 - len(data)
                    if remaining <= 0:
                        truncated = True
                        break
                    data.extend(chunk[:remaining])
                    if len(data) > MAX_DOWNLOAD_BYTES:
                        truncated = True
                        del data[MAX_DOWNLOAD_BYTES:]
                        break

                encoding = response.encoding or "utf-8"
                text = bytes(data).decode(encoding, errors="replace")
                return {
                    "url": str(response.url),
                    "content_type": content_type,
                    "content": text,
                    "download_truncated": truncated,
                }

    raise ValueError("La requête web n'a pas abouti.")


class _ReadableHTMLParser(HTMLParser):
    IGNORED_TAGS = {"script", "style", "noscript", "svg", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignored_depth = 0
        self.in_title = False
        self.title_parts = []
        self.text_parts = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.IGNORED_TAGS:
            self.ignored_depth += 1
        elif tag == "title" and self.ignored_depth == 0:
            self.in_title = True

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.IGNORED_TAGS and self.ignored_depth:
            self.ignored_depth -= 1
        elif tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.ignored_depth:
            return
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        if self.in_title:
            self.title_parts.append(cleaned)
        else:
            self.text_parts.append(cleaned)


def _extract_html(html):
    parser = _ReadableHTMLParser()
    parser.feed(html)
    title = " ".join(parser.title_parts).strip()
    text = "\n".join(parser.text_parts).strip()
    return title, text


def _plain_text_from_html(value):
    _, text = _extract_html(unescape(value or ""))
    return " ".join(text.split())


def _is_freshness_sensitive(query):
    lowered = query.casefold()
    return any(term in lowered for term in FRESHNESS_TERMS)


def _official_hint(query):
    lowered = query.casefold()
    for name, hint in OFFICIAL_SOURCE_HINTS.items():
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            return hint
    return None


def _normalized_words(value):
    normalized = unicodedata.normalize("NFKD", value.casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return {
        word for word in re.findall(r"[a-z0-9]+", normalized)
        if len(word) > 1 and word not in SEARCH_STOPWORDS
    }


def _is_version_intent(query):
    lowered = query.casefold()
    return any(term in lowered for term in VERSION_INTENT_TERMS)


def _parse_published(value):
    if not value or not value.strip():
        return None, None
    try:
        normalized = value.strip().casefold()
        for french, english in FRENCH_RSS_DATE_PARTS.items():
            normalized = normalized.replace(french, english)
        parsed = parsedate_to_datetime(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        return parsed.date().isoformat(), parsed
    except (TypeError, ValueError, OverflowError):
        return value.strip(), None


def _is_official_url(url, official_hint):
    if official_hint is None:
        return False
    hostname = (urlsplit(url).hostname or "").casefold()
    domain = official_hint["domain"]
    return hostname == domain or hostname.endswith("." + domain)


def _extract_rss_results(xml_content, official_hint):
    root = ET.fromstring(xml_content)
    results = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        snippet = _plain_text_from_html(item.findtext("description") or "")
        published, published_datetime = _parse_published(
            item.findtext("pubDate") or item.findtext("date") or ""
        )
        valid, _, _ = _validate_public_url(link)
        if title and link and valid:
            results.append({
                "title": title,
                "url": link,
                "snippet": snippet[:500],
                "published": published,
                "official": _is_official_url(link, official_hint),
                "_published_datetime": published_datetime,
            })
    return results


def _rank_results(results, query, freshness_sensitive, official_hint):
    query_words = _normalized_words(query)
    now = datetime.now(timezone.utc)

    def score(result):
        value = 0.0
        title_words = _normalized_words(result["title"])
        snippet_words = _normalized_words(result["snippet"])
        title_matches = len(query_words & title_words)
        snippet_matches = len(query_words & snippet_words)
        coverage = (title_matches + 0.5 * snippet_matches) / max(len(query_words), 1)
        title_similarity = SequenceMatcher(
            None,
            " ".join(sorted(query_words)),
            " ".join(sorted(title_words)),
        ).ratio()
        value += 220.0 * coverage + 80.0 * title_similarity
        if result["official"]:
            value += 500.0 if freshness_sensitive else 250.0
            preferred_path = (official_hint or {}).get("preferred_path")
            if preferred_path and preferred_path in urlsplit(result["url"]).path.casefold():
                value += 150.0
        published = result["_published_datetime"]
        if freshness_sensitive and published is not None:
            age_days = max((now - published).total_seconds() / 86_400, 0)
            value += max(100.0 - age_days / 10.0, -150.0)
        result["_relevance_score"] = round(value, 2)
        result["_lexical_matches"] = title_matches + snippet_matches
        return value

    return sorted(results, key=score, reverse=True)


def search_web(query, max_results=5):
    """Recherche via le flux RSS public de Bing, sans clé API."""
    if not isinstance(query, str) or not query.strip():
        return _error("La requête de recherche est vide.")
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        return _error("max_results doit être un entier entre 1 et 10.")
    max_results = max(1, min(max_results, 10))

    clean_query = query.strip()
    freshness_sensitive = _is_freshness_sensitive(clean_query)
    official_hint = _official_hint(clean_query)
    queries = [clean_query]
    if official_hint is not None:
        official_query = f"{clean_query} site:{official_hint['domain']}"
        if official_hint["preferred_path"]:
            official_query += f" {official_hint['preferred_path'].strip('/')}"
        queries.insert(0, official_query)

    try:
        collected = []
        seen_urls = set()
        query_errors = []
        for search_query in queries[:2]:
            url = SEARCH_ENDPOINT.format(query=quote_plus(search_query))
            try:
                downloaded = _download_limited(url)
            except (httpx.HTTPError, OSError, ValueError) as error:
                query_errors.append(str(error))
                continue
            for result in _extract_rss_results(downloaded["content"], official_hint):
                if result["url"] not in seen_urls:
                    seen_urls.add(result["url"])
                    collected.append(result)

        if official_hint is not None and not any(
            result["official"] for result in collected
        ):
            official_url = official_hint["official_url"]
            valid, _, _ = _validate_public_url(official_url)
            if valid:
                collected.append({
                    "title": f"Source officielle {official_hint['domain']}",
                    "url": official_url,
                    "snippet": (
                        "Page officielle ajoutée pour vérification de "
                        "l'information actuelle."
                    ),
                    "published": None,
                    "official": True,
                    "_published_datetime": None,
                })

        if not collected and query_errors:
            raise ValueError(query_errors[-1])

        raw_count = len(collected)
        ranked = _rank_results(
            collected,
            clean_query,
            freshness_sensitive,
            official_hint,
        )
        if freshness_sensitive:
            relevant = [
                result for result in ranked
                if result["official"] or result["_lexical_matches"] > 0
            ]
            ranked = relevant[:min(max_results, 3)]
        else:
            ranked = ranked[:max_results]

        results = []
        for result in ranked:
            result = dict(result)
            result.pop("_published_datetime", None)
            result.pop("_lexical_matches", None)
            result.pop("_relevance_score", None)
            results.append(result)

        if freshness_sensitive:
            official_count = sum(result["official"] for result in results)
            print("[Web] Recherche sensible à la fraîcheur")
            print(f"[Web] Résultats bruts : {raw_count}")
            print(f"[Web] Résultats retenus : {len(results)}")
            if results:
                best = results[0]
                domain = urlsplit(best["url"]).hostname or "inconnu"
                official = next((item for item in results if item["official"]), None)
                official_domain = (
                    urlsplit(official["url"]).hostname if official else "aucune"
                )
                print(f"[Web] Source officielle : {official_domain}")
        return {
            "success": True,
            "results": results,
            "query": clean_query,
            "freshness_sensitive": freshness_sensitive,
            "official_domain": (
                official_hint["domain"] if official_hint is not None else None
            ),
            "version_intent": _is_version_intent(clean_query),
        }
    except (httpx.HTTPError, ET.ParseError, OSError, ValueError) as error:
        return _error(f"Recherche web impossible : {error}")


def read_web_page(url):
    """Télécharge une page publique et retourne son texte visible tronqué."""
    valid, error, normalized_url = _validate_public_url(url)
    if not valid:
        return _error(error)
    try:
        downloaded = _download_limited(normalized_url)
        title, text = _extract_html(downloaded["content"])
        text_truncated = len(text) > MAX_TEXT_CHARS
        if text_truncated:
            text = text[:MAX_TEXT_CHARS].rstrip() + "\n[Texte tronqué]"
        return {
            "success": True,
            "url": downloaded["url"],
            "title": title,
            "text": text,
            "truncated": downloaded["download_truncated"] or text_truncated,
        }
    except (httpx.HTTPError, OSError, ValueError) as error:
        return _error(f"Lecture web impossible : {error}")


def extract_version_candidates(items):
    """Extrait des versions et leur contexte sans décider laquelle est correcte."""
    candidates = []
    by_key = {}
    version_pattern = re.compile(r"(?<!\d)(\d+\.\d+(?:\.\d+)?)(?!\d)")
    prerelease_pattern = re.compile(
        r"\b(?:alpha|beta|rc\d*|release candidate|pre-release|prerelease|preview|planned)\b",
        re.IGNORECASE,
    )
    date_pattern = re.compile(
        r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}\s+"
        r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+\d{4})\b",
        re.IGNORECASE,
    )
    for item in items:
        text = item.get("text") or item.get("snippet") or ""
        source = item.get("url") or item.get("source")
        official = bool(item.get("official"))
        for match in version_pattern.finditer(text):
            start = max(0, match.start() - 140)
            end = min(len(text), match.end() + 140)
            context = " ".join(text[start:end].split())
            line_start = text.rfind("\n", 0, match.start())
            line_end = text.find("\n", match.end())
            if line_start >= 0 or line_end >= 0:
                status_context = text[
                    line_start + 1 if line_start >= 0 else 0:
                    line_end if line_end >= 0 else len(text)
                ]
            else:
                status_context = text[
                    max(0, match.start() - 45):min(len(text), match.end() + 60)
                ]
            before = text[max(0, match.start() - 3000):match.start()].casefold()
            stable_section = before.rfind("stable releases") > before.rfind("pre-releases")
            escaped_version = re.escape(match.group(1))
            linked_prerelease = re.search(
                rf"(?:development\s+versions?\s+of\s+python\s+{escaped_version}"
                rf"|python\s+{escaped_version}.{{0,80}}(?:pre-releases?|alpha|beta|rc\d*|preview))",
                context,
                re.IGNORECASE,
            )
            is_prerelease = bool(
                prerelease_pattern.search(status_context) or linked_prerelease
            )
            explicit_latest = bool(re.search(
                rf"latest.{{0,50}}release.{{0,50}}(?:download\s+)?python\s+{escaped_version}\b",
                context,
                re.IGNORECASE,
            ))
            if is_prerelease:
                status = "pre-release"
            elif explicit_latest or "stable" in status_context.casefold() or stable_section:
                status = "stable"
            else:
                status = "unknown"
            date_match = date_pattern.search(context)
            candidate = {
                "version": match.group(1),
                "context": context,
                "source": source,
                "official": official,
                "date": date_match.group(0) if date_match else item.get("published"),
                "status": status,
                "evidence_type": (
                    "explicit_latest_release" if explicit_latest
                    else "stable_releases_section" if stable_section
                    else "version_mention"
                ),
            }
            key = (candidate["version"], source)
            previous = by_key.get(key)
            evidence_rank = {
                "version_mention": 0,
                "stable_releases_section": 1,
                "explicit_latest_release": 2,
            }
            if previous is None or evidence_rank[candidate["evidence_type"]] > evidence_rank[previous["evidence_type"]]:
                by_key[key] = candidate
    candidates.extend(by_key.values())
    return candidates


def rank_version_candidates(candidates, stable_only=True, limit=5):
    """Classe numériquement les versions et limite les faits transmis au modèle."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        limit = 5
    limit = max(1, min(limit, 5))

    def version_tuple(value):
        return tuple(int(part) for part in value.split("."))

    filtered = [
        candidate for candidate in candidates
        if not stable_only or candidate.get("status") != "pre-release"
    ]
    evidence_rank = {
        "explicit_latest_release": 2,
        "stable_releases_section": 1,
        "version_mention": 0,
    }
    return sorted(
        filtered,
        key=lambda candidate: (
            bool(candidate.get("official")),
            evidence_rank.get(candidate.get("evidence_type"), 0),
            candidate.get("status") == "stable",
            version_tuple(candidate["version"]),
            candidate.get("date") or "",
        ),
        reverse=True,
    )[:limit]


def build_high_confidence_version_fact(candidates, subject=None):
    """Construit un fait direct seulement si les preuves HIGH ne se contredisent pas."""
    high_confidence = [
        candidate for candidate in candidates
        if candidate.get("official") is True
        and candidate.get("status") == "stable"
        and candidate.get("evidence_type") == "explicit_latest_release"
    ]
    versions = {candidate["version"] for candidate in high_confidence}
    if len(versions) != 1:
        return None

    candidate = high_confidence[0]
    domain = (urlsplit(candidate.get("source") or "").hostname or "").casefold()
    if domain.startswith("www."):
        domain = domain[4:]
    return {
        "value": candidate["version"],
        "subject": subject or (domain.split(".")[0].title() if domain else "Logiciel"),
        "status": "stable",
        "official": True,
        "source_domain": domain,
        "source_url": candidate.get("source"),
        "date": candidate.get("date"),
        "confidence": "HIGH",
    }
