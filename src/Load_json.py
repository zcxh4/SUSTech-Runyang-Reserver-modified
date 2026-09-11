import json
from pathlib import Path
from typing import Any, Dict, Optional, Union


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
LOCAL_CONFIG_PATH = PROJECT_ROOT / "config.local.json"


class ConfigError(ValueError):
    """Raised when the local configuration cannot be used safely."""


def resolve_config_path(config_path: Optional[Union[str, Path]] = None) -> Path:
    if config_path:
        path = Path(config_path).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        return path

    if LOCAL_CONFIG_PATH.exists():
        return LOCAL_CONFIG_PATH
    return DEFAULT_CONFIG_PATH


def get_config(config_path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Load configuration independently of the process working directory."""

    path = resolve_config_path(config_path)
    try:
        with path.open("r", encoding="utf-8") as file:
            config = json.load(file)
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是有效 JSON: {path}: {exc}") from exc

    if not isinstance(config, dict):
        raise ConfigError("配置文件顶层必须是 JSON 对象")

    # Backward compatibility: older versions called this value student_id.
    config.setdefault("user_id", config.get("student_id", ""))
    config.setdefault("user_num", 1)
    config.setdefault("ground_priority", list(config.get("ground_url", {}).keys()))

    scan = config.setdefault("scan", {})
    scan.setdefault("interval_seconds", 5.0)
    scan.setdefault("jitter_seconds", 0.5)
    scan.setdefault("request_timeout_seconds", 8.0)
    scan.setdefault("max_rounds", 0)
    scan.setdefault("max_concurrent_requests", 3)
    scan.setdefault("selection_mode", "exact")
    scan.setdefault("min_duration_minutes", 30)
    scan.setdefault("third_day_release_time", "20:00")
    scan.setdefault("release_grace_seconds", 3.0)

    browser = config.setdefault("browser", {})
    browser.setdefault("channel", "msedge")
    browser.setdefault("navigation_timeout_seconds", 30)
    browser.setdefault("captcha_timeout_seconds", 300)
    browser.setdefault("slow_mo_ms", 0)

    config["_config_path"] = str(path)
    return config
