"""Utilities for getting data from TrackerNetwork's public API."""
import aiocurl
import backoff
import cloudscraper
import json
import logging
import requests

from io import BytesIO
from email.parser import BytesParser


logger = logging.getLogger(__name__)


class Non200Exception(Exception):
    """Exception raised when a tracker network http request gives a non-200 response."""

    def __init__(self, status_code, response_headers=None):
        """Initialize the Non200Exception, settings response headers."""
        self.status_code = status_code
        self.response_headers = response_headers or {}


def get_mmr_history_uri_by_id(tracker_player_id):
    """Get the uri at which mmr history for the given player can be found."""
    return f"https://api.tracker.gg/api/v1/rocket-league/player-history/mmr/{tracker_player_id}"


def get_profile_uri_for_player(player):
    """Get the uri that should be used to retrieve mmr info from the given player dictionary."""
    suffix = get_profile_suffix_for_player(player)
    return f"https://api.tracker.gg/api/v2/rocket-league/standard/profile/{suffix}"


def get_profile_suffix_for_player(player):
    """Get the suffix to use to generate a players tracker network profile uri."""
    platform = player["id"]["platform"]

    if platform == "steam":
        return f"steam/{player['id']['id']}"

    try:
        player_name = player["name"]
    except KeyError:
        return None

    space_replaced = player_name.replace(" ", "%20")

    if platform.startswith("p"):
        platform = "psn"

    if platform == "xbox":
        platform = "xbl"
    elif platform.startswith("p"):
        platform = "psn"

    return f"{platform}/{space_replaced}"


_tracker_playlist_id_to_name = {
    "10": "Ranked Duel 1v1",
    "11": "Ranked Doubles 2v2",
    "13": "Ranked Standard 3v3",
}


def _simplify_stats(stats):
    return {
        stat_name: data["value"]
        for stat_name, data in stats.items()
    }


def _simplify_mmr_history(items):
    return [(item["collectDate"], item["rating"]) for item in items]


def combine_profile_and_mmr_json(data):
    """Combine and filter the json obtained for a player from the profile and mmr endpoints."""
    profile = data["profile"]["data"]
    platform_info = profile["platformInfo"]
    metadata = profile["metadata"]

    segments = {
        segment.get("metadata").get("name"): segment
        for segment in profile["segments"]
    }

    mmr_data = data["mmr"]["data"]

    return {
        "platform": platform_info,
        "tracker_api_id": metadata["playerId"],
        "last_updated": metadata["lastUpdated"]["value"],
        "stats": _simplify_stats(segments["Lifetime"]["stats"]),
        "playlists": {
            segment_name: _simplify_stats(segment_data["stats"])
            for segment_name, segment_data in segments.items()
            if segment_name != "overview"
        },
        "mmr_history": {
            playlist_name: _simplify_mmr_history(mmr_data[tracker_playlist_id])
            for tracker_playlist_id, playlist_name in _tracker_playlist_id_to_name.items()
            if tracker_playlist_id in mmr_data
        }
    }


class CloudScraperTrackerNetwork:
    """Use the cloudscraper library to perform requests to the tracker network."""

    def __init__(self, scraper=None):
        """Initialize this class."""
        self._scraper = scraper or cloudscraper.create_scraper(delay=1, browser="chrome")

    def refresh_scraper(self):
        """Make a new scraper."""
        self._scraper = cloudscraper.create_scraper(delay=1, browser="chrome")

    def __get(self, uri):
        logger.info(f"Cloud scraper request {uri}")
        resp = self._scraper.get(uri, timeout=6)
        if resp.status_code != 200:
            raise Non200Exception(resp.status_code, resp.headers)
        return resp.json()

    def _get(self, uri):
        try:
            return self.__get(uri)
        except requests.exceptions.Timeout:
            logger.warn("Trying refreshing scraper after a timeout")
            self.refresh_scraper()
            return self.__get(uri)

    def get_player_data(self, player):
        """Combine info from the main player page and the mmr history player page."""
        uri = get_profile_uri_for_player(player)
        profile_result = self._get(uri)
        tracker_api_id = profile_result["data"]["metadata"]["playerId"]

        mmr_history_uri = get_mmr_history_uri_by_id(tracker_api_id)
        mmr_result = self._get(mmr_history_uri)

        return {
            "profile": profile_result,
            "mmr": mmr_result,
        }


class CurlTrackerNetwork:
    """Use the low level aiocurl api to perform requests to the tracker network."""

    def __init__(self, multi=None):
        """Initialize this class."""
        self._multi = multi or aiocurl.CurlMulti()

    async def get_tracker_json(self, uri):
        """Get the json object returned from the provided URI."""
        headers = {"User-Agent": "MMRFetcher"}
        header_strings = ["{}: {}".format(str(each[0]), str(each[1])) for each in headers.items()]

        buf = BytesIO()
        headers_buf = BytesIO()
        handle = aiocurl.Curl()

        handle.setopt(aiocurl.URL, uri)
        handle.setopt(aiocurl.HTTPHEADER, header_strings)
        handle.setopt(aiocurl.WRITEDATA, buf)
        handle.setopt(aiocurl.HEADERFUNCTION, headers_buf.write)

        await self._multi.perform(handle)

        status_code = handle.getinfo(aiocurl.HTTP_CODE)
        if status_code != 200:
            try:
                request_line, headers_alone = headers_buf.getvalue().split(b'\r\n', 1)
                response_headers = dict(BytesParser().parsebytes(headers_alone))
            except Exception:
                response_headers = {}
            raise Non200Exception(status_code, response_headers)

        return json.loads(buf.getvalue().decode('utf-8'))

    async def get_player_data(self, player):
        """Combine info from the main player page and the mmr history player page."""
        uri = get_profile_uri_for_player(player)
        profile_result = await self.get_tracker_json(uri)
        mmr_result = None

        tracker_api_id = profile_result["data"]["metadata"]["playerId"]

        mmr_history_uri = get_mmr_history_uri_by_id(tracker_api_id)
        mmr_result = await self.get_tracker_json(mmr_history_uri)

        return {
            "profile": profile_result,
            "mmr": mmr_result,
        }

    async def get_player_data_with_auto_reset(self, player):
        """Call get_player_data and reset internal aiocurl components 1 time on exception."""
        try:
            return await self.get_player_data(player)
        except aiocurl.error as e:
            logger.warn(f"Encountered {e} on get_player_data, retrying")
            self._multi = aiocurl.CurlMulti()
            return await self.get_player_data(player)


def _log_backoff(details):
    exception = details["exception"]
    logger.info(f"Backing off {exception}")


def _use_retry_after(exception: Non200Exception):
    string_value = exception.response_headers.get('retry-after') or \
        exception.response_headers.get('Retry-After')
    if string_value is None:
        return 60
    return int(string_value)


def get_player_data_with_429_retry(get_player_data=CloudScraperTrackerNetwork().get_player_data):
    """Wrap the provided getter with 429 backoff that uses the retry-after header."""
    return backoff.on_exception(
        backoff.runtime, Non200Exception, max_time=300,
        giveup=lambda e: e.status_code != 429,
        on_backoff=_log_backoff,
        value=_use_retry_after,
        jitter=None,
    )(get_player_data)
