import re
from abc import ABC, abstractmethod
from typing import Optional
import httpx

class ImageProvider(ABC):
    """Abstract interface for pluggable image discovery services."""
    name: str = "base_image_provider"

    @abstractmethod
    async def search_images(self, query: str, limit: int = 6) -> list[dict]:
        pass


class WikimediaCommonsProvider(ImageProvider):
    name: str = "wikimedia_commons"
    API_URL = "https://commons.wikimedia.org/w/api.php"
    USER_AGENT = "ForgeX/1.0 (https://ForgeX.local; contact@ForgeX.local)"
    ALLOWED_MIMES = {"image/jpeg", "image/png", "image/webp"}
    MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024  # 10 MB limit per image

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self.client = client

    async def search_images(self, query: str, limit: int = 6) -> list[dict]:
        """
        Searches Wikimedia Commons for photos of subject, extracting resolution,
        license, artist, and global wiki usage (popularity).
        """
        params = {
            "action": "query",
            "generator": "search",
            "gsrsearch": f"{query} filetype:bitmap",
            "gsrnamespace": "6",  # File namespace
            "gsrlimit": min(limit * 3, 20),
            "prop": "imageinfo|globalusage",
            "iiprop": "url|size|mime|extmetadata",
            "gulimit": "50",
            "format": "json"
        }
        headers = {"User-Agent": self.USER_AGENT}

        try:
            if self.client:
                resp = await self.client.get(self.API_URL, params=params, headers=headers, timeout=15.0)
                resp.raise_for_status()
                data = resp.json()
            else:
                async with httpx.AsyncClient() as c:
                    resp = await c.get(self.API_URL, params=params, headers=headers, timeout=15.0)
                    resp.raise_for_status()
                    data = resp.json()
        except Exception:
            return []

        pages = data.get("query", {}).get("pages", {})
        candidates = []

        for _, page in pages.items():
            image_infos = page.get("imageinfo", [])
            if not image_infos:
                continue

            info = image_infos[0]
            mime = info.get("mime", "").lower()
            if mime not in self.ALLOWED_MIMES:
                continue

            width = info.get("width", 0)
            height = info.get("height", 0)
            size_bytes = info.get("size", 0)

            # Skip tiny icons or massive oversized files (> 10MB)
            if width < 300 or height < 300 or size_bytes > self.MAX_DOWNLOAD_BYTES:
                continue

            ext_meta = info.get("extmetadata", {})
            license_short = ext_meta.get("LicenseShortName", {}).get("value", "Public Domain/Unknown")
            artist_raw = ext_meta.get("Artist", {}).get("value", "Unknown Author")
            artist_clean = re.sub(r"<[^>]+>", "", artist_raw).strip() or "Unknown Author"
            credit_raw = ext_meta.get("Credit", {}).get("value", "")
            credit_clean = re.sub(r"<[^>]+>", "", credit_raw).strip()

            # Global usage count across all Wikimedia wikis as popularity signal
            usage_count = len(page.get("globalusage", []))

            # Quality heuristic score: popularity (20pts each) + good resolution bonus
            res_bonus = min(width, 1600) / 100.0
            popularity_score = (usage_count * 25.0) + res_bonus

            # Flag non-free / uncertain licenses
            is_open_license = any(tag in license_short.lower() for tag in ["cc-by", "cc by", "public domain", "cc0", "pd"])

            candidates.append({
                "provider": self.name,
                "title": page.get("title", ""),
                "url": info.get("url", ""),
                "thumb_url": info.get("thumburl", info.get("url", "")),
                "description_url": info.get("descriptionurl", ""),
                "width": width,
                "height": height,
                "size_bytes": size_bytes,
                "mime": mime,
                "license": license_short,
                "is_open_license": is_open_license,
                "author": artist_clean,
                "credit": credit_clean,
                "popularity_score": popularity_score,
                "usage_count": usage_count,
                "attribution": f"{artist_clean} ({license_short})"
            })

        # Rank by popularity and quality
        candidates.sort(key=lambda x: x["popularity_score"], reverse=True)
        return candidates[:limit]

    async def download_image_bytes(self, url: str) -> bytes:
        """
        Safely streams and downloads image up to 10 MB cap.
        Aborts immediately if Content-Length or total downloaded bytes exceed 10 MB.
        """
        headers = {"User-Agent": self.USER_AGENT}
        async with httpx.AsyncClient() as c:
            async with c.stream("GET", url, headers=headers, timeout=20.0, follow_redirects=True) as resp:
                resp.raise_for_status()

                content_len = resp.headers.get("content-length")
                if content_len and int(content_len) > self.MAX_DOWNLOAD_BYTES:
                    raise ValueError(f"Image Content-Length ({content_len} bytes) exceeds 10 MB limit.")

                chunks = []
                total_bytes = 0
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    total_bytes += len(chunk)
                    if total_bytes > self.MAX_DOWNLOAD_BYTES:
                        raise ValueError(f"Downloaded stream exceeded 10 MB limit.")
                    chunks.append(chunk)

                return b"".join(chunks)
