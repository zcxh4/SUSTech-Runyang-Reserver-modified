"""Compatibility notice for the removed AJ-Captcha implementation.

The production site now uses a TianAi ROTATE challenge.  The project deliberately
keeps that challenge inside the official visible page so a person can complete it.
See browser_checkout.py for the supported flow.
"""


def Verification(*_args, **_kwargs):
    raise RuntimeError(
        "自动验证码模块已停用；请运行 main.py 并在打开的官方页面中人工完成旋转验证码"
    )


if __name__ == "__main__":
    Verification()
