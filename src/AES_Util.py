"""Legacy compatibility module.

The old AJ-Captcha AES protocol is no longer used by the production site. The
current workflow delegates ROTATE verification to the official visible widget.
"""


def _removed(*_args, **_kwargs):
    raise RuntimeError("旧版 AJ-Captcha AES 流程已停用")


aes_decrypt_by_bytes = _removed
aes_encrypt_by_bytes = _removed
