import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Optional

from scanner import AvailableCandidate


ORIGIN = "https://reservation.sustech.edu.cn"


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

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright, browser_config)
        page = None
        try:
            context = browser.new_context(viewport={"width": 430, "height": 900})
            context.add_init_script(script=init_script)
            page = context.new_page()
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
                    return CheckoutResult(
                        CheckoutStatus.STALE,
                        "目标区间内至少有一个时段在官方页面中已经不可预约",
                    )

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
                return CheckoutResult(
                    CheckoutStatus.FAILURE,
                    "等待人工验证码超时或验证码窗口被关闭",
                )

            if "/reservation/success" in page.url:
                raw_order = page.evaluate("localStorage.getItem('order')")
                try:
                    order = json.loads(raw_order) if raw_order else None
                except ValueError:
                    order = None
                return CheckoutResult(CheckoutStatus.SUCCESS, order=order)

            message = _visible_toast_messages(page) or "官方页面返回预约失败"
            return CheckoutResult(CheckoutStatus.FAILURE, message)
        except BrowserCheckoutError:
            raise
        except PlaywrightTimeoutError as exc:
            message = (
                _visible_toast_messages(page) if page is not None else ""
            ) or "等待官方页面加载超时"
            if page is not None and "open.weixin.qq.com" in page.url:
                message = "token 已失效，官方页面要求重新授权"
            raise BrowserCheckoutError(message) from exc
        except Exception as exc:
            raise BrowserCheckoutError(f"官方页面自动操作失败: {exc}") from exc
        finally:
            browser.close()
