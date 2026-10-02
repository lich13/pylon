"""Explicit policy for software actions exposed through pylon.

The Telegram user never supplies an arbitrary APT package name to the root
worker.  Keep this allowlist small and reviewable; packages outside it remain
read-only inventory data.
"""
from __future__ import annotations


APT_PACKAGE_POLICY = {
    "libegl-mesa0": {
        "name": "Mesa EGL",
        "risk": "图形库小版本更新；可能影响浏览器运行时，通常无需重启主机。",
    },
    "libgbm1": {
        "name": "Mesa GBM",
        "risk": "图形库小版本更新；可能影响浏览器运行时，通常无需重启主机。",
    },
    "libgl1-mesa-dri": {
        "name": "Mesa OpenGL 驱动",
        "risk": "图形库小版本更新；可能影响浏览器运行时，通常无需重启主机。",
    },
    "libglx-mesa0": {
        "name": "Mesa GLX",
        "risk": "图形库小版本更新；可能影响浏览器运行时，通常无需重启主机。",
    },
    "mesa-libgallium": {
        "name": "Mesa Gallium",
        "risk": "图形库小版本更新；可能影响浏览器运行时，通常无需重启主机。",
    },
    "sosreport": {
        "name": "sosreport",
        "risk": "诊断工具更新；不修改业务数据。",
    },
    "thermald": {
        "name": "thermald",
        "risk": "系统服务小版本更新；安装时可能重载 thermald，不自动重启主机。",
    },
}


def apt_policy(package: str) -> dict[str, str] | None:
    value = APT_PACKAGE_POLICY.get(str(package or ""))
    return dict(value) if value else None


def is_allowed_apt_package(package: str) -> bool:
    return str(package or "") in APT_PACKAGE_POLICY
