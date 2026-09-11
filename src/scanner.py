from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import requests

from Load_json import ConfigError


BASE_URL = "https://reservation.sustech.edu.cn"
SCHEDULE_PATH = "/api/blade-app/qywx/getOrderTimeConfigList"


class ScannerError(RuntimeError):
    """Base error for availability scanning."""


class AuthenticationError(ScannerError):
    """The current user token is no longer accepted."""


class ScheduleFormatError(ScannerError):
    """The server returned a schedule shape this client does not understand."""


@dataclass(frozen=True)
class ReservationTarget:
    start: datetime
    end: datetime

    @property
    def date_text(self) -> str:
        return self.start.strftime("%Y-%m-%d")


@dataclass(frozen=True)
class AvailableCandidate:
    target: ReservationTarget
    ground_key: str
    ground_id: str
    ground_name: str
    blocks: Tuple[Mapping[str, Any], ...]

    @property
    def checkout_url(self) -> str:
        return (
            f"{BASE_URL}/clientMobile.html#/reservation/ground/"
            f"{self.ground_id}/{self.target.date_text}"
        )


def _parse_datetime(value: Any, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name} 不是有效时间: {value!r}") from exc
    if parsed.second or parsed.microsecond:
        raise ConfigError(f"{field_name} 必须精确到分钟，秒应为 00: {value!r}")
    return parsed


def build_targets(config: Mapping[str, Any]) -> List[ReservationTarget]:
    starts = config.get("start_time")
    ends = config.get("end_time")
    if not isinstance(starts, list) or not isinstance(ends, list) or not starts:
        raise ConfigError("start_time 和 end_time 必须是非空数组")
    if len(starts) != len(ends):
        raise ConfigError("start_time 与 end_time 数量必须一致")

    targets: List[ReservationTarget] = []
    for index, (start_value, end_value) in enumerate(zip(starts, ends), start=1):
        start = _parse_datetime(start_value, f"start_time[{index - 1}]")
        end = _parse_datetime(end_value, f"end_time[{index - 1}]")
        if start.date() != end.date():
            raise ConfigError(f"第 {index} 个目标跨越日期，当前场馆时段预约不支持这种配置")
        if end <= start:
            raise ConfigError(f"第 {index} 个目标的 end_time 必须晚于 start_time")
        if end <= datetime.now():
            raise ConfigError(f"第 {index} 个目标时间已经过去: {end:%Y-%m-%d %H:%M}")
        targets.append(ReservationTarget(start=start, end=end))
    return targets


def _clock_minutes(value: Any) -> int:
    text = str(value or "").strip()
    try:
        hour_text, minute_text = text.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (TypeError, ValueError) as exc:
        raise ScheduleFormatError(f"无法识别时段时间: {value!r}") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ScheduleFormatError(f"时段时间超出范围: {value!r}")
    return hour * 60 + minute


def select_available_blocks(
    day_schedule: Mapping[str, Any],
    target: ReservationTarget,
    selection_mode: str = "exact",
    min_duration_minutes: int = 30,
) -> Optional[Tuple[Mapping[str, Any], ...]]:
    """Return consecutive blocks selected within a configured target window."""

    raw_blocks = day_schedule.get("timeBlockList")
    if not isinstance(raw_blocks, list):
        raise ScheduleFormatError("日期数据缺少 timeBlockList")

    blocks_by_start: Dict[int, Mapping[str, Any]] = {}
    for block in raw_blocks:
        if not isinstance(block, Mapping):
            continue
        start_value = block.get("time")
        end_value = block.get("endTime")
        if start_value is None or end_value is None:
            continue
        blocks_by_start[_clock_minutes(start_value)] = block

    if selection_mode == "longest_contiguous":
        target_start = target.start.hour * 60 + target.start.minute
        target_end = target.end.hour * 60 + target.end.minute
        runs: List[Tuple[Mapping[str, Any], ...]] = []
        current: List[Mapping[str, Any]] = []
        current_end: Optional[int] = None

        for block_start, block in sorted(blocks_by_start.items()):
            block_end = _clock_minutes(block.get("endTime"))
            if block_start < target_start or block_end > target_end:
                continue

            is_available = str(block.get("status")) == "1"
            is_valid = block_end > block_start
            if not is_available or not is_valid:
                if current:
                    runs.append(tuple(current))
                current = []
                current_end = None
                continue

            if current_end is not None and block_start != current_end:
                runs.append(tuple(current))
                current = []

            current.append(block)
            current_end = block_end

        if current:
            runs.append(tuple(current))

        eligible_runs = [
            run
            for run in runs
            if _clock_minutes(run[-1]["endTime"])
            - _clock_minutes(run[0]["time"])
            >= min_duration_minutes
        ]
        if not eligible_runs:
            return None

        # Longest duration wins; an equal-duration tie goes to the earlier run.
        return min(
            eligible_runs,
            key=lambda run: (
                -(
                    _clock_minutes(run[-1]["endTime"])
                    - _clock_minutes(run[0]["time"])
                ),
                _clock_minutes(run[0]["time"]),
            ),
        )

    if selection_mode != "exact":
        raise ConfigError(
            f"未知的 scan.selection_mode: {selection_mode!r}"
        )

    cursor = target.start.hour * 60 + target.start.minute
    target_end = target.end.hour * 60 + target.end.minute
    selected: List[Mapping[str, Any]] = []

    while cursor < target_end:
        block = blocks_by_start.get(cursor)
        if block is None or str(block.get("status")) != "1":
            return None
        block_end = _clock_minutes(block.get("endTime"))
        if block_end <= cursor or block_end > target_end:
            return None
        selected.append(block)
        cursor = block_end

    if cursor != target_end:
        return None
    return tuple(selected)


def _safe_headers(config_headers: Mapping[str, Any]) -> Dict[str, str]:
    # Let requests calculate transport headers itself. In particular, advertising
    # Brotli without a Brotli decoder can make otherwise valid JSON unreadable.
    ignored = {"accept-encoding", "connection", "content-length", "host"}
    headers = {
        str(key): str(value)
        for key, value in config_headers.items()
        if str(key).lower() not in ignored
    }
    headers.setdefault("Accept", "application/json, text/plain, */*")
    headers.setdefault("Referer", f"{BASE_URL}/clientMobile.html")
    return headers


class AvailabilityScanner:
    def __init__(
        self,
        config: Mapping[str, Any],
        session: Optional[requests.Session] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
    ):
        self.config = config
        # An injected session is kept for deterministic tests. Production
        # requests create a separate Session per worker so concurrent requests
        # never mutate or use the same requests.Session instance.
        self.session = session
        self.request_headers = _safe_headers(config.get("headers", {}))
        if self.session is not None:
            self.session.headers.update(self.request_headers)
        scan_config = config["scan"]
        self.timeout = float(scan_config["request_timeout_seconds"])
        self.max_concurrent_requests = int(
            scan_config.get("max_concurrent_requests", 3)
        )
        self.selection_mode = str(
            scan_config.get("selection_mode", "exact")
        ).strip().lower()
        self.min_duration_minutes = int(
            scan_config.get("min_duration_minutes", 30)
        )
        release_time_text = scan_config.get("third_day_release_time")
        self.third_day_release_time = (
            datetime.strptime(str(release_time_text), "%H:%M").time()
            if release_time_text is not None
            else None
        )
        self.release_grace_seconds = float(
            scan_config.get("release_grace_seconds", 0.0)
        )
        self.now_provider = now_provider or datetime.now
        self.last_errors: List[str] = []
        self.unreleased_dates: List[str] = []

    def _is_waiting_for_release(self, target: ReservationTarget) -> bool:
        if self.third_day_release_time is None:
            return False
        release_date = target.start.date() - timedelta(days=2)
        release_at = datetime.combine(
            release_date, self.third_day_release_time
        ) + timedelta(seconds=self.release_grace_seconds)
        return self.now_provider() < release_at

    def _fetch_day_schedule(
        self, ground_id: str, date_text: str
    ) -> Optional[Mapping[str, Any]]:
        params = {
            "groundId": ground_id,
            "startDate": date_text,
            "endDate": date_text,
            "userid": self.config["user_id"],
            "token": self.config["token"],
        }
        request_session = self.session or requests.Session()
        owns_session = self.session is None
        if owns_session:
            request_session.headers.update(self.request_headers)
        try:
            response = request_session.get(
                f"{BASE_URL}{SCHEDULE_PATH}", params=params, timeout=self.timeout
            )
            payload = response.json()
        except requests.RequestException as exc:
            # Do not include the request URL here: it contains the user's token.
            raise ScannerError(
                f"请求场地 {ground_id} 失败: {exc.__class__.__name__}"
            ) from exc
        except ValueError as exc:
            raise ScheduleFormatError(f"场地 {ground_id} 返回了非 JSON 响应") from exc
        finally:
            if owns_session:
                request_session.close()

        if response.status_code in (401, 403):
            raise AuthenticationError(f"HTTP {response.status_code}")
        if not 200 <= response.status_code < 300:
            raise ScannerError(
                f"场地 {ground_id} 返回 HTTP {response.status_code}"
            )

        code = payload.get("code")
        if code != 200:
            message = str(payload.get("msg") or payload.get("message") or f"code={code}")
            if code in (401, 403) or any(
                word in message
                for word in ("鉴权", "认证", "token", "Token", "登录", "登陆")
            ):
                raise AuthenticationError(message)
            raise ScannerError(f"场地 {ground_id} 返回错误 [{code}]: {message}")

        data = payload.get("data")
        config_list = data.get("configList") if isinstance(data, Mapping) else None
        if not isinstance(config_list, list):
            raise ScheduleFormatError("响应缺少 data.configList")

        for day in config_list:
            if isinstance(day, Mapping) and str(day.get("date")) == date_text:
                return day
        # A successful response can legitimately omit the third day before the
        # venue releases it. Treat that as pending data rather than a bad shape.
        return None

    def _ground_keys(self) -> Sequence[str]:
        # Venue priority is intentionally ignored in race mode. The key order is
        # used only to submit work; the first confirmed available response wins.
        return [str(key) for key in self.config["ground_url"].keys()]

    def _candidate_from_schedule(
        self,
        day_schedule: Mapping[str, Any],
        target: ReservationTarget,
        ground_key: str,
        ground_id: str,
    ) -> Optional[AvailableCandidate]:
        blocks = select_available_blocks(
            day_schedule,
            target,
            selection_mode=self.selection_mode,
            min_duration_minutes=self.min_duration_minutes,
        )
        if not blocks:
            return None

        selected_start = _clock_minutes(blocks[0]["time"])
        selected_end = _clock_minutes(blocks[-1]["endTime"])
        selected_target = ReservationTarget(
            start=target.start.replace(
                hour=selected_start // 60,
                minute=selected_start % 60,
            ),
            end=target.end.replace(
                hour=selected_end // 60,
                minute=selected_end % 60,
            ),
        )
        ground_name = str(
            self.config.get("ground_name", {}).get(ground_key, ground_key)
        )
        return AvailableCandidate(
            target=selected_target,
            ground_key=ground_key,
            ground_id=ground_id,
            ground_name=ground_name,
            blocks=blocks,
        )

    @staticmethod
    def _stop_background_requests(
        executor: ThreadPoolExecutor,
        futures: Iterable[Future[Optional[Mapping[str, Any]]]],
    ) -> None:
        for future in futures:
            future.cancel()
        # Running HTTP calls cannot be cancelled, but they must not delay the
        # browser after another venue has already won the race.
        executor.shutdown(wait=False, cancel_futures=True)

    def find_first_available(
        self, targets: Iterable[ReservationTarget]
    ) -> Optional[AvailableCandidate]:
        cache: Dict[Tuple[str, str], Optional[Mapping[str, Any]]] = {}
        completion_order: Dict[str, List[Tuple[str, str]]] = {}
        failed_requests: Dict[Tuple[str, str], str] = {}
        self.last_errors = []
        self.unreleased_dates = []
        ground_keys = self._ground_keys()

        for target in targets:
            if self._is_waiting_for_release(target):
                self.unreleased_dates = [target.date_text]
                return None

            target_pending = False
            target_errors: List[str] = []
            ground_data = [
                (
                    ground_key,
                    str(self.config["ground_url"][ground_key]),
                )
                for ground_key in ground_keys
            ]

            # If an earlier target on this date populated the cache, preserve
            # the response-completion order instead of reintroducing venue
            # priority while evaluating the next time target.
            cached_ground_data = completion_order.get(target.date_text, [])
            for ground_key, ground_id in cached_ground_data:
                cache_key = (ground_id, target.date_text)
                day_schedule = cache[cache_key]
                if day_schedule is None:
                    target_pending = True
                    continue

                try:
                    candidate = self._candidate_from_schedule(
                        day_schedule, target, ground_key, ground_id
                    )
                except ScannerError as exc:
                    message = str(exc)
                    failed_requests[cache_key] = message
                    self.last_errors.append(message)
                    target_errors.append(message)
                    continue
                if candidate is not None:
                    return candidate

            for ground_key, ground_id in ground_data:
                cache_key = (ground_id, target.date_text)
                if cache_key in failed_requests:
                    target_errors.append(failed_requests[cache_key])

            pending_ground_data = [
                (ground_key, ground_id)
                for ground_key, ground_id in ground_data
                if (ground_id, target.date_text) not in cache
                and (ground_id, target.date_text) not in failed_requests
            ]

            if pending_ground_data:
                executor: Optional[ThreadPoolExecutor] = ThreadPoolExecutor(
                    max_workers=min(
                        self.max_concurrent_requests, len(pending_ground_data)
                    ),
                    thread_name_prefix="venue-scan",
                )
                future_data: Dict[
                    Future[Optional[Mapping[str, Any]]], Tuple[str, str]
                ] = {
                    executor.submit(
                        self._fetch_day_schedule, ground_id, target.date_text
                    ): (ground_key, ground_id)
                    for ground_key, ground_id in pending_ground_data
                }

                try:
                    for future in as_completed(future_data):
                        ground_key, ground_id = future_data[future]
                        cache_key = (ground_id, target.date_text)
                        try:
                            day_schedule = future.result()
                        except AuthenticationError:
                            self._stop_background_requests(executor, future_data)
                            executor = None
                            raise
                        except ScannerError as exc:
                            message = str(exc)
                            failed_requests[cache_key] = message
                            self.last_errors.append(message)
                            target_errors.append(message)
                            continue

                        cache[cache_key] = day_schedule
                        completion_order.setdefault(target.date_text, []).append(
                            (ground_key, ground_id)
                        )
                        if day_schedule is None:
                            target_pending = True
                            continue

                        try:
                            candidate = self._candidate_from_schedule(
                                day_schedule, target, ground_key, ground_id
                            )
                        except ScannerError as exc:
                            message = str(exc)
                            failed_requests[cache_key] = message
                            self.last_errors.append(message)
                            target_errors.append(message)
                            continue

                        if candidate is not None:
                            self._stop_background_requests(executor, future_data)
                            executor = None
                            return candidate
                finally:
                    if executor is not None:
                        executor.shutdown(wait=True)

            if target_errors:
                raise ScannerError(target_errors[0])

            if target_pending:
                self.unreleased_dates = [target.date_text]
                return None

        return None
