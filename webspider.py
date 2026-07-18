#!/usr/bin/env python3
# Webspider — a cross-platform website and file-link crawler
# Copyright (C) 2026 Landon Hendee
#
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This program is free software: you may redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed WITHOUT ANY WARRANTY. See LICENSE.md.
#
# Additional warranty and liability terms permitted by AGPLv3 section 7(a)
# are provided in ADDITIONAL-DISCLAIMER.md.

"""
webspider.py — Cross-platform website spider and sitemap generator.

Standard-library only: no pip packages are required.
Project: https://github.com/Pryodon/Webspider
"""

from __future__ import annotations

import argparse
import ftplib
import gzip
import hashlib
import html
import ipaddress
import mimetypes
import json
import os
import posixpath
import re
import shutil
import sqlite3
import ssl
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from html.parser import HTMLParser
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional, TextIO


VERSION = "0.1.0"
VERBOSE_SITEMAP_URL_INTERVAL = 10_000

UPDATE_URL = "https://raw.githubusercontent.com/Pryodon/Webspider/main/webspider.py"
UPDATE_REPOSITORY = "https://github.com/Pryodon/Webspider"
MAX_UPDATE_BYTES = 2_000_000
MIN_UPDATE_BYTES = 10_000

DEFAULT_MAX_SITEMAP_DOCUMENTS = 10_000
DEFAULT_MAX_SITEMAP_DEPTH = 20
DEFAULT_MAX_SITEMAP_MIB = 64
DEFAULT_MAX_SITEMAP_URLS = 0  # 0 means unlimited
DEFAULT_MAX_FTP_ENTRIES = 100_000

DEFAULT_EXTENSIONS = {
    "video": {
        "mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v",
        "ogv", "ts", "m2ts", "srt",
    },
    "audio": {
        "mp3", "mpa", "mp2", "aac", "wav", "flac", "m4a", "ogg",
        "opus", "wma", "alac", "aif", "aiff",
    },
    "images": {
        "jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "svg",
        "avif", "heic", "heif",
    },
    "pages": {
        "html", "htm", "shtml", "xhtml", "php", "phtml", "asp",
        "aspx", "jsp", "jspx", "cfm", "cgi", "pl", "do", "action",
        "md", "markdown",
    },
}

PAGE_CONTENT_TYPES = {
    "text/html",
    "application/xhtml+xml",
}

USER_AGENT = f"webspider/{VERSION} (+https://github.com/Pryodon/Webspider)"


class LinkParser(HTMLParser):
    """Extract crawlable links from common HTML attributes."""

    ATTRIBUTES = {
        "a": ("href",),
        "area": ("href",),
        "link": ("href",),
        "iframe": ("src",),
        "frame": ("src",),
        "img": ("src", "srcset"),
        "source": ("src", "srcset"),
        "video": ("src", "poster"),
        "audio": ("src",),
        "track": ("src",),
        "script": ("src",),
        "object": ("data",),
        "embed": ("src",),
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        wanted = self.ATTRIBUTES.get(tag.lower())
        if not wanted:
            return

        attr_map = {key.lower(): value for key, value in attrs if value is not None}
        for attr in wanted:
            value = attr_map.get(attr)
            if not value:
                continue
            if attr == "srcset":
                for candidate in value.split(","):
                    url = candidate.strip().split()[0] if candidate.strip() else ""
                    if url:
                        self.links.append(url)
            else:
                self.links.append(value)


@dataclass(frozen=True)
class FetchResult:
    requested_url: str
    final_url: str
    status: int
    content_type: str
    body: bytes


@dataclass(frozen=True)
class SitemapDownload:
    requested_url: str
    final_url: str
    content_type: str
    content_encoding: str
    path: Path
    byte_count: int
    compressed: bool


@dataclass
class SitemapStats:
    documents_seen: int = 0
    documents_parsed: int = 0
    child_sitemaps: int = 0
    urls_seen: int = 0
    urls_accepted: int = 0
    urls_out_of_scope: int = 0
    urls_filtered: int = 0
    errors: int = 0


@dataclass
class CrawlOutcome:
    discovered: set[str]
    verified_200: set[str]
    interrupted: bool
    sitemap_stats: SitemapStats


class SpiderError(RuntimeError):
    pass



def parse_version(value: str) -> tuple[int, ...]:
    """Parse a numeric dotted version such as 2.1.0."""
    if not re.fullmatch(r"\d+(?:\.\d+)*", value):
        raise SpiderError(f"Unsupported version format: {value!r}")
    return tuple(int(part) for part in value.split("."))


def version_from_source(source: str) -> str:
    """Read VERSION from Webspider source code."""
    match = re.search(
        r'(?m)^VERSION\s*=\s*["\'](\d+(?:\.\d+)*)["\']\s*$',
        source,
    )
    if not match:
        raise SpiderError("Source file does not contain a valid VERSION assignment")
    return match.group(1)


def download_update_source(
    update_url: str = UPDATE_URL,
    *,
    timeout: float = 30.0,
) -> tuple[bytes, str]:
    """Download the official main-branch Webspider source."""
    request = urllib.request.Request(
        update_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/plain, application/octet-stream;q=0.9, */*;q=0.1",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = getattr(response, "status", None)
            if status_code is not None and int(status_code) != 200:
                raise SpiderError(f"Update server returned HTTP {status_code}")

            final_url = response.geturl()
            data = response.read(MAX_UPDATE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise SpiderError(f"Update download failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SpiderError(f"Could not download update: {exc}") from exc

    if len(data) > MAX_UPDATE_BYTES:
        raise SpiderError(
            f"Downloaded update exceeds the {MAX_UPDATE_BYTES}-byte safety limit"
        )
    if len(data) < MIN_UPDATE_BYTES:
        raise SpiderError(
            "Downloaded update is unexpectedly small; refusing to replace the script"
        )

    return data, final_url


def validate_update_source(data: bytes) -> tuple[str, str]:
    """
    Validate downloaded source before it may replace the installed script.

    Returns the decoded source and declared version.
    """
    try:
        source = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SpiderError("Downloaded update is not valid UTF-8") from exc

    required_markers = (
        "#!/usr/bin/env python3",
        "SPDX-License-Identifier: AGPL-3.0-or-later",
        "Project: https://github.com/Pryodon/Webspider",
        'UPDATE_REPOSITORY = "https://github.com/Pryodon/Webspider"',
    )
    for marker in required_markers:
        if marker not in source:
            raise SpiderError(
                f"Downloaded file is missing the expected Webspider marker: {marker}"
            )

    remote_version = version_from_source(source)

    try:
        compile(source, "<downloaded-webspider-update>", "exec")
    except SyntaxError as exc:
        raise SpiderError(f"Downloaded update has invalid Python syntax: {exc}") from exc

    return source, remote_version


def self_update(
    *,
    target_path: Optional[Path] = None,
    update_url: str = UPDATE_URL,
    timeout: float = 30.0,
) -> bool:
    """
    Update the exact Webspider source file being executed.

    Returns True when the file was replaced, or False when it already matches
    the official copy.
    """
    target = (target_path or Path(__file__)).resolve()

    if not target.is_file():
        raise SpiderError(f"Cannot locate the running script: {target}")

    try:
        local_data = target.read_bytes()
        local_source = local_data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SpiderError(f"The installed script is not valid UTF-8: {target}") from exc
    except OSError as exc:
        raise SpiderError(f"Could not read the installed script {target}: {exc}") from exc

    local_version = version_from_source(local_source)

    print(f"[*] Installed file: {target}")
    print(f"[*] Current version: {local_version}")
    print(f"[*] Checking: {update_url}")

    remote_data, final_url = download_update_source(update_url, timeout=timeout)
    _remote_source, remote_version = validate_update_source(remote_data)

    print(f"[*] GitHub version: {remote_version}")

    local_key = parse_version(local_version)
    remote_key = parse_version(remote_version)

    if remote_key < local_key:
        raise SpiderError(
            f"The GitHub version ({remote_version}) is older than the installed "
            f"version ({local_version}); refusing to downgrade"
        )

    if remote_data == local_data:
        print("[*] This copy already exactly matches the GitHub version.")
        return False

    if remote_key == local_key:
        print(
            "[*] The version number is unchanged, but the GitHub file differs; "
            "refreshing this copy."
        )

    target_directory = target.parent

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    backup = target.with_name(f"{target.name}.bak-{local_version}-{timestamp}")
    temp_path: Optional[Path] = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{target.name}.update-",
            suffix=".tmp",
            dir=target_directory,
            delete=False,
        ) as temp_file:
            temp_file.write(remote_data)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = Path(temp_file.name)

        # Validate the exact bytes written to disk.
        validate_update_source(temp_path.read_bytes())

        old_mode = stat.S_IMODE(target.stat().st_mode)
        shutil.copy2(target, backup)

        if os.name != "nt":
            os.chmod(temp_path, old_mode)

        os.replace(temp_path, target)
        temp_path = None

    except PermissionError as exc:
        raise SpiderError(
            f"Permission denied while updating {target}.\n"
            "Move Webspider to a user-writable directory or run with suitable permissions."
        ) from exc
    except OSError as exc:
        raise SpiderError(f"Could not replace {target}: {exc}") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    digest = hashlib.sha256(remote_data).hexdigest()
    print(f"[*] Updated: {target}")
    print(f"[*] Backup:  {backup}")
    print(f"[*] Source:  {final_url}")
    print(f"[*] SHA-256: {digest}")
    print(f"[*] Installed version is now {remote_version}.")
    print("[*] Run Webspider again to use the updated code.")
    return True


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def parse_extension_pattern(pattern: Optional[str], mode: str) -> set[str]:
    if pattern:
        parts = re.split(r"[|,\s]+", pattern.strip())
        return {part.lower().lstrip(".") for part in parts if part.strip()}
    return set(DEFAULT_EXTENSIONS.get(mode, set()))


def is_literal_ip(hostname: Optional[str]) -> bool:
    if not hostname:
        return False
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        return False


def add_default_scheme(value: str, scheme: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
        return value

    # Bracket bare IPv6 literals before adding a scheme.
    slash = value.find("/")
    hostpart = value if slash == -1 else value[:slash]
    remainder = "" if slash == -1 else value[slash:]

    if ":" in hostpart and not hostpart.startswith("["):
        # Treat a pure IPv6 literal as IPv6. A hostname:port remains unchanged.
        try:
            ipaddress.IPv6Address(hostpart)
            value = f"[{hostpart}]{remainder}"
        except ValueError:
            pass

    return f"{scheme}://{value}"


def quote_component_preserving_escapes(value: str, safe: str) -> str:
    """
    Percent-encode a URL component without double-encoding valid %HH escapes.

    Invalid literal percent signs become %25.
    """
    placeholders: dict[str, str] = {}

    def save_escape(match: re.Match[str]) -> str:
        token = f"__WEBSPIDER_ESC_{len(placeholders)}__"
        placeholders[token] = match.group(0).upper()
        return token

    protected = re.sub(r"%[0-9A-Fa-f]{2}", save_escape, value)
    encoded = urllib.parse.quote(protected, safe=safe)
    for token, escape_value in placeholders.items():
        encoded = encoded.replace(token, escape_value)
    return encoded


def normalize_url(
    raw_url: str,
    *,
    base_url: Optional[str] = None,
    default_scheme: str = "https",
    strip_query: bool = True,
    strip_fragment: bool = True,
) -> Optional[str]:
    raw_url = html.unescape(raw_url.strip())
    if not raw_url:
        return None

    lowered = raw_url.lower()
    if lowered.startswith(("javascript:", "mailto:", "tel:", "data:", "blob:", "about:")):
        return None

    if base_url is not None:
        raw_url = urllib.parse.urljoin(base_url, raw_url)
    else:
        raw_url = add_default_scheme(raw_url, default_scheme)

    try:
        parts = urllib.parse.urlsplit(raw_url)
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in {"http", "https", "ftp"} or not parts.netloc:
        return None

    try:
        hostname = parts.hostname
        if hostname is None:
            return None

        if is_literal_ip(hostname):
            host = f"[{hostname}]" if ":" in hostname else hostname
        else:
            host = hostname.encode("idna").decode("ascii").lower()

        port = parts.port
    except (ValueError, UnicodeError):
        return None

    default_port = {"http": 80, "https": 443, "ftp": 21}[scheme]
    hostport = host if port is None or port == default_port else f"{host}:{port}"

    # Preserve FTP user information when a document explicitly supplies it.
    # Embedded credentials will therefore also be present in the persistent
    # state and logs; anonymous FTP is used when no user is supplied.
    userinfo = ""
    if scheme == "ftp" and parts.username is not None:
        username = quote_component_preserving_escapes(parts.username, safe="-._~")
        userinfo = username
        if parts.password is not None:
            password = quote_component_preserving_escapes(parts.password, safe="-._~")
            userinfo += f":{password}"
        userinfo += "@"

    netloc = f"{userinfo}{hostport}"
    path = parts.path or "/"
    path = quote_component_preserving_escapes(
        path,
        safe="/:@!$&'()*+,;=-._~",
    )

    query = "" if strip_query else quote_component_preserving_escapes(
        parts.query,
        safe="=&?/:@!$'()*+,;%-._~",
    )
    fragment = "" if strip_fragment else quote_component_preserving_escapes(
        parts.fragment,
        safe="=&?/:@!$'()*+,;%-._~",
    )

    return urllib.parse.urlunsplit((scheme, netloc, path, query, fragment))


def canonical_output_url(raw_url: str) -> Optional[str]:
    """Normalize URLs for output and sitemap use."""
    return normalize_url(raw_url, strip_query=True, strip_fragment=True)


def path_extension(url: str) -> str:
    path = urllib.parse.urlsplit(url).path
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def looks_like_directory(url: str) -> bool:
    return urllib.parse.urlsplit(url).path.endswith("/")


def matches_mode(url: str, mode: str, extensions: set[str]) -> bool:
    extension = path_extension(url)

    if mode == "all":
        return True
    if mode == "video":
        return extension in extensions
    if mode == "audio":
        return extension in extensions
    if mode == "images":
        return extension in extensions
    if mode == "pages":
        return looks_like_directory(url) or extension in extensions or extension == ""
    if mode == "files":
        return not looks_like_directory(url) and extension not in {"html", "htm"}
    raise ValueError(f"Unknown mode: {mode}")


def read_seed_arguments(values: Iterable[str], default_scheme: str) -> list[str]:
    seeds: list[str] = []
    for value in values:
        path = Path(value)
        if path.is_file():
            try:
                lines = path.read_text(encoding="utf-8-sig").splitlines()
            except OSError as exc:
                raise SpiderError(f"Could not read seed file {path}: {exc}") from exc

            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                normalized = normalize_url(
                    stripped,
                    default_scheme=default_scheme,
                    strip_query=False,
                    strip_fragment=True,
                )
                if normalized:
                    seeds.append(normalized)
        else:
            normalized = normalize_url(
                value,
                default_scheme=default_scheme,
                strip_query=False,
                strip_fragment=True,
            )
            if normalized:
                seeds.append(normalized)

    return sorted(set(seeds))


def host_variants(host: str) -> set[str]:
    """Return the hostnames accepted for a seed."""
    variants = {host.lower()}
    if not is_literal_ip(host) and host.count(".") == 1 and not host.startswith("www."):
        variants.add(f"www.{host.lower()}")
    return variants


def seed_directory_path(seed: str) -> str:
    """
    Return the directory boundary for a seed.

    A seed ending in "/" is already a directory. A seed naming a page or file
    is scoped to that file's containing directory, matching wget --no-parent.
    """
    path = urllib.parse.urlsplit(seed).path or "/"
    if path.endswith("/"):
        return path

    parent = path.rsplit("/", 1)[0]
    return f"{parent}/" if parent else "/"


def normalized_origin_keys(url: str) -> list[str]:
    """
    Return normalized origin identities for a URL.

    Scheme and effective port are part of the identity. Existing www/apex host
    variants remain grouped, but two services on different ports are external
    to one another.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https", "ftp"} or not parts.hostname:
        return []

    try:
        port = parts.port
    except ValueError:
        return []

    effective_port = port or {"http": 80, "https": 443, "ftp": 21}[parts.scheme]
    keys: list[str] = []
    for variant in host_variants(parts.hostname):
        keys.append(f"{parts.scheme.lower()}://{variant.lower()}:{effective_port}")
    return keys


def crawl_scopes_from_seeds(seeds: Iterable[str]) -> dict[str, set[str]]:
    """
    Build allowed path prefixes for every seed origin.

    Multiple seeds may open multiple independent directory trees on the same
    origin. Scheme and effective port are included so a different service on
    the same hostname is still treated as external.
    """
    scopes: dict[str, set[str]] = {}

    for seed in seeds:
        directory = seed_directory_path(seed)
        for origin_key in normalized_origin_keys(seed):
            scopes.setdefault(origin_key, set()).add(directory)

    return scopes


def url_within_seed_scopes(url: str, scopes: dict[str, set[str]]) -> bool:
    """Return True only when URL is inside a seed origin and directory tree."""
    path = urllib.parse.urlsplit(url).path or "/"
    for origin_key in normalized_origin_keys(url):
        for prefix in scopes.get(origin_key, set()):
            if prefix == "/":
                return True
            if path == prefix.rstrip("/") or path.startswith(prefix):
                return True
    return False


def url_host_is_seed_host(url: str, scopes: dict[str, set[str]]) -> bool:
    """
    Return True when URL uses an original seed origin, regardless of path.

    The historical function name is retained for database compatibility, but
    scheme and port are now considered as well as the hostname.
    """
    return any(origin_key in scopes for origin_key in normalized_origin_keys(url))


def external_depth_for_link(
    parent_url: str,
    parent_external_depth: int,
    link: str,
    scopes: dict[str, set[str]],
) -> Optional[int]:
    """
    Return the off-site depth for a discovered link.

    In-scope links use depth 0. A same-host link outside the saved path scope is
    deliberately rejected with None so external crawling cannot bypass the
    existing wget --no-parent-style path boundary. The first different-host
    link is depth 1; each additional link followed while outside the original
    scope increments the external depth.
    """
    if url_within_seed_scopes(link, scopes):
        return 0
    if url_host_is_seed_host(link, scopes):
        return None
    return max(1, int(parent_external_depth) + 1)


def external_target_allowed(
    *,
    url: str,
    kind: str,
    link_external_depth: int,
    mode_matches: bool,
    external_media: bool,
    max_external_depth: int,
    follow_ftp: bool,
) -> bool:
    """Apply the saved external-link policy to a discovered target."""
    scheme = urllib.parse.urlsplit(url).scheme.lower()

    if scheme == "ftp":
        if not follow_ftp:
            return False
        if kind == "page":
            # A directly linked FTP directory is useful with --follow-ftp.
            # Deeper directory traversal uses the normal external depth.
            return link_external_depth <= max(1, max_external_depth)
        # Files found in an allowed FTP listing are checked when they match the
        # selected mode, much like --external-media for HTTP links.
        return mode_matches

    if scheme not in {"http", "https"}:
        return False
    if kind == "page":
        return max_external_depth >= link_external_depth
    if not mode_matches:
        return False
    return external_media or max_external_depth >= link_external_depth


def ssl_context_for_url(url: str, insecure: bool, insecure_ip_https: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    parts = urllib.parse.urlsplit(url)
    disable_checks = insecure or (
        insecure_ip_https
        and parts.scheme == "https"
        and is_literal_ip(parts.hostname)
    )
    if disable_checks:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def fetch_url(
    url: str,
    *,
    timeout: float,
    insecure: bool,
    insecure_ip_https: bool,
    max_bytes: int,
    read_html_body: bool = True,
) -> FetchResult:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Encoding": "identity",
        },
        method="GET",
    )
    context = ssl_context_for_url(url, insecure, insecure_ip_https)

    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            status = getattr(response, "status", response.getcode())
            final_url = canonical_output_url(response.geturl()) or url
            content_type = response.headers.get_content_type().lower()

            # Do not download media or other large file bodies merely to verify
            # that the URL exists. HTML is read only when link extraction is
            # enabled for this crawl mode.
            body = b""
            if read_html_body and (
                content_type in PAGE_CONTENT_TYPES
                or urllib.parse.urlsplit(url).path.lower().endswith("/robots.txt")
            ):
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    body = body[:max_bytes]

            return FetchResult(url, final_url, int(status), content_type, body)
    except urllib.error.HTTPError as exc:
        content_type = exc.headers.get_content_type().lower() if exc.headers else ""
        return FetchResult(
            url,
            canonical_output_url(exc.geturl()) or url,
            exc.code,
            content_type,
            b"",
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SpiderError(str(exc)) from exc


def decode_html(body: bytes, content_type_header: str = "") -> str:
    # HTMLParser tolerates replacement characters, so UTF-8 fallback is safe.
    for encoding in ("utf-8", "windows-1252", "latin-1"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


def extract_links(page_url: str, body: bytes) -> list[str]:
    parser = LinkParser()
    try:
        parser.feed(decode_html(body))
        parser.close()
    except Exception:
        # Malformed HTML should not abort the whole crawl.
        pass

    links: list[str] = []
    for raw_link in parser.links:
        normalized = normalize_url(
            raw_link,
            base_url=page_url,
            strip_query=True,
            strip_fragment=True,
        )
        if normalized:
            links.append(normalized)
    return links



@dataclass(frozen=True)
class RobotsRule:
    pattern: str
    allow: bool
    specificity: int
    regex: re.Pattern[str]


@dataclass
class RobotsGroup:
    agents: list[str]
    rules: list[RobotsRule]
    crawl_delays: list[float]
    request_rates: list[urllib.robotparser.RequestRate]


def normalize_robots_octets(value: str, *, preserve_wildcards: bool) -> str:
    """
    Normalize non-ASCII and percent-encoded unreserved octets for REP matching.
    """
    safe = "/?&=;:+,$-_.!~'()"
    if preserve_wildcards:
        safe += "*"
    encoded = urllib.parse.quote(value, safe=safe + "%")

    unreserved = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    )

    def decode_unreserved(match: re.Match[str]) -> str:
        character = chr(int(match.group(1), 16))
        return character if character in unreserved else "%" + match.group(1).upper()

    return re.sub(r"%([0-9A-Fa-f]{2})", decode_unreserved, encoded)


def compile_robots_rule(pattern: str, allow: bool) -> Optional[RobotsRule]:
    """
    Compile an Allow or Disallow path using REP wildcard and end-anchor rules.
    """
    pattern = pattern.strip()
    if not pattern:
        # Empty Disallow means no restriction; empty Allow has no useful match.
        return None

    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]

    normalized = normalize_robots_octets(pattern, preserve_wildcards=True)
    segments = normalized.split("*")
    expression = "^" + ".*".join(re.escape(segment) for segment in segments)
    if anchored:
        expression += "$"

    specificity = len(normalized.replace("*", "").encode("utf-8"))
    return RobotsRule(
        pattern=normalized + ("$" if anchored else ""),
        allow=allow,
        specificity=specificity,
        regex=re.compile(expression),
    )


class RobotsPolicyDocument:
    """
    Robots Exclusion Protocol parser with longest-match Allow/Disallow handling.

    It also retains the commonly deployed Crawl-delay and Request-rate
    extensions. Matching groups with the longest user-agent token are combined.
    """

    def __init__(self, body: str) -> None:
        self.groups: list[RobotsGroup] = []
        self._parse(body)

    @staticmethod
    def product_token(user_agent: str) -> str:
        token = user_agent.split(None, 1)[0].split("/", 1)[0].strip().lower()
        return token or "*"

    def _parse(self, body: str) -> None:
        agents: list[str] = []
        rules: list[RobotsRule] = []
        delays: list[float] = []
        rates: list[urllib.robotparser.RequestRate] = []
        group_has_fields = False

        def finish_group() -> None:
            nonlocal agents, rules, delays, rates, group_has_fields
            if agents:
                self.groups.append(
                    RobotsGroup(
                        agents=list(agents),
                        rules=list(rules),
                        crawl_delays=list(delays),
                        request_rates=list(rates),
                    )
                )
            agents = []
            rules = []
            delays = []
            rates = []
            group_has_fields = False

        for raw_line in body.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, value = line.split(":", 1)
            field = field.strip().lower()
            value = value.strip()

            if field == "user-agent":
                if group_has_fields and agents:
                    finish_group()
                if value:
                    agents.append(value.lower())
                continue

            if field == "sitemap":
                # Sitemap declarations are global and parsed separately.
                continue

            if not agents:
                continue

            group_has_fields = True

            if field in {"allow", "disallow"}:
                rule = compile_robots_rule(value, field == "allow")
                if rule is not None:
                    rules.append(rule)
            elif field == "crawl-delay":
                try:
                    delay = float(value)
                except ValueError:
                    continue
                if delay >= 0:
                    delays.append(delay)
            elif field == "request-rate":
                match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", value)
                if not match:
                    continue
                requests = int(match.group(1))
                seconds = int(match.group(2))
                if requests > 0 and seconds > 0:
                    rates.append(
                        urllib.robotparser.RequestRate(
                            requests=requests,
                            seconds=seconds,
                        )
                    )

        finish_group()

    def matching_groups(self, user_agent: str) -> list[RobotsGroup]:
        product = self.product_token(user_agent)
        best_score = -1
        selected: list[RobotsGroup] = []

        for group in self.groups:
            group_score = -1
            for agent in group.agents:
                if agent == "*":
                    score = 0
                elif agent and agent in product:
                    score = len(agent)
                else:
                    continue
                group_score = max(group_score, score)

            if group_score < 0:
                continue
            if group_score > best_score:
                best_score = group_score
                selected = [group]
            elif group_score == best_score:
                selected.append(group)

        return selected

    @staticmethod
    def target_path(url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        return normalize_robots_octets(target, preserve_wildcards=False)

    def can_fetch(self, user_agent: str, url: str) -> bool:
        target = self.target_path(url)
        matching_rules: list[RobotsRule] = []

        for group in self.matching_groups(user_agent):
            for rule in group.rules:
                if rule.regex.search(target):
                    matching_rules.append(rule)

        if not matching_rules:
            return True

        longest = max(rule.specificity for rule in matching_rules)
        finalists = [
            rule for rule in matching_rules if rule.specificity == longest
        ]
        # Allow wins only when specificity is exactly tied.
        return any(rule.allow for rule in finalists)

    def crawl_delay(self, user_agent: str) -> Optional[float]:
        values = [
            value
            for group in self.matching_groups(user_agent)
            for value in group.crawl_delays
        ]
        return max(values) if values else None

    def request_rate(
        self, user_agent: str
    ) -> Optional[urllib.robotparser.RequestRate]:
        rates = [
            rate
            for group in self.matching_groups(user_agent)
            for rate in group.request_rates
        ]
        if not rates:
            return None
        # The highest seconds-per-request value is the most restrictive.
        return max(
            rates,
            key=lambda rate: (
                float(rate.seconds) / float(rate.requests),
                -int(rate.requests),
            ),
        )


class RobotsCache:
    def __init__(
        self,
        *,
        enabled: bool,
        timeout: float,
        insecure: bool,
        insecure_ip_https: bool,
    ) -> None:
        self.enabled = enabled
        self.timeout = timeout
        self.insecure = insecure
        self.insecure_ip_https = insecure_ip_https
        self.cache: dict[str, Optional[RobotsPolicyDocument]] = {}
        self.sitemap_cache: dict[str, list[str]] = {}

    @staticmethod
    def origin_for_url(url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    def _ensure_loaded(self, url: str) -> str:
        origin = self.origin_for_url(url)
        if origin in self.cache:
            return origin

        robots_url = f"{origin}/robots.txt"
        parser: Optional[RobotsPolicyDocument] = None
        sitemap_urls: list[str] = []

        try:
            result = fetch_url(
                robots_url,
                timeout=self.timeout,
                insecure=self.insecure,
                insecure_ip_https=self.insecure_ip_https,
                max_bytes=2_000_000,
                read_html_body=True,
            )
            if result.status == 200:
                body_text = decode_html(result.body)
                lines = body_text.splitlines()
                parser = RobotsPolicyDocument(body_text)

                for line in lines:
                    cleaned = line.split("#", 1)[0].strip()
                    match = re.match(r"(?i)^sitemap\s*:\s*(\S+)\s*$", cleaned)
                    if not match:
                        continue
                    normalized = normalize_url(
                        match.group(1),
                        base_url=f"{origin}/",
                        strip_query=False,
                        strip_fragment=True,
                    )
                    if normalized:
                        sitemap_urls.append(normalized)

                self.cache[origin] = parser
            else:
                self.cache[origin] = None
        except SpiderError:
            self.cache[origin] = None

        self.sitemap_cache[origin] = sorted(set(sitemap_urls))
        return origin

    def allowed(self, url: str) -> bool:
        if not self.enabled:
            return True

        origin = self._ensure_loaded(url)
        parser = self.cache[origin]
        return True if parser is None else parser.can_fetch(USER_AGENT, url)

    def sitemaps_for_url(self, url: str) -> list[str]:
        origin = self._ensure_loaded(url)
        return list(self.sitemap_cache.get(origin, []))


def local_timestamp() -> str:
    """Return an ISO-8601 timestamp using the computer's local timezone."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def format_elapsed(seconds: float) -> str:
    """Format elapsed seconds as HH:MM:SS.mmm without wrapping after 24 hours."""
    total_milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def write_log_line(log_file: TextIO, line: str, *, sync: bool = False) -> None:
    """Write and immediately flush one complete crawl-log record."""
    log_file.write(line + "\n")
    log_file.flush()

    if sync:
        try:
            os.fsync(log_file.fileno())
        except (AttributeError, OSError):
            pass


def sitemap_origin_root(url: str) -> Optional[str]:
    """Return the origin root used as a content scope for a sitemap-only seed."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


def normalize_sitemap_source(value: str, default_scheme: str = "https") -> Optional[str]:
    return normalize_url(
        value,
        default_scheme=default_scheme,
        strip_query=False,
        strip_fragment=True,
    )


def looks_like_sitemap_url(url: str) -> bool:
    """
    Recognize conventional sitemap filenames found in HTML.

    The word ``sitemap`` must be a complete filename token. This accepts names
    such as sitemap.xml, sitemap-index.xml, video-sitemap.xml, and
    sitemap-7.xml.gz, while rejecting unrelated files such as
    make-sitemaps.py.txt.
    """
    path = urllib.parse.urlsplit(url).path.lower()
    name = path.rsplit("/", 1)[-1]

    if name == "sitemap":
        return True

    if name.endswith(".xml.gz"):
        stem = name[:-7]
    elif name.endswith(".xml"):
        stem = name[:-4]
    elif name.endswith(".txt"):
        stem = name[:-4]
    else:
        return False

    tokens = [token for token in re.split(r"[-_.]+", stem) if token]
    return any(
        token == "sitemap" or re.fullmatch(r"sitemap(?:index|\d+)", token)
        for token in tokens
    )


def allowed_sitemap_hosts(
    scope_seeds: Iterable[str],
    sitemap_sources: Iterable[str],
) -> set[str]:
    hosts: set[str] = set()
    for url in list(scope_seeds) + list(sitemap_sources):
        host = urllib.parse.urlsplit(url).hostname
        if host:
            hosts.update(host_variants(host))
    return hosts


def sitemap_document_allowed(url: str, allowed_hosts: set[str]) -> bool:
    host = urllib.parse.urlsplit(url).hostname
    return bool(host and host.lower() in allowed_hosts)


class SitemapWorkspace:
    """Disk-backed temporary storage for downloaded sitemap documents."""

    def __init__(self, log_file: TextIO) -> None:
        self.log_file = log_file
        self.path: Optional[Path] = None
        self.counter = 0

    def ensure(self) -> Path:
        if self.path is None:
            try:
                created = tempfile.mkdtemp(
                    prefix=".webspider-sitemaps-",
                    dir=str(Path.cwd()),
                )
            except OSError as exc:
                raise SpiderError(
                    f"Could not create a sitemap temporary directory in {Path.cwd()}: {exc}"
                ) from exc
            self.path = Path(created)
            write_log_line(self.log_file, f"SITEMAP TEMP DIR: {self.path}", sync=True)
        return self.path

    def new_file(self, suffix: str) -> Path:
        directory = self.ensure()
        self.counter += 1
        return directory / f"sitemap-{self.counter:06d}{suffix}"

    def cleanup(self) -> None:
        if self.path is None:
            return
        path = self.path
        self.path = None
        try:
            shutil.rmtree(path)
            write_log_line(self.log_file, f"SITEMAP TEMP DIR REMOVED: {path}", sync=True)
        except FileNotFoundError:
            write_log_line(self.log_file, f"SITEMAP TEMP DIR ALREADY REMOVED: {path}")
        except OSError as exc:
            write_log_line(self.log_file, f"SITEMAP TEMP DIR CLEANUP ERROR: {path} {exc}", sync=True)



class SitemapURLStore:
    """
    Disk-backed, deduplicated queue of URLs imported from sitemap documents.

    The SQLite database lives inside SitemapWorkspace and is removed with the
    rest of the sitemap temporary directory after completion or Ctrl-C.
    """

    FETCH_BATCH_SIZE = 1000
    COMMIT_INTERVAL = 5000

    def __init__(self, workspace: SitemapWorkspace, log_file: TextIO) -> None:
        self.log_file = log_file
        self.path = workspace.new_file(".sqlite3")
        try:
            self.connection = sqlite3.connect(str(self.path))
            self.connection.execute("PRAGMA journal_mode=DELETE")
            self.connection.execute("PRAGMA synchronous=NORMAL")
            self.connection.execute("PRAGMA temp_store=FILE")
            self.connection.execute(
                """
                CREATE TABLE sitemap_urls (
                    id INTEGER PRIMARY KEY,
                    url TEXT NOT NULL UNIQUE,
                    processed INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self.connection.execute(
                "CREATE INDEX sitemap_urls_processed_id "
                "ON sitemap_urls(processed, id)"
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            raise SpiderError(f"Could not create sitemap URL database: {exc}") from exc

        self.pending_batch: deque[str] = deque()
        self.total_urls = 0
        self.pending_urls = 0
        self.uncommitted_inserts = 0
        self.closed = False
        write_log_line(self.log_file, f"SITEMAP URL DB: {self.path}", sync=True)

    def add(self, url: str) -> bool:
        """Insert a URL once. Return True only for a new unique URL."""
        try:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO sitemap_urls(url) VALUES (?)",
                (url,),
            )
        except sqlite3.Error as exc:
            raise SpiderError(f"Could not store sitemap URL: {exc}") from exc

        if cursor.rowcount != 1:
            return False

        self.total_urls += 1
        self.pending_urls += 1
        self.uncommitted_inserts += 1
        if self.uncommitted_inserts >= self.COMMIT_INTERVAL:
            self.commit()
        return True

    def commit(self) -> None:
        try:
            self.connection.commit()
        except sqlite3.Error as exc:
            raise SpiderError(f"Could not commit sitemap URL database: {exc}") from exc
        self.uncommitted_inserts = 0

    def _load_batch(self) -> None:
        self.commit()
        try:
            rows = self.connection.execute(
                "SELECT id, url FROM sitemap_urls "
                "WHERE processed = 0 ORDER BY id LIMIT ?",
                (self.FETCH_BATCH_SIZE,),
            ).fetchall()
            if not rows:
                return

            self.connection.executemany(
                "UPDATE sitemap_urls SET processed = 1 WHERE id = ?",
                ((row[0],) for row in rows),
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            raise SpiderError(f"Could not read sitemap URL database: {exc}") from exc

        self.pending_batch.extend(row[1] for row in rows)

    def next_url(self) -> Optional[str]:
        """Return the next queued sitemap URL without loading the whole set."""
        if not self.pending_batch:
            self._load_batch()
        if not self.pending_batch:
            return None
        self.pending_urls -= 1
        return self.pending_batch.popleft()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.commit()
        finally:
            self.connection.close()
        write_log_line(
            self.log_file,
            (
                f"SITEMAP URL DB CLOSED: {self.path} "
                f"stored={self.total_urls} pending={self.pending_urls}"
            ),
        )


def download_sitemap_to_disk(
    url: str,
    *,
    workspace: SitemapWorkspace,
    timeout: float,
    insecure: bool,
    insecure_ip_https: bool,
    max_bytes: int,
) -> SitemapDownload:
    """Stream one sitemap document to the isolated temporary directory."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/xml,text/xml,text/plain,application/gzip,*/*;q=0.5",
            "Accept-Encoding": "identity",
        },
        method="GET",
    )
    context = ssl_context_for_url(url, insecure, insecure_ip_https)

    raw_path: Optional[Path] = None
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            status = int(getattr(response, "status", response.getcode()))
            if status != 200:
                raise SpiderError(f"HTTP {status}")

            final_url = normalize_url(
                response.geturl(),
                strip_query=False,
                strip_fragment=True,
            ) or url
            content_type = response.headers.get_content_type().lower()
            content_encoding = response.headers.get("Content-Encoding", "").lower()
            compressed = (
                urllib.parse.urlsplit(final_url).path.lower().endswith(".gz")
                or "gzip" in content_type
                or "gzip" in content_encoding
            )
            raw_path = workspace.new_file(".gz" if compressed else ".download")

            byte_count = 0
            with raw_path.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    byte_count += len(chunk)
                    if byte_count > max_bytes:
                        raise SpiderError(
                            f"sitemap exceeds the {max_bytes}-byte download limit"
                        )
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())

            return SitemapDownload(
                requested_url=url,
                final_url=final_url,
                content_type=content_type,
                content_encoding=content_encoding,
                path=raw_path,
                byte_count=byte_count,
                compressed=compressed,
            )
    except urllib.error.HTTPError as exc:
        raise SpiderError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SpiderError(str(exc)) from exc
    except BaseException:
        if raw_path is not None:
            try:
                raw_path.unlink()
            except FileNotFoundError:
                pass
        raise


def prepare_sitemap_parse_file(
    download: SitemapDownload,
    *,
    workspace: SitemapWorkspace,
    max_uncompressed_bytes: int,
) -> Path:
    if not download.compressed:
        return download.path

    decompressed = workspace.new_file(".decompressed")
    byte_count = 0
    try:
        with gzip.open(download.path, "rb") as source, decompressed.open("wb") as output:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > max_uncompressed_bytes:
                    raise SpiderError(
                        "decompressed sitemap exceeds the "
                        f"{max_uncompressed_bytes}-byte limit"
                    )
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        return decompressed
    except (gzip.BadGzipFile, EOFError, OSError) as exc:
        try:
            decompressed.unlink()
        except FileNotFoundError:
            pass
        raise SpiderError(f"could not decompress gzip sitemap: {exc}") from exc
    except BaseException:
        try:
            decompressed.unlink()
        except FileNotFoundError:
            pass
        raise


def iter_sitemap_entries(path: Path, sitemap_url: str) -> Iterator[tuple[str, str]]:
    """Yield (kind, URL), where kind is 'url' or 'sitemap'."""
    try:
        with path.open("rb") as source:
            raw_prefix = source.read(4096)
    except OSError as exc:
        raise SpiderError(f"could not read temporary sitemap file: {exc}") from exc

    prefix = raw_prefix
    if prefix.startswith(b"\xef\xbb\xbf"):
        prefix = prefix[3:]
    prefix = prefix.lstrip()
    is_xml = prefix.startswith(b"<") or raw_prefix.startswith((b"\xff\xfe", b"\xfe\xff"))

    if not is_xml:
        try:
            with path.open("r", encoding="utf-8-sig", errors="replace") as source:
                for line in source:
                    value = line.strip()
                    if not value or value.startswith("#"):
                        continue
                    normalized = canonical_output_url(value)
                    if normalized:
                        yield "url", normalized
            return
        except OSError as exc:
            raise SpiderError(f"could not parse text sitemap: {exc}") from exc

    stack: list[str] = []
    try:
        for event, element in ET.iterparse(path, events=("start", "end")):
            local_name = element.tag.rsplit("}", 1)[-1].lower()
            if event == "start":
                stack.append(local_name)
                continue

            if len(stack) >= 2 and element.text:
                parent = stack[-2]
                value = element.text.strip()
                if local_name == "loc" and parent == "sitemap":
                    normalized = normalize_url(
                        value,
                        base_url=sitemap_url,
                        strip_query=False,
                        strip_fragment=True,
                    )
                    if normalized:
                        yield "sitemap", normalized
                elif (
                    (local_name == "loc" and parent in {"url", "image"})
                    or (local_name == "content_loc" and parent == "video")
                ):
                    # Standard page URLs, image sitemap URLs, and direct video
                    # content URLs are all checkable sitemap targets.
                    normalized = canonical_output_url(value)
                    if normalized:
                        yield "url", normalized

            if stack:
                stack.pop()
            element.clear()
    except ET.ParseError as exc:
        raise SpiderError(f"invalid XML sitemap: {exc}") from exc
    except OSError as exc:
        raise SpiderError(f"could not parse XML sitemap: {exc}") from exc


def crawl(
    scope_seeds: list[str],
    page_seeds: list[str],
    initial_sitemap_sources: list[str],
    *,
    level: Optional[int],
    delay: float,
    timeout: float,
    status_200_only: bool,
    respect_robots: bool,
    insecure: bool,
    insecure_ip_https: bool,
    max_page_bytes: int,
    verbose: bool,
    log_file: TextIO,
    enable_sitemaps: bool,
    sitemap_only: bool,
    mode: str,
    extensions: set[str],
    max_sitemap_documents: int,
    max_sitemap_depth: int,
    max_sitemap_bytes: int,
    max_sitemap_uncompressed_bytes: int,
    max_sitemap_urls: int,
) -> CrawlOutcome:
    """
    Crawl HTML links, sitemap-listed URLs, or both.

    Sitemap documents are completely discovered and parsed before ordinary URL
    checking begins. Imported sitemap URLs are deduplicated in a temporary
    SQLite database and read back in small batches, so a very large sitemap set
    does not need to be held in an in-memory queue.
    """
    crawl_scopes = crawl_scopes_from_seeds(scope_seeds)
    sitemap_hosts = allowed_sitemap_hosts(scope_seeds, initial_sitemap_sources)
    page_queue: deque[tuple[str, int, str]] = deque()
    sitemap_queue: deque[tuple[str, int, str]] = deque()
    enqueued_pages: set[str] = set()
    enqueued_sitemaps: set[str] = set()
    visited: set[str] = set()
    discovered: set[str] = set()
    verified_200: set[str] = set()
    sitemap_stats = SitemapStats()
    robots = RobotsCache(
        enabled=respect_robots,
        timeout=timeout,
        insecure=insecure,
        insecure_ip_https=insecure_ip_https,
    )
    workspace = SitemapWorkspace(log_file)
    sitemap_store: Optional[SitemapURLStore] = None

    started_wall = local_timestamp()
    started_monotonic = time.monotonic()
    last_request_time = 0.0
    sitemap_url_limit_logged = False
    interrupted = False
    url_phase_started = False
    url_phase_start_monotonic = 0.0
    next_verbose_sitemap_url_report = VERBOSE_SITEMAP_URL_INTERVAL

    def verbose_sitemap(message: str, *, error: bool = False) -> None:
        """Show real-time sitemap progress when -v/--verbose is enabled."""
        if not verbose:
            return
        print(
            f"[sitemap] {message}",
            file=sys.stderr if error else sys.stdout,
            flush=True,
        )

    def wait_for_request_slot() -> None:
        nonlocal last_request_time
        elapsed_since_request = time.monotonic() - last_request_time
        if elapsed_since_request < delay:
            time.sleep(delay - elapsed_since_request)

    def request_finished() -> None:
        nonlocal last_request_time
        last_request_time = time.monotonic()

    def add_page(url: str, depth: int, source: str) -> bool:
        if not url_within_seed_scopes(url, crawl_scopes):
            return False
        if url in enqueued_pages:
            return False
        enqueued_pages.add(url)
        page_queue.append((url, depth, source))
        write_log_line(log_file, f"DISCOVERED: {url} source={source}")
        if not status_200_only and source != "seed":
            discovered.add(url)
        return True

    def add_sitemap(url: str, depth: int, source: str) -> bool:
        normalized = normalize_sitemap_source(url)
        if not normalized:
            return False
        if not sitemap_document_allowed(normalized, sitemap_hosts):
            write_log_line(log_file, f"SITEMAP OUT-OF-SCOPE: {normalized} source={source}")
            verbose_sitemap(f"ignored out-of-scope document: {normalized}")
            return False
        if normalized in enqueued_sitemaps:
            return False
        enqueued_sitemaps.add(normalized)
        sitemap_queue.append((normalized, depth, source))
        write_log_line(log_file, f"SITEMAP FOUND: {normalized} source={source}")
        verbose_sitemap(
            f"queued document: {normalized} (source={source}, depth={depth})"
        )
        return True

    def process_sitemap_phase(label: str) -> None:
        """
        Drain every currently known sitemap and nested sitemap index.

        URLs are inserted into the disk-backed store instead of page_queue.
        """
        nonlocal sitemap_url_limit_logged, next_verbose_sitemap_url_report

        if not sitemap_queue or sitemap_store is None:
            return

        phase_start_wall = local_timestamp()
        phase_start_monotonic = time.monotonic()
        documents_before = sitemap_stats.documents_parsed
        urls_before = sitemap_store.total_urls
        phase_interrupted = False

        write_log_line(
            log_file,
            (
                f"SITEMAP PHASE START: {label} time={phase_start_wall} "
                f"queued_documents={len(sitemap_queue)} stored_urls={sitemap_store.total_urls}"
            ),
            sync=True,
        )
        verbose_sitemap(
            f"phase start: {label}; {len(sitemap_queue)} document(s) queued, "
            f"{sitemap_store.total_urls:,} unique URL(s) stored"
        )

        try:
            while sitemap_queue:
                sitemap_url, sitemap_depth, sitemap_source = sitemap_queue.popleft()

                if sitemap_depth > max_sitemap_depth:
                    write_log_line(
                        log_file,
                        f"SITEMAP DEPTH LIMIT: {sitemap_url} depth={sitemap_depth}",
                    )
                    verbose_sitemap(
                        f"depth limit reached: {sitemap_url} (depth={sitemap_depth})",
                        error=True,
                    )
                    sitemap_stats.errors += 1
                    continue

                if sitemap_stats.documents_seen >= max_sitemap_documents:
                    write_log_line(
                        log_file,
                        f"SITEMAP DOCUMENT LIMIT: {max_sitemap_documents}",
                    )
                    verbose_sitemap(
                        f"document limit reached: {max_sitemap_documents:,}",
                        error=True,
                    )
                    sitemap_stats.errors += 1
                    sitemap_queue.clear()
                    continue

                sitemap_stats.documents_seen += 1
                wait_for_request_slot()
                write_log_line(log_file, f"SITEMAP FETCH: {sitemap_url}")
                verbose_sitemap(
                    f"fetching document {sitemap_stats.documents_seen:,}: {sitemap_url}"
                )

                download: Optional[SitemapDownload] = None
                parse_path: Optional[Path] = None
                accepted_here = 0
                seen_here = 0
                out_scope_here = 0
                filtered_here = 0
                children_here = 0

                try:
                    download = download_sitemap_to_disk(
                        sitemap_url,
                        workspace=workspace,
                        timeout=timeout,
                        insecure=insecure,
                        insecure_ip_https=insecure_ip_https,
                        max_bytes=max_sitemap_bytes,
                    )
                    request_finished()
                    write_log_line(
                        log_file,
                        (
                            f"SITEMAP DOWNLOADED: {download.final_url} "
                            f"bytes={download.byte_count} compressed={int(download.compressed)}"
                        ),
                    )
                    verbose_sitemap(
                        f"downloaded: {download.final_url} "
                        f"({download.byte_count:,} bytes, "
                        f"{'gzip-compressed' if download.compressed else 'uncompressed'})"
                    )
                    parse_path = prepare_sitemap_parse_file(
                        download,
                        workspace=workspace,
                        max_uncompressed_bytes=max_sitemap_uncompressed_bytes,
                    )

                    for kind, entry_url in iter_sitemap_entries(parse_path, download.final_url):
                        if kind == "sitemap":
                            children_here += 1
                            sitemap_stats.child_sitemaps += 1
                            add_sitemap(entry_url, sitemap_depth + 1, download.final_url)
                            continue

                        seen_here += 1
                        sitemap_stats.urls_seen += 1

                        if (
                            max_sitemap_urls
                            and sitemap_store.total_urls >= max_sitemap_urls
                        ):
                            if not sitemap_url_limit_logged:
                                write_log_line(
                                    log_file,
                                    f"SITEMAP URL LIMIT: {max_sitemap_urls}",
                                )
                                verbose_sitemap(
                                    f"URL import limit reached: {max_sitemap_urls:,}",
                                    error=True,
                                )
                                sitemap_url_limit_logged = True
                            sitemap_queue.clear()
                            break

                        if not url_within_seed_scopes(entry_url, crawl_scopes):
                            out_scope_here += 1
                            sitemap_stats.urls_out_of_scope += 1
                            continue

                        if sitemap_only and not matches_mode(entry_url, mode, extensions):
                            filtered_here += 1
                            sitemap_stats.urls_filtered += 1
                            continue

                        if sitemap_store.add(entry_url):
                            accepted_here += 1
                            sitemap_stats.urls_accepted += 1
                            write_log_line(log_file, f"SITEMAP URL: {entry_url}")

                            if (
                                verbose
                                and sitemap_store.total_urls
                                >= next_verbose_sitemap_url_report
                            ):
                                verbose_sitemap(
                                    f"stored {sitemap_store.total_urls:,} unique "
                                    "sitemap URL(s)"
                                )
                                while (
                                    next_verbose_sitemap_url_report
                                    <= sitemap_store.total_urls
                                ):
                                    next_verbose_sitemap_url_report += (
                                        VERBOSE_SITEMAP_URL_INTERVAL
                                    )

                    sitemap_store.commit()
                    sitemap_stats.documents_parsed += 1
                    write_log_line(
                        log_file,
                        (
                            f"SITEMAP PARSED: {download.final_url} urls={seen_here} "
                            f"accepted={accepted_here} out_of_scope={out_scope_here} "
                            f"filtered={filtered_here} child_sitemaps={children_here}"
                        ),
                    )
                    verbose_sitemap(
                        f"parsed: {download.final_url}; "
                        f"{seen_here:,} content URL(s), {accepted_here:,} new unique, "
                        f"{children_here:,} child sitemap(s), "
                        f"{out_scope_here:,} out of scope, {filtered_here:,} filtered"
                    )
                except SpiderError as exc:
                    request_finished()
                    sitemap_stats.errors += 1
                    write_log_line(log_file, f"SITEMAP ERROR: {sitemap_url} {exc}")
                    verbose_sitemap(
                        f"error processing {sitemap_url}: {exc}",
                        error=True,
                    )
                finally:
                    for candidate in (parse_path, download.path if download else None):
                        if candidate is None:
                            continue
                        try:
                            candidate.unlink()
                        except FileNotFoundError:
                            pass
                        except OSError as exc:
                            write_log_line(
                                log_file,
                                f"SITEMAP TEMP FILE CLEANUP ERROR: {candidate} {exc}",
                            )
        except KeyboardInterrupt:
            phase_interrupted = True
            raise
        finally:
            try:
                sitemap_store.commit()
            except SpiderError as exc:
                sitemap_stats.errors += 1
                write_log_line(log_file, f"SITEMAP DB ERROR: {exc}")

            phase_end_wall = local_timestamp()
            phase_elapsed = format_elapsed(time.monotonic() - phase_start_monotonic)
            status = " (interrupted)" if phase_interrupted else ""
            write_log_line(
                log_file,
                (
                    f"SITEMAP PHASE END: {label} time={phase_end_wall}{status} "
                    f"elapsed={phase_elapsed} "
                    f"documents={sitemap_stats.documents_parsed - documents_before} "
                    f"new_urls={sitemap_store.total_urls - urls_before} "
                    f"stored_urls={sitemap_store.total_urls} "
                    f"pending_checks={sitemap_store.pending_urls}"
                ),
                sync=True,
            )
            verbose_sitemap(
                f"phase {'interrupted' if phase_interrupted else 'complete'}: {label}; "
                f"{sitemap_stats.documents_parsed - documents_before:,} document(s), "
                f"{sitemap_store.total_urls - urls_before:,} new unique URL(s), "
                f"{sitemap_store.pending_urls:,} pending check(s), "
                f"elapsed {phase_elapsed}"
            )

    write_log_line(log_file, f"CRAWL START: {started_wall}", sync=True)
    write_log_line(
        log_file,
        (
            f"START version={VERSION} scope_seeds={len(scope_seeds)} "
            f"page_seeds={len(page_seeds)} sitemap_enabled={int(enable_sitemaps)} "
            f"sitemap_only={int(sitemap_only)} status_200_only={int(status_200_only)}"
        ),
    )

    try:
        if not sitemap_only:
            for seed in page_seeds:
                add_page(seed, 0, "seed")

        if enable_sitemaps:
            sitemap_store = SitemapURLStore(workspace, log_file)
            verbose_sitemap(f"temporary directory: {workspace.path}")
            verbose_sitemap(f"SQLite URL queue: {sitemap_store.path}")

            for source in initial_sitemap_sources:
                add_sitemap(source, 0, "explicit-or-seed")

            origins_seen: set[str] = set()
            for seed in scope_seeds:
                origin = RobotsCache.origin_for_url(seed)
                if origin in origins_seen:
                    continue
                origins_seen.add(origin)
                declared = robots.sitemaps_for_url(seed)
                if declared:
                    for sitemap_url in declared:
                        add_sitemap(sitemap_url, 0, "robots.txt")
                else:
                    add_sitemap(f"{origin}/sitemap.xml", 0, "default")

            # All known indexes and child sitemaps are parsed before the first
            # ordinary URL is checked.
            process_sitemap_phase("initial")

        if not interrupted:
            url_phase_started = True
            url_phase_start_monotonic = time.monotonic()
            write_log_line(
                log_file,
                (
                    f"URL CHECK PHASE START: time={local_timestamp()} "
                    f"sitemap_pending={sitemap_store.pending_urls if sitemap_store else 0} "
                    f"html_pending={len(page_queue)}"
                ),
                sync=True,
            )
            if sitemap_store is not None and sitemap_store.pending_urls:
                verbose_sitemap(
                    f"checking {sitemap_store.pending_urls:,} imported sitemap URL(s)"
                )

        while not interrupted:
            # A conventional sitemap link discovered in HTML starts a complete
            # additional sitemap phase before the next ordinary URL check.
            if sitemap_queue:
                process_sitemap_phase("html-discovered")
                continue

            url: Optional[str]
            depth: int
            source: str

            # Check disk-backed sitemap imports before broad HTML crawling.
            if sitemap_store is not None and sitemap_store.pending_urls:
                url = sitemap_store.next_url()
                if url is None:
                    continue
                depth = 0
                source = "sitemap"
            elif page_queue:
                url, depth, source = page_queue.popleft()
            else:
                break

            if url in visited:
                continue
            visited.add(url)

            if not url_within_seed_scopes(url, crawl_scopes):
                write_log_line(log_file, f"OUT-OF-SCOPE {url}")
                continue

            if not robots.allowed(url):
                write_log_line(log_file, f"ROBOTS-DENIED {url}")
                if verbose:
                    print(f"[robots] {url}")
                continue

            wait_for_request_slot()
            try:
                result = fetch_url(
                    url,
                    timeout=timeout,
                    insecure=insecure,
                    insecure_ip_https=insecure_ip_https,
                    max_bytes=max_page_bytes,
                    read_html_body=not sitemap_only,
                )
                request_finished()
            except SpiderError as exc:
                request_finished()
                write_log_line(log_file, f"ERROR {url} {exc}")
                if verbose:
                    eprint(f"[error] {url}: {exc}")
                continue

            write_log_line(
                log_file,
                f"URL: {result.final_url} HTTP/{result.status} source={source}",
            )
            if verbose:
                print(f"[{result.status}] {result.final_url}")

            if not url_within_seed_scopes(result.final_url, crawl_scopes):
                write_log_line(log_file, f"OUT-OF-SCOPE {url} -> {result.final_url}")
                if verbose:
                    print(f"[out-of-scope] {result.final_url}")
                continue

            if result.status == 200:
                verified_200.add(result.final_url)
            if not status_200_only or result.status == 200:
                discovered.add(result.final_url)

            if sitemap_only:
                continue
            if result.status != 200 or result.content_type not in PAGE_CONTENT_TYPES:
                continue
            if level is not None and depth >= level:
                continue

            for link in extract_links(result.final_url, result.body):
                if not url_within_seed_scopes(link, crawl_scopes):
                    continue

                if enable_sitemaps and looks_like_sitemap_url(link):
                    add_sitemap(link, 0, "html")
                    continue

                add_page(link, depth + 1, "html")

    except KeyboardInterrupt:
        interrupted = True
    finally:
        if url_phase_started:
            url_phase_elapsed = format_elapsed(
                time.monotonic() - url_phase_start_monotonic
            )
            status = " (interrupted)" if interrupted else ""
            write_log_line(
                log_file,
                (
                    f"URL CHECK PHASE END: time={local_timestamp()}{status} "
                    f"elapsed={url_phase_elapsed} visited={len(visited)} "
                    f"sitemap_pending={sitemap_store.pending_urls if sitemap_store else 0} "
                    f"html_pending={len(page_queue)}"
                ),
                sync=True,
            )

        workspace_path_before_cleanup = workspace.path
        if sitemap_store is not None:
            try:
                sitemap_store.close()
                verbose_sitemap(
                    f"closed SQLite URL queue: {sitemap_store.path}; "
                    f"{sitemap_store.total_urls:,} URL(s) stored"
                )
            except (SpiderError, sqlite3.Error) as exc:
                sitemap_stats.errors += 1
                write_log_line(log_file, f"SITEMAP DB CLOSE ERROR: {exc}", sync=True)
                verbose_sitemap(f"database close error: {exc}", error=True)
        workspace.cleanup()
        if workspace_path_before_cleanup is not None:
            verbose_sitemap(
                f"removed temporary sitemap storage: {workspace_path_before_cleanup}"
            )

    ended_wall = local_timestamp()
    total_elapsed = format_elapsed(time.monotonic() - started_monotonic)

    if interrupted:
        write_log_line(
            log_file,
            (
                f"INTERRUPTED visited={len(visited)} discovered={len(discovered)} "
                f"verified_200={len(verified_200)} html_queued={len(page_queue)} "
                f"sitemap_documents_queued={len(sitemap_queue)} "
                f"sitemap_urls_pending={sitemap_store.pending_urls if sitemap_store else 0}"
            ),
        )
        write_log_line(log_file, f"CRAWL END: {ended_wall} (interrupted)")
    else:
        write_log_line(
            log_file,
            (
                f"FINISHED visited={len(visited)} discovered={len(discovered)} "
                f"verified_200={len(verified_200)} html_queued=0 "
                f"sitemap_documents_queued=0 sitemap_urls_pending=0"
            ),
        )
        write_log_line(log_file, f"CRAWL END: {ended_wall}")

    write_log_line(
        log_file,
        (
            f"SITEMAP TOTALS: documents_seen={sitemap_stats.documents_seen} "
            f"documents_parsed={sitemap_stats.documents_parsed} "
            f"child_sitemaps={sitemap_stats.child_sitemaps} "
            f"urls_seen={sitemap_stats.urls_seen} "
            f"urls_accepted={sitemap_stats.urls_accepted} "
            f"urls_out_of_scope={sitemap_stats.urls_out_of_scope} "
            f"urls_filtered={sitemap_stats.urls_filtered} errors={sitemap_stats.errors}"
        ),
    )
    write_log_line(log_file, f"TOTAL CRAWL TIME: {total_elapsed}", sync=True)

    return CrawlOutcome(
        discovered=discovered,
        verified_200=verified_200,
        interrupted=interrupted,
        sitemap_stats=sitemap_stats,
    )


def parse_existing_log(log_path: Path, status_200_only: bool) -> set[str]:
    """
    Parse logs produced by this Python program or the older wget-based shell script.
    """
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise SpiderError(f"Could not read log file {log_path}: {exc}") from exc

    urls: set[str] = set()
    pending: Optional[str] = None

    for line in lines:
        # Incremental Python logs record links as soon as they are discovered.
        # They are recovery candidates unless --status-200 requires an observed
        # successful response.
        discovered_match = re.search(r"(?:DISCOVERED|SITEMAP URL):\s*(https?://\S+)", line)
        if discovered_match and not status_200_only:
            raw = discovered_match.group(1).rstrip("),;")
            normalized = canonical_output_url(raw)
            if normalized:
                urls.add(normalized)
            continue

        current_match = re.search(r"URL:\s*(https?://\S+)", line)
        if current_match:
            raw = current_match.group(1).rstrip("),;")
            normalized = canonical_output_url(raw)
            if not normalized:
                pending = None
                continue

            same_line_200 = bool(
                re.search(
                    r"HTTP(?:/[0-9.]+)?\s*[: ]\s*200\b|HTTP/[0-9.]+\s+200\b",
                    line,
                )
            )
            if status_200_only:
                if same_line_200:
                    urls.add(normalized)
                    pending = None
                else:
                    pending = normalized
            else:
                urls.add(normalized)
                pending = normalized
            continue

        if pending and re.search(r"HTTP/[0-9.]+\s+200\b|200\s+OK\b", line):
            urls.add(pending)
            pending = None

    return urls


def filter_urls(urls: Iterable[str], mode: str, extensions: set[str]) -> list[str]:
    normalized: set[str] = set()
    for url in urls:
        clean = canonical_output_url(url)
        if clean and matches_mode(clean, mode, extensions):
            normalized.add(clean)
    return sorted(normalized)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    temp.replace(path)


def chunked(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def xml_escape_url(url: str) -> str:
    # URL percent-encoding happens in canonical_output_url(). XML escaping is
    # a separate step and must happen afterwards.
    return html.escape(url, quote=True)


def sitemap_document(urls: Iterable[str]) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for url in urls:
        lines.append("  <url>")
        lines.append(f"    <loc>{xml_escape_url(url)}</loc>")
        lines.append("  </url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def sitemap_index_document(sitemap_urls: Iterable[str]) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for url in sitemap_urls:
        lines.append("  <sitemap>")
        lines.append(f"    <loc>{xml_escape_url(url)}</loc>")
        lines.append("  </sitemap>")
    lines.append("</sitemapindex>")
    return "\n".join(lines) + "\n"


def validate_xml_file(path: Path) -> None:
    try:
        ET.parse(path)
    except ET.ParseError as exc:
        raise SpiderError(f"Generated invalid XML in {path}: {exc}") from exc


def write_sitemaps(
    urls: list[str],
    *,
    output: Path,
    max_urls: int,
    public_base_url: Optional[str],
) -> list[Path]:
    if max_urls < 1 or max_urls > 50_000:
        raise SpiderError("--sitemap-max-urls must be between 1 and 50000")

    output.parent.mkdir(parents=True, exist_ok=True)
    pieces = list(chunked(urls, max_urls)) or [[]]
    written: list[Path] = []

    if len(pieces) == 1:
        atomic_write_text(output, sitemap_document(pieces[0]))
        validate_xml_file(output)
        return [output]

    stem = output.stem
    suffix = output.suffix or ".xml"
    child_paths: list[Path] = []

    for number, piece in enumerate(pieces, start=1):
        child = output.with_name(f"{stem}-{number}{suffix}")
        atomic_write_text(child, sitemap_document(piece))
        validate_xml_file(child)
        child_paths.append(child)
        written.append(child)

    index_path = output.with_name(f"{stem}-index{suffix}")
    if public_base_url:
        base = public_base_url.rstrip("/") + "/"
        child_urls = [
            normalize_url(
                urllib.parse.urljoin(base, child.name),
                strip_query=True,
                strip_fragment=True,
            )
            for child in child_paths
        ]
        if any(url is None for url in child_urls):
            raise SpiderError("Could not construct sitemap index URLs")
        index_urls = [url for url in child_urls if url is not None]
    else:
        # Relative sitemap locations are useful locally, but Google expects
        # absolute URLs. The README explains --sitemap-base-url.
        index_urls = [child.name for child in child_paths]

    atomic_write_text(index_path, sitemap_index_document(index_urls))
    validate_xml_file(index_path)
    written.insert(0, index_path)

    # Remove obsolete numbered files from prior larger runs.
    expected = {path.name for path in child_paths}
    numbered = re.compile(rf"^{re.escape(stem)}-\d+{re.escape(suffix)}$")
    for candidate in output.parent.glob(f"{stem}-*{suffix}"):
        if numbered.fullmatch(candidate.name) and candidate.name not in expected:
            candidate.unlink()

    return written




STATE_SCHEMA_VERSION = 3
DEFAULT_SITEMAP_MAX_AGE = "10m"
DEFAULT_RECHECK_OLDER_THAN = "0s"


@dataclass(frozen=True)
class HTTPStateResult:
    requested_url: str
    final_url: str
    status: int
    content_type: str
    content_length: Optional[int]
    etag: Optional[str]
    last_modified: Optional[str]
    body: bytes
    body_sha256: Optional[str]
    method: str
    not_modified: bool = False
    content_encoding: str = ""
    discovered_links: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConditionalSitemapDownload:
    result: HTTPStateResult
    path: Optional[Path]
    compressed: bool
    byte_count: int


def parse_duration(value: str) -> float:
    """Parse values such as 30s, 10m, 6h, 7d, or 2w into seconds."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*", value, re.I)
    if not match:
        raise SpiderError(
            f"Invalid duration {value!r}; use a number followed by s, m, h, d, or w"
        )
    number = float(match.group(1))
    unit = match.group(2).lower() or "s"
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return number * multiplier


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def timestamp_age_seconds(value: Optional[str]) -> Optional[float]:
    parsed = parse_iso_timestamp(value)
    if parsed is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def sanitize_state_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return cleaned[:48] or "crawl"


def default_state_path(scope_seeds: list[str], config: dict[str, object]) -> Path:
    host = "crawl"
    if scope_seeds:
        host = urllib.parse.urlsplit(scope_seeds[0]).hostname or host
    digest_source = json.dumps(config, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:10]
    return Path.cwd() / f".webspider-state-{sanitize_state_component(host)}-{digest}.sqlite3"


def archive_existing_state(path: Path) -> Optional[Path]:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    path.replace(backup)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            sidecar.replace(Path(str(backup) + suffix))
    return backup


class StateLock:
    """Simple cross-platform lock file protecting one persistent state DB."""

    def __init__(self, state_path: Path, *, force: bool = False) -> None:
        self.path = Path(str(state_path) + ".lock")
        self.acquired = False
        if force and self.path.exists():
            self.path.unlink()
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "started_at": utc_timestamp(),
                "hostname": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "unknown",
            },
            sort_keys=True,
        )
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            detail = ""
            try:
                detail = self.path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                pass
            raise SpiderError(
                f"State database is already locked: {state_path}\n"
                f"Lock file: {self.path}\n"
                + (f"Lock details: {detail}\n" if detail else "")
                + "Another Webspider process may be using it. If the previous process "
                  "crashed and no Webspider process is active, retry with --force-unlock."
            ) from exc
        try:
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        self.acquired = False
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class CrawlStateDB:
    """Persistent crawl history, queues, validators, and output source."""

    def __init__(self, path: Path, *, create: bool, force_unlock: bool = False) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not create and not self.path.is_file():
            raise SpiderError(f"Crawl state database not found: {self.path}")
        self.lock = StateLock(self.path, force=force_unlock)
        try:
            self.connection = sqlite3.connect(str(self.path), timeout=30)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=NORMAL")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA busy_timeout=30000")
            self._create_schema()
            self._migrate_schema()
            self._validate_schema()
            self.reset_in_progress()
            self.set_meta("active_process_id", str(os.getpid()))
            self.set_meta("active_started_at", utc_timestamp())
            self.set_meta("active_webspider_version", VERSION)
            self.commit()
        except BaseException:
            self.lock.release()
            raise

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                last_activity_at TEXT NOT NULL,
                resumed_count INTEGER NOT NULL DEFAULT 0,
                options_json TEXT NOT NULL,
                output_selection TEXT NOT NULL,
                note TEXT
            );

            CREATE TABLE IF NOT EXISTS resume_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                resumed_at TEXT NOT NULL,
                sitemap_refresh INTEGER NOT NULL,
                note TEXT
            );

            CREATE TABLE IF NOT EXISTS urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL DEFAULT 'unknown',
                source TEXT,
                first_seen_run INTEGER REFERENCES runs(id),
                last_seen_run INTEGER REFERENCES runs(id),
                first_seen_at TEXT,
                last_seen_at TEXT,
                last_checked_run INTEGER REFERENCES runs(id),
                last_checked_at TEXT,
                last_changed_run INTEGER REFERENCES runs(id),
                last_changed_at TEXT,
                gone_run INTEGER REFERENCES runs(id),
                previous_status INTEGER,
                status INTEGER,
                etag TEXT,
                last_modified TEXT,
                content_length INTEGER,
                content_type TEXT,
                final_url TEXT,
                body_sha256 TEXT,
                current_sitemap_listed INTEGER NOT NULL DEFAULT 0,
                html_depth INTEGER,
                external_depth INTEGER NOT NULL DEFAULT 0,
                check_state TEXT NOT NULL DEFAULT 'idle',
                check_run INTEGER REFERENCES runs(id),
                queue_priority INTEGER NOT NULL DEFAULT 50,
                last_method TEXT,
                successful_checks INTEGER NOT NULL DEFAULT 0,
                failed_checks INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS urls_check_queue
                ON urls(check_run, check_state, queue_priority, id);
            CREATE INDEX IF NOT EXISTS urls_run_changes
                ON urls(last_changed_run, first_seen_run, gone_run);

            CREATE TABLE IF NOT EXISTS discoveries (
                parent_url_id INTEGER REFERENCES urls(id) ON DELETE CASCADE,
                child_url_id INTEGER NOT NULL REFERENCES urls(id) ON DELETE CASCADE,
                source TEXT NOT NULL,
                first_seen_run INTEGER REFERENCES runs(id),
                last_seen_run INTEGER REFERENCES runs(id),
                PRIMARY KEY(parent_url_id, child_url_id, source)
            );

            CREATE TABLE IF NOT EXISTS sitemaps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                depth INTEGER NOT NULL DEFAULT 0,
                source TEXT,
                current_listed INTEGER NOT NULL DEFAULT 0,
                first_seen_run INTEGER REFERENCES runs(id),
                last_seen_run INTEGER REFERENCES runs(id),
                last_checked_run INTEGER REFERENCES runs(id),
                last_changed_run INTEGER REFERENCES runs(id),
                last_checked_at TEXT,
                last_changed_at TEXT,
                status INTEGER,
                etag TEXT,
                last_modified TEXT,
                content_length INTEGER,
                content_type TEXT,
                final_url TEXT,
                content_sha256 TEXT,
                parse_state TEXT NOT NULL DEFAULT 'idle',
                parse_run INTEGER REFERENCES runs(id),
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS sitemaps_queue
                ON sitemaps(parse_run, parse_state, depth, id);

            CREATE TABLE IF NOT EXISTS sitemap_children (
                parent_sitemap_id INTEGER NOT NULL REFERENCES sitemaps(id) ON DELETE CASCADE,
                child_sitemap_id INTEGER NOT NULL REFERENCES sitemaps(id) ON DELETE CASCADE,
                first_seen_run INTEGER REFERENCES runs(id),
                last_seen_run INTEGER REFERENCES runs(id),
                currently_listed INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(parent_sitemap_id, child_sitemap_id)
            );

            CREATE TABLE IF NOT EXISTS sitemap_membership (
                sitemap_id INTEGER NOT NULL REFERENCES sitemaps(id) ON DELETE CASCADE,
                url_id INTEGER NOT NULL REFERENCES urls(id) ON DELETE CASCADE,
                first_seen_run INTEGER REFERENCES runs(id),
                last_seen_run INTEGER REFERENCES runs(id),
                currently_listed INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(sitemap_id, url_id)
            );
            CREATE INDEX IF NOT EXISTS sitemap_membership_url
                ON sitemap_membership(url_id, currently_listed);

            CREATE TABLE IF NOT EXISTS robots (
                origin TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                status INTEGER,
                etag TEXT,
                last_modified TEXT,
                content_length INTEGER,
                body_sha256 TEXT,
                body_text TEXT,
                sitemap_urls_json TEXT NOT NULL DEFAULT '[]',
                crawl_delay_seconds REAL,
                request_rate_requests INTEGER,
                request_rate_seconds INTEGER,
                last_checked_run INTEGER REFERENCES runs(id),
                last_checked_at TEXT,
                last_changed_run INTEGER REFERENCES runs(id),
                error TEXT
            );
            """
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(STATE_SCHEMA_VERSION),),
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('created_by_version', ?)",
            (VERSION,),
        )
        self.connection.commit()

    def _migrate_schema(self) -> None:
        """Upgrade older persistent databases without discarding crawl history."""
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            return

        try:
            version = int(row[0])
        except (TypeError, ValueError):
            return

        if version < 2:
            url_columns = {
                str(item["name"])
                for item in self.connection.execute("PRAGMA table_info(urls)").fetchall()
            }
            if "external_depth" not in url_columns:
                self.connection.execute(
                    "ALTER TABLE urls ADD COLUMN external_depth INTEGER NOT NULL DEFAULT 0"
                )
            version = 2

        if version < 3:
            robots_columns = {
                str(item["name"])
                for item in self.connection.execute("PRAGMA table_info(robots)").fetchall()
            }
            additions = (
                ("crawl_delay_seconds", "REAL"),
                ("request_rate_requests", "INTEGER"),
                ("request_rate_seconds", "INTEGER"),
            )
            for name, column_type in additions:
                if name not in robots_columns:
                    self.connection.execute(
                        f"ALTER TABLE robots ADD COLUMN {name} {column_type}"
                    )
            version = 3

        self.connection.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'",
            (str(version),),
        )
        self.connection.execute(
            "INSERT INTO meta(key,value) VALUES('last_migrated_by_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (VERSION,),
        )
        self.connection.commit()

    def _validate_schema(self) -> None:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if row is None or int(row[0]) != STATE_SCHEMA_VERSION:
            found = "missing" if row is None else row[0]
            raise SpiderError(
                f"Unsupported crawl-state schema {found}; this Webspider expects "
                f"schema {STATE_SCHEMA_VERSION}. Preserve the database and use a "
                "compatible Webspider version or start a new state with --fresh."
            )

    def close(self) -> None:
        try:
            try:
                self.set_meta("active_process_id", "")
                self.set_meta("active_finished_at", utc_timestamp())
                self.connection.commit()
            finally:
                self.connection.close()
        finally:
            self.lock.release()

    def commit(self) -> None:
        self.connection.commit()

    def reset_in_progress(self) -> None:
        self.connection.execute(
            "UPDATE urls SET check_state='pending' WHERE check_state='processing'"
        )
        self.connection.execute(
            "UPDATE sitemaps SET parse_state='pending' WHERE parse_state='processing'"
        )
        self.connection.commit()

    def set_meta(self, key: str, value: object) -> None:
        encoded = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        self.connection.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, encoded),
        )

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return default if row is None else str(row[0])

    def set_config(self, config: dict[str, object]) -> None:
        self.set_meta("crawl_config", config)
        self.set_meta("webspider_version", VERSION)
        self.commit()

    def get_config(self) -> dict[str, object]:
        value = self.get_meta("crawl_config")
        if not value:
            raise SpiderError(f"State database has no crawl configuration: {self.path}")
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SpiderError("Stored crawl configuration is invalid JSON") from exc
        if not isinstance(loaded, dict):
            raise SpiderError("Stored crawl configuration has an invalid format")
        return loaded

    def create_run(self, kind: str, config: dict[str, object], output_selection: str) -> int:
        now = utc_timestamp()
        cursor = self.connection.execute(
            "INSERT INTO runs(kind,status,started_at,last_activity_at,options_json,output_selection) "
            "VALUES(?,?,?,?,?,?)",
            (kind, "running", now, now, json.dumps(config, sort_keys=True), output_selection),
        )
        run_id = int(cursor.lastrowid)
        self.set_meta("active_run_id", str(run_id))
        self.commit()
        return run_id

    def latest_run(self) -> Optional[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()

    def resume_run(self, *, refresh: bool, note: str = "") -> sqlite3.Row:
        row = self.latest_run()
        if row is None:
            raise SpiderError("The state database contains no crawl run to resume")
        if row["status"] == "completed":
            raise SpiderError(
                "The most recent crawl completed successfully. Use --recrawl to start "
                "a new conditional recheck, or --export-state to query stored results."
            )
        if row["status"] not in {"running", "interrupted", "failed"}:
            raise SpiderError(f"Cannot resume a run whose status is {row['status']!r}")
        now = utc_timestamp()
        self.connection.execute(
            "UPDATE runs SET status='running', last_activity_at=?, "
            "resumed_count=resumed_count+1, note=? WHERE id=?",
            (now, note, row["id"]),
        )
        self.connection.execute(
            "INSERT INTO resume_events(run_id,resumed_at,sitemap_refresh,note) VALUES(?,?,?,?)",
            (row["id"], now, int(refresh), note),
        )
        self.set_meta("active_run_id", str(row["id"]))
        self.commit()
        return self.connection.execute("SELECT * FROM runs WHERE id=?", (row["id"],)).fetchone()

    def touch_run(self, run_id: int) -> None:
        self.connection.execute(
            "UPDATE runs SET last_activity_at=? WHERE id=?", (utc_timestamp(), run_id)
        )

    def finish_run(self, run_id: int, status: str, note: str = "") -> None:
        now = utc_timestamp()
        self.connection.execute(
            "UPDATE runs SET status=?, ended_at=?, last_activity_at=?, note=? WHERE id=?",
            (status, now, now, note, run_id),
        )
        self.set_meta("active_run_id", "")
        self.commit()

    def run_row(self, run_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise SpiderError(f"Run {run_id} does not exist")
        return row

    def upsert_url(
        self,
        url: str,
        *,
        run_id: int,
        kind: str,
        source: str,
        depth: Optional[int] = None,
        external_depth: int = 0,
        sitemap_listed: bool = False,
    ) -> tuple[int, bool]:
        now = utc_timestamp()
        row = self.connection.execute(
            "SELECT id,kind,html_depth,external_depth FROM urls WHERE url=?",
            (url,),
        ).fetchone()
        if row is None:
            cursor = self.connection.execute(
                """
                INSERT INTO urls(
                    url,kind,source,first_seen_run,last_seen_run,first_seen_at,last_seen_at,
                    last_changed_run,last_changed_at,current_sitemap_listed,html_depth,
                    external_depth
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    url, kind, source, run_id, run_id, now, now, run_id, now,
                    int(sitemap_listed), depth, max(0, int(external_depth)),
                ),
            )
            return int(cursor.lastrowid), True
        url_id = int(row["id"])
        existing_depth = row["html_depth"]
        chosen_depth = depth
        if existing_depth is not None and (chosen_depth is None or existing_depth < chosen_depth):
            chosen_depth = int(existing_depth)
        existing_external_depth = int(row["external_depth"] or 0)
        chosen_external_depth = min(existing_external_depth, max(0, int(external_depth)))
        chosen_kind = kind if row["kind"] in {None, "unknown"} else row["kind"]
        self.connection.execute(
            """
            UPDATE urls SET kind=?, source=COALESCE(source,?), last_seen_run=?, last_seen_at=?,
                current_sitemap_listed=CASE WHEN ? THEN 1 ELSE current_sitemap_listed END,
                html_depth=COALESCE(?,html_depth), external_depth=?
            WHERE id=?
            """,
            (
                chosen_kind, source, run_id, now, int(sitemap_listed),
                chosen_depth, chosen_external_depth, url_id,
            ),
        )
        return url_id, False

    def add_discovery(
        self,
        parent_id: Optional[int],
        child_id: int,
        source: str,
        run_id: int,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO discoveries(parent_url_id,child_url_id,source,first_seen_run,last_seen_run)
            VALUES(?,?,?,?,?)
            ON CONFLICT(parent_url_id,child_url_id,source)
            DO UPDATE SET last_seen_run=excluded.last_seen_run
            """,
            (parent_id, child_id, source, run_id, run_id),
        )

    def schedule_url(self, url_id: int, run_id: int, *, priority: int) -> bool:
        """
        Queue a URL at most once per crawl run.

        Directory indexes commonly contain links back to themselves, including
        sort links such as ``?C=N;O=D`` that normalize to the same directory
        URL. Rediscovery must not turn a URL already completed in this run back
        into a pending URL.
        """
        cursor = self.connection.execute(
            """
            UPDATE urls SET check_state='pending', check_run=?, queue_priority=?, error=NULL
            WHERE id=? AND COALESCE(check_run,-1)<>?
            """,
            (run_id, priority, url_id, run_id),
        )
        return cursor.rowcount > 0

    def next_pending_url(self, run_id: int) -> Optional[sqlite3.Row]:
        row = self.connection.execute(
            """
            SELECT * FROM urls WHERE check_run=? AND check_state='pending'
            ORDER BY queue_priority,id LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        self.connection.execute(
            "UPDATE urls SET check_state='processing' WHERE id=?", (row["id"],)
        )
        self.commit()
        return row

    def pending_url_count(self, run_id: int) -> int:
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM urls WHERE check_run=? AND check_state='pending'",
            (run_id,),
        ).fetchone()[0])

    def mark_url_error(self, url_id: int, run_id: int, error: str) -> None:
        self.connection.execute(
            "UPDATE urls SET check_state='done',last_checked_run=?,last_checked_at=?,"
            "failed_checks=failed_checks+1,error=? WHERE id=?",
            (run_id, utc_timestamp(), error, url_id),
        )

    @staticmethod
    def _available(status: Optional[int]) -> bool:
        return status is not None and 200 <= int(status) < 300

    def apply_url_result(self, row: sqlite3.Row, result: HTTPStateResult, run_id: int) -> bool:
        now = utc_timestamp()
        old_status = row["status"]
        old_available = self._available(old_status)
        if result.not_modified:
            self.connection.execute(
                """
                UPDATE urls SET check_state='done',last_checked_run=?,last_checked_at=?,
                    last_method=?,successful_checks=successful_checks+1,error=NULL WHERE id=?
                """,
                (run_id, now, result.method, row["id"]),
            )
            return False

        new_status = result.status
        new_available = self._available(new_status)
        changed = False
        if old_status is None:
            changed = True
        elif int(old_status) != int(new_status):
            changed = True
        elif row["final_url"] and row["final_url"] != result.final_url:
            changed = True
        elif row["etag"] is not None and result.etag is not None and row["etag"] != result.etag:
            changed = True
        elif row["last_modified"] is not None and result.last_modified is not None and row["last_modified"] != result.last_modified:
            changed = True
        elif row["content_length"] is not None and result.content_length is not None and int(row["content_length"]) != int(result.content_length):
            changed = True
        elif row["content_type"] is not None and result.content_type and row["content_type"] != result.content_type:
            changed = True
        elif row["body_sha256"] is not None and result.body_sha256 is not None and row["body_sha256"] != result.body_sha256:
            changed = True

        gone_run = row["gone_run"]
        if old_available and not new_available:
            gone_run = run_id
            changed = True
        elif new_available:
            gone_run = None
            if not old_available and old_status is not None:
                changed = True

        self.connection.execute(
            """
            UPDATE urls SET
                check_state='done',last_checked_run=?,last_checked_at=?,
                last_changed_run=CASE WHEN ? THEN ? ELSE last_changed_run END,
                last_changed_at=CASE WHEN ? THEN ? ELSE last_changed_at END,
                gone_run=?,previous_status=?,status=?,etag=?,last_modified=?,
                content_length=?,content_type=?,final_url=?,body_sha256=COALESCE(?,body_sha256),
                last_method=?,
                successful_checks=successful_checks+CASE WHEN ? THEN 1 ELSE 0 END,
                failed_checks=failed_checks+CASE WHEN ? THEN 0 ELSE 1 END,
                error=NULL
            WHERE id=?
            """,
            (
                run_id, now, int(changed), run_id, int(changed), now,
                gone_run, old_status, new_status, result.etag, result.last_modified,
                result.content_length, result.content_type, result.final_url,
                result.body_sha256, result.method, int(new_available), int(new_available), row["id"],
            ),
        )
        return changed

    def upsert_sitemap(
        self,
        url: str,
        *,
        run_id: int,
        depth: int,
        source: str,
        current: bool = True,
        schedule: bool = True,
    ) -> tuple[int, bool, bool]:
        """
        Insert or update a sitemap and optionally schedule it once per run.

        Returns ``(sitemap_id, is_new, was_scheduled)``.
        """
        row = self.connection.execute(
            "SELECT id,depth,parse_run,parse_state FROM sitemaps WHERE url=?",
            (url,),
        ).fetchone()
        if row is None:
            cursor = self.connection.execute(
                """
                INSERT INTO sitemaps(
                    url,depth,source,current_listed,first_seen_run,last_seen_run,
                    parse_state,parse_run,last_changed_run,last_changed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    url, depth, source, int(current), run_id, run_id,
                    "pending" if schedule else "idle", run_id if schedule else None,
                    run_id, utc_timestamp(),
                ),
            )
            return int(cursor.lastrowid), True, bool(schedule)

        sitemap_id = int(row["id"])
        minimum_depth = min(int(row["depth"]), depth)
        was_scheduled = bool(schedule and row["parse_run"] != run_id)

        self.connection.execute(
            """
            UPDATE sitemaps SET depth=?,source=COALESCE(source,?),last_seen_run=?,
                current_listed=CASE WHEN ? THEN 1 ELSE current_listed END,
                parse_state=CASE WHEN ? THEN 'pending' ELSE parse_state END,
                parse_run=CASE WHEN ? THEN ? ELSE parse_run END,
                error=CASE WHEN ? THEN NULL ELSE error END
            WHERE id=?
            """,
            (
                minimum_depth,
                source,
                run_id,
                int(current),
                int(was_scheduled),
                int(was_scheduled),
                run_id,
                int(was_scheduled),
                sitemap_id,
            ),
        )
        return sitemap_id, False, was_scheduled

    def schedule_sitemap(self, sitemap_id: int, run_id: int) -> bool:
        """Schedule a known sitemap only if it has not been handled this run."""
        cursor = self.connection.execute(
            """
            UPDATE sitemaps
            SET parse_state='pending',parse_run=?,error=NULL
            WHERE id=? AND COALESCE(parse_run,-1)<>?
            """,
            (run_id, sitemap_id, run_id),
        )
        return cursor.rowcount > 0

    def next_pending_sitemap(self, run_id: int) -> Optional[sqlite3.Row]:
        row = self.connection.execute(
            """
            SELECT * FROM sitemaps WHERE parse_run=? AND parse_state='pending'
            ORDER BY depth,id LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        self.connection.execute(
            "UPDATE sitemaps SET parse_state='processing' WHERE id=?", (row["id"],)
        )
        self.commit()
        return row

    def pending_sitemap_count(self, run_id: int) -> int:
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM sitemaps WHERE parse_run=? AND parse_state='pending'",
            (run_id,),
        ).fetchone()[0])

    def begin_changed_sitemap_parse(self, sitemap_id: int) -> None:
        self.connection.execute(
            "UPDATE sitemap_children SET currently_listed=0 WHERE parent_sitemap_id=?",
            (sitemap_id,),
        )
        self.connection.execute(
            "UPDATE sitemap_membership SET currently_listed=0 WHERE sitemap_id=?",
            (sitemap_id,),
        )

    def add_sitemap_child(self, parent_id: int, child_id: int, run_id: int) -> None:
        self.connection.execute(
            """
            INSERT INTO sitemap_children(
                parent_sitemap_id,child_sitemap_id,first_seen_run,last_seen_run,currently_listed
            ) VALUES(?,?,?,?,1)
            ON CONFLICT(parent_sitemap_id,child_sitemap_id)
            DO UPDATE SET last_seen_run=excluded.last_seen_run,currently_listed=1
            """,
            (parent_id, child_id, run_id, run_id),
        )

    def existing_children(self, parent_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT s.* FROM sitemap_children c JOIN sitemaps s ON s.id=c.child_sitemap_id
            WHERE c.parent_sitemap_id=? AND c.currently_listed=1
            """,
            (parent_id,),
        ).fetchall()

    def add_sitemap_member(self, sitemap_id: int, url_id: int, run_id: int) -> None:
        self.connection.execute(
            """
            INSERT INTO sitemap_membership(
                sitemap_id,url_id,first_seen_run,last_seen_run,currently_listed
            ) VALUES(?,?,?,?,1)
            ON CONFLICT(sitemap_id,url_id)
            DO UPDATE SET last_seen_run=excluded.last_seen_run,currently_listed=1
            """,
            (sitemap_id, url_id, run_id, run_id),
        )

    def recompute_current_membership(self) -> None:
        self.connection.execute(
            """
            UPDATE urls SET current_sitemap_listed=CASE WHEN EXISTS(
                SELECT 1 FROM sitemap_membership m
                JOIN sitemaps s ON s.id=m.sitemap_id
                WHERE m.url_id=urls.id AND m.currently_listed=1 AND s.current_listed=1
            ) THEN 1 ELSE 0 END
            """
        )

    def apply_sitemap_result(
        self,
        row: sqlite3.Row,
        result: HTTPStateResult,
        run_id: int,
        *,
        content_sha256: Optional[str],
        error: Optional[str] = None,
    ) -> bool:
        now = utc_timestamp()
        if result.not_modified:
            self.connection.execute(
                """
                UPDATE sitemaps SET parse_state='done',last_checked_run=?,last_checked_at=?,
                    status=COALESCE(status,200),error=NULL WHERE id=?
                """,
                (run_id, now, row["id"]),
            )
            return False
        old_status = row["status"]
        changed = old_status is None or int(old_status) != int(result.status)
        if row["etag"] is not None and result.etag is not None and row["etag"] != result.etag:
            changed = True
        if row["last_modified"] is not None and result.last_modified is not None and row["last_modified"] != result.last_modified:
            changed = True
        if row["content_sha256"] is not None and content_sha256 is not None and row["content_sha256"] != content_sha256:
            changed = True
        self.connection.execute(
            """
            UPDATE sitemaps SET parse_state=?,last_checked_run=?,last_checked_at=?,
                last_changed_run=CASE WHEN ? THEN ? ELSE last_changed_run END,
                last_changed_at=CASE WHEN ? THEN ? ELSE last_changed_at END,
                status=?,etag=?,last_modified=?,content_length=?,content_type=?,final_url=?,
                content_sha256=COALESCE(?,content_sha256),error=? WHERE id=?
            """,
            (
                "error" if error else "done", run_id, now,
                int(changed), run_id, int(changed), now,
                result.status, result.etag, result.last_modified, result.content_length,
                result.content_type, result.final_url, content_sha256, error, row["id"],
            ),
        )
        return changed

    def mark_sitemap_error(self, sitemap_id: int, run_id: int, error: str) -> None:
        self.connection.execute(
            "UPDATE sitemaps SET parse_state='error',last_checked_run=?,last_checked_at=?,error=? WHERE id=?",
            (run_id, utc_timestamp(), error, sitemap_id),
        )

    def mark_all_sitemaps_not_current(self) -> None:
        self.connection.execute("UPDATE sitemaps SET current_listed=0")

    def schedule_known_urls_for_recrawl(
        self,
        run_id: int,
        *,
        mode: str,
        extensions: set[str],
        current_sitemap_only: bool,
        older_than_seconds: float,
    ) -> int:
        rows = self.connection.execute("SELECT id,url,kind,last_checked_at,current_sitemap_listed FROM urls").fetchall()
        scheduled = 0
        now = datetime.now(timezone.utc)
        for row in rows:
            url = str(row["url"])
            is_page = row["kind"] == "page" or looks_like_page_url(url)
            relevant = matches_mode(url, mode, extensions)
            if not (is_page or relevant):
                continue
            if current_sitemap_only and not is_page and not row["current_sitemap_listed"]:
                continue
            checked = parse_iso_timestamp(row["last_checked_at"])
            if older_than_seconds > 0 and checked is not None:
                if (now - checked).total_seconds() < older_than_seconds:
                    continue
            self.schedule_url(int(row["id"]), run_id, priority=10 if is_page else 50)
            scheduled += 1
        self.commit()
        return scheduled

    def query_urls(
        self,
        *,
        run_id: Optional[int],
        selection: str,
        mode: str,
        extensions: set[str],
        status_200_only: bool,
        current_sitemap_only: bool,
    ) -> list[str]:
        rows = self.connection.execute("SELECT * FROM urls ORDER BY url").fetchall()
        output: list[str] = []
        for row in rows:
            url = str(row["url"])
            if not matches_mode(url, mode, extensions):
                continue
            if current_sitemap_only and not row["current_sitemap_listed"]:
                continue
            status = row["status"]
            if selection == "all-known":
                if status is None:
                    continue
                if status_200_only and not (200 <= int(status) < 300):
                    continue
            elif selection == "changes-only":
                if run_id is None or row["last_changed_run"] != run_id:
                    continue
                if status is None or not (200 <= int(status) < 300):
                    continue
                if status_200_only and not (200 <= int(status) < 300):
                    continue
            elif selection == "new-only":
                if run_id is None or row["first_seen_run"] != run_id:
                    continue
                if status is None or not (200 <= int(status) < 300):
                    continue
                if status_200_only and not (200 <= int(status) < 300):
                    continue
            elif selection == "changed-only":
                if run_id is None or row["last_changed_run"] != run_id:
                    continue
                if row["first_seen_run"] == run_id:
                    continue
                if status is None or not (200 <= int(status) < 300):
                    continue
                if status_200_only and not (200 <= int(status) < 300):
                    continue
            elif selection == "gone-only":
                if run_id is None or row["gone_run"] != run_id:
                    continue
            else:
                raise SpiderError(f"Unknown output selection: {selection}")
            output.append(url)
        return output

    def verified_urls(self, *, mode: str, extensions: set[str], current_sitemap_only: bool) -> list[str]:
        rows = self.connection.execute(
            "SELECT url,status,current_sitemap_listed FROM urls WHERE status BETWEEN 200 AND 299 ORDER BY url"
        ).fetchall()
        return [
            str(row["url"])
            for row in rows
            if matches_mode(str(row["url"]), mode, extensions)
            and (not current_sitemap_only or row["current_sitemap_listed"])
        ]

    def summary(self) -> dict[str, object]:
        latest = self.latest_run()
        return {
            "path": str(self.path),
            "schema_version": STATE_SCHEMA_VERSION,
            "latest_run": dict(latest) if latest is not None else None,
            "urls": int(self.connection.execute("SELECT COUNT(*) FROM urls").fetchone()[0]),
            "available": int(self.connection.execute("SELECT COUNT(*) FROM urls WHERE status BETWEEN 200 AND 299").fetchone()[0]),
            "pending_urls": int(self.connection.execute("SELECT COUNT(*) FROM urls WHERE check_state='pending'").fetchone()[0]),
            "sitemaps": int(self.connection.execute("SELECT COUNT(*) FROM sitemaps").fetchone()[0]),
            "pending_sitemaps": int(self.connection.execute("SELECT COUNT(*) FROM sitemaps WHERE parse_state='pending'").fetchone()[0]),
        }


def classify_url_kind(url: str) -> str:
    return "page" if looks_like_page_url(url) else "file"


def looks_like_page_url(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path
    if path.endswith("/"):
        return True
    ext = path_extension(url)
    return ext == "" or ext in DEFAULT_EXTENSIONS["pages"]


def conditional_headers(row: sqlite3.Row) -> dict[str, str]:
    headers: dict[str, str] = {}
    if row["etag"]:
        headers["If-None-Match"] = str(row["etag"])
    if row["last_modified"]:
        headers["If-Modified-Since"] = str(row["last_modified"])
    return headers


def header_content_length(headers: object) -> Optional[int]:
    try:
        content_range = headers.get("Content-Range")
        if content_range:
            match = re.search(r"/(\d+)$", content_range)
            if match:
                return int(match.group(1))
        value = headers.get("Content-Length")
        return int(value) if value is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def make_http_state_result(
    url: str,
    response: object,
    *,
    body: bytes,
    method: str,
    not_modified: bool = False,
) -> HTTPStateResult:
    headers = response.headers
    final = normalize_url(
        response.geturl(), strip_query=False, strip_fragment=True
    ) or url
    status = int(getattr(response, "status", response.getcode()))
    content_type = headers.get_content_type().lower() if headers else ""
    body_hash = hashlib.sha256(body).hexdigest() if body else None
    return HTTPStateResult(
        requested_url=url,
        final_url=final,
        status=status,
        content_type=content_type,
        content_length=header_content_length(headers),
        etag=headers.get("ETag") if headers else None,
        last_modified=headers.get("Last-Modified") if headers else None,
        body=body,
        body_sha256=body_hash,
        method=method,
        not_modified=not_modified,
        content_encoding=headers.get("Content-Encoding", "") if headers else "",
    )


def request_with_state(
    url: str,
    *,
    method: str,
    validators: dict[str, str],
    timeout: float,
    insecure: bool,
    insecure_ip_https: bool,
    accept: str,
    max_body_bytes: int,
    read_body: bool,
    range_byte: bool = False,
) -> HTTPStateResult:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Accept-Encoding": "identity",
        **validators,
    }
    if range_byte:
        headers["Range"] = "bytes=0-0"
    result_method = f"{method} Range" if range_byte else method
    request = urllib.request.Request(url, headers=headers, method=method)
    context = ssl_context_for_url(url, insecure, insecure_ip_https)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            body = b""
            if read_body:
                body = response.read(max_body_bytes + 1)
                if len(body) > max_body_bytes:
                    body = body[:max_body_bytes]
            return make_http_state_result(url, response, body=body, method=result_method)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return make_http_state_result(
                url, exc, body=b"", method=result_method, not_modified=True
            )
        content_type = exc.headers.get_content_type().lower() if exc.headers else ""
        final = normalize_url(exc.geturl(), strip_query=False, strip_fragment=True) or url
        return HTTPStateResult(
            requested_url=url,
            final_url=final,
            status=int(exc.code),
            content_type=content_type,
            content_length=header_content_length(exc.headers),
            etag=exc.headers.get("ETag") if exc.headers else None,
            last_modified=exc.headers.get("Last-Modified") if exc.headers else None,
            body=b"",
            body_sha256=None,
            method=result_method,
            content_encoding=exc.headers.get("Content-Encoding", "") if exc.headers else "",
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SpiderError(str(exc)) from exc



def ftp_connection_details(url: str) -> tuple[str, int, str, str, str]:
    """Return host, port, user, password, and decoded path for an FTP URL."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "ftp" or not parts.hostname:
        raise SpiderError(f"Invalid FTP URL: {url}")
    try:
        port = parts.port or 21
    except ValueError as exc:
        raise SpiderError(f"Invalid FTP port in URL: {url}") from exc

    username = urllib.parse.unquote(parts.username or "anonymous")
    password = urllib.parse.unquote(
        parts.password or f"webspider/{VERSION}@"
    )
    path = urllib.parse.unquote(parts.path or "/")
    for value, label in ((username, "username"), (password, "password"), (path, "path")):
        if "\r" in value or "\n" in value:
            raise SpiderError(f"FTP {label} contains a forbidden newline")
    return parts.hostname, port, username, password, path


def open_ftp(url: str, timeout: float) -> tuple[ftplib.FTP, str]:
    host, port, username, password, path = ftp_connection_details(url)
    ftp = ftplib.FTP()
    try:
        ftp.connect(host, port, timeout=timeout)
        ftp.login(username, password)
        ftp.set_pasv(True)
        return ftp, path
    except Exception:
        try:
            ftp.close()
        except Exception:
            pass
        raise


def close_ftp(ftp: ftplib.FTP) -> None:
    try:
        ftp.quit()
    except Exception:
        try:
            ftp.close()
        except Exception:
            pass


def ftp_error_status(exc: BaseException) -> int:
    message = str(exc)
    try:
        code = int(message[:3])
    except (ValueError, TypeError):
        return 503
    if code == 530:
        return 401
    if code == 550:
        return 404
    if code in {421, 425, 426, 450, 451, 452}:
        return 503
    if 500 <= code < 600:
        return 403
    return 503


def parse_ftp_mlst_response(response: str) -> dict[str, str]:
    """Parse the fact line from a multiline MLST response."""
    for line in response.splitlines():
        stripped = line.strip()
        if ";" not in stripped:
            continue
        facts_text = stripped.split(" ", 1)[0]
        facts: dict[str, str] = {}
        for item in facts_text.split(";"):
            if not item or "=" not in item:
                continue
            key, value = item.split("=", 1)
            facts[key.lower()] = value
        if facts:
            return facts
    return {}


def ftp_mlst_facts(ftp: ftplib.FTP, path: str) -> dict[str, str]:
    try:
        return parse_ftp_mlst_response(ftp.sendcmd(f"MLST {path}"))
    except ftplib.all_errors:
        return {}


def ftp_file_metadata(
    ftp: ftplib.FTP,
    path: str,
    facts: Optional[dict[str, str]] = None,
) -> tuple[Optional[int], Optional[str], str]:
    facts = facts or {}
    size: Optional[int] = None
    modified: Optional[str] = facts.get("modify")
    method = "FTP MLST" if facts else "FTP SIZE"

    if facts.get("size"):
        try:
            size = int(facts["size"])
        except ValueError:
            size = None

    if size is None:
        try:
            value = ftp.size(path)
            size = int(value) if value is not None else None
        except ftplib.all_errors:
            pass

    if modified is None:
        try:
            response = ftp.sendcmd(f"MDTM {path}")
            if response.startswith("213 "):
                modified = response[4:].strip()
        except ftplib.all_errors:
            pass

    return size, modified, method


def ftp_child_url(parent_url: str, directory_path: str, name: str, is_dir: bool) -> Optional[str]:
    clean_name = name.rsplit("/", 1)[-1]
    if clean_name in {"", ".", ".."}:
        return None
    base = directory_path if directory_path.endswith("/") else f"{directory_path}/"
    child_path = posixpath.normpath(posixpath.join(base, clean_name))
    if not child_path.startswith("/"):
        child_path = f"/{child_path}"
    if is_dir and not child_path.endswith("/"):
        child_path += "/"
    parts = urllib.parse.urlsplit(parent_url)
    raw = urllib.parse.urlunsplit((parts.scheme, parts.netloc, child_path, "", ""))
    return normalize_url(raw, strip_query=True, strip_fragment=True)


def ftp_list_directory(
    ftp: ftplib.FTP,
    url: str,
    path: str,
    max_entries: int,
) -> tuple[list[str], str]:
    links: list[str] = []
    method = "FTP MLSD"

    try:
        iterator = ftp.mlsd(path)
        for name, facts in iterator:
            kind = str(facts.get("type", "")).lower()
            if kind in {"cdir", "pdir"}:
                continue
            child = ftp_child_url(url, path, name, kind == "dir")
            if child:
                links.append(child)
            if len(links) > max_entries:
                raise SpiderError(
                    f"FTP directory exceeds the {max_entries:,}-entry safety limit"
                )
    except ftplib.error_perm:
        method = "FTP NLST"
        current = None
        try:
            current = ftp.pwd()
        except ftplib.all_errors:
            pass
        try:
            ftp.cwd(path)
            names = ftp.nlst()
        finally:
            if current is not None:
                try:
                    ftp.cwd(current)
                except ftplib.all_errors:
                    pass

        for raw_name in names:
            name = raw_name.rstrip("/").rsplit("/", 1)[-1]
            if name in {"", ".", ".."}:
                continue
            candidate_path = posixpath.join(
                path if path.endswith("/") else f"{path}/",
                name,
            )
            facts = ftp_mlst_facts(ftp, candidate_path)
            is_dir = facts.get("type", "").lower() == "dir"
            if not facts:
                old = None
                try:
                    old = ftp.pwd()
                    ftp.cwd(candidate_path)
                    is_dir = True
                except ftplib.all_errors:
                    is_dir = False
                finally:
                    if old is not None:
                        try:
                            ftp.cwd(old)
                        except ftplib.all_errors:
                            pass
            child = ftp_child_url(url, path, name, is_dir)
            if child:
                links.append(child)
            if len(links) > max_entries:
                raise SpiderError(
                    f"FTP directory exceeds the {max_entries:,}-entry safety limit"
                )

    unique = sorted(set(links))
    return unique, method


def check_ftp_url(
    row: sqlite3.Row,
    *,
    timeout: float,
    max_ftp_entries: int,
) -> HTTPStateResult:
    """Validate an FTP file or list an FTP directory without downloading media."""
    url = str(row["url"])
    ftp: Optional[ftplib.FTP] = None
    try:
        ftp, path = open_ftp(url, timeout)
        facts = ftp_mlst_facts(ftp, path)
        fact_type = facts.get("type", "").lower()
        is_directory = path.endswith("/") or fact_type == "dir"

        if is_directory:
            final_url = url if url.endswith("/") else f"{url}/"
            final_url = normalize_url(final_url) or final_url
            links, method = ftp_list_directory(
                ftp,
                final_url,
                path,
                max_ftp_entries,
            )
            digest = hashlib.sha256(
                ("\n".join(links) + ("\n" if links else "")).encode("utf-8")
            ).hexdigest()
            return HTTPStateResult(
                requested_url=url,
                final_url=final_url,
                status=200,
                content_type="inode/directory",
                content_length=len(links),
                etag=None,
                last_modified=facts.get("modify"),
                body=b"",
                body_sha256=digest,
                method=method,
                discovered_links=tuple(links),
            )

        size, modified, method = ftp_file_metadata(ftp, path, facts)
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return HTTPStateResult(
            requested_url=url,
            final_url=url,
            status=200,
            content_type=content_type,
            content_length=size,
            etag=None,
            last_modified=modified,
            body=b"",
            body_sha256=None,
            method=method,
        )
    except ftplib.error_perm as exc:
        return HTTPStateResult(
            requested_url=url,
            final_url=url,
            status=ftp_error_status(exc),
            content_type="",
            content_length=None,
            etag=None,
            last_modified=None,
            body=b"",
            body_sha256=None,
            method="FTP",
        )
    except ftplib.all_errors as exc:
        raise SpiderError(f"FTP error: {exc}") from exc
    finally:
        if ftp is not None:
            close_ftp(ftp)


def fetch_ftp_robots(
    origin: str,
    *,
    old: Optional[sqlite3.Row],
    timeout: float,
    max_body_bytes: int = 2_000_000,
) -> HTTPStateResult:
    """
    Read /robots.txt from FTP.

    FTP has no standardized robots protocol, but Webspider honors a root
    robots.txt as a conservative extension when --follow-ftp is enabled.
    """
    robots_url = f"{origin}/robots.txt"
    ftp: Optional[ftplib.FTP] = None
    try:
        ftp, path = open_ftp(robots_url, timeout)
        facts = ftp_mlst_facts(ftp, path)
        size, modified, _ = ftp_file_metadata(ftp, path, facts)
        if (
            old is not None
            and old["body_text"]
            and old["content_length"] is not None
            and size is not None
            and int(old["content_length"]) == int(size)
            and old["last_modified"] is not None
            and modified is not None
            and str(old["last_modified"]) == str(modified)
        ):
            return HTTPStateResult(
                requested_url=robots_url,
                final_url=robots_url,
                status=304,
                content_type="text/plain",
                content_length=size,
                etag=None,
                last_modified=modified,
                body=b"",
                body_sha256=None,
                method="FTP SIZE/MDTM",
                not_modified=True,
            )

        chunks: list[bytes] = []
        total = 0

        def receive(chunk: bytes) -> None:
            nonlocal total
            total += len(chunk)
            if total > max_body_bytes:
                raise SpiderError(
                    f"FTP robots.txt exceeds the {max_body_bytes:,}-byte limit"
                )
            chunks.append(chunk)

        ftp.retrbinary(f"RETR {path}", receive)
        body = b"".join(chunks)
        return HTTPStateResult(
            requested_url=robots_url,
            final_url=robots_url,
            status=200,
            content_type="text/plain",
            content_length=size if size is not None else len(body),
            etag=None,
            last_modified=modified,
            body=body,
            body_sha256=hashlib.sha256(body).hexdigest(),
            method="FTP RETR",
        )
    except ftplib.error_perm as exc:
        return HTTPStateResult(
            requested_url=robots_url,
            final_url=robots_url,
            status=ftp_error_status(exc),
            content_type="text/plain",
            content_length=None,
            etag=None,
            last_modified=None,
            body=b"",
            body_sha256=None,
            method="FTP",
        )
    except ftplib.all_errors as exc:
        raise SpiderError(f"FTP robots error: {exc}") from exc
    finally:
        if ftp is not None:
            close_ftp(ftp)


def check_regular_url(
    row: sqlite3.Row,
    *,
    timeout: float,
    insecure: bool,
    insecure_ip_https: bool,
    max_page_bytes: int,
    max_ftp_entries: int,
    before_request,
    after_request,
) -> HTTPStateResult:
    url = str(row["url"])
    if urllib.parse.urlsplit(url).scheme.lower() == "ftp":
        before_request(url)
        try:
            return check_ftp_url(
                row,
                timeout=timeout,
                max_ftp_entries=max_ftp_entries,
            )
        finally:
            after_request(url)

    validators = conditional_headers(row)
    page = row["kind"] == "page" or looks_like_page_url(url)
    if page:
        before_request(url)
        try:
            return request_with_state(
                url,
                method="GET",
                validators=validators,
                timeout=timeout,
                insecure=insecure,
                insecure_ip_https=insecure_ip_https,
                accept="text/html,application/xhtml+xml,*/*;q=0.8",
                max_body_bytes=max_page_bytes,
                read_body=True,
            )
        finally:
            after_request(url)

    before_request(url)
    try:
        head = request_with_state(
            url,
            method="HEAD",
            validators=validators,
            timeout=timeout,
            insecure=insecure,
            insecure_ip_https=insecure_ip_https,
            accept="*/*",
            max_body_bytes=0,
            read_body=False,
        )
    finally:
        after_request(url)

    if head.status not in {403, 405, 501}:
        return head

    before_request(url)
    try:
        return request_with_state(
            url,
            method="GET",
            validators=validators,
            timeout=timeout,
            insecure=insecure,
            insecure_ip_https=insecure_ip_https,
            accept="*/*",
            max_body_bytes=1,
            read_body=True,
            range_byte=True,
        )
    finally:
        after_request(url)


def download_sitemap_conditional(
    row: sqlite3.Row,
    *,
    workspace: SitemapWorkspace,
    timeout: float,
    insecure: bool,
    insecure_ip_https: bool,
    max_bytes: int,
) -> ConditionalSitemapDownload:
    url = str(row["url"])
    validators = conditional_headers(row)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/xml,text/xml,text/plain,application/gzip,*/*;q=0.5",
        "Accept-Encoding": "identity",
        **validators,
    }
    request = urllib.request.Request(url, headers=headers, method="GET")
    context = ssl_context_for_url(url, insecure, insecure_ip_https)
    raw_path: Optional[Path] = None
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            status = int(getattr(response, "status", response.getcode()))
            final_url = normalize_url(
                response.geturl(), strip_query=False, strip_fragment=True
            ) or url
            content_type = response.headers.get_content_type().lower()
            content_encoding = response.headers.get("Content-Encoding", "").lower()
            compressed = (
                urllib.parse.urlsplit(final_url).path.lower().endswith(".gz")
                or "gzip" in content_type
                or "gzip" in content_encoding
            )
            raw_path = workspace.new_file(".gz" if compressed else ".download")
            total = 0
            digest = hashlib.sha256()
            with raw_path.open("wb") as target:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise SpiderError(
                            f"sitemap exceeds the {max_bytes}-byte download limit"
                        )
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            result = HTTPStateResult(
                requested_url=url,
                final_url=final_url,
                status=status,
                content_type=content_type,
                content_length=header_content_length(response.headers),
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
                body=b"",
                body_sha256=digest.hexdigest(),
                method="GET",
                content_encoding=content_encoding,
            )
            return ConditionalSitemapDownload(result, raw_path, compressed, total)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            result = make_http_state_result(
                url, exc, body=b"", method="GET", not_modified=True
            )
            return ConditionalSitemapDownload(result, None, False, 0)
        result = HTTPStateResult(
            requested_url=url,
            final_url=normalize_url(exc.geturl(), strip_query=False, strip_fragment=True) or url,
            status=int(exc.code),
            content_type=exc.headers.get_content_type().lower() if exc.headers else "",
            content_length=header_content_length(exc.headers),
            etag=exc.headers.get("ETag") if exc.headers else None,
            last_modified=exc.headers.get("Last-Modified") if exc.headers else None,
            body=b"",
            body_sha256=None,
            method="GET",
        )
        return ConditionalSitemapDownload(result, None, False, 0)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if raw_path is not None:
            try:
                raw_path.unlink()
            except FileNotFoundError:
                pass
        raise SpiderError(str(exc)) from exc


def extract_robots_sitemaps(origin: str, body: str) -> list[str]:
    found: set[str] = set()
    for line in body.splitlines():
        cleaned = line.split("#", 1)[0].strip()
        match = re.match(r"(?i)^sitemap\s*:\s*(\S+)\s*$", cleaned)
        if not match:
            continue
        normalized = normalize_url(
            match.group(1), base_url=f"{origin}/", strip_query=False, strip_fragment=True
        )
        if normalized and urllib.parse.urlsplit(normalized).scheme in {"http", "https"}:
            found.add(normalized)
    return sorted(found)


class PersistentRobotsPolicy:
    """
    Persistent robots policy for every original and external origin.

    Original seed origins may opt out with --no-robots. Every external
    HTTP, HTTPS, or FTP origin always enforces its own user-agent group,
    Allow/Disallow, Crawl-delay, and Request-rate. Sitemap declarations are
    retained separately by refresh_robots().
    """

    def __init__(
        self,
        state: CrawlStateDB,
        *,
        respect_original: bool,
        original_scopes: dict[str, set[str]],
        run_id: int,
        timeout: float,
        insecure: bool,
        insecure_ip_https: bool,
        log_file: TextIO,
        verbose: bool,
    ) -> None:
        self.state = state
        self.respect_original = respect_original
        self.original_scopes = original_scopes
        self.run_id = run_id
        self.timeout = timeout
        self.insecure = insecure
        self.insecure_ip_https = insecure_ip_https
        self.log_file = log_file
        self.verbose = verbose
        self.cache: dict[str, Optional[RobotsPolicyDocument]] = {}
        self.deny_all_origins: set[str] = set()
        self.last_request: dict[str, float] = {}
        self.request_times: dict[str, deque[float]] = {}

    @staticmethod
    def origin_for_url(url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    def is_original_origin(self, url: str) -> bool:
        """Return True when URL belongs to an original saved seed origin."""
        return url_host_is_seed_host(url, self.original_scopes)

    def robots_required(self, url: str) -> bool:
        """
        External origins always require robots enforcement.

        --no-robots only disables enforcement for the original seed origins.
        """
        return self.respect_original or not self.is_original_origin(url)

    def _ensure_loaded(self, url: str) -> str:
        origin = self.origin_for_url(url)
        if not self.robots_required(url):
            return origin
        if origin in self.cache or origin in self.deny_all_origins:
            return origin

        row = self.state.connection.execute(
            "SELECT * FROM robots WHERE origin=?",
            (origin,),
        ).fetchone()

        # Every origin, including an external one, is conditionally refreshed
        # once per crawl run before any URL on that origin is accessed.
        if row is None or row["last_checked_run"] != self.run_id:
            refresh_robots(
                self.state,
                origins=[origin],
                run_id=self.run_id,
                timeout=self.timeout,
                insecure=self.insecure,
                insecure_ip_https=self.insecure_ip_https,
                log_file=self.log_file,
                verbose=self.verbose,
            )
            # The robots request itself is the first request to this origin.
            # Count it when enforcing Crawl-delay and Request-rate before the
            # first content or sitemap request.
            checked_at = time.monotonic()
            self.last_request[origin] = checked_at
            self.request_times.setdefault(origin, deque()).append(checked_at)
            row = self.state.connection.execute(
                "SELECT * FROM robots WHERE origin=?",
                (origin,),
            ).fetchone()

        if row is None:
            # A network failure with no cached policy is treated conservatively.
            self.deny_all_origins.add(origin)
            write_log_line(
                self.log_file,
                f"ROBOTS UNAVAILABLE: {origin} denying crawl until policy can be read",
            )
            return origin

        status = int(row["status"] or 0)
        if status == 200 and row["body_text"]:
            parser = RobotsPolicyDocument(str(row["body_text"]))
            self.cache[origin] = parser
        elif status in {401, 403, 429} or status >= 500 or status == 0:
            # Preserve and enforce a previously cached body when available.
            if row["body_text"]:
                parser = RobotsPolicyDocument(str(row["body_text"]))
                self.cache[origin] = parser
            else:
                self.deny_all_origins.add(origin)
        else:
            # A missing robots.txt (for example 404/410) means no restrictions.
            self.cache[origin] = None

        parser = self.cache.get(origin)
        if self.verbose and origin not in self.deny_all_origins:
            crawl_delay = parser.crawl_delay(USER_AGENT) if parser else None
            request_rate = parser.request_rate(USER_AGENT) if parser else None
            details = []
            if crawl_delay is not None:
                details.append(f"crawl-delay={float(crawl_delay):g}s")
            if request_rate is not None:
                details.append(
                    f"request-rate={request_rate.requests}/{request_rate.seconds}s"
                )
            suffix = ", ".join(details) if details else "no rate directives"
            print(f"[robots] policy loaded for {origin}: {suffix}", flush=True)

        return origin

    def allowed(self, url: str) -> bool:
        if not self.robots_required(url):
            return True
        origin = self._ensure_loaded(url)
        if origin in self.deny_all_origins:
            return False
        parser = self.cache.get(origin)
        return True if parser is None else parser.can_fetch(USER_AGENT, url)

    def _crawl_delay(self, origin: str) -> float:
        if not self.robots_required(origin + "/"):
            return 0.0
        parser = self.cache.get(origin)
        value = parser.crawl_delay(USER_AGENT) if parser else None
        return max(0.0, float(value)) if value is not None else 0.0

    def _request_rate(self, origin: str) -> Optional[urllib.robotparser.RequestRate]:
        if not self.robots_required(origin + "/"):
            return None
        parser = self.cache.get(origin)
        return parser.request_rate(USER_AGENT) if parser else None

    def before_request(self, url: str, user_delay: float) -> None:
        """
        Wait until both the user delay and robots rate limits allow a request.

        Limits are maintained independently for every origin. The stricter of
        --delay, Crawl-delay, and Request-rate controls each request.
        """
        origin = self._ensure_loaded(url)
        now = time.monotonic()
        wait_seconds = 0.0

        minimum_gap = max(float(user_delay), self._crawl_delay(origin))
        previous = self.last_request.get(origin)
        if previous is not None:
            wait_seconds = max(wait_seconds, previous + minimum_gap - now)

        request_rate = self._request_rate(origin)
        if request_rate is not None and request_rate.requests > 0 and request_rate.seconds > 0:
            history = self.request_times.setdefault(origin, deque())
            cutoff = now - float(request_rate.seconds)
            while history and history[0] <= cutoff:
                history.popleft()
            if len(history) >= int(request_rate.requests):
                wait_seconds = max(
                    wait_seconds,
                    history[0] + float(request_rate.seconds) - now,
                )

        if wait_seconds > 0:
            write_log_line(
                self.log_file,
                f"ROBOTS WAIT: {origin} seconds={wait_seconds:.3f}",
            )
            if self.verbose:
                print(
                    f"[robots] waiting {wait_seconds:.3f}s for {origin}",
                    flush=True,
                )
            time.sleep(wait_seconds)

    def after_request(self, url: str) -> None:
        origin = self.origin_for_url(url)
        now = time.monotonic()
        self.last_request[origin] = now
        history = self.request_times.setdefault(origin, deque())
        history.append(now)


def refresh_robots(
    state: CrawlStateDB,
    *,
    origins: list[str],
    run_id: int,
    timeout: float,
    insecure: bool,
    insecure_ip_https: bool,
    log_file: TextIO,
    verbose: bool,
) -> list[str]:
    all_sitemaps: set[str] = set()
    for origin in origins:
        robots_url = f"{origin}/robots.txt"
        old = state.connection.execute("SELECT * FROM robots WHERE origin=?", (origin,)).fetchone()
        validators = {}
        if old is not None:
            if old["etag"]:
                validators["If-None-Match"] = str(old["etag"])
            if old["last_modified"]:
                validators["If-Modified-Since"] = str(old["last_modified"])
        if verbose:
            print(f"[robots] checking {robots_url}", flush=True)
        try:
            if urllib.parse.urlsplit(origin).scheme.lower() == "ftp":
                result = fetch_ftp_robots(
                    origin,
                    old=old,
                    timeout=timeout,
                )
            else:
                result = request_with_state(
                    robots_url,
                    method="GET",
                    validators=validators,
                    timeout=timeout,
                    insecure=insecure,
                    insecure_ip_https=insecure_ip_https,
                    accept="text/plain,*/*;q=0.5",
                    max_body_bytes=2_000_000,
                    read_body=True,
                )
        except SpiderError as exc:
            write_log_line(log_file, f"ROBOTS ERROR: {robots_url} {exc}")
            if old is not None:
                try:
                    all_sitemaps.update(json.loads(old["sitemap_urls_json"] or "[]"))
                except json.JSONDecodeError:
                    pass
            continue
        if result.not_modified and old is not None:
            body_text = str(old["body_text"] or "")
            sitemap_urls = json.loads(old["sitemap_urls_json"] or "[]")
            crawl_delay_seconds = old["crawl_delay_seconds"]
            request_rate_requests = old["request_rate_requests"]
            request_rate_seconds = old["request_rate_seconds"]
            changed = False
            protocol = urllib.parse.urlsplit(origin).scheme.upper()
            write_log_line(
                log_file,
                f"ROBOTS UNCHANGED: {robots_url} {protocol}/not-modified",
            )
        else:
            body_text = decode_html(result.body) if result.status == 200 else ""
            sitemap_urls = extract_robots_sitemaps(origin, body_text)
            parser = RobotsPolicyDocument(body_text)
            parsed_delay = parser.crawl_delay(USER_AGENT)
            parsed_rate = parser.request_rate(USER_AGENT)
            crawl_delay_seconds = (
                float(parsed_delay) if parsed_delay is not None else None
            )
            request_rate_requests = (
                int(parsed_rate.requests) if parsed_rate is not None else None
            )
            request_rate_seconds = (
                int(parsed_rate.seconds) if parsed_rate is not None else None
            )
            old_hash = old["body_sha256"] if old is not None else None
            changed = old is None or old_hash != result.body_sha256 or old["status"] != result.status
            write_log_line(
                log_file,
                f"ROBOTS CHECK: {robots_url} "
                f"{urllib.parse.urlsplit(origin).scheme.upper()}/{result.status} "
                f"crawl_delay={crawl_delay_seconds} "
                f"request_rate={request_rate_requests}/{request_rate_seconds}",
            )
        now = utc_timestamp()
        state.connection.execute(
            """
            INSERT INTO robots(
                origin,url,status,etag,last_modified,content_length,body_sha256,body_text,
                sitemap_urls_json,crawl_delay_seconds,request_rate_requests,
                request_rate_seconds,last_checked_run,last_checked_at,last_changed_run,error
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
            ON CONFLICT(origin) DO UPDATE SET
                url=excluded.url,
                status=CASE WHEN excluded.status=304 THEN robots.status ELSE excluded.status END,
                etag=COALESCE(excluded.etag,robots.etag),
                last_modified=COALESCE(excluded.last_modified,robots.last_modified),
                content_length=COALESCE(excluded.content_length,robots.content_length),
                body_sha256=COALESCE(excluded.body_sha256,robots.body_sha256),
                body_text=CASE WHEN excluded.status=304 THEN robots.body_text ELSE excluded.body_text END,
                sitemap_urls_json=CASE WHEN excluded.status=304 THEN robots.sitemap_urls_json ELSE excluded.sitemap_urls_json END,
                crawl_delay_seconds=excluded.crawl_delay_seconds,
                request_rate_requests=excluded.request_rate_requests,
                request_rate_seconds=excluded.request_rate_seconds,
                last_checked_run=excluded.last_checked_run,last_checked_at=excluded.last_checked_at,
                last_changed_run=CASE WHEN ? THEN excluded.last_checked_run ELSE robots.last_changed_run END,
                error=NULL
            """,
            (
                origin, robots_url, result.status, result.etag, result.last_modified,
                result.content_length, result.body_sha256, body_text,
                json.dumps(sitemap_urls), crawl_delay_seconds,
                request_rate_requests, request_rate_seconds,
                run_id, now,
                run_id if changed else (old["last_changed_run"] if old else run_id),
                int(changed),
            ),
        )
        all_sitemaps.update(sitemap_urls)
    state.commit()
    return sorted(all_sitemaps)


def origin_roots(scope_seeds: list[str]) -> list[str]:
    roots: set[str] = set()
    for url in scope_seeds:
        parts = urllib.parse.urlsplit(url)
        roots.add(urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", "")))
    return sorted(roots)


def schedule_initial_work(
    state: CrawlStateDB,
    config: dict[str, object],
    run_id: int,
    *,
    refresh_sitemaps: bool,
    schedule_page_seeds: bool,
    timeout: float,
    insecure: bool,
    insecure_ip_https: bool,
    log_file: TextIO,
    verbose: bool,
) -> None:
    scope_seeds = [str(v) for v in config["scope_seeds"]]
    page_seeds = [str(v) for v in config["page_seeds"]]
    explicit = [str(v) for v in config["sitemap_sources"]]
    enable_sitemaps = bool(config["enable_sitemaps"])
    sitemap_only = bool(config["sitemap_only"])

    if not sitemap_only and schedule_page_seeds:
        for seed in page_seeds:
            url_id, _ = state.upsert_url(
                seed, run_id=run_id, kind="page", source="seed", depth=0
            )
            state.schedule_url(url_id, run_id, priority=0)

    if not enable_sitemaps:
        state.commit()
        return

    if refresh_sitemaps:
        state.mark_all_sitemaps_not_current()
        declared = refresh_robots(
            state,
            origins=origin_roots(scope_seeds),
            run_id=run_id,
            timeout=timeout,
            insecure=insecure,
            insecure_ip_https=insecure_ip_https,
            log_file=log_file,
            verbose=verbose,
        )
        roots = set(explicit) | set(declared)
        if not declared:
            roots.update(
                f"{origin}/sitemap.xml"
                for origin in origin_roots(scope_seeds)
                if urllib.parse.urlsplit(origin).scheme in {"http", "https"}
            )
        for source in sorted(roots):
            state.upsert_sitemap(
                source, run_id=run_id, depth=0, source="root", current=True, schedule=True
            )
    state.commit()


def run_sitemap_phase(
    state: CrawlStateDB,
    workspace: SitemapWorkspace,
    *,
    run_id: int,
    config: dict[str, object],
    timeout: float,
    delay: float,
    insecure: bool,
    insecure_ip_https: bool,
    robots: PersistentRobotsPolicy,
    log_file: TextIO,
    verbose: bool,
) -> tuple[int, int]:
    scope_seeds = [str(v) for v in config["scope_seeds"]]
    scopes = crawl_scopes_from_seeds(scope_seeds)
    allowed_hosts = allowed_sitemap_hosts(scope_seeds, [str(v) for v in config["sitemap_sources"]])
    max_docs = int(config["max_sitemap_documents"])
    max_depth = int(config["max_sitemap_depth"])
    max_bytes = int(config["max_sitemap_mib"]) * 1024 * 1024
    max_urls = int(config["max_sitemap_urls"])
    mode = str(config["mode"])
    extensions = set(str(v) for v in config["extensions"])
    sitemap_only = bool(config["sitemap_only"])
    external_media = bool(config.get("external_media", False))
    follow_ftp = bool(config.get("follow_ftp", False))
    max_external_depth = int(config.get("external_depth", 0))
    level_value = config["level"]
    level = None if level_value is None else int(level_value)
    documents = 0
    imported = 0
    phase_started = time.monotonic()
    if state.pending_sitemap_count(run_id):
        write_log_line(log_file, f"SITEMAP PHASE START: run={run_id} pending={state.pending_sitemap_count(run_id)}", sync=True)
        if verbose:
            print(f"[sitemap] phase start: {state.pending_sitemap_count(run_id):,} document(s) pending", flush=True)
    while True:
        row = state.next_pending_sitemap(run_id)
        if row is None:
            break
        if documents >= max_docs:
            state.mark_sitemap_error(int(row["id"]), run_id, f"document limit {max_docs} reached")
            break
        if int(row["depth"]) > max_depth:
            state.mark_sitemap_error(int(row["id"]), run_id, f"depth limit {max_depth} reached")
            continue
        url = str(row["url"])
        documents += 1
        if not robots.allowed(url):
            state.mark_sitemap_error(int(row["id"]), run_id, "robots denied")
            write_log_line(log_file, f"ROBOTS-DENIED SITEMAP: {url}")
            if verbose:
                print(f"[robots] denied sitemap: {url}", flush=True)
            state.commit()
            continue

        write_log_line(log_file, f"SITEMAP FETCH: {url}")
        if verbose:
            print(f"[sitemap] fetching {documents:,}: {url}", flush=True)
        try:
            robots.before_request(url, delay)
            try:
                download = download_sitemap_conditional(
                    row,
                    workspace=workspace,
                    timeout=timeout,
                    insecure=insecure,
                    insecure_ip_https=insecure_ip_https,
                    max_bytes=max_bytes,
                )
            finally:
                robots.after_request(url)
        except SpiderError as exc:
            state.mark_sitemap_error(int(row["id"]), run_id, str(exc))
            write_log_line(log_file, f"SITEMAP ERROR: {url} {exc}")
            if verbose:
                eprint(f"[sitemap] error: {url}: {exc}")
            state.commit()
            continue
        result = download.result
        if result.not_modified:
            state.apply_sitemap_result(row, result, run_id, content_sha256=None)
            state.connection.execute("UPDATE sitemaps SET current_listed=1 WHERE id=?", (row["id"],))
            for child in state.existing_children(int(row["id"])):
                state.connection.execute("UPDATE sitemaps SET current_listed=1 WHERE id=?", (child["id"],))
                state.schedule_sitemap(int(child["id"]), run_id)
            write_log_line(log_file, f"SITEMAP UNCHANGED: {url} HTTP/304")
            if verbose:
                print(f"[sitemap] unchanged: {url} (HTTP 304)", flush=True)
            state.commit()
            continue
        if result.status != 200 or download.path is None:
            state.apply_sitemap_result(row, result, run_id, content_sha256=None, error=f"HTTP {result.status}")
            write_log_line(log_file, f"SITEMAP HTTP ERROR: {url} HTTP/{result.status}")
            state.commit()
            continue
        parse_path: Optional[Path] = None
        try:
            wrapped = SitemapDownload(
                requested_url=url,
                final_url=result.final_url,
                content_type=result.content_type,
                content_encoding=result.content_encoding,
                path=download.path,
                byte_count=download.byte_count,
                compressed=download.compressed,
            )
            parse_path = prepare_sitemap_parse_file(
                wrapped, workspace=workspace, max_uncompressed_bytes=max_bytes
            )
            state.begin_changed_sitemap_parse(int(row["id"]))
            seen = accepted = children = out_scope = filtered = 0
            for kind, entry in iter_sitemap_entries(parse_path, result.final_url):
                if kind == "sitemap":
                    child_host = urllib.parse.urlsplit(entry).hostname
                    if not child_host or child_host.lower() not in allowed_hosts:
                        out_scope += 1
                        continue
                    child_id, _, _ = state.upsert_sitemap(
                        entry,
                        run_id=run_id,
                        depth=int(row["depth"]) + 1,
                        source=url,
                        current=True,
                        schedule=True,
                    )
                    state.add_sitemap_child(int(row["id"]), child_id, run_id)
                    children += 1
                    continue
                seen += 1
                if max_urls and imported >= max_urls:
                    filtered += 1
                    continue
                kind_name = classify_url_kind(entry)
                entry_in_scope = url_within_seed_scopes(entry, scopes)
                entry_external_depth = 0
                if not entry_in_scope:
                    if url_host_is_seed_host(entry, scopes):
                        out_scope += 1
                        continue
                    entry_external_depth = 1
                    if not external_target_allowed(
                        url=entry,
                        kind=kind_name,
                        link_external_depth=entry_external_depth,
                        mode_matches=matches_mode(entry, mode, extensions),
                        external_media=external_media,
                        max_external_depth=max_external_depth,
                        follow_ftp=follow_ftp,
                    ):
                        filtered += 1
                        continue
                    if kind_name == "page" and sitemap_only:
                        filtered += 1
                        continue
                    write_log_line(
                        log_file,
                        f"SITEMAP EXTERNAL URL: {entry} depth={entry_external_depth}",
                    )
                url_id, is_new = state.upsert_url(
                    entry,
                    run_id=run_id,
                    kind=kind_name,
                    source=f"sitemap:{url}",
                    depth=0 if kind_name == "page" else None,
                    external_depth=entry_external_depth,
                    sitemap_listed=True,
                )
                state.add_sitemap_member(int(row["id"]), url_id, run_id)
                imported += int(is_new)
                accepted += int(is_new)
                should_schedule = matches_mode(entry, mode, extensions)
                if not sitemap_only and kind_name == "page":
                    depth = 0
                    if (
                        (
                            entry_external_depth == 0
                            or max_external_depth >= entry_external_depth
                            or (
                                urllib.parse.urlsplit(entry).scheme == "ftp"
                                and follow_ftp
                                and entry_external_depth <= max(1, max_external_depth)
                            )
                        )
                        and (level is None or depth <= level)
                    ):
                        should_schedule = True
                        state.connection.execute(
                            "UPDATE urls SET html_depth=COALESCE(html_depth,0) WHERE id=?",
                            (url_id,),
                        )
                    else:
                        should_schedule = False
                if should_schedule:
                    state.schedule_url(url_id, run_id, priority=10 if kind_name == "page" else 50)
                write_log_line(log_file, f"SITEMAP URL: {entry}")
            state.apply_sitemap_result(
                row, result, run_id, content_sha256=result.body_sha256
            )
            state.connection.execute("UPDATE sitemaps SET current_listed=1 WHERE id=?", (row["id"],))
            state.recompute_current_membership()
            state.commit()
            write_log_line(
                log_file,
                f"SITEMAP PARSED: {url} urls={seen} accepted={accepted} child_sitemaps={children} out_of_scope={out_scope} filtered={filtered}",
            )
            if verbose:
                print(
                    f"[sitemap] parsed: {url}; {seen:,} URL(s), {accepted:,} new, "
                    f"{children:,} child sitemap(s), {out_scope:,} out of scope, {filtered:,} filtered",
                    flush=True,
                )
        except SpiderError as exc:
            state.mark_sitemap_error(int(row["id"]), run_id, str(exc))
            write_log_line(log_file, f"SITEMAP PARSE ERROR: {url} {exc}")
            if verbose:
                eprint(f"[sitemap] parse error: {url}: {exc}")
            state.commit()
        finally:
            for candidate in {download.path, parse_path}:
                if candidate is not None:
                    try:
                        candidate.unlink()
                    except FileNotFoundError:
                        pass
    if documents:
        elapsed = format_elapsed(time.monotonic() - phase_started)
        write_log_line(log_file, f"SITEMAP PHASE END: run={run_id} documents={documents} new_urls={imported} elapsed={elapsed}", sync=True)
        if verbose:
            print(f"[sitemap] phase complete: {documents:,} document(s), {imported:,} new URL(s), elapsed {elapsed}", flush=True)
    return documents, imported


def run_url_phase(
    state: CrawlStateDB,
    workspace: SitemapWorkspace,
    *,
    run_id: int,
    config: dict[str, object],
    timeout: float,
    delay: float,
    insecure: bool,
    insecure_ip_https: bool,
    max_page_bytes: int,
    robots: PersistentRobotsPolicy,
    log_file: TextIO,
    verbose: bool,
) -> tuple[int, int]:
    scope_seeds = [str(v) for v in config["scope_seeds"]]
    scopes = crawl_scopes_from_seeds(scope_seeds)
    sitemap_hosts = allowed_sitemap_hosts(scope_seeds, [str(v) for v in config["sitemap_sources"]])
    enable_sitemaps = bool(config["enable_sitemaps"])
    sitemap_only = bool(config["sitemap_only"])
    external_media = bool(config.get("external_media", False))
    follow_ftp = bool(config.get("follow_ftp", False))
    max_external_depth = int(config.get("external_depth", 0))
    mode = str(config["mode"])
    extensions = set(str(v) for v in config["extensions"])
    level_value = config["level"]
    level = None if level_value is None else int(level_value)
    checked = changed = 0
    write_log_line(log_file, f"URL CHECK PHASE START: run={run_id} pending={state.pending_url_count(run_id)}", sync=True)
    while True:
        # A sitemap discovered from HTML takes priority before the next URL.
        if state.pending_sitemap_count(run_id):
            run_sitemap_phase(
                state,
                workspace,
                run_id=run_id,
                config=config,
                timeout=timeout,
                delay=delay,
                insecure=insecure,
                insecure_ip_https=insecure_ip_https,
                robots=robots,
                log_file=log_file,
                verbose=verbose,
            )
        row = state.next_pending_url(run_id)
        if row is None:
            break
        url = str(row["url"])
        url_scheme = urllib.parse.urlsplit(url).scheme.lower()
        row_external_depth = int(row["external_depth"] or 0)
        if url_scheme == "ftp" and not follow_ftp:
            state.mark_url_error(int(row["id"]), run_id, "FTP disabled; use --follow-ftp")
            write_log_line(log_file, f"FTP SKIPPED: {url} --follow-ftp not enabled")
            state.commit()
            continue
        if not url_within_seed_scopes(url, scopes):
            same_seed_host = url_host_is_seed_host(url, scopes)
            kind = str(row["kind"] or classify_url_kind(url))
            allowed_external = (
                not same_seed_host
                and row_external_depth > 0
                and external_target_allowed(
                    url=url,
                    kind=kind,
                    link_external_depth=row_external_depth,
                    mode_matches=matches_mode(url, mode, extensions),
                    external_media=external_media,
                    max_external_depth=max_external_depth,
                    follow_ftp=follow_ftp,
                )
            )
            if not allowed_external:
                state.mark_url_error(int(row["id"]), run_id, "out of scope")
                write_log_line(
                    log_file,
                    f"OUT-OF-SCOPE {url} external_depth={row_external_depth}",
                )
                state.commit()
                continue
        if not robots.allowed(url):
            state.mark_url_error(int(row["id"]), run_id, "robots denied")
            write_log_line(log_file, f"ROBOTS-DENIED {url}")
            state.commit()
            continue
        try:
            result = check_regular_url(
                row,
                timeout=timeout,
                insecure=insecure,
                insecure_ip_https=insecure_ip_https,
                max_page_bytes=max_page_bytes,
                max_ftp_entries=int(config.get("max_ftp_entries", DEFAULT_MAX_FTP_ENTRIES)),
                before_request=lambda request_url: robots.before_request(
                    request_url, delay
                ),
                after_request=robots.after_request,
            )
        except SpiderError as exc:
            state.mark_url_error(int(row["id"]), run_id, str(exc))
            write_log_line(log_file, f"ERROR {url} {exc}")
            if verbose:
                eprint(f"[error] {url}: {exc}")
            state.commit()
            continue
        was_changed = state.apply_url_result(row, result, run_id)
        checked += 1
        changed += int(was_changed)
        status_text = "304 unchanged" if result.not_modified else f"HTTP/{result.status}"
        write_log_line(log_file, f"URL: {result.final_url} {status_text} method={result.method}")
        if verbose:
            print(f"[{status_text}] {result.final_url} ({result.method})", flush=True)

        discovered_links: list[str] = []
        discovery_source = "html"
        if (
            not sitemap_only
            and not result.not_modified
            and result.status == 200
        ):
            if result.content_type in PAGE_CONTENT_TYPES and result.body:
                discovered_links = extract_links(result.final_url, result.body)
                discovery_source = "html"
            elif result.discovered_links:
                discovered_links = list(result.discovered_links)
                discovery_source = "ftp-listing"

        if discovered_links:
            parent_depth = row["html_depth"] if row["html_depth"] is not None else 0
            parent_external_depth = int(row["external_depth"] or 0)
            if level is None or int(parent_depth) < level:
                for link in discovered_links:
                    link_external_depth = external_depth_for_link(
                        result.final_url,
                        parent_external_depth,
                        link,
                        scopes,
                    )
                    if link_external_depth is None:
                        write_log_line(
                            log_file,
                            f"OUT-OF-SCOPE LINK: {link} source={result.final_url}",
                        )
                        continue
                    link_is_external = link_external_depth > 0
                    if (
                        enable_sitemaps
                        and urllib.parse.urlsplit(link).scheme in {"http", "https"}
                        and not link_is_external
                        and looks_like_sitemap_url(link)
                    ):
                        link_host = urllib.parse.urlsplit(link).hostname
                        if link_host and link_host.lower() in sitemap_hosts:
                            sitemap_id, is_new_sitemap, was_scheduled = state.upsert_sitemap(
                                link,
                                run_id=run_id,
                                depth=0,
                                source="html",
                                current=True,
                                schedule=True,
                            )
                            if was_scheduled:
                                write_log_line(
                                    log_file,
                                    f"SITEMAP FOUND: {link} source=html",
                                )
                            else:
                                write_log_line(
                                    log_file,
                                    f"SITEMAP ALREADY HANDLED: {link} source=html run={run_id}",
                                )
                                if verbose:
                                    print(
                                        f"[sitemap] already handled this run: {link}",
                                        flush=True,
                                    )
                            continue
                    kind = classify_url_kind(link)
                    mode_matches = matches_mode(link, mode, extensions)
                    if link_is_external and not external_target_allowed(
                        url=link,
                        kind=kind,
                        link_external_depth=link_external_depth,
                        mode_matches=mode_matches,
                        external_media=external_media,
                        max_external_depth=max_external_depth,
                        follow_ftp=follow_ftp,
                    ):
                        write_log_line(
                            log_file,
                            f"EXTERNAL SKIPPED: {link} depth={link_external_depth} "
                            f"source={result.final_url}",
                        )
                        continue

                    child_id, is_new = state.upsert_url(
                        link,
                        run_id=run_id,
                        kind=kind,
                        source=(
                            "ftp-listing"
                            if discovery_source == "ftp-listing"
                            else ("external-html" if link_is_external else "html")
                        ),
                        depth=int(parent_depth) + 1,
                        external_depth=link_external_depth,
                    )
                    state.add_discovery(
                        int(row["id"]),
                        child_id,
                        (
                            "ftp-listing"
                            if discovery_source == "ftp-listing"
                            else ("external-html" if link_is_external else "html")
                        ),
                        run_id,
                    )
                    if is_new:
                        prefix = "EXTERNAL DISCOVERED" if link_is_external else "DISCOVERED"
                        write_log_line(
                            log_file,
                            f"{prefix}: {link} depth={link_external_depth} "
                            f"source={discovery_source}",
                        )
                        if verbose and link_is_external:
                            print(
                                f"[external depth {link_external_depth}] discovered: {link}",
                                flush=True,
                            )
                    schedule = mode_matches
                    if kind == "page" and (level is None or int(parent_depth) + 1 <= level):
                        schedule = (
                            not link_is_external
                            or (
                                urllib.parse.urlsplit(link).scheme == "ftp"
                                and follow_ftp
                                and link_external_depth <= max(1, max_external_depth)
                            )
                            or max_external_depth >= link_external_depth
                        )
                    if schedule:
                        state.schedule_url(
                            child_id,
                            run_id,
                            priority=10 if kind == "page" else 50,
                        )
        state.touch_run(run_id)
        state.commit()
    elapsed = ""
    write_log_line(log_file, f"URL CHECK PHASE END: run={run_id} checked={checked} changed={changed}", sync=True)
    return checked, changed


def resolve_output_selection(args: argparse.Namespace, operation: str) -> str:
    explicitly = [
        name
        for name in ("all_known", "changes_only", "new_only", "changed_only", "gone_only")
        if getattr(args, name)
    ]
    if len(explicitly) > 1:
        raise SpiderError("Only one URL-output selection may be used at a time")
    if explicitly:
        return explicitly[0].replace("_", "-")
    return "changes-only" if operation == "recrawl" else "all-known"


def validate_conflicts(args: argparse.Namespace, provided: set[str]) -> None:
    operations = [
        name for name, value in (
            ("--resume", args.resume), ("--recrawl", args.recrawl),
            ("--export-state", args.export_state), ("--state-info", args.state_info),
            ("--delete-state", args.delete_state),
        ) if value is not None
    ]
    if len(operations) > 1:
        raise SpiderError(
            "Choose only one state operation: --resume, --recrawl, --export-state, "
            "--state-info, or --delete-state. They act on a database in different ways."
        )
    if args.fresh and (args.resume or args.recrawl or args.export_state or args.state_info or args.delete_state):
        raise SpiderError(
            "--fresh starts a brand-new crawl state and therefore cannot be combined "
            "with an operation that reads or resumes an existing state database."
        )
    if args.force_unlock and not (args.resume or args.recrawl or args.export_state or args.state_info or args.delete_state or args.state):
        raise SpiderError("--force-unlock requires a state database option")
    if args.sitemap_only and args.no_sitemaps:
        raise SpiderError(
            "--sitemap-only requires sitemap parsing, while --no-sitemaps disables it. "
            "Choose sitemap-only crawling or HTML-only crawling, not both."
        )
    if args.max_ftp_entries < 1:
        raise SpiderError("--max-ftp-entries must be at least 1")
    if args.external_depth < 0:
        raise SpiderError(
            "--external-depth must be zero or a positive integer. Zero keeps the "
            "crawl on the original seed hosts."
        )
    if args.sitemap_only and args.external_depth > 0:
        raise SpiderError(
            "--sitemap-only does not crawl HTML pages, so an external HTML crawl depth "
            "would never be used. Use --external-media to accept matching external "
            "media URLs from sitemaps, or remove --sitemap-only."
        )
    if args.external_media and args.mode == "pages":
        raise SpiderError(
            "--external-media applies to non-page files such as video, audio, and "
            "images. With --pages, use --external-depth N to permit external pages."
        )
    if args.no_sitemaps and args.sitemap_source:
        raise SpiderError(
            "--sitemap-source supplies a sitemap to parse, but --no-sitemaps disables all "
            "sitemap parsing. Remove one of these options."
        )
    if args.refresh_sitemaps and args.no_refresh_sitemaps:
        raise SpiderError("--refresh-sitemaps and --no-refresh-sitemaps are opposites and cannot be combined")
    if args.recheck_all and args.recheck_older_than:
        raise SpiderError("--recheck-all and --recheck-older-than choose different recrawl schedules; use only one")
    if (args.recheck_all or args.recheck_older_than) and not args.recrawl:
        raise SpiderError("--recheck-all and --recheck-older-than apply only to --recrawl")
    if (args.refresh_sitemaps or args.no_refresh_sitemaps) and not (args.resume or args.recrawl):
        raise SpiderError(
            "Sitemap refresh switches apply only when continuing an existing database "
            "with --resume or --recrawl. A new crawl always reads its sitemap roots."
        )
    if "--sitemap-max-age" in provided and not args.resume:
        raise SpiderError("--sitemap-max-age controls resume freshness and therefore requires --resume")
    if args.current_sitemap_only and args.no_sitemaps and not (args.export_state or args.state_info):
        raise SpiderError("--current-sitemap-only requires sitemap data, but --no-sitemaps disables sitemap processing")
    if args.update and any((args.inputs, args.state, args.resume, args.recrawl, args.export_state, args.state_info, args.delete_state)):
        raise SpiderError("--update only updates the program and cannot be combined with crawl or state-database operations")
    existing_operation = bool(args.resume or args.recrawl or args.export_state or args.state_info or args.delete_state)
    if existing_operation and args.state:
        raise SpiderError(
            "--state names a database for a new crawl, while the selected operation already "
            "names an existing database. Use only the operation's database argument."
        )
    if existing_operation and args.inputs:
        raise SpiderError(
            "Seed URLs cannot be supplied with an existing-state operation. The database "
            "already stores the original seeds and path scopes."
        )
    if existing_operation and args.sitemap_source:
        raise SpiderError(
            "--sitemap-source cannot be changed while resuming, recrawling, or exporting "
            "an existing state. Start a fresh state to change sitemap roots."
        )
    saved_crawl_flags = {
        "--sitemap-only", "--no-sitemaps", "--http", "--https", "--ext",
        "--level", "--no-robots", "--insecure", "--insecure-ip-https",
        "--max-sitemap-documents", "--max-sitemap-depth", "--max-sitemap-mib",
        "--max-sitemap-urls", "--video", "--audio", "--images", "--pages",
        "--files", "--all", "--external-media", "--external-depth",
        "--follow-ftp", "--max-ftp-entries",
    }
    if args.resume or args.recrawl:
        used = sorted(saved_crawl_flags & provided)
        if used:
            operation_name = "Resume" if args.resume else "Recrawl"
            raise SpiderError(
                f"{operation_name} uses the crawl definition saved in SQLite. These supplied "
                f"options would silently change that saved crawl: {', '.join(used)}. "
                "Start a new --state database to change scope or crawl mode."
            )
    if args.state_info:
        irrelevant = sorted((saved_crawl_flags | {"--all-known","--changes-only","--new-only","--changed-only","--gone-only"}) & provided)
        if irrelevant:
            raise SpiderError(
                "--state-info only displays database metadata; crawl and URL-selection "
                f"options do not apply: {', '.join(irrelevant)}"
            )
    if args.parse_only and any((
        args.sitemap_only, args.no_sitemaps, args.sitemap_source,
        args.refresh_sitemaps, args.no_refresh_sitemaps,
        args.recheck_all, args.recheck_older_than,
        args.external_media, args.external_depth, args.follow_ftp,
        "--max-ftp-entries" in provided,
    )):
        raise SpiderError(
            "--parse-only reads an old text log and performs no crawling, so sitemap and "
            "recrawl scheduling switches do not apply."
        )
    if args.resume and (args.recheck_all or args.recheck_older_than):
        raise SpiderError(
            "Resume continues the unfinished queue exactly as saved. Recheck scheduling "
            "belongs to --recrawl, which starts a new run."
        )
    if args.delete_state and any((args.sitemap_txt, args.sitemap_xml, args.out != Path("urls"))):
        raise SpiderError("--delete-state only removes a state database; URL and sitemap output options do not apply")
    if (args.export_state or args.state_info or args.delete_state) and any((
        args.refresh_sitemaps, args.no_refresh_sitemaps,
        args.recheck_all, args.recheck_older_than, args.fresh,
        args.external_media, args.external_depth, args.follow_ftp,
        "--max-ftp-entries" in provided,
    )):
        raise SpiderError(
            "This state operation performs no network crawl, so refresh, recheck, and "
            "fresh-start switches do not apply."
        )


class FullHelpArgumentParser(argparse.ArgumentParser):
    BASIC_USAGE = """\
Basic usage:
  webspider.py [OPTIONS] URL...
  webspider.py --resume STATE.sqlite3
  webspider.py --recrawl STATE.sqlite3 [--changes-only]
  webspider.py --export-state STATE.sqlite3 [OUTPUT OPTIONS]
  webspider.py --update

Every crawl uses a persistent SQLite state database in the current directory.
Use --help for the complete reference and conflict explanations.
"""

    def error(self, message: str) -> None:
        self._print_message(self.BASIC_USAGE, sys.stderr)
        self.exit(2, f"\n{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = FullHelpArgumentParser(
        prog="webspider.py",
        description=(
            "Cross-platform, standard-library-only website spider with persistent "
            "SQLite crawl history, resumable queues, conditional revalidation, "
            "sitemap discovery, and verified sitemap generation."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=r"""
STATE DATABASE AND RESUME
  Every new crawl writes a persistent SQLite database in the current directory.
  It is retained after completion, interruption, or failure. The database—not
  the text log—is the source used to generate URL lists.

  --state FILE
      Choose the database filename for a new crawl. Without this option,
      Webspider generates a stable hidden name from the site scope and options.

  --resume FILE
      Continue the latest interrupted or failed run. Pending sitemap documents,
      HTML pages, and file checks continue from SQLite.

  --recrawl FILE
      Start a new run using the saved seeds and options. Known HTML pages,
      sitemaps, and matching files are conditionally revalidated. The default
      URL output for a recrawl is --changes-only.

  --fresh
      Archive an existing state as a timestamped .bak file and start over.

  --export-state FILE
      Generate a URL list directly from the database without network access.

  --state-info FILE
      Display run, URL, sitemap, and pending-work counts.

  --delete-state FILE
      Delete the database and its WAL/SHM/lock sidecars. This is the only normal
      operation that deletes persistent crawl history.

  --force-unlock
      Remove a stale .lock file. Use only after confirming no other Webspider
      process is using the database.

DIRECTORY INDEX LOOP PROTECTION
  Auto-generated directory listings often expose sitemap files and links back
  to the directory itself, including sort links whose query string normalizes
  to the same URL. Webspider schedules each ordinary URL and each sitemap at
  most once per crawl run. Rediscovering an already handled item updates its
  history but does not place it back into the current run's queue.

SITEMAP REFRESH ON RESUME
  --sitemap-max-age DURATION
      Default: 10m. A resume newer than this continues immediately. An older
      resume conditionally refreshes robots.txt and known sitemap roots using
      stored ETag and Last-Modified validators.

  --refresh-sitemaps
      Force conditional sitemap refresh even for a very recent interruption.

  --no-refresh-sitemaps
      Continue pending work without revalidating already processed sitemaps.
      Sitemap documents that were still pending are always processed.

GENERAL CRAWL OPTIONS
  --video, --audio, --images, --pages, --files, --all
      Select the URL type. Video is the default.

  --ext PATTERN
      Replace the selected mode's extension list. Separate values with commas,
      spaces, or vertical bars.

  --level N|inf
      Maximum HTML-link depth. Sitemap-index depth is controlled separately.

  --delay SECONDS
      Minimum delay between network requests. Default: 0.5.

  --timeout SECONDS
      Per-request timeout. Default: 30.

  --status-200
      Limit ordinary URL output to successful 2xx checks. This includes HTTP
      206 returned by the one-byte Range fallback.

  --no-robots
      Ignore robots.txt crawl restrictions. Sitemap declarations are still read
      unless --no-sitemaps is also used.

SITEMAP LIMITS
  --max-sitemap-documents N   Maximum sitemap documents. Default: 10000.
  --max-sitemap-depth N       Maximum nested index depth. Default: 20.
  --max-sitemap-mib N         Download/decompression limit per sitemap. Default: 64.
  --max-sitemap-urls N        Imported sitemap URL limit. Default: 0 (unlimited).

CONDITIONAL FILE CHECKS
  HTML, robots.txt, and sitemap documents use conditional GET requests. A 304
  response avoids downloading and reparsing unchanged content.

  Media and other non-page files use conditional HEAD first. Servers returning
  403, 405, or 501 for HEAD are retried using conditional GET with
  Range: bytes=0-0. ETag, Last-Modified, Content-Length, Content-Type, redirects,
  status history, and body hashes where applicable are retained in SQLite.

RECRAWL SCHEDULING
  --recheck-all
      Revalidate every known page and matching file. This is the recrawl default.

  --recheck-older-than DURATION
      Revalidate only records whose last check is at least this old, such as 7d.

URL OUTPUT SELECTION
  --all-known
      All checked matching URLs in the database. Initial crawls and resume use
      this by default.

  --changes-only
      Matching URLs newly discovered, modified, or restored during this run.
      This is the default for --recrawl.

  --new-only
      Matching available URLs first discovered during this run.

  --changed-only
      Matching available URLs that existed before this run and changed now.

  --gone-only
      URLs that became unavailable during this run.

  --current-sitemap-only
      Limit output and non-page recrawl scheduling to URLs currently listed by
      a sitemap that is still reachable from the current sitemap roots.

EXTERNAL LINKS
  By default, Webspider stays on the original seed hosts and path scopes.

  --external-media
      Check matching external non-page links directly discovered on pages that
      Webspider is already allowed to crawl. External HTML pages are not opened
      solely because of this switch. This is useful when a local index page
      links directly to videos hosted by another site.

  --external-depth N
      Permit links after leaving the original seed hosts. The first off-site
      link is external depth 1. A link found on an external page at depth 1 is
      depth 2, and so on. External HTML pages and matching external files are
      scheduled only while they are within N.

      Examples:
        --external-depth 1
            Check direct external links and open directly linked external pages,
            but do not follow links found on those external pages.

        --external-depth 2
            Also follow one additional link from those external pages.

  --external-media and --external-depth may be combined. In that case, matching
  media found on any permitted crawled page is checked even when the media link
  itself would be one hop beyond the external HTML depth.

  Same-host links outside the original saved path boundary remain excluded.
  External crawling cannot be used to bypass the normal --no-parent behavior.

  ROBOTS RULES ON EVERY SITE
      Webspider conditionally fetches and caches robots.txt separately for each
      origin and follows the matching User-agent group, Allow/Disallow,
      Crawl-delay, and Request-rate directives.

      --no-robots disables those rules only for the original saved seed origins.
      Every external HTTP, HTTPS, or FTP origin always enforces its own robots
      policy. There is no switch that disables robots rules for external sites.

      Sitemap documents and the HEAD/Range validation fallback use the same
      per-origin rate limiter. The strictest of --delay, Crawl-delay, and
      Request-rate wins.

      For Allow and Disallow rules, the most specific matching path wins.
      Allow wins only when matching rules have equal specificity. Wildcards
      and a terminal $ end anchor are supported.

FTP LINKS
  FTP is disabled by default.

  --follow-ftp
      Follow ftp:// links found in permitted HTML pages, sitemaps, and FTP
      directory listings. Direct FTP files matching the selected mode are
      validated with metadata commands such as MLST, SIZE, and MDTM; media
      bodies are not downloaded merely to test existence.

      A directly linked FTP directory may be listed. Matching files in that
      listing are checked. Deeper FTP directory traversal uses --external-depth:
      a directly linked FTP directory is depth 1, its subdirectory is depth 2,
      and so on.

  --max-ftp-entries N
      Stop parsing one FTP directory after N entries. Default: 100000.

  FTP ROBOTS POLICY
      FTP has no standardized robots.txt protocol. As a conservative extension,
      Webspider checks /robots.txt at the FTP root and honors matching
      User-agent, Allow, Disallow, Crawl-delay, and Request-rate directives when
      that file exists. For an FTP origin reached externally, these rules are
      always enforced. --no-robots applies only when that FTP origin was one of
      the original seeds.

      Anonymous FTP is used when a URL supplies no username. FTP URLs containing
      embedded credentials are supported, but those credentials will also be
      present in the persistent state and logs.

CRAWL SOURCES
  Normal crawling follows HTML and reads sitemaps by default. --sitemap-only
  checks only matching sitemap-listed URLs. --no-sitemaps disables sitemap
  discovery. --sitemap-source URL may be repeated for nonstandard sitemap roots.

  The initial sitemap tree is completed before ordinary URL checking. Sitemap
  downloads use a unique temporary directory and are deleted after parsing;
  the persistent crawl database remains in the current directory.

LEGACY LOG IMPORT
  --parse-only remains available only for old pre-state Webspider logs. New URL
  output should use --export-state, --resume, or --recrawl.

CONFLICT EXAMPLES
  --resume cannot be combined with new seed URLs because the saved state already
  defines scope. --fresh cannot be combined with --resume because one discards
  old progress while the other continues it. Opposing selectors such as
  --refresh-sitemaps and --no-refresh-sitemaps are rejected with explanations.

EXAMPLES
  New video crawl with automatic state filename:
    webspider.py --video https://example.com/media/

  New crawl with a memorable database name:
    webspider.py --state media.sqlite3 --video https://example.com/media/

  Check direct external videos linked by a local index page:
    webspider.py --video --external-media https://example.com/index.html

  Crawl external HTML up to two off-site link hops:
    webspider.py --video --external-depth 2 https://example.com/index.html

  Follow direct FTP media and a linked FTP directory:
    webspider.py --video --follow-ftp https://example.com/index.html

  Follow nested FTP directories through external depth 3:
    webspider.py --video --follow-ftp --external-depth 3 https://example.com/

  Resume after Ctrl-C:
    webspider.py --resume media.sqlite3

  Repeat a month later and output only new/changed videos:
    webspider.py --recrawl media.sqlite3 --changes-only

  Force sitemap refresh during a recent resume:
    webspider.py --resume media.sqlite3 --refresh-sitemaps

  Recheck only records at least seven days old:
    webspider.py --recrawl media.sqlite3 --recheck-older-than 7d

  Export all known videos without contacting the site:
    webspider.py --export-state media.sqlite3 --all-known --out videos.txt

Project: https://github.com/Pryodon/Webspider
License: GNU Affero General Public License v3.0 or later
""",
    )
    parser.add_argument("inputs", nargs="*", metavar="SEED")
    scheme = parser.add_mutually_exclusive_group()
    scheme.add_argument("--http", action="store_const", dest="default_scheme", const="http")
    scheme.add_argument("--https", action="store_const", dest="default_scheme", const="https")
    parser.set_defaults(default_scheme="https")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--video", action="store_const", dest="mode", const="video")
    mode.add_argument("--audio", action="store_const", dest="mode", const="audio")
    mode.add_argument("--images", action="store_const", dest="mode", const="images")
    mode.add_argument("--pages", action="store_const", dest="mode", const="pages")
    mode.add_argument("--files", action="store_const", dest="mode", const="files")
    mode.add_argument("--all", action="store_const", dest="mode", const="all")
    parser.set_defaults(mode="video")
    parser.add_argument("--sitemap-only", action="store_true")
    parser.add_argument("--no-sitemaps", action="store_true")
    parser.add_argument("--sitemap-source", action="append", default=[], metavar="URL")
    parser.add_argument("--max-sitemap-documents", type=int, default=DEFAULT_MAX_SITEMAP_DOCUMENTS, metavar="N")
    parser.add_argument("--max-sitemap-depth", type=int, default=DEFAULT_MAX_SITEMAP_DEPTH, metavar="N")
    parser.add_argument("--max-sitemap-mib", type=int, default=DEFAULT_MAX_SITEMAP_MIB, metavar="N")
    parser.add_argument("--max-sitemap-urls", type=int, default=DEFAULT_MAX_SITEMAP_URLS, metavar="N")
    parser.add_argument("--ext", metavar="PATTERN")
    parser.add_argument("--delay", type=float, default=0.5, metavar="SECONDS")
    parser.add_argument("--level", default="inf", metavar="N|inf")
    parser.add_argument(
        "--external-media",
        action="store_true",
        help="Check matching external non-page links without crawling external HTML",
    )
    parser.add_argument(
        "--external-depth",
        type=int,
        default=0,
        metavar="N",
        help="Maximum link depth after leaving the original seed hosts (default: 0)",
    )
    parser.add_argument(
        "--follow-ftp",
        action="store_true",
        help="Follow and validate FTP links found in permitted documents",
    )
    parser.add_argument(
        "--max-ftp-entries",
        type=int,
        default=DEFAULT_MAX_FTP_ENTRIES,
        metavar="N",
        help="Maximum entries accepted from one FTP directory listing",
    )
    parser.add_argument("--timeout", type=float, default=30.0, metavar="SECONDS")
    parser.add_argument("--max-page-bytes", type=int, default=10_000_000, metavar="BYTES")
    parser.add_argument("--status-200", action="store_true")
    parser.add_argument("--no-robots", action="store_true")
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--insecure-ip-https", action="store_true")
    parser.add_argument("--state", type=Path, metavar="FILE")
    parser.add_argument("--resume", type=Path, metavar="FILE")
    parser.add_argument("--recrawl", type=Path, metavar="FILE")
    parser.add_argument("--export-state", type=Path, metavar="FILE")
    parser.add_argument("--state-info", type=Path, metavar="FILE")
    parser.add_argument("--delete-state", type=Path, metavar="FILE")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--force-unlock", action="store_true")
    parser.add_argument("--sitemap-max-age", default=DEFAULT_SITEMAP_MAX_AGE, metavar="DURATION")
    parser.add_argument("--refresh-sitemaps", action="store_true")
    parser.add_argument("--no-refresh-sitemaps", action="store_true")
    parser.add_argument("--recheck-all", action="store_true")
    parser.add_argument("--recheck-older-than", metavar="DURATION")
    parser.add_argument("--all-known", action="store_true")
    parser.add_argument("--changes-only", action="store_true")
    parser.add_argument("--new-only", action="store_true")
    parser.add_argument("--changed-only", action="store_true")
    parser.add_argument("--gone-only", action="store_true")
    parser.add_argument("--current-sitemap-only", action="store_true")
    parser.add_argument("--parse-only", action="store_true")
    parser.add_argument("--log", type=Path, default=Path("log"), metavar="FILE")
    parser.add_argument("--out", "-o", "--output", type=Path, default=Path("urls"), metavar="FILE")
    parser.add_argument("--sitemap-txt", action="store_true")
    parser.add_argument("--sitemap-xml", action="store_true")
    parser.add_argument("--sitemap-output", type=Path, default=Path("sitemap.xml"), metavar="FILE")
    parser.add_argument("--sitemap-max-urls", type=int, default=10_000, metavar="N")
    parser.add_argument("--sitemap-base-url", metavar="URL")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def provided_flags(argv: list[str]) -> set[str]:
    result: set[str] = set()
    for value in argv:
        if value.startswith("--"):
            result.add(value.split("=", 1)[0])
        elif value.startswith("-") and value != "-":
            if "v" in value[1:]:
                result.add("--verbose")
    return result


def parse_level(value: str) -> Optional[int]:
    if value.lower() == "inf":
        return None
    try:
        number = int(value)
    except ValueError as exc:
        raise SpiderError("--level must be a non-negative integer or inf") from exc
    if number < 0:
        raise SpiderError("--level must not be negative")
    return number


def config_from_new_args(args: argparse.Namespace) -> tuple[dict[str, object], Path]:
    raw_seeds = read_seed_arguments(args.inputs, args.default_scheme)
    explicit: list[str] = []
    for value in args.sitemap_source:
        normalized = normalize_sitemap_source(value, args.default_scheme)
        if (
            not normalized
            or urllib.parse.urlsplit(normalized).scheme not in {"http", "https"}
        ):
            raise SpiderError(
                f"Invalid sitemap source: {value}. Sitemap documents must use "
                "HTTP or HTTPS; FTP URLs may still be imported from sitemap entries "
                "when --follow-ftp is enabled."
            )
        explicit.append(normalized)
    ftp_seeds = [
        seed for seed in raw_seeds
        if urllib.parse.urlsplit(seed).scheme.lower() == "ftp"
    ]
    if ftp_seeds and not args.follow_ftp:
        raise SpiderError(
            "An FTP seed was supplied, but FTP crawling is disabled by default. "
            "Add --follow-ftp to crawl or validate FTP URLs."
        )
    enable_sitemaps = not args.no_sitemaps
    direct_sitemaps: list[str] = []
    page_seeds: list[str] = []
    for seed in raw_seeds:
        if (
            enable_sitemaps
            and urllib.parse.urlsplit(seed).scheme in {"http", "https"}
            and looks_like_sitemap_url(seed)
        ):
            direct_sitemaps.append(seed)
        else:
            page_seeds.append(seed)
    sitemap_sources = sorted(set(explicit + direct_sitemaps))
    scope_seeds = list(page_seeds)
    if not scope_seeds:
        for source in sitemap_sources:
            root = sitemap_origin_root(source)
            if root:
                scope_seeds.append(root)
    scope_seeds = sorted(set(scope_seeds))
    page_seeds = sorted(set(page_seeds))
    if not scope_seeds:
        raise SpiderError("Could not determine a valid crawl scope")
    level = parse_level(args.level)
    extensions = sorted(parse_extension_pattern(args.ext, args.mode))
    config: dict[str, object] = {
        "scope_seeds": scope_seeds,
        "page_seeds": page_seeds,
        "sitemap_sources": sitemap_sources,
        "enable_sitemaps": enable_sitemaps,
        "sitemap_only": bool(args.sitemap_only),
        "mode": args.mode,
        "extensions": extensions,
        "level": level,
        "external_media": bool(args.external_media),
        "external_depth": int(args.external_depth),
        "follow_ftp": bool(args.follow_ftp),
        "max_ftp_entries": int(args.max_ftp_entries),
        "respect_robots": not args.no_robots,
        "insecure": bool(args.insecure),
        "insecure_ip_https": bool(args.insecure_ip_https),
        "max_page_bytes": args.max_page_bytes,
        "max_sitemap_documents": args.max_sitemap_documents,
        "max_sitemap_depth": args.max_sitemap_depth,
        "max_sitemap_mib": args.max_sitemap_mib,
        "max_sitemap_urls": args.max_sitemap_urls,
    }
    state_path = args.state or default_state_path(scope_seeds, config)
    return config, state_path


def delete_state_files(path: Path, *, force_unlock: bool) -> None:
    absolute = path.resolve()
    lock_path = Path(str(absolute) + ".lock")
    if lock_path.exists() and not force_unlock:
        raise SpiderError(
            f"State lock exists: {lock_path}. Confirm no Webspider process is active, "
            "then use --force-unlock to delete it."
        )
    removed: list[Path] = []
    for candidate in (absolute, Path(str(absolute) + "-wal"), Path(str(absolute) + "-shm"), lock_path):
        try:
            candidate.unlink()
            removed.append(candidate)
        except FileNotFoundError:
            pass
    if not removed:
        raise SpiderError(f"No state database files were found for: {absolute}")
    for candidate in removed:
        print(f"[*] Deleted: {candidate}")


def write_outputs_from_state(
    state: CrawlStateDB,
    *,
    run_id: Optional[int],
    selection: str,
    mode: str,
    extensions: set[str],
    status_200_only: bool,
    current_sitemap_only: bool,
    out_path: Path,
    sitemap_txt: bool,
    sitemap_xml: bool,
    sitemap_output: Path,
    sitemap_max_urls: int,
    sitemap_base_url: Optional[str],
) -> int:
    urls = state.query_urls(
        run_id=run_id,
        selection=selection,
        mode=mode,
        extensions=extensions,
        status_200_only=status_200_only,
        current_sitemap_only=current_sitemap_only,
    )
    atomic_write_text(out_path, "\n".join(urls) + ("\n" if urls else ""))
    print(f"[*] Wrote {len(urls):,} {selection} URL(s): {out_path}")
    verified = state.verified_urls(
        mode=mode, extensions=extensions, current_sitemap_only=current_sitemap_only
    )
    if sitemap_txt:
        txt = out_path.parent / "sitemap.txt"
        atomic_write_text(txt, "\n".join(verified) + ("\n" if verified else ""))
        print(f"[*] Wrote verified text sitemap with {len(verified):,} URL(s): {txt}")
    if sitemap_xml:
        for path in write_sitemaps(
            verified,
            output=sitemap_output,
            max_urls=sitemap_max_urls,
            public_base_url=sitemap_base_url,
        ):
            print(f"[*] Wrote verified XML sitemap: {path}")
    return len(urls)


def main(argv: Optional[list[str]] = None) -> int:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(actual_argv)
    state: Optional[CrawlStateDB] = None
    workspace: Optional[SitemapWorkspace] = None
    log_file: Optional[TextIO] = None
    run_id: Optional[int] = None
    config: dict[str, object] = {}
    mode = "video"
    extensions: set[str] = set(DEFAULT_EXTENSIONS["video"])
    selection = "all-known"
    started = time.monotonic()
    try:
        validate_conflicts(args, provided_flags(actual_argv))
        if args.update:
            self_update(timeout=args.timeout)
            return 0
        if args.delete_state:
            delete_state_files(args.delete_state, force_unlock=args.force_unlock)
            return 0
        if args.delay < 0:
            raise SpiderError("--delay must not be negative")
        if args.external_depth < 0:
            raise SpiderError("--external-depth must not be negative")
        if args.max_ftp_entries < 1:
            raise SpiderError("--max-ftp-entries must be at least 1")
        if args.timeout <= 0:
            raise SpiderError("--timeout must be greater than zero")
        if args.max_page_bytes < 1:
            raise SpiderError("--max-page-bytes must be greater than zero")
        if args.sitemap_max_urls < 1 or args.sitemap_max_urls > 50_000:
            raise SpiderError("--sitemap-max-urls must be between 1 and 50000")
        parse_duration(args.sitemap_max_age)
        if args.recheck_older_than:
            parse_duration(args.recheck_older_than)

        if args.parse_only:
            if any((args.resume, args.recrawl, args.export_state, args.state_info, args.state)):
                raise SpiderError("--parse-only is a legacy log operation and cannot be combined with state-database options")
            if not args.log.is_file():
                raise SpiderError(f"Log file not found: {args.log}")
            extensions = parse_extension_pattern(args.ext, args.mode)
            found = filter_urls(parse_existing_log(args.log, args.status_200), args.mode, extensions)
            atomic_write_text(args.out, "\n".join(found) + ("\n" if found else ""))
            print(f"[*] Legacy log export wrote {len(found):,} URL(s): {args.out}")
            return 0

        operation = "new"
        state_path: Path
        if args.resume:
            operation, state_path = "resume", args.resume
        elif args.recrawl:
            operation, state_path = "recrawl", args.recrawl
        elif args.export_state:
            operation, state_path = "export", args.export_state
        elif args.state_info:
            operation, state_path = "info", args.state_info
        else:
            if not args.inputs and not args.sitemap_source:
                parser.error("a new crawl requires at least one seed URL or --sitemap-source")
            config, state_path = config_from_new_args(args)
            if state_path.exists():
                if not args.fresh:
                    raise SpiderError(
                        f"State database already exists: {state_path.resolve()}\n"
                        "Use --resume if its latest run was interrupted, --recrawl for a new "
                        "conditional run, --export-state to query it, or --fresh to archive it and start over."
                    )
                backup = archive_existing_state(state_path)
                print(f"[*] Archived previous state: {backup}")

        state = CrawlStateDB(state_path, create=(operation == "new"), force_unlock=args.force_unlock)
        if operation == "info":
            print(json.dumps(state.summary(), indent=2, sort_keys=True, default=str))
            return 0

        if operation in {"resume", "recrawl", "export"}:
            config = state.get_config()
        if operation == "export" and any(flag in provided_flags(actual_argv) for flag in {"--video","--audio","--images","--pages","--files","--all","--ext"}):
            mode = args.mode
            extensions = parse_extension_pattern(args.ext, mode)
        else:
            mode = str(config["mode"])
            extensions = set(str(v) for v in config["extensions"])
        selection = resolve_output_selection(args, operation)

        if operation == "export":
            latest = state.latest_run()
            export_run = int(latest["id"]) if latest is not None else None
            write_outputs_from_state(
                state,
                run_id=export_run,
                selection=selection,
                mode=mode,
                extensions=extensions,
                status_200_only=args.status_200,
                current_sitemap_only=args.current_sitemap_only,
                out_path=args.out,
                sitemap_txt=args.sitemap_txt,
                sitemap_xml=args.sitemap_xml,
                sitemap_output=args.sitemap_output,
                sitemap_max_urls=args.sitemap_max_urls,
                sitemap_base_url=args.sitemap_base_url,
            )
            return 0

        args.log.parent.mkdir(parents=True, exist_ok=True)
        log_file = args.log.open("a" if operation in {"resume", "recrawl"} else "w", encoding="utf-8", newline="\n", buffering=1)
        workspace = SitemapWorkspace(log_file)

        if operation == "new":
            state.set_config(config)
            run_id = state.create_run("initial", config, selection)
            refresh = bool(config["enable_sitemaps"])
        elif operation == "resume":
            latest = state.latest_run()
            if latest is None:
                raise SpiderError("No run exists to resume")
            age = timestamp_age_seconds(latest["last_activity_at"])
            max_age = parse_duration(args.sitemap_max_age)
            if args.refresh_sitemaps:
                refresh = True
                refresh_reason = "forced by --refresh-sitemaps"
            elif args.no_refresh_sitemaps:
                refresh = False
                refresh_reason = "disabled by --no-refresh-sitemaps"
            else:
                refresh = age is None or age > max_age
                refresh_reason = (
                    f"last activity {age:.0f}s ago exceeds {max_age:.0f}s" if refresh and age is not None
                    else f"last activity is within {max_age:.0f}s freshness window"
                )
            resumed = state.resume_run(refresh=refresh, note=refresh_reason)
            run_id = int(resumed["id"])
            write_log_line(log_file, f"RESUME: run={run_id} sitemap_refresh={int(refresh)} reason={refresh_reason}", sync=True)
        else:
            run_id = state.create_run("recrawl", config, selection)
            refresh = not args.no_refresh_sitemaps
            if args.refresh_sitemaps:
                refresh = True
            older = parse_duration(args.recheck_older_than) if args.recheck_older_than else 0.0
            scheduled = state.schedule_known_urls_for_recrawl(
                run_id,
                mode=mode,
                extensions=extensions,
                current_sitemap_only=args.current_sitemap_only,
                older_than_seconds=older,
            )
            write_log_line(log_file, f"RECRAWL SCHEDULED: run={run_id} urls={scheduled} older_than={older}", sync=True)

        assert run_id is not None
        write_log_line(log_file, f"CRAWL START: {local_timestamp()}", sync=True)
        write_log_line(log_file, f"STATE DB: {state.path}")
        print(f"[*] Crawl state: {state.path}")
        print(f"[*] Run ID: {run_id} ({operation})")
        print(f"[*] URL output selection: {selection}")
        print(f"[*] Incremental log: {args.log}")
        print(f"[*] Resume later with: {parser.prog} --resume {state.path}")

        schedule_initial_work(
            state,
            config,
            run_id,
            refresh_sitemaps=refresh,
            schedule_page_seeds=(operation == "new"),
            timeout=args.timeout,
            insecure=bool(config["insecure"]),
            insecure_ip_https=bool(config["insecure_ip_https"]),
            log_file=log_file,
            verbose=args.verbose,
        )
        robots_policy = PersistentRobotsPolicy(
            state,
            respect_original=bool(config["respect_robots"]),
            original_scopes=crawl_scopes_from_seeds(
                [str(value) for value in config["scope_seeds"]]
            ),
            run_id=run_id,
            timeout=args.timeout,
            insecure=bool(config["insecure"]),
            insecure_ip_https=bool(config["insecure_ip_https"]),
            log_file=log_file,
            verbose=args.verbose,
        )
        run_sitemap_phase(
            state,
            workspace,
            run_id=run_id,
            config=config,
            timeout=args.timeout,
            delay=args.delay,
            insecure=bool(config["insecure"]),
            insecure_ip_https=bool(config["insecure_ip_https"]),
            robots=robots_policy,
            log_file=log_file,
            verbose=args.verbose,
        )
        checked, changed = run_url_phase(
            state,
            workspace,
            run_id=run_id,
            config=config,
            timeout=args.timeout,
            delay=args.delay,
            insecure=bool(config["insecure"]),
            insecure_ip_https=bool(config["insecure_ip_https"]),
            max_page_bytes=int(config["max_page_bytes"]),
            robots=robots_policy,
            log_file=log_file,
            verbose=args.verbose,
        )
        state.recompute_current_membership()
        state.finish_run(run_id, "completed", f"checked={checked} changed={changed}")
        elapsed = format_elapsed(time.monotonic() - started)
        write_log_line(log_file, f"CRAWL END: {local_timestamp()}")
        write_log_line(log_file, f"TOTAL CRAWL TIME: {elapsed}", sync=True)
        write_outputs_from_state(
            state,
            run_id=run_id,
            selection=selection,
            mode=mode,
            extensions=extensions,
            status_200_only=args.status_200,
            current_sitemap_only=args.current_sitemap_only,
            out_path=args.out,
            sitemap_txt=args.sitemap_txt,
            sitemap_xml=args.sitemap_xml,
            sitemap_output=args.sitemap_output,
            sitemap_max_urls=args.sitemap_max_urls,
            sitemap_base_url=args.sitemap_base_url,
        )
        print(f"[*] Persistent state retained: {state.path}")
        print("[*] Done.")
        return 0
    except KeyboardInterrupt:
        if state is not None and run_id is not None:
            try:
                state.finish_run(run_id, "interrupted", "Ctrl-C")
            except Exception:
                pass
        if log_file is not None:
            try:
                write_log_line(log_file, f"CRAWL END: {local_timestamp()} (interrupted)")
                write_log_line(log_file, f"TOTAL CRAWL TIME: {format_elapsed(time.monotonic() - started)}", sync=True)
            except Exception:
                pass
        if state is not None and run_id is not None:
            try:
                write_outputs_from_state(
                    state,
                    run_id=run_id,
                    selection=selection,
                    mode=mode,
                    extensions=extensions,
                    status_200_only=args.status_200,
                    current_sitemap_only=args.current_sitemap_only,
                    out_path=args.out,
                    sitemap_txt=False,
                    sitemap_xml=False,
                    sitemap_output=args.sitemap_output,
                    sitemap_max_urls=args.sitemap_max_urls,
                    sitemap_base_url=args.sitemap_base_url,
                )
            except Exception as output_exc:
                eprint(f"[!] Could not write partial URL output: {output_exc}")
        eprint("\n[!] Crawl interrupted. Persistent queues and validators were retained.")
        if state is not None:
            eprint(f"[!] Resume with: webspider.py --resume {state.path}")
        return 130
    except SpiderError as exc:
        if state is not None and run_id is not None:
            try:
                state.finish_run(run_id, "failed", str(exc))
            except Exception:
                pass
        eprint(f"[ERROR] {exc}")
        return 2
    finally:
        if workspace is not None:
            workspace.cleanup()
        if log_file is not None:
            log_file.close()
        if state is not None:
            state.close()


if __name__ == "__main__":
    raise SystemExit(main())
