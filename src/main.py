import argparse
import random
import sys
import time
from datetime import datetime
from typing import Any, Dict

from Load_json import ConfigError, get_config
from browser_checkout import (
    BrowserCheckoutError,
    CheckoutStatus,
    run_browser_checkout,
)
from scanner import (
    AuthenticationError,
    AvailabilityScanner,
    ScannerError,
    build_targets,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="扫描可预约时段，并在官方页面中等待人工完成旋转验证码。"
    )
    parser.add_argument("--config", help="配置文件路径；默认优先读取 config.local.json")
    parser.add_argument(
        "--once",
        action="store_true",
        help="只扫描一轮；没有空位时立即退出",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="命中空位后只打印结果，不打开浏览器",
    )
    return parser.parse_args()


def _looks_like_placeholder(value: Any) -> bool:
    normalized = str(value or "").strip().lower()
    return not normalized or normalized.startswith("fill ") or "your own" in normalized


def _validate_runtime_config(config: Dict[str, Any]) -> None:
    if _looks_like_placeholder(config.get("token")):
        raise ConfigError("请先在配置文件中填写当前企业微信会话的 token")
    if _looks_like_placeholder(config.get("user_id")):
        raise ConfigError("请先填写 user_id（当前前端用户对象里的 wxOpenid）")
    ground_urls = config.get("ground_url")
    if not isinstance(ground_urls, dict) or not ground_urls:
        raise ConfigError("ground_url 至少需要配置一个场地")

    try:
        interval = float(config["scan"]["interval_seconds"])
        timeout = float(config["scan"]["request_timeout_seconds"])
        max_concurrent = int(config["scan"]["max_concurrent_requests"])
        min_duration = int(config["scan"]["min_duration_minutes"])
        release_grace = float(config["scan"]["release_grace_seconds"])
        datetime.strptime(str(config["scan"]["third_day_release_time"]), "%H:%M")
        user_num = int(config["user_num"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(
            "scan 时间参数、放出时间、最短预约时长和 user_num 配置无效"
        ) from exc
    if interval <= 0 or timeout <= 0:
        raise ConfigError("扫描间隔和请求超时必须大于 0")
    if max_concurrent <= 0:
        raise ConfigError("scan.max_concurrent_requests 必须大于 0")
    if release_grace < 0:
        raise ConfigError("scan.release_grace_seconds 不能小于 0")
    selection_mode = str(config["scan"]["selection_mode"]).strip().lower()
    if selection_mode not in {"exact", "longest_contiguous"}:
        raise ConfigError(
            "scan.selection_mode 必须是 exact 或 longest_contiguous"
        )
    if min_duration <= 0:
        raise ConfigError("scan.min_duration_minutes 必须大于 0")
    if user_num <= 0:
        raise ConfigError("user_num 必须大于 0")


def _timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _wait_for_next_round(config: Dict[str, Any]) -> None:
    scan = config["scan"]
    interval = max(float(scan["interval_seconds"]), 1.0)
    jitter = max(float(scan["jitter_seconds"]), 0.0)
    time.sleep(interval + random.uniform(0.0, jitter))


def main() -> int:
    args = _parse_args()
    try:
        config = get_config(args.config)
        _validate_runtime_config(config)
        targets = build_targets(config)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    scanner = AvailabilityScanner(config)
    configured_max_rounds = int(config["scan"].get("max_rounds", 0))
    max_rounds = 1 if args.once else configured_max_rounds
    round_number = 0

    print(f"配置文件: {config['_config_path']}")
    print(
        f"开始扫描：{len(targets)} 个目标时段，"
        f"{len(config['ground_url'])} 个候选场地，"
        f"最多 {config['scan']['max_concurrent_requests']} 路并发。按 Ctrl+C 停止。"
    )

    try:
        while max_rounds <= 0 or round_number < max_rounds:
            round_number += 1
            print(f"[{_timestamp()}] 第 {round_number} 轮扫描...", flush=True)
            try:
                candidate = scanner.find_first_available(targets)
            except AuthenticationError as exc:
                print(f"认证失效: {exc}\n请从企业微信页面更新 token/user_id 后重新运行。", file=sys.stderr)
                return 3
            except ScannerError as exc:
                print(f"本轮扫描失败: {exc}", file=sys.stderr)
                if max_rounds == 1:
                    return 4
                _wait_for_next_round(config)
                continue

            if candidate is None:
                if scanner.unreleased_dates:
                    dates = "、".join(scanner.unreleased_dates)
                    print(
                        f"[{_timestamp()}] 目标日期 {dates} 的场次尚未放出，"
                        "等待下一轮。"
                    )
                else:
                    print(f"[{_timestamp()}] 暂无符合条件的连续空闲时段。")
                if max_rounds == 1:
                    return 1
                _wait_for_next_round(config)
                continue

            print(
                f"发现空位：{candidate.ground_name}，"
                f"{candidate.target.start:%Y-%m-%d %H:%M} - "
                f"{candidate.target.end:%H:%M}"
            )
            if args.dry_run:
                print(f"官方页面: {candidate.checkout_url}")
                return 0

            try:
                result = run_browser_checkout(config, candidate)
            except BrowserCheckoutError as exc:
                print(f"浏览器流程失败: {exc}", file=sys.stderr)
                return 5

            if result.status is CheckoutStatus.SUCCESS:
                print("预约成功。")
                if result.order:
                    order_number = result.order.get("orderNo", "")
                    if order_number:
                        print(f"订单号: {order_number}")
                return 0

            if result.status is CheckoutStatus.STALE:
                print("该空位在打开页面时已被占用，恢复扫描。")
                if max_rounds == 1:
                    return 1
                _wait_for_next_round(config)
                continue

            print(f"预约未成功: {result.message}", file=sys.stderr)
            return 6
    except KeyboardInterrupt:
        print("\n已停止扫描。")
        return 130

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
