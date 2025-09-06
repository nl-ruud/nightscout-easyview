"""Nightscout uploader for CGM data from Medtrum EasyView."""

from __future__ import annotations

import functools
import hashlib
import logging
import pathlib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterator

import requests
import yaml

logger = logging.getLogger(__name__)


@dataclass
class SensorStatus:
    """Dataclass representing a Medtrum sensor status."""

    device_type: str
    glucose: float
    glucose_rate: int
    sensor_id: int
    sequence: int
    serial: int
    status: Status | None
    update_time: float
    app_name: str | None = None
    battery_percent: float | None = None
    current: int | None = None

    class Status(Enum):
        """Medtrum Status"""

        WARMING_UP = 2
        NORMAL = 3
        NEEDS_CALIBRATION = 10

    @property
    def unix_timestamp(self) -> int:
        """Return the timestamp as an integer."""
        return round(self.update_time)

    @property
    def timestamp(self) -> datetime:
        """Return the timestamp as a datetime object."""
        return datetime.fromtimestamp(self.unix_timestamp, tz=timezone.utc)

    @property
    def key(self) -> tuple[int, int]:
        """Return unique key for the entry."""
        return (self.sensor_id, self.sequence)

    @property
    def preceding_key(self) -> tuple[int, int]:
        """Return preceding key for the entry."""
        return (self.sensor_id, self.sequence - 1)

    @property
    def direction(self) -> str | None:
        """Return direction of glucose change."""
        directions = {
            0: "Flat",
            1: "FortyFiveUp",
            2: "SingleUp",
            3: "DoubleUp",
            4: "FortyFiveDown",
            5: "SingleDown",
            6: "DoubleDown",
            8: "Flat",  # both 0 and 8 seem to show as Flat in the app
        }
        if self.glucose_rate not in directions:
            logger.warning(
                "unknown glucose rate %i on entry %i",
                self.glucose_rate,
                self.sequence,
            )
        return directions.get(self.glucose_rate)

    @property
    def nightscout_entry(self) -> dict[str, str | int]:
        """Return sensor status as Nightscout entry."""
        return {
            "type": "sgv",
            "date": self.unix_timestamp * 1000,
            "dateString": self.timestamp.isoformat(),
            "sgv": round(self.glucose * 18),
            "direction": self.direction or "NONE",
            "device": self.device_type,
        }

    @classmethod
    def from_easyview(cls, data: dict[str, Any]) -> SensorStatus:
        """Create a SensorStatus from EasyView sensor_status dictionary."""
        return cls(
            app_name=data["appName"],
            battery_percent=data["batteryPercent"],
            current=data["current"],
            device_type=data["deviceType"],
            glucose=data["glucose"],
            glucose_rate=data["glucoseRate"],
            sensor_id=data["sensorId"],
            sequence=data["sequence"],
            serial=data["serial"],
            status=cls.Status(data["status"]),
            update_time=data["updateTime"],
        )

    @classmethod
    def from_download(
        cls,
        record: tuple[str, float, float, float, str, float],
        device_type,
    ) -> SensorStatus:
        """Create a SensorStatus from EasyView download record."""
        pattern = r"^(?P<uid>\d+)-(?P<serial>\d+)-(?P<sensorId>\d+)-(?P<sequence>\d+)$"
        match = re.match(pattern, record[0])
        if not match:
            raise ValueError("invalid EasyView download record")
        status = {
            "C": cls.Status.NORMAL,
            "H": cls.Status.WARMING_UP,
            "XC": cls.Status.NEEDS_CALIBRATION,
        }
        return cls(
            device_type=device_type,
            glucose=record[3],
            glucose_rate=round(record[5]),
            sensor_id=int(match.group("sensorId")),
            sequence=int(match.group("sequence")),
            serial=int(match.group("serial")),
            status=status.get(record[4]),
            update_time=record[1],
        )

    @classmethod
    def from_timestamp(cls, timestamp: datetime, device_type: str) -> SensorStatus:
        """Create a SensorStatus with only timestamp set."""
        return cls(
            device_type=device_type,
            glucose=0,
            glucose_rate=0,
            sensor_id=0,
            sequence=0,
            serial=0,
            status=None,
            update_time=datetime.timestamp(timestamp),
        )


def with_retry(delay: int):
    """Decorator to retry on session Timeout or ConnectionError."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            while True:
                try:
                    return func(*args, **kwargs)
                except requests.exceptions.ReadTimeout:
                    logger.info("Network timeout, retrying")
                except requests.exceptions.ConnectionError:
                    logger.info("Network connection error, retrying")
                time.sleep(delay)

        return wrapper

    return decorator


class EasyFollow:
    """Class that interacts with the EasyView API from a Follow account."""

    BASE_URL = "https://easyview.medtrum.eu/mobile/ajax"

    _sensor_status: SensorStatus | None = None

    def __init__(
        self, username: str, password: str, timestamp: datetime | None = None
    ) -> None:
        """Initialize with username, password and optional resume timestamp."""
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
        self.resume_timestamp = timestamp
        self._queue: list[SensorStatus] = []
        self._next_interval = datetime.now(timezone.utc)

    def __enter__(self):
        """Context manager entry, opens connection to EasyView."""
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit, closes connection to EasyView."""
        self.close()

    def __iter__(self) -> Iterator[SensorStatus]:
        return self

    def __next__(self) -> SensorStatus:
        """Returns next SensorStatus from EasyView"""
        while not self._queue:
            cur_stat = self.sensor_status
            delta = (self._next_interval - datetime.now(timezone.utc)).total_seconds()
            if delta > 0:
                time.sleep(delta)
            self._next_interval = datetime.now(timezone.utc) + timedelta(seconds=30)

            raw_status = self.get_status()["monitorlist"][0]["sensor_status"]
            new_stat = SensorStatus.from_easyview(raw_status)
            if new_stat.key == cur_stat.key:
                logger.debug(
                    "no new data on EasyView (sensor=%i, sequence=%i)",
                    cur_stat.sensor_id,
                    cur_stat.sequence,
                )
                continue
            if new_stat.preceding_key != cur_stat.key:
                for s in self.history(cur_stat.timestamp, new_stat.timestamp):
                    if new_stat.key > s.key > cur_stat.key:
                        self._queue.append(s)
            self._queue.append(new_stat)
            self._next_interval = max(
                new_stat.timestamp + timedelta(seconds=150), self._next_interval
            )

        self.sensor_status = self._queue.pop(0)
        return self.sensor_status

    @with_retry(delay=10)
    def _post(self, endpoint: str, data: dict) -> dict[str, Any]:
        """Send a POST request to the specified endpoint with the given data."""
        response = self.session.post(
            f"{self.BASE_URL}/{endpoint}", data=data, timeout=10
        )
        response.raise_for_status()
        return response.json()

    @with_retry(delay=10)
    def _get(self, endpoint: str, params: dict | None = None) -> dict[str, Any]:
        """Send a GET request to the specified endpoint with the given parameters."""
        response = self.session.get(
            f"{self.BASE_URL}/{endpoint}", params=params, timeout=10
        )
        response.raise_for_status()
        return response.json()

    @functools.cached_property
    def cgm_username(self) -> str:
        """Get the username of the user carrying the CGM."""
        status = self.get_status()
        return status["monitorlist"][0]["username"]

    @property
    def sensor_status(self) -> SensorStatus:
        """Return the last retrieved sensor status."""
        if self._sensor_status is None:
            status = self.get_status()
            if len(status["monitorlist"]) != 1:
                logger.error(
                    "Follower should have exactly one CGM user, got %i",
                    len(status["monitorlist"]),
                )
                raise ValueError("Account should follow exactly one CGM user.")
            self._sensor_status = SensorStatus.from_easyview(
                status["monitorlist"][0]["sensor_status"]
            )
            if self.resume_timestamp is None:
                self._sensor_status = SensorStatus.from_timestamp(
                    self._sensor_status.timestamp - timedelta(hours=48),
                    self._sensor_status.device_type,
                )
            elif self.resume_timestamp != self._sensor_status.timestamp:
                self._sensor_status = SensorStatus.from_timestamp(
                    self.resume_timestamp, self._sensor_status.device_type
                )
        return self._sensor_status

    @sensor_status.setter
    def sensor_status(self, value: SensorStatus):
        """Update sensor status"""
        if value.key == self.sensor_status.key:
            logger.debug(
                "status is current (sensor=%i, sequence=%i)",
                value.sensor_id,
                value.sequence,
            )
            raise ValueError("sensor status is already current")
        if self.sensor_status.key > value.key:
            logger.error(
                "sensor status is outdated (sensor=%i, sequence=%i)",
                value.sensor_id,
                value.sequence,
            )
            raise ValueError("invalid sensor status")
        self._sensor_status = value
        logger.debug(
            "sensor status updated (sensor=%i, sequence=%i)",
            self.sensor_status.sensor_id,
            self.sensor_status.sequence,
        )

    def open(self) -> None:
        """Establish a connection to EasyView."""
        data = {
            "apptype": "Follow",
            "user_name": self.username,
            "password": self.password,
            "platform": "google",
            "user_type": "M",
        }
        self._post("login", data=data)
        logger.info("logged in to EasyView as %s", self.username)

    def close(self) -> None:
        """Closes the connection to EasyView."""
        logger.info("closed connection to EasyView")
        self.session.close()

    def get_status(self) -> dict[str, Any]:
        """Get CGM data from the EasyView API."""
        return self._get("logindata")

    def get_downloads(self, start: datetime, end: datetime) -> dict[str, Any]:
        """Get historical sensor status data from EasyView API."""
        params = {
            "flag": "sg",
            "st": (start + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S"),
            "et": (end - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S"),
            "user_name": self.cgm_username,
        }
        return self._get("download", params)

    def history(self, start, end) -> Iterator[SensorStatus]:
        """Returns iterator of SensorStatus objects for requested period."""
        downloads = self.get_downloads(start, end)["data"]
        device_type = self.sensor_status.device_type
        for rec in map(tuple, downloads):
            try:
                yield SensorStatus.from_download(rec, device_type=device_type)
            except ValueError:
                pass


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
        """Get last sensor value timestamp from Nightscout."""
        response = self.session.get(
            f"{self.url}/api/v1/entries.json", params={"count": 1}, timeout=10
        )
        response.raise_for_status()
        data = response.json()
        if data:
            return datetime.fromtimestamp(data[0]["date"] / 1000, tz=timezone.utc)
        return None

    @with_retry(delay=10)
    def add(self, sensor_status: SensorStatus) -> dict[str, Any]:
        """Add a sensor value to Nightscout."""
        response = self.session.post(
            f"{self.url}/api/v1/entries.json",
            json=[sensor_status.nightscout_entry],
            timeout=10,
        )
        response.raise_for_status()
        logger.info(
            "submitted sensor value to nightscout (sensor=%i, sequence=%i)",
            sensor_status.sensor_id,
            sensor_status.sequence,
        )
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
            for sensor_status in ef:
                if sensor_status.status is not SensorStatus.Status.WARMING_UP:
                    ns.add(sensor_status)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)-7s - %(message)s",
    )
    main()
