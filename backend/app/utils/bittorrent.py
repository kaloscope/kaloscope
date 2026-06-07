import base64
import contextlib
import re
from dataclasses import dataclass

import httpx
from sanic import Sanic
from sanic.log import logger
from torrentool.api import Torrent
from torrentool.bencode import Bencode


@dataclass
class MagnetLink:
    """The magnet link information."""

    link: str
    info_hash: str | None = None
    info_hash_v2: str | None = None


async def standardize_magnet(link: str) -> MagnetLink | None:
    """Standardize a magnet link.

    Args:
        link: The magnet link string, or an HTTP/HTTPS URL to a torrent file.

    Returns:
        The standardized magnet link object if successful, None otherwise.
    """
    link = link.strip()
    if not link:
        return None
    # HTTP/HTTPS link
    if link.startswith(("http://", "https://")):
        return await _http_to_magnet(link)
    # Magnet link
    if link.startswith("magnet:"):
        hash = get_magnet_hash(link)
        hash_v2 = get_magnet_hash_v2(link)
        if not hash and not hash_v2:
            return None
        return MagnetLink(link, hash, hash_v2)
    # BitTorrent info hash v2 (BTMH)
    if is_info_hash_v2(link):
        hash = None
        hash_v2 = link
        return MagnetLink(f"magnet:?xt=urn:btmh:{hash_v2}", hash, hash_v2)
    # BitTorrent info hash (BTIH)
    link = base32_to_sha1(link)
    if is_info_hash(link):
        hash = link
        hash_v2 = None
        return MagnetLink(f"magnet:?xt=urn:btih:{hash}", hash, hash_v2)


async def _http_to_magnet(url: str) -> MagnetLink | None:
    """Download a torrent file from an HTTP/HTTPS URL and convert to a magnet link.

    Args:
        url: The HTTP/HTTPS URL to the torrent file.

    Returns:
        The magnet link object if successful, None otherwise.
    """
    try:
        client: httpx.AsyncClient = Sanic.get_app().ctx.httpx
        # send a HEAD request to check the content type
        head_response = await client.head(url)
        content_type = head_response.headers.get("content-type", "")
        if "application/x-bittorrent" not in content_type:
            return None
        # download the torrent file
        response = await client.get(url)
        torrent_bytes = response.content
        # decode and convert to magnet link
        torrent = decode_torrent(torrent_bytes)
        if torrent is None:
            return None
        return MagnetLink(
            link=torrent.magnet_link,
            info_hash=torrent.info_hash,
        )
    except Exception:
        logger.error("Failed to convert HTTP link to magnet: %s", url, exc_info=True)
        return None


def is_info_hash(hash: str) -> bool:
    """Check if a string is a valid info hash.

    Args:
        hash: The string to check.

    Returns:
        True if the string is a valid info hash, False otherwise.
    """
    # support the 32 character base32 encoded info-hash
    # https://bittorrent.org/beps/bep_0009.html
    hash = base32_to_sha1(hash)
    return re.match(r"^[0-9a-fA-F]{40}$", hash) is not None


def is_info_hash_v2(hash: str) -> bool:
    """Check if a string is a valid info hash v2.

    Args:
        hash: The string to check.

    Returns:
        True if the string is a valid info hash v2, False otherwise.
    """
    return re.match(r"^1220[0-9a-fA-F]{64}$", hash) is not None


def get_magnet_hash(link: str) -> str | None:
    """Extract the info hash from a magnet link.

    Args:
        link: The magnet link.

    Returns:
        The info hash if found, None otherwise.
    """
    match = re.search(r"urn:btih:([0-9a-fA-F]{40}|[0-9a-zA-Z]{32})(?=&|$)", link)
    return base32_to_sha1(match.group(1)) if match else None


def get_magnet_hash_v2(link: str) -> str | None:
    """Extract the info hash v2 from a magnet link.

    Args:
        link: The magnet link.

    Returns:
        The info hash v2 if found, None otherwise.
    """
    match = re.search(r"urn:btmh:(1220[0-9a-fA-F]{64})(?=&|$)", link)
    return match.group(1) if match else None


def base32_to_sha1(hash: str) -> str:
    """Converts a Base32 hash (from a magnet link) to a SHA1 hash.

    Args:
        hash: The Base32 encoded hash string.

    Returns:
        The SHA1 hash string, or the original hash if conversion fails.
    """
    if len(hash) == 32:
        with contextlib.suppress(Exception):
            hash = base64.b32decode(hash, casefold=True).hex()
    return hash


def decode_torrent(torrent: bytes) -> Torrent | None:
    """Decode a torrent file.

    Args:
        torrent: The torrent file content.

    Returns:
        The decoded torrent object if successful, None otherwise.
    """
    try:
        decoded = Bencode.read_string(torrent, byte_keys={"pieces"})
        if isinstance(decoded, dict):
            return Torrent(decoded)
    except Exception:
        logger.error("Failed to decode the torrent file!", exc_info=True)
    return None


def get_torrent_hash(torrent: bytes) -> str | None:
    """Extract the info hash from a torrent file.

    Args:
        torrent: The torrent file content.

    Returns:
        The info hash if found, None otherwise.
    """
    decoded = decode_torrent(torrent)
    return decoded.info_hash if decoded else None
