"""把访问地址生成二维码，贴在图板或群公告里扫。

    python -m building_kb.commands.qr https://规范.example.com

为什么不用网上那些「在线二维码生成器」：那等于把内部系统的地址交给
一个不相干的网站。地址本身就是半个口令——知道地址的人才有机会试口令。
在本机生成，一个字节都不出去。

依赖 qrcode（纯 Python，很小）。没装的话下面会提示怎么装。
"""

from __future__ import annotations

from building_kb import paths

import sys
from pathlib import Path

OUT = paths.VAR / "qr"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    url = argv[0].strip()

    try:
        import qrcode
    except ImportError:
        print(
            "没装 qrcode。先装一下：\n"
            "    python -m pip install qrcode\n"
            "（别用在线生成器——那会把内部地址发给第三方网站）",
            file=sys.stderr,
        )
        return 1

    # 微信里扫码打开网页有两条硬要求，现在提醒比上线后返工便宜：
    if not url.startswith("https://"):
        print("! 地址不是 https。微信内置浏览器对 http 页面会拦一道警告，"
              "同事多半就退出去了。", file=sys.stderr)
    if "localhost" in url or "127.0.0.1" in url:
        print("! 这是本机地址，只有你这台电脑能打开，扫了也没用。", file=sys.stderr)

    qr = qrcode.QRCode(
        # 纠错等级 H：印出来贴墙上会脏、会折、会被挡住一角，
        # 留 30% 冗余，缺一块照样能扫出来
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)

    OUT.mkdir(parents=True, exist_ok=True)
    png = OUT / "access.png"
    qr.make_image(fill_color="black", back_color="white").save(png)

    # 顺手在终端里打一个，扫一下就知道对不对，不用先去翻文件
    qr.print_ascii(invert=True)
    print(f"\n地址：{url}")
    print(f"图片：{png}")
    print("扫之前自己先扫一遍，确认跳转的是这个地址再发出去。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
