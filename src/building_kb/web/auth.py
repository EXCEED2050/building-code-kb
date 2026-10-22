"""访问口令。

这一层刻意做得很薄：一个全员共用的口令，验过就发一张有期限的签名票，
不建账号、不存手机号、不记谁问了什么。

理由是合规而不是偷懒。这套东西要挂在微信里给同事扫码用，一旦开始收集
个人信息，就落进《个人信息保护法》的适用范围，得有隐私政策、告知同意、
留存期限那一整套。查规范这件事根本不需要知道是谁在查，那就一个字段都别碰。

口令本身只从环境变量读，不写进代码也不进仓库——仓库是会被拷来拷去的。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

COOKIE = "kb_session"
TTL = 12 * 3600  # 一张票管半天；过期重新输口令，比长期有效安全


class NoPasscode(RuntimeError):
    pass


_DEV_CODE = ""


def passcode() -> str:
    global _DEV_CODE
    code = os.environ.get("KB_PASSCODE", "").strip()
    if code:
        return code

    # 本地验证用。必须显式打开 KB_DEV，且口令是每次启动现随机生成、只打在日志里——
    # 这样既能在本机把页面跑起来，仓库里也不会躺着一个可用的口令。
    # 绝不能在这里放固定的默认值：带默认口令的内部系统等于没有口令，
    # 而这种默认值最后总会跟着代码一起上线。
    if os.environ.get("KB_DEV") == "1":
        if not _DEV_CODE:
            _DEV_CODE = secrets.token_hex(4)
            print(f"\n[本地开发] 本次访问口令：{_DEV_CODE}\n"
                  f"[本地开发] 仅供本机验证，正式部署必须设 KB_PASSCODE\n", flush=True)
        return _DEV_CODE

    raise NoPasscode(
        "没设访问口令。启动前先设： export KB_PASSCODE='你的口令'\n"
        "本机试跑可以用 KB_DEV=1，会现生成一个随机口令打在日志里。\n"
        "不给默认值是有意的——一个有默认口令的内部系统，等于没有口令。"
    )


# 每次启动换一把新钥匙：重启即全员重新输口令。
# 这是有意的取舍——口令万一外传，重启一次就能把已发出去的票全作废。
# 想让票跨重启存活就设 KB_SECRET。
_SECRET = os.environ.get("KB_SECRET", "").encode() or secrets.token_bytes(32)


def _sign(payload: str) -> str:
    return hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()


def issue() -> str:
    """发一张票：到期时间 + 签名。"""
    exp = str(int(time.time()) + TTL)
    return f"{exp}.{_sign(exp)}"


def valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.rsplit(".", 1)
    # 必须用 compare_digest：普通 == 会因为提前返回而泄漏比对了多少位，
    # 理论上能被一点点试出正确签名
    if not hmac.compare_digest(sig, _sign(exp)):
        return False
    try:
        return int(exp) > time.time()
    except ValueError:
        return False


def check(given: str) -> bool:
    """核口令。同样走 compare_digest，理由同上。"""
    try:
        return hmac.compare_digest(given.strip(), passcode())
    except NoPasscode:
        return False
