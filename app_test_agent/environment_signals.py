"""Conservative visible-text detection for execution environment blockers."""

from __future__ import annotations

from collections.abc import Iterable


STRONG_ENV_BLOCKER_PHRASES = (
    "isn't responding",
    "not responding",
    "no available way to open",
    "no available opener",
    "log in",
    "please log in",
    "login required",
    "请先登录",
    "登录后查看",
    "网络异常",
    "无网络",
    "未连接",
    "暂无可用打开方式",
    "崩溃",
    "无响应",
)

EXACT_ENV_BLOCKER_LABELS = (
    "crash", "anr", "login", "log in", "sign in", "permission",
    "network", "offline", "retry", "登录", "权限", "网络", "重试",
)


def detect_environment_blocker(texts: Iterable[object]) -> str | None:
    """Return a blocker without matching generic words inside unrelated content."""

    normalized = [str(item).strip().casefold() for item in texts if str(item).strip()]
    for text in normalized:
        for phrase in STRONG_ENV_BLOCKER_PHRASES:
            if phrase.casefold() in text:
                return phrase
        for label in EXACT_ENV_BLOCKER_LABELS:
            if text == label.casefold():
                return label
    return None
