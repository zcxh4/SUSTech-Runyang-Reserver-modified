import sys
import unittest
from datetime import datetime
from pathlib import Path
from threading import Event


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scanner import (  # noqa: E402
    AuthenticationError,
    AvailabilityScanner,
    ReservationTarget,
    ScannerError,
    select_available_blocks,
)


class SelectAvailableBlocksTests(unittest.TestCase):
    def setUp(self):
        self.target = ReservationTarget(
            datetime(2099, 1, 1, 20, 0), datetime(2099, 1, 1, 21, 0)
        )

    def test_selects_consecutive_available_blocks(self):
        day = {
            "date": "2099-01-01",
            "timeBlockList": [
                {"time": "20:00", "endTime": "20:30", "status": "1"},
                {"time": "20:30", "endTime": "21:00", "status": 1},
            ],
        }
        selected = select_available_blocks(day, self.target)
        self.assertIsNotNone(selected)
        self.assertEqual(2, len(selected))

    def test_selects_every_block_in_a_long_consecutive_range(self):
        target = ReservationTarget(
            datetime(2099, 1, 1, 20, 0), datetime(2099, 1, 1, 22, 0)
        )
        day = {
            "date": "2099-01-01",
            "timeBlockList": [
                {"time": "20:00", "endTime": "20:30", "status": "1"},
                {"time": "20:30", "endTime": "21:00", "status": "1"},
                {"time": "21:00", "endTime": "21:30", "status": "1"},
                {"time": "21:30", "endTime": "22:00", "status": "1"},
            ],
        }

        selected = select_available_blocks(day, target)

        self.assertIsNotNone(selected)
        self.assertEqual(4, len(selected))
        self.assertEqual("20:00", selected[0]["time"])
        self.assertEqual("22:00", selected[-1]["endTime"])

    def test_rejects_a_long_range_with_an_occupied_middle_block(self):
        target = ReservationTarget(
            datetime(2099, 1, 1, 20, 0), datetime(2099, 1, 1, 22, 0)
        )
        day = {
            "date": "2099-01-01",
            "timeBlockList": [
                {"time": "20:00", "endTime": "20:30", "status": "1"},
                {"time": "20:30", "endTime": "21:00", "status": "1"},
                {"time": "21:00", "endTime": "21:30", "status": "2"},
                {"time": "21:30", "endTime": "22:00", "status": "1"},
            ],
        }

        self.assertIsNone(select_available_blocks(day, target))

    def test_rejects_an_occupied_block(self):
        day = {
            "date": "2099-01-01",
            "timeBlockList": [
                {"time": "20:00", "endTime": "20:30", "status": "1"},
                {"time": "20:30", "endTime": "21:00", "status": "2"},
            ],
        }
        self.assertIsNone(select_available_blocks(day, self.target))

    def test_rejects_a_gap(self):
        day = {
            "date": "2099-01-01",
            "timeBlockList": [
                {"time": "20:00", "endTime": "20:30", "status": "1"},
                {"time": "20:45", "endTime": "21:00", "status": "1"},
            ],
        }
        self.assertIsNone(select_available_blocks(day, self.target))

    def test_selects_longest_contiguous_run_inside_target(self):
        target = ReservationTarget(
            datetime(2099, 1, 1, 20, 0), datetime(2099, 1, 1, 22, 0)
        )
        day = {
            "date": "2099-01-01",
            "timeBlockList": [
                {"time": "20:00", "endTime": "20:30", "status": "1"},
                {"time": "20:30", "endTime": "21:00", "status": "2"},
                {"time": "21:00", "endTime": "21:30", "status": "1"},
                {"time": "21:30", "endTime": "22:00", "status": "1"},
            ],
        }

        selected = select_available_blocks(
            day,
            target,
            selection_mode="longest_contiguous",
            min_duration_minutes=30,
        )

        self.assertIsNotNone(selected)
        self.assertEqual(2, len(selected))
        self.assertEqual("21:00", selected[0]["time"])
        self.assertEqual("22:00", selected[-1]["endTime"])

    def test_longest_contiguous_tie_prefers_earlier_run(self):
        target = ReservationTarget(
            datetime(2099, 1, 1, 20, 0), datetime(2099, 1, 1, 22, 0)
        )
        day = {
            "date": "2099-01-01",
            "timeBlockList": [
                {"time": "20:00", "endTime": "20:30", "status": "1"},
                {"time": "20:30", "endTime": "21:00", "status": "2"},
                {"time": "21:00", "endTime": "21:30", "status": "1"},
                {"time": "21:30", "endTime": "22:00", "status": "2"},
            ],
        }

        selected = select_available_blocks(
            day,
            target,
            selection_mode="longest_contiguous",
            min_duration_minutes=30,
        )

        self.assertIsNotNone(selected)
        self.assertEqual("20:00", selected[0]["time"])
        self.assertEqual("20:30", selected[-1]["endTime"])

    def test_longest_contiguous_honors_minimum_duration(self):
        day = {
            "date": "2099-01-01",
            "timeBlockList": [
                {"time": "20:00", "endTime": "20:30", "status": "1"},
                {"time": "20:30", "endTime": "21:00", "status": "2"},
            ],
        }

        selected = select_available_blocks(
            day,
            self.target,
            selection_mode="longest_contiguous",
            min_duration_minutes=60,
        )

        self.assertIsNone(selected)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.headers = {}
        self.payload = payload
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        return FakeResponse(self.payload)


class GroundPayloadSession:
    def __init__(self, payloads):
        self.headers = {}
        self.payloads = payloads
        self.calls = 0

    def get(self, *_args, **kwargs):
        self.calls += 1
        return FakeResponse(self.payloads[kwargs["params"]["groundId"]])


class RacingSession(GroundPayloadSession):
    def __init__(self, payloads):
        super().__init__(payloads)
        self.slow_started = Event()
        self.release_slow = Event()
        self.slow_finished = Event()

    def get(self, *_args, **kwargs):
        ground_id = kwargs["params"]["groundId"]
        if ground_id == "slow-ground":
            self.slow_started.set()
            self.release_slow.wait(timeout=2.0)
            self.slow_finished.set()
        elif not self.slow_started.wait(timeout=1.0):
            raise AssertionError("慢场地查询没有并发启动")
        return super().get(*_args, **kwargs)


class AvailabilityScannerTests(unittest.TestCase):
    def _config(self):
        return {
            "user_id": "user",
            "token": "token",
            "headers": {},
            "ground_url": {"1": "ground-id"},
            "ground_name": {"1": "1号场"},
            "ground_priority": ["1"],
            "scan": {"request_timeout_seconds": 1},
        }

    def test_reuses_a_schedule_for_targets_on_the_same_day(self):
        payload = {
            "code": 200,
            "data": {
                "configList": [
                    {
                        "date": "2099-01-01",
                        "timeBlockList": [
                            {"time": "20:00", "endTime": "21:00", "status": 2},
                            {"time": "21:00", "endTime": "22:00", "status": 1},
                        ],
                    }
                ]
            },
        }
        session = FakeSession(payload)
        scanner = AvailabilityScanner(self._config(), session=session)
        targets = [
            ReservationTarget(
                datetime(2099, 1, 1, 20, 0), datetime(2099, 1, 1, 21, 0)
            ),
            ReservationTarget(
                datetime(2099, 1, 1, 21, 0), datetime(2099, 1, 1, 22, 0)
            ),
        ]

        candidate = scanner.find_first_available(targets)

        self.assertIsNotNone(candidate)
        self.assertEqual("1号场", candidate.ground_name)
        self.assertEqual(1, session.calls)

    def test_stops_immediately_when_authentication_expires(self):
        session = FakeSession({"code": 400, "msg": "鉴权失败"})
        scanner = AvailabilityScanner(self._config(), session=session)
        targets = [
            ReservationTarget(
                datetime(2099, 1, 1, 20, 0), datetime(2099, 1, 1, 21, 0)
            )
        ]

        with self.assertRaises(AuthenticationError):
            scanner.find_first_available(targets)

    def test_candidate_uses_selected_subrange_in_longest_mode(self):
        payload = {
            "code": 200,
            "data": {
                "configList": [
                    {
                        "date": "2099-01-01",
                        "timeBlockList": [
                            {"time": "20:00", "endTime": "20:30", "status": 2},
                            {"time": "20:30", "endTime": "21:00", "status": 1},
                            {"time": "21:00", "endTime": "21:30", "status": 1},
                            {"time": "21:30", "endTime": "22:00", "status": 2},
                        ],
                    }
                ]
            },
        }
        config = self._config()
        config["scan"].update(
            {
                "selection_mode": "longest_contiguous",
                "min_duration_minutes": 30,
            }
        )
        scanner = AvailabilityScanner(config, session=FakeSession(payload))
        target = ReservationTarget(
            datetime(2099, 1, 1, 20, 0), datetime(2099, 1, 1, 22, 0)
        )

        candidate = scanner.find_first_available([target])

        self.assertIsNotNone(candidate)
        self.assertEqual(datetime(2099, 1, 1, 20, 30), candidate.target.start)
        self.assertEqual(datetime(2099, 1, 1, 21, 30), candidate.target.end)
        self.assertEqual(2, len(candidate.blocks))

    def test_unreleased_date_is_pending_and_becomes_scannable_later(self):
        session = FakeSession({"code": 200, "data": {"configList": []}})
        scanner = AvailabilityScanner(self._config(), session=session)
        target = ReservationTarget(
            datetime(2099, 1, 3, 20, 0), datetime(2099, 1, 3, 21, 0)
        )

        candidate = scanner.find_first_available([target])

        self.assertIsNone(candidate)
        self.assertEqual(["2099-01-03"], scanner.unreleased_dates)
        self.assertEqual([], scanner.last_errors)

        session.payload = {
            "code": 200,
            "data": {
                "configList": [
                    {
                        "date": "2099-01-03",
                        "timeBlockList": [
                            {"time": "20:00", "endTime": "21:00", "status": 1}
                        ],
                    }
                ]
            },
        }

        candidate = scanner.find_first_available([target])

        self.assertIsNotNone(candidate)
        self.assertEqual([], scanner.unreleased_dates)
        self.assertEqual(2, session.calls)

    def test_unreleased_date_is_cached_for_same_day_targets(self):
        session = FakeSession({"code": 200, "data": {"configList": []}})
        scanner = AvailabilityScanner(self._config(), session=session)
        targets = [
            ReservationTarget(
                datetime(2099, 1, 3, 20, 0), datetime(2099, 1, 3, 21, 0)
            ),
            ReservationTarget(
                datetime(2099, 1, 3, 21, 0), datetime(2099, 1, 3, 22, 0)
            ),
        ]

        candidate = scanner.find_first_available(targets)

        self.assertIsNone(candidate)
        self.assertEqual(["2099-01-03"], scanner.unreleased_dates)
        self.assertEqual(1, session.calls)

    def test_third_day_is_not_requested_until_release_time(self):
        payload = {
            "code": 200,
            "data": {
                "configList": [
                    {
                        "date": "2099-01-03",
                        "timeBlockList": [
                            {"time": "20:00", "endTime": "21:00", "status": 1}
                        ],
                    }
                ]
            },
        }
        session = FakeSession(payload)
        config = self._config()
        config["scan"].update(
            {
                "third_day_release_time": "20:00",
                "release_grace_seconds": 3,
            }
        )
        current_time = [datetime(2099, 1, 1, 19, 59, 59)]
        scanner = AvailabilityScanner(
            config,
            session=session,
            now_provider=lambda: current_time[0],
        )
        target = ReservationTarget(
            datetime(2099, 1, 3, 20, 0), datetime(2099, 1, 3, 21, 0)
        )

        candidate = scanner.find_first_available([target])

        self.assertIsNone(candidate)
        self.assertEqual(["2099-01-03"], scanner.unreleased_dates)
        self.assertEqual(0, session.calls)

        current_time[0] = datetime(2099, 1, 1, 20, 0, 3)
        candidate = scanner.find_first_available([target])

        self.assertIsNotNone(candidate)
        self.assertEqual(1, session.calls)
        self.assertEqual([], scanner.unreleased_dates)

    def test_business_code_400_is_not_always_an_authentication_error(self):
        session = FakeSession({"code": 400, "msg": "目标日期暂不可预约"})
        scanner = AvailabilityScanner(self._config(), session=session)
        target = ReservationTarget(
            datetime(2099, 1, 1, 20, 0), datetime(2099, 1, 1, 21, 0)
        )

        try:
            scanner.find_first_available([target])
        except AuthenticationError as exc:
            self.fail(f"普通业务错误被误判为认证失效: {exc}")
        except ScannerError:
            pass
        else:
            self.fail("业务 code 400 应当结束本轮扫描")

    def test_first_available_response_wins_without_waiting_for_ground_priority(self):
        available_payload = {
            "code": 200,
            "data": {
                "configList": [
                    {
                        "date": "2099-01-01",
                        "timeBlockList": [
                            {"time": "20:00", "endTime": "21:00", "status": 1}
                        ],
                    }
                ]
            },
        }
        session = RacingSession(
            {
                "slow-ground": available_payload,
                "fast-ground": available_payload,
            }
        )
        config = self._config()
        config["ground_url"] = {"1": "slow-ground", "2": "fast-ground"}
        config["ground_name"] = {"1": "1号场", "2": "2号场"}
        config["ground_priority"] = ["1", "2"]
        config["scan"]["max_concurrent_requests"] = 2
        scanner = AvailabilityScanner(config, session=session)
        target = ReservationTarget(
            datetime(2099, 1, 1, 20, 0), datetime(2099, 1, 1, 21, 0)
        )

        try:
            candidate = scanner.find_first_available([target])
            slow_finished_before_return = session.slow_finished.is_set()
        finally:
            session.release_slow.set()
            session.slow_finished.wait(timeout=1.0)

        self.assertIsNotNone(candidate)
        self.assertEqual("2", candidate.ground_key)
        self.assertFalse(slow_finished_before_return)

    def test_time_target_priority_is_preserved_across_venues(self):
        session = GroundPayloadSession(
            {
                "ground-1": {
                    "code": 200,
                    "data": {
                        "configList": [
                            {
                                "date": "2099-01-01",
                                "timeBlockList": [
                                    {
                                        "time": "20:00",
                                        "endTime": "21:00",
                                        "status": 1,
                                    },
                                    {
                                        "time": "21:00",
                                        "endTime": "22:00",
                                        "status": 2,
                                    },
                                ],
                            }
                        ]
                    },
                },
                "ground-2": {
                    "code": 200,
                    "data": {
                        "configList": [
                            {
                                "date": "2099-01-01",
                                "timeBlockList": [
                                    {
                                        "time": "20:00",
                                        "endTime": "21:00",
                                        "status": 2,
                                    },
                                    {
                                        "time": "21:00",
                                        "endTime": "22:00",
                                        "status": 1,
                                    },
                                ],
                            }
                        ]
                    },
                },
            }
        )
        config = self._config()
        config["ground_url"] = {"1": "ground-1", "2": "ground-2"}
        config["ground_name"] = {"1": "1号场", "2": "2号场"}
        config["ground_priority"] = ["2", "1"]
        config["scan"]["max_concurrent_requests"] = 2
        scanner = AvailabilityScanner(config, session=session)
        targets = [
            ReservationTarget(
                datetime(2099, 1, 1, 20, 0), datetime(2099, 1, 1, 21, 0)
            ),
            ReservationTarget(
                datetime(2099, 1, 1, 21, 0), datetime(2099, 1, 1, 22, 0)
            ),
        ]

        candidate = scanner.find_first_available(targets)

        self.assertIsNotNone(candidate)
        self.assertEqual(datetime(2099, 1, 1, 20, 0), candidate.target.start)
        self.assertEqual("1", candidate.ground_key)

if __name__ == "__main__":
    unittest.main()
