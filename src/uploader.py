"""Nightscout uploader for CGM data from Medtrum Easyview."""

import functools
import hashlib
import logging
import pathlib
import re
import time
from datetime import datetime, timedelta, timezone
from functools import cached_property
from typing import Any

import requests
import yaml
from requests.exceptions import ConnectionError, ReadTimeout

logger = logging.getLogger(__name__)


def with_retry(delay: int):
    """Decorator to retry on session Timeout or ConnectionError."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            while True:
                try:
                    return func(*args, **kwargs)
                except ReadTimeout:
                    logger.info("EasyView API timeout, retrying in 30 seconds")
                    time.sleep(delay)
                except ConnectionError:
                    logger.info("EasyView API connection error, retrying in 30 seconds")
                    time.sleep(delay)

        return wrapper

    return decorator


class EasyFollow:
    """Class that interacts with the EasyFollow API to get CGM data."""

    BASE_URL = "https://easyview.medtrum.eu/mobile/ajax"

    last_seq: int | None = None

    def __init__(
        self, username: str, password: str, last_timestamp: datetime | None = None
    ) -> None:
        self.username = username
        self.password = password
        self.session: requests.Session = requests.Session()
        self.session.headers.update(
            {
                "DevInfo": "Android 12;Xiamoi vayu;Android 12",
                "AppTag": "v=1.2.70(112);n=eyfo;p=android",
                "User-Agent": "okhttp/3.5.0",
            }
        )
        self.last_timestamp = last_timestamp

    @cached_property
    def cgm_username(self) -> str:
        """Get the username from the user carrying the CGM."""
        status = self.get("logindata")
        if len(status["monitorlist"]) != 1:
            raise ValueError("Follower should have only one CGM user.")
        return status["monitorlist"][0]["username"]

    @cached_property
    def device(self) -> str:
        """Get the device type from the EasyFollow API."""
        status = self.get("logindata")
        if len(status["monitorlist"]) != 1:
            raise ValueError("Follower should have only one CGM user.")
        return status["monitorlist"][0]["sensor_status"]["deviceType"]

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _missed_cgm_data(self, current_timestamp):
        """Return missed CGM data from EasyView history."""
        for seq, entry in map(
            self.parse_cgm_hist,
            self.history(
                self.last_timestamp + timedelta(seconds=30),
                current_timestamp - timedelta(seconds=30),
            ),
        ):
            timestamp = datetime.fromtimestamp(entry["date"] / 1000, tz=timezone.utc)
            yield seq, timestamp, entry

    def _cgm_stream(self):
        """Poll CGM data from EasyView indefinitely."""
        while True:
            seq, entry = self.parse_cgm_status(self.status())
            timestamp = datetime.fromtimestamp(entry["date"] / 1000, tz=timezone.utc)
            if self.last_timestamp is None:
                self.last_timestamp = timestamp - timedelta(hours=48)

            if self.last_seq is None and timestamp <= self.last_timestamp:
                logger.info("resuming from CGM entry %i (already processed)", seq)
                self.last_seq = seq
                continue

            if seq == self.last_seq:
                logger.debug("no new CGM entry on EasyView, retrying in 30 seconds")
                time.sleep(30)
            else:
                if seq - 1 != self.last_seq:
                    yield from self._missed_cgm_data(timestamp)
                yield seq, timestamp, entry
                time.sleep(max(150 + entry["date"] / 1000 - time.time(), 30))

    def __iter__(self):
        logger.info("start polling EasyView")
        for seq, timestamp, entry in self._cgm_stream():
            if self.last_seq is not None and seq > self.last_seq + 1:
                for i in range(self.last_seq + 1, seq):
                    logger.warning("missed CGM entry %i", i)
            logger.info("processed CGM entry %i", seq)
            self.last_seq = seq
            self.last_timestamp = timestamp
            yield entry

    @with_retry(delay=30)
    def post(self, endpoint: str, data: dict) -> dict[str, Any]:
        """Send a POST request to the specified endpoint with the given data."""
        response = self.session.post(
            f"{self.BASE_URL}/{endpoint}", data=data, timeout=10
        )
        response.raise_for_status()
        return response.json()

    @with_retry(delay=30)
    def get(self, endpoint: str, params: dict | None = None) -> dict[str, Any]:
        """Send a GET request to the specified endpoint with the given parameters."""
        response = self.session.get(
            f"{self.BASE_URL}/{endpoint}", params=params, timeout=10
        )
        response.raise_for_status()
        return response.json()

    def open(self) -> None:
        """Establish a connection to the EasyFollow API."""
        data = {
            "apptype": "Follow",
            "user_name": self.username,
            "password": self.password,
            "platform": "google",
            "user_type": "M",
        }
        self.post("login", data=data)
        logger.info("logged in to EasyView as %s", self.username)

    def close(self) -> None:
        """Close the connection to the EasyFollow API."""
        logger.info("closed connection to EasyView")
        self.session.close()

    def direction(self, glucose_rate: int) -> str:
        """Return nighscout direction for the given glucose rate."""
        directions = {
            0: "Flat",
            1: "FortyFiveUp",
            2: "SingleUp",
            3: "DoubleUp",
            4: "FortyFiveDown",
            5: "SingleDown",
            6: "DoubleDown",
            8: "Flat",
        }
        if glucose_rate not in directions:
            logger.warning("unknown glucose rate: %i, returned NONE", glucose_rate)
            return "NONE"
        return directions[glucose_rate]

    def status(self) -> dict[str, str | int]:
        """Get CGM data from the EasyFollow API."""
        return self.get("logindata")

    def history(self, start: datetime, end: datetime):
        """Get historical glucose status data from the EasyFollow API."""
        params = {
            "flag": "sg",
            "st": start.strftime("%Y-%m-%d %H:%M:%S"),
            "et": end.strftime("%Y-%m-%d %H:%M:%S"),
            "user_name": self.cgm_username,
        }
        yield from self.get("download", params)["data"]

    def parse_cgm_status(
        self, status: dict[str, Any]
    ) -> tuple[int, dict[str, str | int]]:
        """Return nightsout CGM entry from an Easyview status request."""
        cgm_status = status["monitorlist"][0]["sensor_status"]
        timestamp = datetime.fromtimestamp(
            round(cgm_status["updateTime"]), tz=timezone.utc
        )
        return cgm_status["sequence"], {
            "type": "sgv",
            "date": round(cgm_status["updateTime"]) * 1000,
            "dateString": timestamp.isoformat(),
            "sgv": round(cgm_status["glucose"] * 18),
            "direction": self.direction(cgm_status["glucoseRate"]),
            "device": cgm_status["deviceType"],
        }

    def parse_cgm_hist(self, rec: list) -> tuple[int, dict[str, str | int]]:
        """Return nightsout CGM entry from an Easyview history record."""
        pattern = r"^(?P<uid>\d+)-(?P<serial>\d+)-(?P<sensorId>\d+)-(?P<sequence>\d+)$"
        match = re.match(pattern, rec[0])
        if not match:
            raise ValueError("unknown CGM history record")
        timestamp = datetime.fromtimestamp(round(rec[1]), tz=timezone.utc)
        return int(match.group("sequence")), {
            "type": "sgv",
            "date": round(rec[1]) * 1000,
            "dateString": timestamp.isoformat(),
            "sgv": round(rec[3] * 18),
            "direction": self.direction(rec[5]),
            "device": self.device,
        }


class NightScout:
    """Class that interacts with Nightscout to sync CGM data."""

    def __init__(self, url, api_secret):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "api-secret": hashlib.sha1(api_secret.encode("utf-8")).hexdigest(),
            }
        )
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.session.close()

    @property
    def timestamp(self) -> datetime | None:
        """Get last CGM entry timestamp from Nightscout."""
        response = self.session.get(
            f"{self.url}/api/v1/entries.json", params={"count": 1}, timeout=10
        )
        response.raise_for_status()
        data = response.json()
        if data:
            return datetime.fromtimestamp(data[0]["date"] / 1000, tz=timezone.utc)
        return None

    @with_retry(delay=30)
    def add(self, entry) -> dict[str, Any]:
        """Add a CGM entry to Nightscout."""
        response = self.session.post(
            f"{self.url}/api/v1/entries.json", json=[entry], timeout=10
        )
        response.raise_for_status()
        logger.info("submitted CGM entry to nightscout")
        return response.json()


def main():
    """Main function to sync CGM data from EasyView to Nightscout."""

    secrets_file = pathlib.Path.home() / ".nightscout_easyview/secrets.yaml"
    with secrets_file.open(encoding="utf-8") as f:
        secrets = yaml.safe_load(f)
    username = secrets["easyview"]["username"]
    password = secrets["easyview"]["password"]
    ns_url = secrets["nightscout"]["url"]
    api_secret = secrets["nightscout"]["secret"]

    with NightScout(ns_url, api_secret) as ns:
        with EasyFollow(username, password, ns.timestamp) as ef:
            for cgm_entry in ef:
                ns.add(cgm_entry)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)-7s - %(message)s",
    )
    main()
