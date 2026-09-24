import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlsplit
from uuid import uuid4

from scanner import AvailableCandidate


ORIGIN = "https://reservation.sustech.edu.cn"
DIAGNOSTICS_DIR = Path(__file__).resolve().parents[1] / "diagnostics"


class BrowserCheckoutError(RuntimeError):
    """The visible official-page checkout could not be completed."""


class CheckoutStatus(Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    STALE = "stale"


@dataclass(frozen=True)
class CheckoutResult:
    status: CheckoutStatus
    message: str = ""
    order: Optional[Dict[str, Any]] = None


def _redact_diagnostic_text(value: Any, config: Mapping[str, Any]) -> str:
    text = str(value or "")
    for key in ("token", "user_id", "student_tel"):
        secret = str(config.get(key) or "")
        if secret:
            text = text.replace(secret, "[已隐藏]")
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[已隐藏手机号]", text)
    text = re.sub(
        r"(?i)(\b(?:captchaid|captchatoken|captchaverification|pointjson|"
        r"verification|authorization|token|wxopenid|userid)\b\s*[:=]\s*)"
        r"[^\s,;，；}\]]+",
        r"\1[已隐藏]",
        text,
    )
    return text[:1000]


class CheckoutDiagnostics:
    """Keep only safe response metadata for the official order submission."""

    def __init__(self, config: Mapping[str, Any], candidate: AvailableCandidate):
        self.config = config
        self.candidate = candidate
        self.responses: List[Any] = []

    def record_response(self, response: Any) -> None:
        if urlsplit(response.url).path.rstrip("/").lower().endswith("/saveorder"):
            self.responses.append(response)

    def write(
        self,
        outcome: str,
        message: str = "",
        output_dir: Path = DIAGNOSTICS_DIR,
    ) -> Path:
        lines = [
            f"记录时间: {datetime.now().astimezone().isoformat(timespec='seconds')}",
            f"预约结果: {outcome}",
            f"场地: {self.candidate.ground_name} ({self.candidate.ground_id})",
            f"时段: {self.candidate.target.start:%Y-%m-%d %H:%M} - "
            f"{self.candidate.target.end:%H:%M}",
        ]
        if message:
            lines.append(f"页面提示: {_redact_diagnostic_text(message, self.config)}")

        if not self.responses:
            lines.append("saveOrder 响应: 未收到")
        for index, response in enumerate(self.responses, start=1):
            lines.extend(
                [
                    f"saveOrder 响应 #{index}:",
                    f"  HTTP 状态: {response.status}",
                    f"  路径: {urlsplit(response.url).path}",
                ]
            )
            try:
                body = response.json()
            except Exception:
                lines.append("  响应体: 无法解析为 JSON 或未能读取")
                continue
            if not isinstance(body, Mapping):
                lines.append("  响应体: 非 JSON 对象")
                continue
            for key in ("code", "success", "msg", "message", "error", "traceId"):
                value = body.get(key)
                if isinstance(value, (str, int, float, bool)):
                    lines.append(
                        f"  {key}: {_redact_diagnostic_text(value, self.config)}"
                    )

        output_dir.mkdir(parents=True, exist_ok=True)
        filename = (
            f"checkout-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}.txt"
        )
        path = output_dir / filename
        with path.open("x", encoding="utf-8") as file:
            file.write("\n".join(lines) + "\n")
        return path


def _vuex_state(config: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "lang": "zh",
        "user": {
            "wxOpenid": str(config["user_id"]),
            "token": str(config["token"]),
        },
        "gym": {"canOrderMaxDay": "", "minOrderTime": ""},
        "code": "",
        "isHaveCancelBtn": False,
    }


def _slot_locator(page: Any, label: str) -> Any:
    slots = page.locator("ul.list li")
    normalized_label = "".join(label.split())
    for index in range(slots.count()):
        slot = slots.nth(index)
        if "".join(slot.inner_text().split()) == normalized_label:
            return slot
    raise BrowserCheckoutError(f"官方页面中没有找到时段 {label}")


def _visible_toast_messages(page: Any) -> str:
    try:
        values = page.locator(".van-toast__text, .van-dialog__message").all_text_contents()
    except Exception:
        return ""
    return "；".join(value.strip() for value in values if value.strip())


def _launch_browser(playwright: Any, browser_config: Mapping[str, Any]) -> Any:
    channel = browser_config.get("channel")
    kwargs = {
        "headless": False,
        "slow_mo": max(int(browser_config.get("slow_mo_ms", 0)), 0),
    }
    try:
        if channel:
            return playwright.chromium.launch(channel=str(channel), **kwargs)
        return playwright.chromium.launch(**kwargs)
    except Exception as exc:
        hint = (
            f"无法启动浏览器 channel={channel!r}。可安装 Edge/Chrome，或执行 "
            "`python -m playwright install chromium` 后将 browser.channel 设为 null。"
        )
        raise BrowserCheckoutError(hint) from exc


def run_browser_checkout(
    config: Mapping[str, Any], candidate: AvailableCandidate
) -> CheckoutResult:
    """Use the official UI and stop for the user to solve the official CAPTCHA."""

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserCheckoutError(
            "缺少 Playwright，请先执行 `pip install -r requirements.txt`"
        ) from exc

    browser_config = config["browser"]
    navigation_timeout_ms = int(browser_config["navigation_timeout_seconds"] * 1000)
    captcha_timeout_ms = int(browser_config["captcha_timeout_seconds"] * 1000)
    state_text = json.dumps(
        _vuex_state(config), ensure_ascii=False, separators=(",", ":")
    )
    init_script = (
        f"if (location.origin === {json.dumps(ORIGIN)}) {{"
        f"localStorage.setItem('vuex', {json.dumps(state_text)});"
        "}"
    )
    diagnostics = CheckoutDiagnostics(config, candidate)
    checkout_result: Optional[CheckoutResult] = None
    terminal_error = ""

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright, browser_config)
        page = None
        try:
            context = browser.new_context(viewport={"width": 430, "height": 900})
            context.add_init_script(script=init_script)
            page = context.new_page()
            page.on("response", diagnostics.record_response)
            page.set_default_timeout(navigation_timeout_ms)
            page.goto(candidate.checkout_url, wait_until="domcontentloaded")

            if "open.weixin.qq.com" in page.url:
                raise BrowserCheckoutError("token 已失效，官方页面要求重新进行企业微信授权")

            page.wait_for_selector("ul.list li", state="visible")
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightTimeoutError:
                pass

            first_block = candidate.blocks[0]
            last_block = candidate.blocks[-1]
            block_labels = [
                f"{block['time']}-{block['endTime']}" for block in candidate.blocks
            ]
            slots = [_slot_locator(page, label) for label in block_labels]

            # The official page selects a range by clicking its first and last
            # blocks. Recheck every block immediately before doing so: checking
            # only the endpoints could otherwise include a newly occupied middle
            # block when availability changed after the API scan.
            for slot in slots:
                classes = (slot.get_attribute("class") or "").split()
                if "no" in classes or "full" in classes:
                    checkout_result = CheckoutResult(
                        CheckoutStatus.STALE,
                        "目标区间内至少有一个时段在官方页面中已经不可预约",
                    )
                    return checkout_result

            slots[0].click()
            if len(slots) > 1:
                slots[-1].click()

            selected_text = page.locator(".select-time-wrap").inner_text()
            expected_start = str(first_block["time"])
            expected_end = str(last_block["endTime"])
            if expected_start not in selected_text or expected_end not in selected_text:
                raise BrowserCheckoutError(
                    f"时段预选校验失败，页面显示: {selected_text.strip()}"
                )

            phone_input = page.locator('input[name="customerTel"]')
            if phone_input.count() and config.get("student_tel"):
                phone_input.fill(str(config["student_tel"]))

            user_num_input = page.locator('input[name="userNum"]')
            if user_num_input.count():
                user_num_input.fill(str(config.get("user_num", 1)))

            title_input = page.locator('input[name="title"]')
            if title_input.count() and title_input.is_visible():
                title = str(config.get("title", "场地使用"))
                title_input.fill(title)

            reserve_button = page.get_by_role(
                "button", name=re.compile(r"预约|Reserve", re.IGNORECASE)
            ).last
            reserve_button.click()

            try:
                page.wait_for_selector(
                    "#tianai-captcha-parent", state="attached", timeout=20_000
                )
            except PlaywrightTimeoutError as exc:
                message = _visible_toast_messages(page) or "官方验证码窗口没有出现"
                if "open.weixin.qq.com" in page.url:
                    message = "token 已失效，官方页面要求重新授权"
                raise BrowserCheckoutError(message) from exc

            try:
                import winsound

                winsound.MessageBeep()
            except (ImportError, OSError, RuntimeError):
                pass

            print("浏览器已停在官方旋转验证码，请手动完成；通过后页面会自动提交。")
            try:
                page.wait_for_function(
                    "location.hash.includes('/reservation/success') || "
                    "location.hash.includes('/reservation/fail')",
                    timeout=captcha_timeout_ms,
                )
            except PlaywrightTimeoutError:
                checkout_result = CheckoutResult(
                    CheckoutStatus.FAILURE,
                    "等待人工验证码超时或验证码窗口被关闭",
                )
                return checkout_result

            if "/reservation/success" in page.url:
                raw_order = page.evaluate("localStorage.getItem('order')")
                try:
                    order = json.loads(raw_order) if raw_order else None
                except ValueError:
                    order = None
                checkout_result = CheckoutResult(CheckoutStatus.SUCCESS, order=order)
                return checkout_result

            message = _visible_toast_messages(page) or "官方页面返回预约失败"
            checkout_result = CheckoutResult(CheckoutStatus.FAILURE, message)
            return checkout_result
        except BrowserCheckoutError as exc:
            terminal_error = str(exc)
            raise
        except PlaywrightTimeoutError as exc:
            message = (
                _visible_toast_messages(page) if page is not None else ""
            ) or "等待官方页面加载超时"
            if page is not None and "open.weixin.qq.com" in page.url:
                message = "token 已失效，官方页面要求重新授权"
            terminal_error = message
            raise BrowserCheckoutError(message) from exc
        except Exception as exc:
            terminal_error = f"官方页面自动操作失败: {exc}"
            raise BrowserCheckoutError(f"官方页面自动操作失败: {exc}") from exc
        finally:
            if checkout_result is None or checkout_result.status is not CheckoutStatus.STALE:
                outcome = (
                    checkout_result.status.value
                    if checkout_result is not None
                    else "error"
                )
                message = (
                    checkout_result.message
                    if checkout_result is not None
                    else terminal_error
                )
                try:
                    path = diagnostics.write(outcome, message)
                    print(f"浏览器响应快照: {path}")
                except Exception as exc:
                    print(
                        f"无法保存浏览器响应快照: {exc.__class__.__name__}",
                        file=sys.stderr,
                    )
            browser.close()
