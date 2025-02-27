# -*- coding: utf-8 -*-

# Copyright 2022-2023 Mike Fährmann
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

"""Extractors for https://bunkr.si/"""

from .common import Extractor
from .lolisafe import LolisafeAlbumExtractor
from .. import text, util, config, exception
import random

import base64
import math

if config.get(("extractor", "bunkr"), "tlds"):
    BASE_PATTERN = (
        r"(?:bunkr:(?:https?://)?([^/?#]+)|"
        r"(?:https?://)?(?:app\.)?(bunkr+\.\w+))"
    )
else:
    BASE_PATTERN = (
        r"(?:bunkr:(?:https?://)?([^/?#]+)|"
        r"(?:https?://)?(?:app\.)?(bunkr+"
        r"\.(?:s[kiu]|c[ir]|fi|p[hks]|ru|la|is|to|a[cx]"
        r"|black|cat|media|red|site|ws|org)))"
    )

DOMAINS = [
    "bunkr.ac",
    "bunkr.ci",
    "bunkr.cr",
    "bunkr.fi",
    "bunkr.ph",
    "bunkr.pk",
    "bunkr.ps",
    "bunkr.si",
    "bunkr.sk",
    "bunkr.ws",
    "bunkr.black",
    "bunkr.red",
    "bunkr.media",
    "bunkr.site",
]
LEGACY_DOMAINS = {
    "bunkr.ax",
    "bunkr.cat",
    "bunkr.ru",
    "bunkrr.ru",
    "bunkr.su",
    "bunkrr.su",
    "bunkr.la",
    "bunkr.is",
    "bunkr.to",
}
CF_DOMAINS = set()


def decrypt_url(encrypted_b64: str, timestamp: int) -> str:
    # 1. Base64 decode
    decoded_bytes = base64.b64decode(encrypted_b64)

    # 2. Generate XOR key
    key_str = f"SECRET_KEY_{math.floor(timestamp / 3600)}"
    key_bytes = key_str.encode()

    # 3. XOR decryption
    decrypted = bytes(
        [
            decoded_bytes[i] ^ key_bytes[i % len(key_bytes)]
            for i in range(len(decoded_bytes))
        ]
    )

    return decrypted.decode()


class BunkrAlbumExtractor(LolisafeAlbumExtractor):
    """Extractor for bunkr.si albums"""

    category = "bunkr"
    root = "https://bunkr.si"
    archive_fmt = "{album_id}_{id|id_url}"
    pattern = BASE_PATTERN + r"/a/([^/?#]+)"
    example = "https://bunkr.si/a/ID"
    referer = "https://get.bunkrr.su"

    def __init__(self, match):
        LolisafeAlbumExtractor.__init__(self, match)
        domain = self.groups[0] or self.groups[1]
        if domain not in LEGACY_DOMAINS:
            self.root = "https://" + domain
        self.offset = 0

    def skip(self, num):
        self.offset = num
        return num

    def request(self, url, **kwargs):
        kwargs["encoding"] = "utf-8"
        kwargs["allow_redirects"] = False

        while True:
            try:
                response = Extractor.request(self, url, **kwargs)
                if response.status_code < 300:
                    return response

                # redirect
                url = response.headers["Location"]
                if url[0] == "/":
                    url = self.root + url
                    continue
                root, path = self._split(url)
                if root not in CF_DOMAINS:
                    continue
                self.log.debug("Redirect to known CF challenge domain '%s'", root)

            except exception.HttpError as exc:
                if exc.status != 403:
                    raise

                # CF challenge
                root, path = self._split(url)
                CF_DOMAINS.add(root)
                self.log.debug("Added '%s' to CF challenge domains", root)

                try:
                    DOMAINS.remove(root.rpartition("/")[2])
                except ValueError:
                    pass
                else:
                    if not DOMAINS:
                        raise exception.StopExtraction(
                            "All Bunkr domains require solving a CF challenge"
                        )

            # select alternative domain
            self.root = root = "https://" + random.choice(DOMAINS)
            self.log.debug("Trying '%s' as fallback", root)
            url = root + path

    def fetch_album(self, album_id):
        # album metadata
        page = self.request(self.root + "/a/" + album_id).text
        title = text.unescape(
            text.unescape(text.extr(page, 'property="og:title" content="', '"'))
        )

        # files
        items = list(text.extract_iter(page, '<div class="grid-images_box', "</a>"))

        return self._extract_files(items), {
            "album_id": album_id,
            "album_name": title,
            "album_size": text.extr(page, '<span class="font-semibold">(', ")"),
            "count": len(items),
        }

    def _extract_files(self, items):
        if self.offset:
            items = util.advance(items, self.offset)

        for item in items:
            try:
                url = text.unescape(text.extr(item, ' href="', '"'))
                if url[0] == "/":
                    url = self.root + url
                file = self._extract_file(url)
                info = text.split_html(item)
                if not file["name"]:
                    file["name"] = info[-3]
                file["size"] = info[-2]
                file["date"] = text.parse_datetime(info[-1], "%H:%M:%S %d/%m/%Y")

                yield file
            except exception.StopExtraction:
                raise
            except Exception as exc:
                self.log.error("%s: %s", exc.__class__.__name__, exc)
                self.log.debug("", exc_info=exc)

    def _extract_file(self, webpage_url):
        response = self.request(webpage_url)
        page = response.text

        data_id = text.extr(page, f'href="{self.referer}/file/', '"')

        if not data_id:
            raise exception.StopExtraction("Missing data-id")

        api_url = f"{self.referer}/api/vs"
        api_response = self.request(
            api_url,
            method="POST",
            headers={"Content-Type": "application/json"},
            json={"id": data_id},
        )
        result = api_response.json()

        try:
            file_url = decrypt_url(result["url"], result["timestamp"])
        except Exception as e:
            self.log.error("Decryption failed: %s", e)
            raise exception.StopExtraction("URL decryption failed")

        headers = {"Referer": self.referer}
        test_resp = self.request(file_url, headers=headers, method="HEAD")

        if test_resp.status_code != 200:
            self.log.warning(
                "CDN URL validation failed (HTTP %d)", test_resp.status_code
            )

        file_name = text.extr(page, 'property="og:title" content="', '"') or text.extr(
            page, "<title>", " | Bunkr<"
        )
        fallback = text.extr(page, 'property="og:url" content="', '"')

        return {
            "file": text.unescape(file_url),
            "name": text.unescape(file_name),
            "id_url": webpage_url.rpartition("/")[2],
            "_fallback": (fallback,) if fallback else (),
            "_http_headers": {"Referer": response.url},
            "_http_validate": self._validate,
        }

    def _validate(self, response):
        if response.history and response.url.endswith("/maintenance-vid.mp4"):
            self.log.warning("File server in maintenance mode")
            return False
        return True

    def _split(self, url):
        pos = url.index("/", 8)
        return url[:pos], url[pos:]


class BunkrMediaExtractor(BunkrAlbumExtractor):
    """Extractor for bunkr.si media links"""

    subcategory = "media"
    directory_fmt = ("{category}",)
    pattern = BASE_PATTERN + r"(/[fvid]/[^/?#]+)"
    example = "https://bunkr.si/f/FILENAME"

    def fetch_album(self, album_id):
        try:
            file = self._extract_file(self.root + album_id)
        except Exception as exc:
            self.log.error("%s: %s", exc.__class__.__name__, exc)
            return (), {}

        return (file,), {
            "album_id": "",
            "album_name": "",
            "album_size": -1,
            "description": "",
            "count": 1,
        }
