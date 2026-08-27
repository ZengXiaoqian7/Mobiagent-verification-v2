from openai import OpenAI
import uiautomator2 as u2
import base64
from PIL import Image
import json
import io
import logging
import time
import re
import os
import argparse
import atexit
import textwrap
import cv2
import sys
import requests
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from PIL import Image, ImageDraw, ImageFont


# Experience retrieval is loaded lazily because App-test step execution does
# not need the local index or its default LLM configuration.
PromptTemplateSearch = None
from pathlib import Path
try:
    from hmdriver2.driver import Driver
    from hmdriver2.proto import KeyCode
except ModuleNotFoundError:
    Driver = None

    class _MissingKeyCode:
        pass

    KeyCode = _MissingKeyCode()
from utils.load_md_prompt import load_prompt
from dotenv import load_dotenv
from .decider_adapters import (
    DECIDER_PROTOCOL_QWEN_JSON,
    DECIDER_PROTOCOL_STEPFUN_TSV,
    SUPPORTED_DECIDER_PROTOCOLS,
    get_decider_adapter,
)
from .json_utils import load_json_from_text as _load_json_from_text
from .json_utils import robust_json_loads
from .user_preference_extractor import (
    PreferenceExtractor, 
    retrieve_user_preferences, 
    should_extract_preferences,
    combine_context
)
 
# 清除可能已存在的 handlers，避免重复配置
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout,
    force=True  # Python 3.8+ 支持 force=True，强制重置
)
# >>>>>>>>>> logging 配置结束 <<<<<<<<<<



# 截图缩放比例
factor = 0.5

# ============ 常数定义 ============
MAX_STEPS = 15
MAX_RETRIES = 5
TEMP_INCREMENT = 0.1
INITIAL_TEMP = 0.0
API_TIMEOUT = 30
DECIDER_MAX_TOKENS = 256
GROUNDER_MAX_TOKENS = 128
DEVICE_WAIT_TIME = 0.5
APP_STOP_WAIT = 3
CLICK_INPUT_FOCUS_WAIT = float(os.getenv("MOBIAGENT_CLICK_INPUT_FOCUS_WAIT", "1.0"))
COORD_MODE_RESIZED_PIXEL = "resized_pixel"
COORD_MODE_QWEN_NORMALIZED = "qwen_normalized"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FONT_PATH = PROJECT_ROOT / "msyh.ttf"


def load_overlay_font(size):
    try:
        return ImageFont.truetype(str(DEFAULT_FONT_PATH), size)
    except OSError:
        return ImageFont.load_default()
COORD_MODE_AUTO = "auto"
SUPPORTED_COORD_MODES = {
    COORD_MODE_RESIZED_PIXEL,
    COORD_MODE_QWEN_NORMALIZED,
    COORD_MODE_AUTO,
}
DECIDER_COORD_MODE = os.getenv("MOBIAGENT_COORD_MODE", COORD_MODE_RESIZED_PIXEL).strip().lower()

ANSI_RESET = "\033[0m"
ANSI_REASONING_GREEN = "\033[92m"

# 滑动坐标缩放比例
SWIPE_V_START = 0.3
SWIPE_V_END = 0.7
SWIPE_H_START = 0.3
SWIPE_H_END = 0.7

class Device(ABC):
    @abstractmethod
    def start_app(self, app):
        pass
    
    @abstractmethod
    def app_stop(self, package_name):
        pass

    @abstractmethod
    def screenshot(self, path):
        pass

    @abstractmethod
    def click(self, x, y):
        pass

    @abstractmethod
    def input(self, text):
        pass

    @abstractmethod
    def swipe(self, direction):
        pass

    @abstractmethod
    def swipe_with_coords(self, start_x, start_y, end_x, end_y):
        pass

    @abstractmethod
    def long_press(self, x, y, duration=1.0):
        pass

    @abstractmethod
    def keyevent(self, key):
        pass

    @abstractmethod
    def dump_hierarchy(self):
        pass

    def click_coordinate_size(self):
        return None

class AndroidDevice(Device):
    def __init__(self, adb_endpoint=None):
        super().__init__()
        if adb_endpoint:
            self.d = u2.connect(adb_endpoint)
        else:
            self.d = u2.connect()
        self.app_package_names = {
            "携程": "ctrip.android.view",
            "同城": "com.tongcheng.android",
            "飞猪": "com.taobao.trip",
            "去哪儿": "com.Qunar",
            "华住会": "com.htinns",
            "饿了么": "me.ele",
            "支付宝": "com.eg.android.AlipayGphone",
            "淘宝": "com.taobao.taobao",
            "京东": "com.jingdong.app.mall",
            "美团": "com.sankuai.meituan",
            "滴滴出行": "com.sdu.didi.psnger",
            "微信": "com.tencent.mm",
            "微博": "com.sina.weibo",
            "携程": "ctrip.android.view",
            "华为商城": "com.vmall.client",
            "华为视频": "com.huawei.himovie",
            "华为音乐": "com.huawei.music",
            "华为应用市场": "com.huawei.appmarket",
            "拼多多": "com.xunmeng.pinduoduo",
            "大众点评": "com.dianping.v1",
            "小红书": "com.xingin.xhs",
            "bilibili": "tv.danmaku.bili",
            "网易云音乐": "com.netease.cloudmusic",
            "高德": "com.autonavi.minimap",
            "浏览器": "com.microsoft.emmx"
        }

    def start_app(self, app):
        package_name = self.app_package_names.get(app)
        if not package_name:
            raise ValueError(f"App '{app}' is not registered with a package name.")
        self.d.app_start(package_name, stop=True)
        time.sleep(3)
        if not self.d.app_wait(package_name, timeout=10):
            raise RuntimeError(f"Failed to start app '{app}' with package '{package_name}'")
    
    def app_start(self, package_name):
        self.d.app_start(package_name, stop=True)
        time.sleep(DEVICE_WAIT_TIME * 2)
        if not self.d.app_wait(package_name, timeout=10):
            raise RuntimeError(f"Failed to start package '{package_name}'")

    def app_stop(self, package_name):
        self.d.app_stop(package_name)

    def screenshot(self, path):
        self.d.screenshot(path)

    def click(self, x, y):
        self.d.click(x, y)
        time.sleep(DEVICE_WAIT_TIME)

    def clear_input(self):
    # 按下全选（需要 Android 支持 keyevent META_CTRL_ON）
        self.d.shell(['input', 'keyevent', 'KEYCODE_MOVE_END'])
        self.d.shell(['input', 'keyevent', 'KEYCODE_MOVE_HOME'])
        self.d.shell(['input', 'keyevent', 'KEYCODE_DEL'])

    def input(self, text):
        current_ime = self.d.current_ime()
        self.d.shell(['settings', 'put', 'secure', 'default_input_method', 'com.android.adbkeyboard/.AdbIME'])
        time.sleep(DEVICE_WAIT_TIME)
        # add clear text command, depending on 'ADB Keyboard'
        self.d.shell(['am', 'broadcast', '-a', 'ADB_CLEAR_TEXT'])
        time.sleep(0.2)

        charsb64 = base64.b64encode(text.encode('utf-8')).decode('utf-8')
        self.d.shell(['am', 'broadcast', '-a', 'ADB_INPUT_B64', '--es', 'msg', charsb64])
        time.sleep(DEVICE_WAIT_TIME)
        self.d.shell(['settings', 'put', 'secure', 'default_input_method', current_ime])
        # time.sleep(DEVICE_WAIT_TIME)
        # Press Enter key to confirm input
        self.d.shell(['input', 'keyevent', 'KEYCODE_ENTER'])


    def swipe(self, direction, scale=0.5):
        # self.d.swipe_ext(direction, scale)
        # self.d.swipe_ext(direction=direction, scale=scale)
        if direction.lower() == "up":
            self.d.swipe(0.5,SWIPE_V_END,0.5,SWIPE_V_START,duration=0.2)
        elif direction.lower() == "down":
            self.d.swipe(0.5,SWIPE_V_START,0.5,SWIPE_V_END,duration=0.2)
        elif direction.lower() == "left":
            self.d.swipe(SWIPE_H_END,0.5,SWIPE_H_START,0.5,duration=0.2)
        elif direction.lower() == "right":
            self.d.swipe(SWIPE_H_START,0.5,SWIPE_H_END,0.5,duration=0.2)

    def swipe_with_coords(self, start_x, start_y, end_x, end_y):
        """Swipe from (start_x, start_y) to (end_x, end_y)"""
        self.d.swipe(start_x, start_y, end_x, end_y, duration=0.2)

    def long_press(self, x, y, duration=1.0):
        if hasattr(self.d, "long_click"):
            self.d.long_click(x, y, duration=duration)
        else:
            self.d.swipe(x, y, x, y, duration=max(duration, 0.5))
        time.sleep(DEVICE_WAIT_TIME)

    def keyevent(self, key):
        self.d.keyevent(key)

    def dump_hierarchy(self):
        return self.d.dump_hierarchy()

    def click_coordinate_size(self):
        try:
            width, height = self.d.window_size()
            return int(width), int(height)
        except Exception as e:
            logging.warning("Failed to read Android window_size: %s", e)
            info = getattr(self.d, "info", {}) or {}
            width = info.get("displayWidth")
            height = info.get("displayHeight")
            if width and height:
                return int(width), int(height)
            return None

class HarmonyDevice(Device):
    def __init__(self, serial=None):
        if Driver is None:
            raise RuntimeError("HarmonyDevice requires hmdriver2 to be installed")
        super().__init__()
        self.d = Driver(serial=serial)
        # hmdriver2 otherwise releases its socket only from Driver.__del__ while
        # Python is already shutting down.  On Windows that can leave the CLI
        # alive after every task has completed.  Register an idempotent explicit
        # close while modules and logging are still usable.
        atexit.register(self.close)
        self.app_package_names = {
            "携程": "com.ctrip.harmonynext",
            "飞猪": "com.fliggy.hmos",
            "IntelliOS": "ohos.hongmeng.intellios",
            "同城": "com.tongcheng.hmos",
            "携程旅行": "com.ctrip.harmonynext",
            "饿了么": "me.ele.eleme",
            "知乎": "com.zhihu.hmos",
            "哔哩哔哩": "yylx.danmaku.bili",
            "微信": "com.tencent.wechat",
            "小红书": "com.xingin.xhs_hos",
            "网易云音乐": "com.netease.cloudmusic",
            "QQ音乐": "com.tencent.hm.qqmusic",
            "高德地图": "com.amap.hmapp",
            "淘宝": "com.taobao.taobao4hmos",
            "微博": "com.sina.weibo.stage",
            "京东": "com.jd.hm.mall",
            "飞猪旅行": "com.fliggy.hmos",
            "天气": "com.huawei.hmsapp.totemweather",
            "什么值得买": "com.smzdm.client.hmos",
            "闲鱼": "com.taobao.idlefish4ohos",
            "慧通差旅": "com.smartcom.itravelhm",
            "PowerAgent": "com.example.osagent",
            "航旅纵横": "com.umetrip.hm.app",
            "滴滴出行": "com.sdu.didi.hmos.psnger",
            "电子邮件": "com.huawei.hmos.email",
            "图库": "com.huawei.hmos.photos",
            "日历": "com.huawei.hmos.calendar",
            "心声社区": "com.huawei.it.hmxinsheng",
            "信息": "com.ohos.mms",
            "文件管理": "com.huawei.hmos.files",
            "运动健康": "com.huawei.hmos.health",
            "智慧生活": "com.huawei.hmos.ailife",
            "豆包": "com.larus.nova.hm",
            "WeLink": "com.huawei.it.welink",
            "设置": "com.huawei.hmos.settings",
            "懂车帝": "com.ss.dcar.auto",
            "美团外卖": "com.meituan.takeaway",
            "大众点评": "com.sankuai.dianping",
            "美团": "com.sankuai.hmeituan",
            "浏览器": "com.huawei.hmos.browser",
            "微博": "com.sina.weibo.stage",
            "饿了么": "me.ele.eleme",
            "拼多多": "com.xunmeng.pinduoduo.hos"
        }

    def close(self):
        """Release the Harmony UITest client exactly once."""
        driver = getattr(self, "d", None)
        client = getattr(driver, "_client", None)
        if client is None:
            return
        try:
            client.release()
        finally:
            # Prevent hmdriver2.Driver.__del__ from releasing the same client
            # again during interpreter teardown.
            driver._client = None
            serial = getattr(driver, "serial", None)
            if Driver._instance.get(serial) is driver:
                Driver._instance.pop(serial, None)

    def start_app(self, app):
        package_name = self.app_package_names.get(app)
        if not package_name:
            raise ValueError(f"App '{app}' is not registered with a package name.")
        # Mate X7 runs the Android CloudMusic package through Harmony's shell
        # assistant compatibility container. It has a mission/ability but no
        # native `bm dump` record, so hmdriver2.start_app() cannot resolve it.
        if app == "网易云音乐":
            self.d.hdc.shell(
                "aa start -b com.netease.cloudmusic "
                "-a com.netease.cloudmusic.activity.IconChangeDefaultAlias"
            )
        else:
            self.d.start_app(package_name)
        time.sleep(2)

    def app_start(self, package_name):
        if package_name == "com.netease.cloudmusic":
            self.d.hdc.shell(
                "aa start -b com.netease.cloudmusic "
                "-a com.netease.cloudmusic.activity.IconChangeDefaultAlias"
            )
        else:
            self.d.force_start_app(package_name)
        time.sleep(1.5)

    def app_stop(self, package_name):
        if package_name == "com.netease.cloudmusic":
            # Android-compatibility missions have no native bundle record, so
            # aa force-stop/hmdriver2.stop_app cannot address them reliably.
            self.d.press_key(1)
        else:
            self.d.stop_app(package_name)

    def screenshot(self, path):
        self.d.screenshot(path)

    def click(self, x, y):
        self.d.click(x, y)
        time.sleep(DEVICE_WAIT_TIME)

    def input(self, text):
        self.d.shell("uitest uiInput keyEvent 2072 2017")
        self.d.press_key(2071)
        self.d.input_text(text)
        if self._confirm_clipboard_suggestion(text):
            return
        # Some Harmony IMEs expose the clipboard candidate only after the
        # confirm key event. Never send it when the editor already contains the
        # requested value.
        self.d.press_key(KeyCode.ENTER)
        self._confirm_clipboard_suggestion(text)

    def _confirm_clipboard_suggestion(self, text):
        """Confirm Harmony IME clipboard suggestions without fixed coordinates."""
        deadline = time.time() + 1.5
        while time.time() < deadline:
            try:
                hierarchy = self.d.dump_hierarchy()
            except Exception as exc:  # noqa: BLE001 - input itself already succeeded.
                logging.debug("Harmony input hierarchy probe failed: %s", exc)
                return False
            if _harmony_input_already_present(hierarchy, text):
                return True
            candidate = _find_harmony_clipboard_candidate(hierarchy, text)
            if candidate is not None:
                x1, y1, x2, y2 = candidate
                self.d.click((x1 + x2) // 2, (y1 + y2) // 2)
                time.sleep(DEVICE_WAIT_TIME)
                logging.info("Confirmed Harmony clipboard input suggestion for text input")
                return True
            time.sleep(0.1)
        return False

    def swipe(self, direction, scale=0.5):
        # self.d.swipe_ext(direction, scale=scale)
        if direction.lower() == "up":
            self.d.swipe(0.5,SWIPE_V_END,0.5,SWIPE_V_START,speed=1000)
        elif direction.lower() == "down":
            self.d.swipe(0.5,SWIPE_V_START,0.5,SWIPE_V_END,speed=1000)
        elif direction.lower() == "left":
            self.d.swipe(SWIPE_H_END,0.5,SWIPE_H_START,0.5,speed=1000)
        elif direction.lower() == "right":
            self.d.swipe(SWIPE_H_START,0.5,SWIPE_H_END,0.5,speed=1000)

    def swipe_with_coords(self, start_x, start_y, end_x, end_y):
        """Perform a native-pixel swipe, preferring UITest's absolute command."""
        start_x, start_y, end_x, end_y = (
            int(start_x), int(start_y), int(end_x), int(end_y)
        )
        # hmdriver2 versions disagree on whether Driver.swipe's numeric
        # arguments are pixels or normalized ratios.  The mature mobiinfra
        # workflow uses uiInput for workflow-defined absolute swipes; retain
        # Driver.swipe only as a compatibility fallback when the shell bridge
        # is unavailable.
        hdc = getattr(self.d, "hdc", None)
        shell = getattr(hdc, "shell", None)
        if callable(shell):
            try:
                shell(f"uitest uiInput swipe {start_x} {start_y} {end_x} {end_y}")
                time.sleep(DEVICE_WAIT_TIME)
                return
            except Exception as exc:  # noqa: BLE001 - retain Driver fallback.
                logging.warning("Absolute UITest swipe failed; falling back to Driver.swipe: %s", exc)
        self.d.swipe(start_x, start_y, end_x, end_y, speed=1000)

    def long_press(self, x, y, duration=1.0):
        if hasattr(self.d, "long_click"):
            self.d.long_click(x, y)
        elif hasattr(self.d, "long_press"):
            self.d.long_press(x, y)
        else:
            self.d.swipe(x, y, x, y, speed=max(200, int(1000 / max(duration, 0.2))))
        time.sleep(DEVICE_WAIT_TIME)

    def keyevent(self, key):
        self.d.press_key(key)

    def dump_hierarchy(self):
        return self.d.dump_hierarchy()

    def click_coordinate_size(self):
        return None


def _find_harmony_clipboard_candidate(hierarchy, text):
    if isinstance(hierarchy, str):
        try:
            hierarchy = json.loads(hierarchy)
        except (TypeError, json.JSONDecodeError):
            return None
    if not isinstance(hierarchy, dict):
        return None

    clipboard_bounds = []
    candidates = []

    def visit(item):
        if not isinstance(item, dict):
            return
        attributes = item.get("attributes") or {}
        if not isinstance(attributes, dict):
            attributes = {}
        values = [
            attributes.get("text"),
            attributes.get("originalText"),
            attributes.get("description"),
            attributes.get("hint"),
            attributes.get("id"),
            attributes.get("key"),
            attributes.get("type"),
        ]
        signature = " ".join(str(value or "") for value in values).casefold()
        bounds = _parse_harmony_bounds(attributes.get("bounds"))
        if "clipboard" in signature or "来自剪贴板" in signature:
            if bounds is not None:
                clipboard_bounds.append(bounds)
        if (
            text
            and any(str(value or "") == text for value in values[:4])
            and str(attributes.get("clickable", "")).casefold() == "true"
            and str(attributes.get("visible", "true")).casefold() != "false"
            and bounds is not None
        ):
            candidates.append(bounds)
        for child in item.get("children", []):
            visit(child)

    visit(hierarchy)
    if not clipboard_bounds or not candidates:
        return None

    def distance(candidate, marker):
        cx = (candidate[0] + candidate[2]) / 2
        cy = (candidate[1] + candidate[3]) / 2
        mx = (marker[0] + marker[2]) / 2
        my = (marker[1] + marker[3]) / 2
        return abs(cx - mx) + abs(cy - my)

    return min(
        candidates,
        key=lambda candidate: min(distance(candidate, marker) for marker in clipboard_bounds),
    )


def _harmony_input_already_present(hierarchy, text):
    if isinstance(hierarchy, str):
        try:
            hierarchy = json.loads(hierarchy)
        except (TypeError, json.JSONDecodeError):
            return False
    if not isinstance(hierarchy, dict):
        return False

    def visit(item):
        if not isinstance(item, dict):
            return False
        attributes = item.get("attributes") or {}
        if not isinstance(attributes, dict):
            attributes = {}
        signature = " ".join(
            str(attributes.get(key) or "")
            for key in ("id", "key", "type", "class")
        ).casefold()
        values = (
            attributes.get("text"),
            attributes.get("originalText"),
            attributes.get("value"),
        )
        input_role = any(
            marker in signature
            for marker in (
                "richeditor",
                "textinput",
                "edittext",
                "textfield",
                "inputfield",
                "text_input",
                "输入框",
                "输入栏",
                "文本框",
            )
        )
        if input_role and any(text and text in str(value or "") for value in values):
            return True
        return any(visit(child) for child in item.get("children", []))

    return visit(hierarchy)


def _parse_harmony_bounds(value):
    if isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            parsed = tuple(int(item) for item in value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed[2] >= parsed[0] and parsed[3] >= parsed[1] else None
    match = re.search(
        r"\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]",
        str(value or ""),
    )
    if not match:
        return None
    parsed = tuple(int(item) for item in match.groups())
    return parsed if parsed[2] >= parsed[0] and parsed[3] >= parsed[1] else None


decider_client = None
grounder_client = None
planner_client = None

planner_model = ""
decider_model = ""
grounder_model = ""


def _env_first(*names, default=None):
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _openai_client_for_role(role, service_ip, port, api_key):
    role = role.upper()
    base_url = _env_first(
        f"MOBIAGENT_{role}_BASE_URL",
        "MOBIAGENT_BASE_URL",
    )
    if not base_url:
        base_url = f"http://{service_ip}:{port}/v1"
    logging.info("%s client base_url=%s", role.title(), base_url)
    return OpenAI(api_key=api_key, base_url=base_url)


def _base_url_for_role(role):
    role = role.upper()
    return _env_first(
        f"MOBIAGENT_{role}_BASE_URL",
        "MOBIAGENT_BASE_URL",
    )


class ModelServiceConfigurationError(RuntimeError):
    """A model provider rejected a request that cannot succeed by retrying."""

    def __init__(self, context, status_code, cause):
        self.status_code = status_code
        self.is_model_service_blocker = True
        super().__init__(
            f"{context} model service rejected the request with HTTP {status_code}; "
            "verify MOBIAGENT_API_KEY (or MOBIAGENT_API_KEY_FILE), model access, "
            f"and provider/IP policy. Detail: {cause}"
        )
        self.__cause__ = cause


def _configured_api_key():
    """Return an explicitly configured credential without ever logging it.

    ``MOBIAGENT_API_KEY_FILE`` is intentionally opt-in: a repository key file
    must not be discovered implicitly.  This permits a local secret file to be
    used in a PowerShell session without copying the secret into a command line.
    """

    api_key = os.getenv("MOBIAGENT_API_KEY", "").strip()
    if api_key:
        return api_key
    key_file = os.getenv("MOBIAGENT_API_KEY_FILE", "").strip()
    if not key_file:
        return ""
    try:
        value = Path(key_file).expanduser().read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("MOBIAGENT_API_KEY_FILE is unreadable") from exc
    if not value:
        raise RuntimeError("MOBIAGENT_API_KEY_FILE is empty")
    return value


def validate_model_service_environment():
    """Fail before device mutation when a remote model endpoint lacks a key."""

    # Keep the preflight's configuration view identical to ``init``.
    load_dotenv(Path(__file__).parent / ".env")
    has_remote_endpoint = any(
        os.getenv(name, "").strip()
        for name in (
            "MOBIAGENT_BASE_URL",
            "MOBIAGENT_DECIDER_BASE_URL",
            "MOBIAGENT_GROUNDER_BASE_URL",
            "MOBIAGENT_PLANNER_BASE_URL",
        )
    )
    if has_remote_endpoint and not _configured_api_key():
        raise ModelServiceConfigurationError(
            "MobiAgent",
            401,
            RuntimeError("remote endpoint configured without MOBIAGENT_API_KEY"),
        )


def _api_key_for_requests():
    return _configured_api_key()


def _model_error_status_code(error):
    """Extract an HTTP status from OpenAI SDK and raw-HTTP exceptions."""

    status = getattr(error, "status_code", None)
    if status is None:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
    try:
        if status is not None:
            return int(status)
    except (TypeError, ValueError):
        pass
    match = re.search(r"\bHTTP\s+(\d{3})\b", str(error), flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _is_non_retryable_model_error(error):
    status = _model_error_status_code(error)
    # Retrying 408/409/429 and 5xx can be useful.  Other client errors are
    # deterministic request/configuration/provider-policy failures.
    return status, bool(status and 400 <= status < 500 and status not in {408, 409, 429})


def _use_raw_http_transport():
    """Choose the wire transport without changing the OpenAI-compatible API.

    Some hosted OpenAI-compatible gateways block the OpenAI Python SDK's
    HTTP signature even when the same key, payload and model are accepted via
    standard HTTP.  Prefer the small ``requests`` transport for explicitly
    configured remote endpoints; callers may still force the SDK for a
    provider that requires it.
    """

    transport = os.getenv("MOBIAGENT_LLM_TRANSPORT", "auto").strip().lower()
    if transport in {"raw_http", "requests", "http"}:
        return True
    if transport in {"openai", "openai_sdk", "sdk"}:
        return False
    if transport not in {"", "auto"}:
        logging.warning(
            "Unknown MOBIAGENT_LLM_TRANSPORT=%r; using automatic transport selection",
            transport,
        )
    configured_urls = (
        _env_first("MOBIAGENT_BASE_URL"),
        _env_first("MOBIAGENT_DECIDER_BASE_URL"),
        _env_first("MOBIAGENT_GROUNDER_BASE_URL"),
        _env_first("MOBIAGENT_PLANNER_BASE_URL"),
    )
    for base_url in configured_urls:
        normalized = str(base_url or "").strip().lower()
        if normalized.startswith(("https://", "http://")) and not any(
            marker in normalized
            for marker in ("localhost", "127.0.0.1", "0.0.0.0")
        ):
            return True
    return False


def _requests_chat_completion(role, model, messages, temperature, timeout, max_tokens):
    if not _use_raw_http_transport():
        return None

    base_url = _base_url_for_role(role)
    if not base_url:
        return None

    endpoint = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    logging.info("%s request transport=raw_http endpoint=%s", role, endpoint)
    response = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {_api_key_for_requests()}",
            "Content-Type": "application/json",
        },
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
    body = response.json()
    return body["choices"][0]["message"]["content"]


def _model_for_role(role, current_model):
    role = role.upper()
    return _env_first(
        f"MOBIAGENT_{role}_MODEL",
        "MOBIAGENT_MODEL",
        default=current_model,
    )


# 全局偏好提取器
preference_extractor = None
def init(
    service_ip,
    decider_port,
    grounder_port,
    planner_port,
    enable_user_profile=False,
    use_graphrag=False,
):
    global decider_client, grounder_client, planner_client, general_client, general_model, apps, preference_extractor
    global decider_model, grounder_model, planner_model
    
    # 加载环境变量
    env_path = Path(__file__).parent / ".env"
    load_dotenv(env_path)
    api_key = _configured_api_key()
    decider_client = _openai_client_for_role("decider", service_ip, decider_port, api_key)
    grounder_client = _openai_client_for_role("grounder", service_ip, grounder_port, api_key)
    planner_client = _openai_client_for_role("planner", service_ip, planner_port, api_key)

    # Model routing stays internal to the service or environment. The CLI only
    # selects ports and protocol, which keeps the public entrypoints simpler.
    decider_model = _model_for_role("decider", decider_model)
    grounder_model = _model_for_role("grounder", grounder_model)
    planner_model = _model_for_role("planner", planner_model)
    logging.info("Decider model=%s", decider_model or "<empty>")
    logging.info("Grounder model=%s", grounder_model or "<empty>")
    logging.info("Planner model=%s", planner_model or "<empty>")
    
    # 初始化偏好提取器（可由命令行开关控制）
    if enable_user_profile:
        preference_extractor = PreferenceExtractor(planner_client, planner_model, use_graphrag=use_graphrag)
    else:
        preference_extractor = None
    

def format_model_response_for_log(context, response_str):
    """Format model responses for logs without leaking prompt contents."""
    if context != "Decider":
        return response_str

    parsed_response = _load_json_from_text(response_str)
    if not isinstance(parsed_response, dict):
        return response_str

    reasoning = parsed_response.get("reasoning")
    try:
        formatted_response = json.dumps(parsed_response, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return response_str

    if not (isinstance(reasoning, str) and reasoning):
        return formatted_response

    if not sys.stdout.isatty():
        return formatted_response

    reasoning_json = json.dumps(reasoning, ensure_ascii=False)
    reasoning_line_pattern = rf'(^\s*"reasoning":\s*){re.escape(reasoning_json)}(,?)$'

    return re.sub(
        reasoning_line_pattern,
        rf'\1{ANSI_REASONING_GREEN}{reasoning_json}{ANSI_RESET}\2',
        formatted_response,
        count=1,
        flags=re.MULTILINE,
    )


# ============ 工具函数 ============

def call_model_with_validation_retry(client, model, messages, validator_func, max_retries=MAX_RETRIES, max_tokens=256, context="Model", parser_func=None):
    """
    通用模型API调用函数：支持JSON解析 + 自定义校验 + 校验失败自动重试和温度递增
    
    Args:
        client: OpenAI客户端
        model: 模型名称
        messages: 消息列表
        validator_func: 校验函数，接收解析后的JSON对象，如果校验失败则抛异常
        max_retries: 最大重试次数
        max_tokens: 最大生成Token数
        context: 日志上下文（如"Decider", "Grounder"）
    
    Returns:
        解析后的 JSON 响应对象（已通过校验）
    
    Raises:
        Exception: 重试失败或校验一直失败后抛出异常
    """
    temperature = INITIAL_TEMP
    for attempt in range(max_retries):
        try:
            start_time = time.time()
            logging.info(
                "%s request: model=%s max_tokens=%s temperature=%.1f",
                context,
                model or "<empty>",
                max_tokens,
                temperature,
            )
            response_str = _requests_chat_completion(
                context,
                model,
                messages,
                temperature,
                API_TIMEOUT,
                max_tokens,
            )
            if response_str is None:
                response_str = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    timeout=API_TIMEOUT,
                    max_tokens=max_tokens,
                ).choices[0].message.content
            end_time = time.time()
            logging.info(f"[evaluation] {context} time taken: {end_time - start_time:.2f} seconds")
            logging.info(f"{context} response: \n{format_model_response_for_log(context, response_str)}")
            
            # 尝试解析 JSON
            parser = parser_func or robust_json_loads
            parsed_response = parser(response_str)
            
            # 执行校验函数
            validator_func(parsed_response)
            
            return parsed_response
            
        except Exception as e:
            status_code, non_retryable = _is_non_retryable_model_error(e)
            if non_retryable:
                logging.error(
                    "%s request rejected permanently (HTTP %s); no retry will be attempted: %s",
                    context,
                    status_code,
                    e,
                )
                raise ModelServiceConfigurationError(context, status_code, e) from e
            temperature = INITIAL_TEMP + (attempt + 1) * TEMP_INCREMENT
            logging.error(f"{context} 调用或校验失败: {e}, 正在重试 temperature={temperature:.1f}...")
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                raise
    
    raise RuntimeError(f"{context} API calls exhausted after {max_retries} attempts")


def parse_json_response(response_str: str, is_guided_decoding: bool = True) -> dict:
    """
    解析 JSON 响应，支持 guided decoding 和普通模式
    
    Args:
        response_str: 模型返回的响应字符串
        is_guided_decoding: 是否启用了 guided decoding（默认 True）
        
    Returns:
        解析后的 JSON 对象
        
    说明：
        - 当启用 guided decoding 时，模型输出应该是纯 JSON 格式
        - 当禁用 guided decoding 时，可能包含 markdown code block 或其他文本
    """
    parsed = _load_json_from_text(response_str)
    if parsed is None:
        logging.error(f"无法在响应中找到有效的 JSON")
        logging.error(f"原始响应: {str(response_str)[:200]}...")
        raise ValueError(f"无法解析 JSON 响应，响应格式不正确")
    return parsed

def get_screenshot(device, device_type="Android"):
    """
    获取设备截图并编码为base64
    
    Args:
        device: 设备对象
        device_type: 设备类型，"Android" 或 "Harmony"
        
    Returns:
        Base64编码的截图字符串
    """
    # 根据设备类型使用不同的截图路径，避免冲突
    if device_type == "Android":
        screenshot_path = "screenshot-Android.jpg"
    else:
        screenshot_path = "screenshot-Harmony.jpg"
    
    device.screenshot(screenshot_path)
    # resize the screenshot to reduce the size for processing
    img = Image.open(screenshot_path)
    img = img.resize((int(img.width * factor), int(img.height * factor)), Image.Resampling.LANCZOS)
    buffered = io.BytesIO()
    img.save(buffered, format="JPEG")
    screenshot = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return screenshot

def _clamp(value, low, high):
    return max(low, min(high, int(value)))


def _coordinate_mode():
    mode = (DECIDER_COORD_MODE or COORD_MODE_RESIZED_PIXEL).strip().lower()
    if mode not in SUPPORTED_COORD_MODES:
        logging.warning("Unknown MOBIAGENT_COORD_MODE=%s; using %s", mode, COORD_MODE_RESIZED_PIXEL)
        return COORD_MODE_RESIZED_PIXEL
    return mode


def get_click_coordinate_size(device, img):
    size = None
    if hasattr(device, "click_coordinate_size"):
        size = device.click_coordinate_size()
    if not size:
        return img.width, img.height

    width, height = size
    if width <= 0 or height <= 0:
        return img.width, img.height
    if width != img.width or height != img.height:
        logging.info(
            "Mapping model screenshot coordinates %sx%s to click coordinate surface %sx%s",
            img.width,
            img.height,
            width,
            height,
        )
    return width, height


def convert_qwen3_coordinates_to_absolute(
    bbox_or_coords,
    img_width,
    img_height,
    is_bbox=True,
    target_width=None,
    target_height=None,
):
    """
    Convert model coordinates to absolute device coordinates.

    The current OpenAI-compatible Qwen service usually returns pixel
    coordinates in the resized screenshot sent to the model. Native Qwen-style
    normalized coordinates can still be enabled with:

        MOBIAGENT_COORD_MODE=qwen_normalized
    
    Args:
        bbox_or_coords: 相对坐标或边界框，范围为 0-1000
        img_width: screenshot image width before Runner's resize
        img_height: screenshot image height before Runner's resize
        target_width: click coordinate surface width, defaults to img_width
        target_height: click coordinate surface height, defaults to img_height
        is_bbox: 是否为边界框（True）或坐标点（False）
        
    Returns:
        转换后的绝对坐标或边界框
    """
    target_width = target_width or img_width
    target_height = target_height or img_height
    mode = _coordinate_mode()
    if mode == COORD_MODE_AUTO:
        values = list(bbox_or_coords)
        # Preserve the established pixel-first behaviour, but allow the
        # resized-pixel branch to repair a native/mixed axis.  The old test
        # compared against only the resized extent and mislabeled a valid
        # native y coordinate as Qwen-normalized coordinates.
        if max(values) <= max(img_width, img_height) + 5:
            mode = COORD_MODE_RESIZED_PIXEL
        else:
            mode = COORD_MODE_QWEN_NORMALIZED

    if mode == COORD_MODE_RESIZED_PIXEL:
        # The image sent to the model is normally half-size.  In practice a
        # vision model can occasionally retain one axis from the native image
        # while using the resized image for the other (for example x=430 on a
        # 540px-wide image and y=1700 on a 2444px-high image).  Applying one
        # global scale in that case turns a valid y coordinate into a click on
        # the system navigation area.  Infer the unit independently per axis:
        # values outside the supplied resized extent but inside the native
        # extent are native pixels for that axis.  This is geometry based and
        # applies to every screen/app, not a particular test case.
        values = list(bbox_or_coords)
        x_values = values[0::2]
        y_values = values[1::2]
        resized_w = img_width * factor
        resized_h = img_height * factor
        scale_x, x_mode = _resized_pixel_axis_scale(
            x_values, img_width, resized_w, target_width, "x"
        )
        scale_y, y_mode = _resized_pixel_axis_scale(
            y_values, img_height, resized_h, target_height, "y"
        )
        if x_mode != y_mode:
            logging.warning(
                "Mixed model coordinate units detected; using %s pixels for x and %s pixels for y",
                x_mode,
                y_mode,
            )
        if is_bbox:
            x1, y1, x2, y2 = bbox_or_coords
            x1 = _clamp(x1 * scale_x, 0, target_width - 1)
            x2 = _clamp(x2 * scale_x, 0, target_width - 1)
            y1 = _clamp(y1 * scale_y, 0, target_height - 1)
            y2 = _clamp(y2 * scale_y, 0, target_height - 1)
            return [x1, y1, x2, y2]
        x, y = bbox_or_coords
        x = _clamp(x * scale_x, 0, target_width - 1)
        y = _clamp(y * scale_y, 0, target_height - 1)
        return [x, y]

    if is_bbox:
        # bbox: [x1, y1, x2, y2]
        x1, y1, x2, y2 = bbox_or_coords
        x1 = _clamp(x1 / 1000 * target_width, 0, target_width - 1)
        x2 = _clamp(x2 / 1000 * target_width, 0, target_width - 1)
        y1 = _clamp(y1 / 1000 * target_height, 0, target_height - 1)
        y2 = _clamp(y2 / 1000 * target_height, 0, target_height - 1)
        return [x1, y1, x2, y2]
    else:
        # coordinates: [x, y]
        x, y = bbox_or_coords
        x = _clamp(x / 1000 * target_width, 0, target_width - 1)
        y = _clamp(y / 1000 * target_height, 0, target_height - 1)
        return [x, y]


def _resized_pixel_axis_scale(values, native_extent, resized_extent, target_extent, axis):
    """Return a scale for one coordinate axis in resized-pixel mode.

    The accepted default is resized-image pixels.  Native pixels are selected
    only when at least one value cannot fit in the resized image yet fits the
    original screenshot, making the choice unambiguous.  Keeping this decision
    per axis avoids corrupting a correct coordinate on the other axis.
    """
    try:
        largest = max(abs(float(value)) for value in values)
    except (TypeError, ValueError):
        # Parameter validation supplies numeric coordinates; retain the normal
        # behaviour defensively if this helper is ever called earlier.
        largest = 0.0
    tolerance = 5.0
    if largest > float(resized_extent) + tolerance and largest <= float(native_extent) + tolerance:
        logging.info(
            "Model %s coordinate %.1f exceeds resized extent %.1f but fits native extent %s; "
            "using native-pixel scaling for this axis",
            axis,
            largest,
            resized_extent,
            native_extent,
        )
        return target_extent / native_extent, "native"
    return target_extent / resized_extent, "resized"


def _normalize_non_empty_string(value, field_name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing required parameter: '{field_name}'")
    return value.strip()


def _normalize_point(value, field_name):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"Invalid parameter '{field_name}': expected [x, y]")
    try:
        return [int(value[0]), int(value[1])]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid parameter '{field_name}': expected numeric coordinates") from exc


def _normalize_bbox(value, field_name="bbox"):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"Invalid parameter '{field_name}': expected [x1, y1, x2, y2]")
    try:
        return [int(value[0]), int(value[1]), int(value[2]), int(value[3])]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid parameter '{field_name}': expected numeric bbox values") from exc


def _normalize_optional_seconds(value):
    if value is None:
        return DEVICE_WAIT_TIME * 2
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid parameter 'seconds': expected a numeric value") from exc
    if seconds < 0:
        raise ValueError("Invalid parameter 'seconds': must be >= 0")
    return seconds


def _infer_swipe_direction_from_points(start_coords, end_coords):
    delta_x = end_coords[0] - start_coords[0]
    delta_y = end_coords[1] - start_coords[1]
    if abs(delta_x) >= abs(delta_y):
        return "RIGHT" if delta_x >= 0 else "LEFT"
    return "DOWN" if delta_y >= 0 else "UP"


def _canonicalize_runtime_parameters(action, parameters):
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        raise ValueError("Field 'parameters' must be an object")

    bbox = parameters.get("bbox")
    coords = parameters.get("coords")
    target_element = parameters.get("target_element")
    start_coords = parameters.get("start_coords")
    end_coords = parameters.get("end_coords")
    direction = parameters.get("direction")

    if action == "click":
        if bbox is not None:
            canonical = {"bbox": _normalize_bbox(bbox)}
            if isinstance(target_element, str) and target_element.strip():
                canonical["target_element"] = target_element.strip()
            return canonical
        if coords is not None:
            canonical = {"coords": _normalize_point(coords, "coords")}
            if isinstance(target_element, str) and target_element.strip():
                canonical["target_element"] = target_element.strip()
            return canonical
        return {"target_element": _normalize_non_empty_string(target_element, "target_element")}

    if action == "click_input":
        text = parameters.get("text")
        if not isinstance(text, str):
            raise ValueError("Click_input action missing required parameter: 'text'")
        canonical = {"text": text}
        if isinstance(target_element, str) and target_element.strip():
            canonical["target_element"] = target_element.strip()
        if bbox is not None:
            canonical["bbox"] = _normalize_bbox(bbox)
            return canonical
        if coords is not None:
            canonical["coords"] = _normalize_point(coords, "coords")
            return canonical
        raise ValueError("Click_input action missing required parameter: 'bbox' or 'coords'")

    if action == "input":
        text = parameters.get("text")
        if not isinstance(text, str):
            raise ValueError("Input action missing required parameter: 'text'")
        return {"text": text}

    if action == "swipe":
        canonical = {}
        normalized_direction = None
        if direction is not None:
            normalized_direction = _normalize_non_empty_string(direction, "direction").upper()
            if normalized_direction not in ["UP", "DOWN", "LEFT", "RIGHT"]:
                raise ValueError(
                    f"Invalid swipe direction: '{direction}'. Must be one of: UP, DOWN, LEFT, RIGHT"
                )
            canonical["direction"] = normalized_direction

        if (start_coords is None) != (end_coords is None):
            raise ValueError("Swipe action requires both 'start_coords' and 'end_coords'")

        if start_coords is not None:
            normalized_start = _normalize_point(start_coords, "start_coords")
            normalized_end = _normalize_point(end_coords, "end_coords")
            canonical["start_coords"] = normalized_start
            canonical["end_coords"] = normalized_end
            canonical.setdefault(
                "direction",
                _infer_swipe_direction_from_points(normalized_start, normalized_end),
            )

        if "direction" not in canonical:
            raise ValueError("Swipe action missing required parameter: 'direction'")

        return canonical

    if action == "done":
        canonical = {"status": _normalize_non_empty_string(parameters.get("status"), "status")}
        message = parameters.get("message")
        if message is not None:
            canonical["message"] = str(message)
        return canonical

    if action == "long_press":
        if bbox is not None:
            return {"bbox": _normalize_bbox(bbox)}
        if coords is not None:
            return {"coords": _normalize_point(coords, "coords")}
        raise ValueError("Long_press action missing required parameter: 'bbox' or 'coords'")

    if action == "open_app":
        return {"app_name": _normalize_non_empty_string(parameters.get("app_name"), "app_name")}

    if action in {"press_home", "press_back"}:
        return {}

    if action == "wait":
        return {"seconds": _normalize_optional_seconds(parameters.get("seconds"))}

    if action == "info":
        return {"question": _normalize_non_empty_string(parameters.get("question"), "question")}

    if action == "call_user":
        canonical = {"message": _normalize_non_empty_string(parameters.get("message"), "message")}
        tag = parameters.get("tag")
        if tag is not None:
            canonical["tag"] = _normalize_non_empty_string(tag, "tag")
        return canonical

    if action == "abort":
        return {"reason": _normalize_non_empty_string(parameters.get("reason"), "reason")}

    raise ValueError(f"Unknown action: '{action}'")

def validate_action_parameters(decider_response):
    """
    校验并规范化共享 runtime action schema。

    这里不关心当前是 Qwen 还是 StepFun，只负责把 adapter 输出收敛成
    runtime 能直接消费的一份 canonical schema。协议专属的 prompt / parser /
    protocol-field 校验由 decider adapter 负责。
    
    Args:
        decider_response: 解析后的 JSON 响应字典
    
    Raises:
        ValueError: 当必需字段缺失时
    """
    action = decider_response.get("action")
    if not isinstance(action, str) or not action.strip():
        raise ValueError("Missing required field: 'action'")

    reasoning = decider_response.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise ValueError("Missing required field: 'reasoning'")

    normalized_action = action.strip().lower()
    decider_response["action"] = normalized_action
    decider_response["reasoning"] = reasoning.strip()
    decider_response["parameters"] = _canonicalize_runtime_parameters(
        normalized_action,
        decider_response.get("parameters", {}),
    )

    return True
def create_swipe_visualization(data_dir, image_index, direction, start_x=None, start_y=None, end_x=None, end_y=None):
    """为滑动动作创建可视化图像"""
    try:
        # 读取原始截图
        img_path = os.path.join(data_dir, f"{image_index}.jpg")
        if not os.path.exists(img_path):
            return
            
        img = cv2.imread(img_path)
        if img is None:
            return
            
        height, width = img.shape[:2]
        
        # 如果提供了具体坐标，使用具体坐标；否则根据方向计算
        if start_x is not None and start_y is not None and end_x is not None and end_y is not None:
            start_point = (int(start_x), int(start_y))
            end_point = (int(end_x), int(end_y))
        else:
            # 根据方向计算箭头起点和终点
            center_x, center_y = width // 2, height // 2
            arrow_length = min(width, height) // 4
            
            if direction == "up":
                start_point = (center_x, center_y + arrow_length // 2)
                end_point = (center_x, center_y - arrow_length // 2)
            elif direction == "down":
                start_point = (center_x, center_y - arrow_length // 2)
                end_point = (center_x, center_y + arrow_length // 2)
            elif direction == "left":
                start_point = (center_x + arrow_length // 2, center_y)
                end_point = (center_x - arrow_length // 2, center_y)
            elif direction == "right":
                start_point = (center_x - arrow_length // 2, center_y)
                end_point = (center_x + arrow_length // 2, center_y)
            else:
                return
            
        # 绘制箭头
        cv2.arrowedLine(img, start_point, end_point, (255, 0, 0), 8, tipLength=0.3)  # 蓝色箭头
        
        # 添加文字说明
        font = cv2.FONT_HERSHEY_SIMPLEX
        text = f"SWIPE {direction.upper()}"
        text_size = cv2.getTextSize(text, font, 1.5, 3)[0]
        text_x = (width - text_size[0]) // 2
        text_y = 50
        cv2.putText(img, text, (text_x, text_y), font, 1.5, (255, 0, 0), 3)  # 蓝色文字
        
        # 保存可视化图像
        swipe_path = os.path.join(data_dir, f"{image_index}_swipe.jpg")
        cv2.imwrite(swipe_path, img)
        
    except Exception as e:
        logging.warning(f"Failed to create swipe visualization: {e}")

def get_decider_parser(decider_protocol: str):
    return get_decider_adapter(decider_protocol).parse_response


def get_device_paths(device_type, image_index=None):
    """
    获取设备相关的文件路径
    
    Args:
        device_type: 设备类型 ("Android" 或 "Harmony")
        image_index: 图像索引（可选）
    
    Returns:
        dict: 包含 img_path, screenshot_name, current_dir 的字典
    """
    current_dir = os.getcwd()
    screenshot_name = "screenshot-Android.jpg" if device_type == "Android" else "screenshot-Harmony.jpg"
    img_path = os.path.join(current_dir, screenshot_name)
    return {
        "img_path": img_path,
        "screenshot_name": screenshot_name,
        "current_dir": current_dir
    }


def validate_decider_response(response_dict, use_e2e=False, decider_protocol=DECIDER_PROTOCOL_QWEN_JSON):
    """
    校验 Decider 模型响应，包括 runtime canonical schema 和 e2e 模式特殊校验。
    
    Args:
        response_dict: 解析后的JSON响应对象
        use_e2e: 是否使用e2e模式
    
    Raises:
        ValueError: 校验失败时抛出异常
    """
    # 基础参数校验
    validate_action_parameters(response_dict)

    # Protocol-specific rules are delegated to the selected adapter. This keeps
    # model-specific prompt and schema validation outside the shared runtime.
    get_decider_adapter(decider_protocol).validate_response(response_dict, use_e2e)


def validate_grounder_response(response_dict):
    """
    校验 Grounder 模型响应
    
    Args:
        response_dict: 解析后的JSON响应对象
    
    Raises:
        ValueError: 校验失败时抛出异常
    """
    # Grounder需要至少返回coordinates或bbox
    if "coordinates" not in response_dict and not any(
        key.lower() in ["bbox", "bbox_2d", "bbox-2d", "bbox_2D", "bbox2d"]
        for key in response_dict.keys()
    ):
        raise ValueError("Grounder response must contain 'coordinates' or 'bbox' field")

def compute_swipe_positions(direction, img_width, img_height):
    direction = direction.upper()
    if direction == "DOWN":
        return (
            0.5 * img_width,
            SWIPE_V_START * img_height,
            0.5 * img_width,
            SWIPE_V_END * img_height,
        )
    if direction == "UP":
        return (
            0.5 * img_width,
            SWIPE_V_END * img_height,
            0.5 * img_width,
            SWIPE_V_START * img_height,
        )
    if direction == "LEFT":
        return (
            SWIPE_H_END * img_width,
            0.5 * img_height,
            SWIPE_H_START * img_width,
            0.5 * img_height,
        )
    if direction == "RIGHT":
        return (
            SWIPE_H_START * img_width,
            0.5 * img_height,
            SWIPE_H_END * img_width,
            0.5 * img_height,
        )
    raise ValueError(f"Unknown swipe direction: {direction}")


def should_use_explicit_swipe_coords(start, end, direction, width, height):
    """Accept model swipe coordinates only when they agree with the action."""
    normalized = str(direction or "").lower()
    if not normalized:
        return True
    try:
        sx, sy = (int(value) for value in start)
        ex, ey = (int(value) for value in end)
    except (TypeError, ValueError):
        return False
    dx, dy = abs(ex - sx), abs(ey - sy)
    if normalized in {"up", "down"}:
        if dy < height * 0.12 or dy < dx:
            return False
        if not (height * 0.28 <= sy <= height * 0.88):
            return False
        return ey > sy if normalized == "down" else ey < sy
    if normalized in {"left", "right"}:
        if dx < width * 0.12 or dx < dy:
            return False
        if not (width * 0.12 <= sx <= width * 0.88):
            return False
        return ex > sx if normalized == "right" else ex < sx
    return True


def _previous_click_input_point(actions):
    """Return the active input point only if no later action invalidated focus."""
    for record in reversed(actions):
        action_type = str(record.get("type") or "").lower()
        if action_type == "click_input":
            point = record.get("click_point")
            if isinstance(point, (tuple, list)) and len(point) == 2:
                try:
                    return int(point[0]), int(point[1])
                except (TypeError, ValueError):
                    return None
            try:
                return int(record["position_x"]), int(record["position_y"])
            except (KeyError, TypeError, ValueError):
                return None
        if action_type in {"click", "swipe", "long_press", "press_back", "press_home", "open_app"}:
            return None
    return None


def _remember_input_focus(device, point):
    try:
        setattr(device, "_mobiagent_input_focus", (int(point[0]), int(point[1])))
    except (AttributeError, TypeError, ValueError, IndexError):
        pass


def _clear_input_focus(device):
    try:
        setattr(device, "_mobiagent_input_focus", None)
    except (AttributeError, TypeError):
        pass


def _device_input_focus(device):
    point = getattr(device, "_mobiagent_input_focus", None)
    if not isinstance(point, (tuple, list)) or len(point) != 2:
        return None
    try:
        return int(point[0]), int(point[1])
    except (TypeError, ValueError):
        return None


def build_decider_messages(task, history, screenshot, e2e, decider_protocol=DECIDER_PROTOCOL_QWEN_JSON, device_type="Android"):
    adapter = get_decider_adapter(decider_protocol)
    return adapter.build_messages(task, history, screenshot, e2e, device_type)


def append_action_and_history(actions, history, decider_response, action_record):
    """统一记录动作和历史，减少重复代码。"""
    actions.append(action_record)
    history.append(json.dumps(decider_response, ensure_ascii=False))


def save_action_point_visualization(img, data_dir, image_index, action_label, bounds, position_x, position_y):
    save_path = os.path.join(data_dir, f"{image_index}_{action_label}_highlighted.jpg")
    bounds_path = os.path.join(data_dir, f"{image_index}_{action_label}_bounds.jpg")
    click_point_path = os.path.join(data_dir, f"{image_index}_{action_label}_click_point.jpg")

    img_highlighted = img.copy()
    draw = ImageDraw.Draw(img_highlighted)
    try:
        font = load_overlay_font(40)
    except OSError:
        font = ImageFont.load_default()
    text = f"{action_label.upper()} [{position_x}, {position_y}]"
    text = textwrap.fill(text, width=24)
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    draw.text((img_highlighted.width / 2 - text_width / 2, 0), text, fill="red", font=font)
    img_highlighted.save(save_path)

    img_bounds = Image.open(save_path)
    draw_bounds = ImageDraw.Draw(img_bounds)
    x1, y1, x2, y2 = bounds
    draw_bounds.rectangle([x1, y1, x2, y2], outline="red", width=5)
    img_bounds.save(bounds_path)

    # Keep Unicode paths working on Windows; OpenCV's narrow-path imread may
    # mojibake directories such as "淘宝".
    click_image = Image.open(bounds_path)
    click_draw = ImageDraw.Draw(click_image)
    radius = 15
    click_draw.ellipse(
        [position_x - radius, position_y - radius, position_x + radius, position_y + radius],
        fill=(0, 255, 0),
    )
    click_image.save(click_point_path)


def handle_click_input_action(decider_response, device, img, data_dir, image_index, actions, history, hierarchy=None):
    text = decider_response["parameters"]["text"]
    target_width, target_height = get_click_coordinate_size(device, img)
    if decider_response["parameters"].get("coords"):
        position_x, position_y = convert_qwen3_coordinates_to_absolute(
            decider_response["parameters"]["coords"],
            img.width,
            img.height,
            is_bbox=False,
            target_width=target_width,
            target_height=target_height,
        )
        x1 = x2 = position_x
        y1 = y2 = position_y
    elif decider_response["parameters"].get("bbox"):
        bbox = decider_response["parameters"]["bbox"]
        bbox = convert_qwen3_coordinates_to_absolute(
            bbox,
            img.width,
            img.height,
            is_bbox=True,
            target_width=target_width,
            target_height=target_height,
        )
        x1, y1, x2, y2 = bbox
        position_x = (x1 + x2) // 2
        position_y = (y1 + y2) // 2
    else:
        raise ValueError("Click_input requires canonical 'coords' or 'bbox'")

    raw_click_point = [position_x, position_y]
    (position_x, position_y), xml_hit_test = align_click_to_xml_node(
        (position_x, position_y), [x1, y1, x2, y2],
        decider_response["parameters"].get("target_element", ""), hierarchy,
        target_width, target_height, action_type="click_input",
    )
    save_action_point_visualization(img, data_dir, image_index, "click_input", [x1, y1, x2, y2], position_x, position_y)
    device.click(position_x, position_y)
    _remember_input_focus(device, (position_x, position_y))
    if CLICK_INPUT_FOCUS_WAIT > 0:
        time.sleep(CLICK_INPUT_FOCUS_WAIT)
    device.input(text)
    append_action_and_history(actions, history, decider_response, {
        "type": "click_input",
        "target_element": decider_response["parameters"].get("target_element", ""),
        "position_x": position_x,
        "position_y": position_y,
        "click_point": [position_x, position_y],
        "bounds": [x1, y1, x2, y2],
        "raw_model_bbox": decider_response["parameters"].get("bbox"),
        "converted_bounds": [x1, y1, x2, y2],
        "click_point_before_xml_alignment": raw_click_point,
        "xml_hit_test_result": xml_hit_test,
        "click_coordinate_size": [target_width, target_height],
        "screenshot_size": [img.width, img.height],
        "text": f"{text}",
        "focus_wait_seconds": CLICK_INPUT_FOCUS_WAIT,
        "action_index": image_index
    })


def handle_input_action(decider_response, device, image_index, actions, history):
    text = decider_response["parameters"]["text"]
    focus_point = _previous_click_input_point(actions) or _device_input_focus(device)
    if focus_point is None:
        raise ValueError(
            "Input action requires a preceding click_input target; refusing to type into unknown focus"
        )
    device.click(*focus_point)
    _remember_input_focus(device, focus_point)
    if CLICK_INPUT_FOCUS_WAIT > 0:
        time.sleep(CLICK_INPUT_FOCUS_WAIT)
    device.input(text)
    append_action_and_history(actions, history, decider_response, {
        "type": "input",
        "text": text,
        "focus_reactivated": True,
        "focus_point": list(focus_point),
        "action_index": image_index
    })


def handle_open_app_action(decider_response, device, image_index, actions, history):
    app_name = decider_response["parameters"]["app_name"]
    _clear_input_focus(device)
    try:
        device.start_app(app_name)
    except Exception as e:
        logging.warning(f"Open app by app_name failed: {e}, trying as package name")
        device.app_start(app_name)

    append_action_and_history(actions, history, decider_response, {
        "type": "open_app",
        "app_name": app_name,
        "action_index": image_index
    })


def handle_press_home_action(decider_response, device, device_type, image_index, actions, history):
    _clear_input_focus(device)
    if device_type == "Android":
        device.keyevent("home")
    else:
        if hasattr(KeyCode, "HOME"):
            device.keyevent(KeyCode.HOME)
        else:
            device.keyevent(1)

    append_action_and_history(actions, history, decider_response, {
        "type": "press_home",
        "action_index": image_index
    })


def handle_press_back_action(decider_response, device, device_type, image_index, actions, history):
    _clear_input_focus(device)
    if device_type == "Android":
        device.keyevent("back")
    else:
        if hasattr(KeyCode, "BACK"):
            device.keyevent(KeyCode.BACK)
        else:
            device.keyevent(2)

    append_action_and_history(actions, history, decider_response, {
        "type": "press_back",
        "action_index": image_index
    })


def handle_wait_action(decider_response, image_index, actions, history):
    print("Waiting for a while...")
    seconds = float(decider_response.get("parameters", {}).get("seconds", DEVICE_WAIT_TIME * 2))
    append_action_and_history(actions, history, decider_response, {
        "type": "wait",
        "seconds": seconds,
        "action_index": image_index
    })
    time.sleep(seconds)


def handle_long_press_action(decider_response, device, img, image_index, actions, history):
    _clear_input_focus(device)
    target_width, target_height = get_click_coordinate_size(device, img)
    if decider_response["parameters"].get("coords"):
        position_x, position_y = convert_qwen3_coordinates_to_absolute(
            decider_response["parameters"]["coords"],
            img.width,
            img.height,
            is_bbox=False,
            target_width=target_width,
            target_height=target_height,
        )
    elif decider_response["parameters"].get("bbox"):
        x1, y1, x2, y2 = convert_qwen3_coordinates_to_absolute(
            decider_response["parameters"]["bbox"],
            img.width,
            img.height,
            is_bbox=True,
            target_width=target_width,
            target_height=target_height,
        )
        position_x = (x1 + x2) // 2
        position_y = (y1 + y2) // 2
    else:
        raise ValueError("Long_press requires canonical 'coords' or 'bbox'")

    device.long_press(position_x, position_y)
    append_action_and_history(actions, history, decider_response, {
        "type": "long_press",
        "position_x": position_x,
        "position_y": position_y,
        "click_coordinate_size": [target_width, target_height],
        "screenshot_size": [img.width, img.height],
        "action_index": image_index
    })


def handle_info_action(decider_response, image_index, actions, history):
    question = decider_response["parameters"]["question"]
    print(f"Model asks for more information: {question}")
    if not sys.stdin.isatty():
        append_action_and_history(actions, history, decider_response, {
            "type": "info",
            "question": question,
            "response": None,
            "action_index": image_index
        })
        return False

    response = input("Please provide the missing information and press Enter: ").strip()
    append_action_and_history(actions, history, decider_response, {
        "type": "info",
        "question": question,
        "response": response,
        "action_index": image_index
    })
    history.append(json.dumps({"user_reply": response}, ensure_ascii=False))
    return True


def handle_call_user_action(decider_response, image_index, actions, history):
    message = decider_response["parameters"]["message"]
    tag = decider_response["parameters"].get("tag", "confirm_action")
    print(f"User intervention required ({tag}): {message}")
    if not sys.stdin.isatty():
        append_action_and_history(actions, history, decider_response, {
            "type": "call_user",
            "tag": tag,
            "message": message,
            "handled": False,
            "action_index": image_index
        })
        return False

    input("Handle the requested intervention manually, then press Enter to continue: ")
    append_action_and_history(actions, history, decider_response, {
        "type": "call_user",
        "tag": tag,
        "message": message,
        "handled": True,
        "action_index": image_index
    })
    history.append(json.dumps({"call_user": {"tag": tag, "handled": True}}, ensure_ascii=False))
    return True


def handle_abort_action(decider_response, image_index, actions, history):
    reason = decider_response["parameters"]["reason"]
    print(f"Task aborted by model: {reason}")
    append_action_and_history(actions, history, decider_response, {
        "type": "abort",
        "reason": reason,
        "action_index": image_index
    })


def _harmony_hierarchy_to_xml(hierarchy):
    """Project Harmony JSON hierarchy into the legacy XML evidence interface.

    The original JSON remains the source artifact. This deterministic projection
    only exposes equivalent observable attributes to the frozen MobiFlow loader;
    it does not add task rules or success semantics.
    """
    root = ET.Element("hierarchy", {"source": "harmony_json_projection"})

    def append(parent, item, inherited_package=""):
        if not isinstance(item, dict):
            return
        raw = item.get("attributes") or {}
        package = str(raw.get("bundleName") or inherited_package or "")
        description = str(raw.get("description") or raw.get("hint") or "")
        resource_id = str(raw.get("id") or raw.get("key") or "")
        attrs = {
            "text": str(raw.get("text") or raw.get("originalText") or ""),
            "content-desc": description,
            "resource-id": resource_id,
            "class": str(raw.get("type") or ""),
            "package": package,
            "bounds": str(raw.get("bounds") or ""),
            "clickable": str(raw.get("clickable") or "false").lower(),
            "enabled": str(raw.get("enabled") or "true").lower(),
            "visible-to-user": str(raw.get("visible") or "true").lower(),
            "focused": str(raw.get("focused") or "false").lower(),
            "selected": str(raw.get("selected") or "false").lower(),
            "scrollable": str(raw.get("scrollable") or "false").lower(),
            "checkable": str(raw.get("checkable") or "false").lower(),
            "checked": str(raw.get("checked") or "false").lower(),
            "long-clickable": str(raw.get("longClickable") or "false").lower(),
        }
        node = ET.SubElement(parent, "node", attrs)
        for child in item.get("children", []):
            append(node, child, package)

    append(root, hierarchy)
    return ET.tostring(root, encoding="unicode")


def save_hierarchy(device, device_type, data_dir, image_index):
    """保存原始 hierarchy；Harmony 同时保存 legacy-compatible XML projection。"""
    if device_type == "Android":
        logging.info("Dumping UI hierarchy...")
        try:
            hierarchy = device.dump_hierarchy()
        except Exception as e:
            logging.error(f"Failed to dump UI hierarchy: {e}")
            hierarchy = "<hierarchy_dump_failed/>"

        hierarchy_path = os.path.join(data_dir, f"{image_index}.xml")
        with open(hierarchy_path, "w", encoding="utf-8") as f:
            f.write(hierarchy)
        return hierarchy

    try:
        hierarchy = device.dump_hierarchy()
    except Exception as e:
        logging.error(f"Failed to dump UI hierarchy: {e}")
        hierarchy = {}

    hierarchy_path = os.path.join(data_dir, f"{image_index}.json")
    try:
        if isinstance(hierarchy, str):
            hierarchy_json = json.loads(hierarchy)
        else:
            hierarchy_json = hierarchy
        with open(hierarchy_path, "w", encoding="utf-8") as f:
            json.dump(hierarchy_json, f, ensure_ascii=False, indent=2)
        projection_path = os.path.join(data_dir, f"{image_index}.xml")
        with open(projection_path, "w", encoding="utf-8") as f:
            f.write(_harmony_hierarchy_to_xml(hierarchy_json))
    except (json.JSONDecodeError, TypeError):
        logging.warning(f"Failed to parse hierarchy as JSON, saving as plain text")
        with open(hierarchy_path, "w", encoding="utf-8") as f:
            f.write(str(hierarchy))
    return hierarchy


_ANDROID_BOUNDS_RE = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")


def _android_clickable_nodes(hierarchy, surface_width, surface_height):
    """Extract specific clickable nodes from Android XML or Harmony JSON."""
    surface_area = max(1, surface_width * surface_height)
    nodes = []

    if isinstance(hierarchy, dict):
        def context_from_attrs(attrs):
            return " ".join(
                str(attrs.get(key, ""))
                for key in ("text", "description", "hint", "originalText", "id", "key", "type")
                if attrs.get(key)
            )

        def visit(item, ancestor_context=""):
            if not isinstance(item, dict):
                return ""
            attrs = item.get("attributes") or {}
            node_context = context_from_attrs(attrs)
            child_context = " ".join(part for part in (ancestor_context, node_context) if part)
            descendant_context = " ".join(
                part
                for part in (
                    visit(child, child_context)
                    for child in item.get("children", [])
                    if isinstance(child, dict)
                )
                if part
            )
            if attrs.get("clickable") != "true" or attrs.get("enabled") == "false":
                return " ".join(part for part in (node_context, descendant_context) if part)
            if attrs.get("visible") == "false":
                return " ".join(part for part in (node_context, descendant_context) if part)
            match = _ANDROID_BOUNDS_RE.match(str(attrs.get("bounds", "")))
            if not match:
                return " ".join(part for part in (node_context, descendant_context) if part)
            bounds = [int(value) for value in match.groups()]
            x1, y1, x2, y2 = bounds
            area = max(0, x2 - x1) * max(0, y2 - y1)
            if area <= 0 or area > surface_area * 0.35:
                return " ".join(part for part in (node_context, descendant_context) if part)
            description = attrs.get("description") or attrs.get("hint") or attrs.get("originalText") or ""
            resource_id = attrs.get("id") or attrs.get("key") or ""
            nodes.append({
                "bounds": bounds,
                "text": attrs.get("text", ""),
                "content_desc": description,
                "resource_id": resource_id,
                "class": attrs.get("type", ""),
                "semantic_context": " ".join(
                    part
                    for part in (ancestor_context, node_context, descendant_context)
                    if part
                ),
                "surface_width": surface_width,
            })
            return " ".join(part for part in (node_context, descendant_context) if part)

        visit(hierarchy)
        return nodes

    if not isinstance(hierarchy, str) or not hierarchy.strip():
        return []
    try:
        root = ET.fromstring(hierarchy)
    except ET.ParseError:
        return []

    for node in root.iter("node"):
        attrs = node.attrib
        if attrs.get("clickable") != "true" or attrs.get("enabled") == "false":
            continue
        if attrs.get("visible-to-user") == "false":
            continue
        match = _ANDROID_BOUNDS_RE.match(attrs.get("bounds", ""))
        if not match:
            continue
        bounds = [int(value) for value in match.groups()]
        x1, y1, x2, y2 = bounds
        area = max(0, x2 - x1) * max(0, y2 - y1)
        # Full-screen clickable ancestors are not useful grounding targets.
        if area <= 0 or area > surface_area * 0.35:
            continue
        nodes.append({
            "bounds": bounds,
            "text": attrs.get("text", ""),
            "content_desc": attrs.get("content-desc", ""),
            "resource_id": attrs.get("resource-id", ""),
            "class": attrs.get("class", ""),
            "semantic_context": "",
            "surface_width": surface_width,
        })
    return nodes


def _point_in_bounds(x, y, bounds):
    x1, y1, x2, y2 = bounds
    return x1 <= x <= x2 and y1 <= y <= y2


def _intersection_ratio(bounds_a, bounds_b):
    ax1, ay1, ax2, ay2 = bounds_a
    bx1, by1, bx2, by2 = bounds_b
    width = max(0, min(ax2, bx2) - max(ax1, bx1))
    height = max(0, min(ay2, by2) - max(ay1, by1))
    intersection = width * height
    node_area = max(1, (bx2 - bx1) * (by2 - by1))
    return intersection / node_area


def _normalized_text(value):
    return str(value or "").lower().replace(" ", "")


def _node_semantic_parts(node):
    desc = _normalized_text(node.get("content_desc", ""))
    text = _normalized_text(node.get("text", ""))
    resource_id = _normalized_text(node.get("resource_id", ""))
    class_name = _normalized_text(node.get("class", ""))
    return desc, text, resource_id, class_name


def _target_semantic_score(target_element, node):
    target = _normalized_text(target_element)
    desc, text, resource_id, class_name = _node_semantic_parts(node)
    context_labels = tuple(
        _normalized_text(part)
        for part in str(node.get("semantic_context", "")).split()
        if _normalized_text(part)
    )
    score = 0
    for label in (desc, text, resource_id, class_name, *context_labels):
        contextual_search_mention = (
            label == "搜索"
            and any(word in target for word in ("清除", "叉号", "关闭", "返回"))
        )
        if label and label in target and not contextual_search_mention:
            score += 4

    # Common wording differences between visual descriptions and accessibility labels.
    wants_search_field = any(word in target for word in ("搜索框", "搜索栏", "输入区域", "关键词文本"))
    is_search_field = desc == "搜索栏" or "searchedit" in resource_id
    if wants_search_field and is_search_field:
        score += 8
    wants_button = target in ("搜索", "搜索键", "搜索按钮") or "搜索按钮" in target
    is_search_button = desc == "搜索" or text == "搜索" or "searchbtn" in resource_id
    if wants_button and is_search_button:
        score += 8
    return score


def _target_wants_text_entry(target_element, action_type=None):
    if action_type == "click_input":
        return True
    target = _normalized_text(target_element)
    if _target_wants_button_like_control(target):
        return False
    return any(word in target for word in (
        "搜索框", "搜索栏", "输入框", "输入栏", "输入区域", "关键词文本", "文本框", "编辑框",
        "searchbox", "searchbar", "searchfield", "inputfield", "textfield", "edittext",
    ))


def _target_wants_button_like_control(target_element):
    target = _normalized_text(target_element)
    return any(word in target for word in (
        "按钮", "按键", "搜索键", "提交", "确认", "取消", "关闭", "清除", "返回",
        "button", "key",
    ))


def _target_wants_floating_action_button(target_element):
    """Return whether a target explicitly describes a create/FAB control."""
    target = _normalized_text(target_element)
    return (
        "floatingactionbutton" in target
        or "浮动按钮" in target
        or "右下角+" in target
        or ("新建" in target and "笔记" in target)
        or ("创建" in target and "笔记" in target)
        # Some apps expose a primary create action as an unlabeled plus in
        # the bottom navigation rather than as a conventional FAB.  Keep
        # this semantic and visual fallback generic: it is enabled only when
        # the target describes an add/create action and a plus icon.
        or (
            ("+" in target or "plus" in target or "加号" in target)
            and any(word in target for word in ("发布", "创建", "新建", "添加", "add", "create", "plus"))
        )
    )


def _target_wants_bottom_navigation_add_control(target_element):
    target = _normalized_text(target_element)
    return (
        _target_wants_floating_action_button(target_element)
        and any(word in target for word in ("底部", "bottom", "导航", "navigation"))
    )


def _red_pixel_ratio(image, bounds):
    """Measure saturated-red coverage for a candidate control bounds."""
    if image is None:
        return 0, 0.0
    try:
        x1, y1, x2, y2 = (int(value) for value in bounds)
        width, height = image.size
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(width, x2), min(height, y2)
        if x2 <= x1 or y2 <= y1:
            return 0, 0.0
        crop = image.crop((x1, y1, x2, y2)).convert("RGB")
        pixels = (
            crop.get_flattened_data()
            if hasattr(crop, "get_flattened_data")
            else crop.getdata()
        )
    except (AttributeError, TypeError, ValueError):
        return 0, 0.0
    red_pixels = sum(
        1
        for red, green, blue in pixels
        if red >= 170 and red >= green * 1.55 and red >= blue * 1.55
    )
    return red_pixels, red_pixels / max(1, crop.width * crop.height)


def find_visual_floating_action_button(target_element, hierarchy, image, surface_width, surface_height):
    """Find an unlabeled red FAB from hierarchy geometry plus screenshot pixels.

    Harmony accessibility often exposes a red FAB only as a generic ViewGroup.
    A model bbox is not allowed to redirect a click to another unlabeled icon;
    this resolver instead requires both a compact lower-right control and a
    substantial saturated-red region inside that exact control.
    """
    if not _target_wants_floating_action_button(target_element):
        return None
    bottom_navigation_add = _target_wants_bottom_navigation_add_control(target_element)
    candidates = []
    for node in _android_clickable_nodes(hierarchy, surface_width, surface_height):
        if not _node_is_compact_clickable_control(node):
            continue
        x1, y1, x2, y2 = node["bounds"]
        center_x = (x1 + x2) // 2
        center_y = (y1 + y2) // 2
        if bottom_navigation_add:
            if not (surface_height * 0.78 <= center_y <= surface_height * 0.99):
                continue
        elif center_x < surface_width * 0.65 or not (surface_height * 0.35 <= center_y <= surface_height * 0.88):
            continue
        red_pixels, red_ratio = _red_pixel_ratio(image, node["bounds"])
        if red_pixels < 1200 or red_ratio < 0.18:
            continue
        candidates.append((red_ratio, red_pixels, node, center_x, center_y))
    if not candidates:
        return None
    red_ratio, red_pixels, node, center_x, center_y = max(candidates, key=lambda item: (item[0], item[1]))
    return {
        "click_point": [center_x, center_y],
        "selected_node": node,
        "candidate_count": len(candidates),
        "red_pixel_count": red_pixels,
        "red_pixel_ratio": round(red_ratio, 4),
    }


def _node_is_text_entry(node):
    desc, text, resource_id, class_name = _node_semantic_parts(node)
    context = _normalized_text(node.get("semantic_context", ""))
    signature = f"{desc}|{text}|{resource_id}|{class_name}|{context}"
    if any(word in signature for word in (
        "textinput", "textarea", "edittext", "searchedit", "searchinput", "searchfield",
        "inputfield", "textfield", "输入框", "搜索框", "搜索栏",
    )):
        return True
    if ("搜索" in (desc or text)) and any(word in signature for word in ("input", "edit", "field", "栏", "框")):
        return True
    if any(word in signature for word in ("searchcontainer", "searchbg", "searchbox", "searchbar")):
        x1, _, x2, _ = node.get("bounds", [0, 0, 0, 0])
        surface_width = max(1, int(node.get("surface_width") or 1))
        if (x2 - x1) / surface_width >= 0.25:
            return True
    return False


def _node_is_low_information_container(node):
    desc, text, resource_id, class_name = _node_semantic_parts(node)
    if desc or text:
        return False
    generic_classes = (
        "stack", "row", "column", "layout", "viewgroup", "framelayout", "linearlayout",
        "relativelayout", "constraintlayout", "container", "webx_capi_div", "view",
    )
    generic_ids = (
        "wrapper", "container", "layout", "tab", "item", "cell", "card", "view", "panel",
    )
    return (
        any(word in class_name for word in generic_classes)
        or any(word in resource_id for word in generic_ids)
    )


def _node_is_compact_clickable_control(node):
    """Recognise an icon/FAB-sized control even when accessibility has no label."""
    bounds = node.get("bounds") or [0, 0, 0, 0]
    if len(bounds) != 4:
        return False
    width = max(0, int(bounds[2]) - int(bounds[0]))
    height = max(0, int(bounds[3]) - int(bounds[1]))
    if width < 32 or height < 32 or width > 260 or height > 260:
        return False
    aspect = width / max(1, height)
    return 0.5 <= aspect <= 2.0


def _alignment_acceptance_reason(
    target_element,
    action_type,
    node,
    semantic,
    overlap,
    distance,
    *,
    node_center_in_model_bounds,
):
    wants_text_entry = _target_wants_text_entry(target_element, action_type)
    text_entry_node = _node_is_text_entry(node)
    low_info_container = _node_is_low_information_container(node)

    if wants_text_entry:
        if text_entry_node:
            return True, "text_entry_semantic_match"
        if semantic >= 8:
            return True, "strong_target_semantic_match"
        return False, "text_entry_target_rejects_non_input_node"

    if semantic > 0:
        return True, "target_semantic_match"

    if low_info_container:
        # Many cross-platform FABs/icon buttons expose only a generic
        # ViewGroup.  Geometry alone is weak evidence, though: an adjacent
        # unlabeled icon can overlap a loose VLM box by a few pixels.  Never
        # move a model click to a node whose centre is outside the model box;
        # that is how a nearby control can hijack an otherwise correct click.
        if (
            _node_is_compact_clickable_control(node)
            and node_center_in_model_bounds
            and overlap >= 0.50
            and distance <= 96
        ):
            return True, "compact_button_geometry_match"
        if (
            _target_wants_button_like_control(target_element)
            and node_center_in_model_bounds
            and overlap >= 0.45
            and distance <= 64
        ):
            return True, "button_like_geometry_match"
        if node_center_in_model_bounds and overlap >= 0.60 and distance <= 48:
            return True, "strong_geometry_low_information_node"
        return False, "geometry_only_rejects_low_information_container"

    if node_center_in_model_bounds and overlap >= 0.45 and distance <= 48:
        return True, "strong_geometry_near_miss"

    return False, "weak_geometry_without_semantics"


def align_click_to_xml_node(raw_point, converted_bounds, target_element, hierarchy, surface_width, surface_height, action_type=None):
    """Snap a near-miss model point to a strongly supported clickable XML node.

    The model remains the source of intent. XML is only used when the converted
    point misses every specific clickable node and a nearby/intersecting node has
    adequate geometric or semantic support. Text-entry targets are guarded more
    strictly because snapping them to generic containers often opens unrelated
    tabs or overlays before text is entered.
    """
    x, y = raw_point
    nodes = _android_clickable_nodes(hierarchy, surface_width, surface_height)
    direct_hits = [node for node in nodes if _point_in_bounds(x, y, node["bounds"])]
    wants_text_entry = _target_wants_text_entry(target_element, action_type)
    audit = {
        "raw_point": [x, y],
        "direct_hits": direct_hits,
        "snapped": False,
        "selected_node": None,
        "candidate_count": 0,
        "target_wants_text_entry": wants_text_entry,
    }
    semantic_nodes_exist = any(_target_semantic_score(target_element, node) > 0 for node in nodes)
    direct_semantic_hit = any(_target_semantic_score(target_element, node) > 0 for node in direct_hits)
    direct_text_entry_hit = any(_node_is_text_entry(node) for node in direct_hits)
    direct_compact_control_hit = any(_node_is_compact_clickable_control(node) for node in direct_hits)
    if direct_hits and wants_text_entry and direct_text_entry_hit:
        audit["alignment_basis"] = "direct_text_entry_hit"
        return (x, y), audit
    if direct_hits and not wants_text_entry and (
        direct_semantic_hit
        or direct_compact_control_hit
        # A raw click within a large unlabeled card is not enough evidence for
        # a requested icon/FAB: a compact control may overlap its edge.  Let
        # the candidate ranking below inspect that control instead of treating
        # the enclosing card as a successful direct hit.
        or (not semantic_nodes_exist and not _target_wants_button_like_control(target_element))
    ):
        audit["alignment_basis"] = "direct_supported_hit"
        return (x, y), audit

    model_bounds = converted_bounds or [x, y, x, y]
    candidates = []
    for node in nodes:
        x1, y1, x2, y2 = node["bounds"]
        center_x = (x1 + x2) // 2
        center_y = (y1 + y2) // 2
        distance = ((center_x - x) ** 2 + (center_y - y) ** 2) ** 0.5
        overlap = _intersection_ratio(model_bounds, node["bounds"])
        semantic = _target_semantic_score(target_element, node)
        if overlap <= 0 and distance > 64:
            continue
        text_entry_bonus = 8 if wants_text_entry and _node_is_text_entry(node) else 0
        low_info_penalty = 4 if _node_is_low_information_container(node) else 0
        score = (semantic + text_entry_bonus) * 10 + overlap * 5 - distance / max(surface_width, surface_height) - low_info_penalty
        area = max(1, (x2 - x1) * (y2 - y1))
        candidates.append((score, semantic, area, overlap, distance, node, center_x, center_y))

    audit["candidate_count"] = len(candidates)
    if not candidates:
        audit["rejection_reason"] = "no_candidate"
        return (x, y), audit
    # When a clickable parent inherits the same descendant label as its
    # clickable child, prefer the smaller node because it is the more
    # specific interaction target. Semantic agreement remains primary.
    candidates.sort(key=lambda item: (item[1], -item[2], item[0]), reverse=True)
    _, semantic, _, overlap, distance, node, center_x, center_y = candidates[0]
    node_center_in_model_bounds = _point_in_bounds(center_x, center_y, model_bounds)
    accepted, reason = _alignment_acceptance_reason(
        target_element,
        action_type,
        node,
        semantic,
        overlap,
        distance,
        node_center_in_model_bounds=node_center_in_model_bounds,
    )
    if not accepted:
        audit.update({
            "selected_node": node,
            "semantic_score": semantic,
            "intersection_ratio": round(overlap, 4),
            "center_distance": round(distance, 2),
            "candidate_center_in_model_bounds": node_center_in_model_bounds,
            "rejection_reason": reason,
        })
        return (x, y), audit

    audit.update({
        "snapped": True,
        "selected_node": node,
        "semantic_score": semantic,
        "intersection_ratio": round(overlap, 4),
        "center_distance": round(distance, 2),
        "candidate_center_in_model_bounds": node_center_in_model_bounds,
        "alignment_basis": reason,
    })
    return (center_x, center_y), audit


def _alignment_rejection_blocks_click(audit):
    """Return whether XML alignment has disproved a safe click target.

    ``direct_hits`` is retained in these audits for diagnosis, but a weak
    intersection with an unrelated node must never reach ``device.click``.
    """
    if not isinstance(audit, dict):
        return False
    return str(audit.get("rejection_reason") or "").strip().casefold() in {
        "wrong_target",
        "outside_target",
        "weak_geometry_without_semantics",
        "geometry_only_rejects_low_information_container",
    }


def _bboxes_do_not_overlap(first, second):
    """Return true only when two native-coordinate target boxes are disjoint."""
    try:
        ax1, ay1, ax2, ay2 = (int(value) for value in first)
        bx1, by1, bx2, by2 = (int(value) for value in second)
    except (TypeError, ValueError):
        return False
    return min(ax2, bx2) <= max(ax1, bx1) or min(ay2, by2) <= max(ay1, by1)


def _is_usable_click_bbox(bbox, surface_width, surface_height):
    """Reject a converted bbox that collapsed onto a display boundary.

    Clamping must be a last-resort safety net, never evidence that a model
    found a control.  A zero-area box cannot provide a meaningful centre click
    and commonly means that an incompatible coordinate convention was used.
    """
    try:
        x1, y1, x2, y2 = (int(value) for value in bbox)
    except (TypeError, ValueError):
        return False
    return (
        0 <= x1 < x2 < int(surface_width)
        and 0 <= y1 < y2 < int(surface_height)
    )


def handle_click_action(decider_response, device, img, screenshot_resize, grounder_prompt_template_bbox,
                        grounder_prompt_template_no_bbox, bbox_flag, use_qwen3, use_e2e,
                        data_dir, device_paths, current_image, image_index, actions, history, hierarchy=None):
    reasoning = decider_response["reasoning"]
    target_element = decider_response["parameters"].get("target_element", "stepfun_click_target")
    target_width, target_height = get_click_coordinate_size(device, img)
    decider_native_bbox = None
    if bbox_flag and decider_response["parameters"].get("bbox"):
        try:
            decider_bbox = decider_response["parameters"]["bbox"]
            decider_native_bbox = (
                convert_qwen3_coordinates_to_absolute(
                    decider_bbox,
                    img.width,
                    img.height,
                    is_bbox=True,
                    target_width=target_width,
                    target_height=target_height,
                )
                if use_qwen3
                else [int(coord / factor) for coord in decider_bbox]
            )
        except (TypeError, ValueError):
            # The validated response will still be handled by the existing
            # Grounder path; this guard must never broaden dispatch failure.
            decider_native_bbox = None
    bbox_source = "decider" if use_e2e else "grounder"

    if decider_response["parameters"].get("coords"):
        x, y = convert_qwen3_coordinates_to_absolute(
            decider_response["parameters"]["coords"],
            img.width,
            img.height,
            is_bbox=False,
            target_width=target_width,
            target_height=target_height,
        )
        _clear_input_focus(device)
        device.click(x, y)
        append_action_and_history(actions, history, decider_response, {
            "type": "click",
            "target_element": target_element,
            "position_x": x,
            "position_y": y,
            "click_coordinate_size": [target_width, target_height],
            "screenshot_size": [img.width, img.height],
            "action_index": image_index
        })
        return

    if use_e2e:
        bbox = decider_response["parameters"]["bbox"]
        if bbox is None:
            logging.error("E2E mode: bbox not found in decider response")
            raise ValueError("E2E mode requires bbox in decider response")

        logging.info(f"E2E mode: Using bbox directly from decider: {bbox}")
        if use_qwen3:
            bbox = convert_qwen3_coordinates_to_absolute(
                bbox,
                img.width,
                img.height,
                is_bbox=True,
                target_width=target_width,
                target_height=target_height,
            )
        x1, y1, x2, y2 = bbox
    else:
        grounder_prompt = (grounder_prompt_template_bbox if bbox_flag else grounder_prompt_template_no_bbox).format(
            reasoning=reasoning, description=target_element
        )
        grounder_prompt += (
            "\nCoordinate contract: report pixel coordinates in the supplied image only "
            f"(width={int(img.width * factor)}, height={int(img.height * factor)}). "
            "Do not use native-screen or normalized coordinates."
        )

        try:
            grounder_response = call_model_with_validation_retry(
                grounder_client,
                grounder_model,
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screenshot_resize}"}},
                            {"type": "text", "text": grounder_prompt},
                        ]
                    }
                ],
                validator_func=validate_grounder_response,
                max_retries=MAX_RETRIES,
                max_tokens=GROUNDER_MAX_TOKENS,
                context="Grounder"
            )
        except Exception as e:
            logging.error(f"Grounder 处理失败: {e}")
            raise

        if bbox_flag:
            bbox = None
            for key in grounder_response:
                if key.lower() in ["bbox", "bbox_2d", "bbox-2d", "bbox_2D", "bbox2d"]:
                    bbox = grounder_response[key]
                    break

            if bbox is None:
                raise ValueError("Grounder response validation failed: no bbox field found")

            if use_qwen3:
                bbox = convert_qwen3_coordinates_to_absolute(
                    bbox,
                    img.width,
                    img.height,
                    is_bbox=True,
                    target_width=target_width,
                    target_height=target_height,
                )
                x1, y1, x2, y2 = bbox
            else:
                x1, y1, x2, y2 = [int(coord / factor) for coord in bbox]
            if (
                decider_native_bbox is not None
                and _is_usable_click_bbox(decider_native_bbox, target_width, target_height)
                and _bboxes_do_not_overlap(
                decider_native_bbox, [x1, y1, x2, y2]
                )
            ):
                # A Grounder that identifies a completely disjoint region is
                # not a refinement of the Decider's target. Preserve the
                # Decider's centre instead of letting a second model turn one
                # intended click into an unrelated action.
                logging.warning(
                    "Grounder bbox %s is disjoint from Decider bbox %s; using Decider geometry",
                    [x1, y1, x2, y2], decider_native_bbox,
                )
                x1, y1, x2, y2 = decider_native_bbox
                bbox_source = "decider_grounder_disagreement"
            elif decider_native_bbox is not None and not _is_usable_click_bbox(
                decider_native_bbox, target_width, target_height
            ):
                logging.warning(
                    "Converted Decider bbox %s is degenerate or clipped to the display edge; "
                    "it will not override the Grounder result",
                    decider_native_bbox,
                )
        else:
            coordinates = grounder_response["coordinates"]
            if use_qwen3:
                coordinates = convert_qwen3_coordinates_to_absolute(
                    coordinates,
                    img.width,
                    img.height,
                    is_bbox=False,
                    target_width=target_width,
                    target_height=target_height,
                )
                x, y = coordinates
            else:
                x, y = [int(coord / factor) for coord in coordinates]

    if bbox_flag or use_e2e:
        print(f"Clicking on bbox: [{x1}, {y1}, {x2}, {y2}]")
        print(f"Image size: width={img.width}, height={img.height}")
        print(f"Adjusted bbox: [{x1}, {y1}, {x2}, {y2}]")
        position_x = (x1 + x2) // 2
        position_y = (y1 + y2) // 2
        raw_click_point = [position_x, position_y]
        # The Decider/Grounder point remains authoritative. Hierarchy is used
        # only for generic geometric alignment and evidence; a
        # screenshot-specific FAB/add-button resolver must not redirect a
        # model decision to a control selected by a special case.
        (position_x, position_y), xml_hit_test = align_click_to_xml_node(
            (position_x, position_y), [x1, y1, x2, y2], target_element,
            hierarchy, target_width, target_height, action_type="click",
        )
        if xml_hit_test.get("snapped"):
            logging.info("XML-aligned click point %s -> (%s, %s), node=%s", raw_click_point, position_x, position_y, xml_hit_test.get("selected_node"))
        if _alignment_rejection_blocks_click(xml_hit_test):
            reason = str(xml_hit_test.get("rejection_reason") or "unknown")
            raise ValueError(
                f"target alignment rejected before dispatch: {reason}"
            )
        _clear_input_focus(device)
        device.click(position_x, position_y)
        append_action_and_history(actions, history, decider_response, {
            "type": "click",
            "target_element": target_element,
            "position_x": position_x,
            "position_y": position_y,
            "click_point": [position_x, position_y],
            "bounds": [x1, y1, x2, y2],
            "raw_model_bbox": decider_response["parameters"].get("bbox"),
            "bbox_source": bbox_source,
            "converted_bounds": [x1, y1, x2, y2],
            "click_point_before_xml_alignment": raw_click_point,
            "xml_hit_test_result": xml_hit_test,
            "click_coordinate_size": [target_width, target_height],
            "screenshot_size": [img.width, img.height],
            "action_index": image_index
        })

        img_path = os.path.join(device_paths["current_dir"], current_image)
        save_path = os.path.join(data_dir, f"{image_index}_highlighted.jpg")
        img = Image.open(img_path)
        draw = ImageDraw.Draw(img)
        font = load_overlay_font(40)
        text = f"CLICK [{position_x}, {position_y}]"
        text = textwrap.fill(text, width=20)
        text_width, text_height = draw.textbbox((0, 0), text, font=font)[2:]
        draw.text((img.width / 2 - text_width / 2, 0), text, fill="red", font=font)
        img.save(save_path)

        bounds_path = os.path.join(data_dir, f"{image_index}_bounds.jpg")
        img_bounds = Image.open(save_path)
        draw_bounds = ImageDraw.Draw(img_bounds)
        draw_bounds.rectangle([x1, y1, x2, y2], outline='red', width=5)
        img_bounds.save(bounds_path)

        # 绘制点击位置。使用 Pillow 避免 Windows OpenCV Unicode 路径乱码。
        click_point_path = os.path.join(data_dir, f"{image_index}_click_point.jpg")
        click_image = Image.open(bounds_path)
        click_draw = ImageDraw.Draw(click_image)
        radius = 15
        click_draw.ellipse(
            [position_x - radius, position_y - radius, position_x + radius, position_y + radius],
            fill=(0, 255, 0),
        )
        click_image.save(click_point_path)
    else:
        _clear_input_focus(device)
        device.click(x, y)
        append_action_and_history(actions, history, decider_response, {
            "type": "click",
            "target_element": target_element,
            "position_x": x,
            "position_y": y,
            "click_coordinate_size": [target_width, target_height],
            "screenshot_size": [img.width, img.height],
            "action_index": image_index
        })


def handle_swipe_action(decider_response, device, img, use_e2e, use_qwen3, data_dir, image_index, actions, history):
    direction = decider_response["parameters"].get("direction", "UP").upper()
    _clear_input_focus(device)
    target_width, target_height = get_click_coordinate_size(device, img)

    start_coords = decider_response["parameters"].get("start_coords")
    end_coords = decider_response["parameters"].get("end_coords")

    if start_coords and end_coords:
        if use_qwen3:
            start_coords = convert_qwen3_coordinates_to_absolute(
                start_coords,
                img.width,
                img.height,
                is_bbox=False,
                target_width=target_width,
                target_height=target_height,
            )
            end_coords = convert_qwen3_coordinates_to_absolute(
                end_coords,
                img.width,
                img.height,
                is_bbox=False,
                target_width=target_width,
                target_height=target_height,
            )

        start_x, start_y = start_coords
        end_x, end_y = end_coords
        if should_use_explicit_swipe_coords(
            (start_x, start_y), (end_x, end_y), direction, target_width, target_height,
        ):
            logging.info(f"Swipe from [{start_x}, {start_y}] to [{end_x}, {end_y}]")
            device.swipe_with_coords(start_x, start_y, end_x, end_y)

            append_action_and_history(actions, history, decider_response, {
                "type": "swipe",
                "press_position_x": start_x,
                "press_position_y": start_y,
                "release_position_x": end_x,
                "release_position_y": end_y,
                "direction": direction.lower(),
                "coordinate_source": "model_explicit_safe",
                "click_coordinate_size": [target_width, target_height],
                "screenshot_size": [img.width, img.height],
                "action_index": image_index
            })
            create_swipe_visualization(data_dir, image_index, direction.lower(), start_x, start_y, end_x, end_y)
            return
        logging.warning(
            "Unsafe explicit swipe [%s, %s] -> [%s, %s] for direction=%s; using directional fallback",
            start_x, start_y, end_x, end_y, direction,
        )

    if use_e2e:
        logging.warning("E2E mode: start_coords or end_coords not found, falling back to direction-based swipe")

    press_position_x, press_position_y, release_position_x, release_position_y = compute_swipe_positions(direction, target_width, target_height)
    device.swipe_with_coords(press_position_x, press_position_y, release_position_x, release_position_y,)
    append_action_and_history(actions, history, decider_response, {
        "type": "swipe",
        "press_position_x": press_position_x,
        "press_position_y": press_position_y,
        "release_position_x": release_position_x,
        "release_position_y": release_position_y,
        "direction": direction.lower(),
        "coordinate_source": "directional_fallback_after_unsafe_explicit_coords" if start_coords and end_coords else "directional_default",
        "click_coordinate_size": [target_width, target_height],
        "screenshot_size": [img.width, img.height],
        "action_index": image_index
    })
    create_swipe_visualization(data_dir, image_index, direction.lower())

def task_in_app(app, old_task, task, device, data_dir, bbox_flag=True, use_qwen3=True, device_type="Android", use_e2e=False, decider_protocol=DECIDER_PROTOCOL_QWEN_JSON):
    history = []
    actions = []
    reacts = []
    stop_reason = "UNKNOWN"
    decider_adapter = get_decider_adapter(decider_protocol)
    grounder_prompt_template_bbox = None
    grounder_prompt_template_no_bbox = None

    if use_e2e:
        # 在e2e模式下使用e2e流程
        logging.info("Using e2e mode with e2e_qwen3.md")

    elif use_qwen3:
        grounder_prompt_template_bbox = load_prompt("grounder_qwen3_bbox.md")
        grounder_prompt_template_no_bbox = load_prompt("grounder_qwen3_coordinates.md")

    else:
        grounder_prompt_template_bbox = load_prompt("grounder_bbox.md")
        grounder_prompt_template_no_bbox = load_prompt("grounder_coordinates.md")

    logging.info("Using decider adapter: %s (%s)", decider_adapter.display_name, decider_protocol)
    while True:     
        if len(actions) >= MAX_STEPS:
            logging.info("Reached maximum steps, stopping the task.")
            stop_reason = "MAX_STEPS_REACHED"
            break

        screenshot_resize = get_screenshot(device, device_type)
        # State the coordinate frame explicitly.  The screenshot is resized
        # before it is sent to the model, while device clicks use native
        # coordinates.  Omitting these dimensions makes mixed coordinate
        # responses much more likely on tall displays.
        screenshot_name = "screenshot-Android.jpg" if device_type == "Android" else "screenshot-Harmony.jpg"
        with Image.open(screenshot_name) as coordinate_image:
            model_width = int(coordinate_image.width * factor)
            model_height = int(coordinate_image.height * factor)
        task_with_coordinate_contract = (
            f"{task}\n\n"
            "### Coordinate contract\n"
            f"The supplied screenshot is {model_width}x{model_height} pixels. "
            "For every click or text-entry bbox, use only this screenshot's pixel coordinates "
            "([x1, y1, x2, y2]); do not use native-screen or normalized coordinates."
        )

        messages = decider_adapter.build_messages(
            task_with_coordinate_contract, history, screenshot_resize, use_e2e, device_type
        )
        logging.info(f"Decider messages[200]: \n{messages[200:]}")

        # --- 调用 Decider 模型 ---
        try:
            # 为e2e模式创建特定的校验器
            def decider_validator(response):
                validate_decider_response(response, use_e2e=use_e2e, decider_protocol=decider_protocol)
            
            decider_response = call_model_with_validation_retry(
                decider_client,
                decider_model,
                messages,
                validator_func=decider_validator,
                parser_func=decider_adapter.parse_response,
                max_retries=MAX_RETRIES,
                max_tokens=DECIDER_MAX_TOKENS,
                context="Decider"
            )

            converted_item = {
                "reasoning": decider_response["reasoning"],
                "function": {
                    "name": decider_response["action"],
                    "parameters": decider_response["parameters"]
                }
            }
            if decider_response.get("raw_protocol"):
                converted_item["raw_protocol"] = decider_response["raw_protocol"]
            if decider_response.get("stepfun_fields"):
                converted_item["stepfun_fields"] = decider_response["stepfun_fields"]
        except Exception as e:
            logging.error(f"Decider 处理失败: {e}")
            raise

        
        reacts.append(converted_item)
        action = decider_response["action"]

        # compute image index for this loop iteration (1-based)
        image_index = len(actions) + 1
        device_paths = get_device_paths(device_type)
        img_path = device_paths["img_path"]
        current_image = device_paths["screenshot_name"]
        
        save_path = os.path.join(data_dir, f"{image_index}.jpg")
        img = Image.open(img_path)
        img.save(save_path)

        # attach index to the most recent react (reasoning)
        if reacts:
            try:
                reacts[-1]["action_index"] = image_index
            except Exception:
                pass

        # 根据设备类型保存hierarchy
        hierarchy = save_hierarchy(device, device_type, data_dir, image_index)
        
        if action == "done":
            print("Task completed.")
            status = decider_response["parameters"]["status"]
            stop_reason = f"TASK_COMPLETED_{status.upper()}"
            actions.append({
                "type": "done",
                "status": status,
                "message": decider_response["parameters"].get("message"),
                "action_index": image_index
            })
            break

        if action == "abort":
            handle_abort_action(decider_response, image_index, actions, history)
            stop_reason = "TASK_ABORTED_BY_MODEL"
            break

        if action == "info":
            should_continue = handle_info_action(decider_response, image_index, actions, history)
            if not should_continue:
                stop_reason = "USER_INPUT_REQUIRED"
                break
            continue

        if action == "call_user":
            should_continue = handle_call_user_action(decider_response, image_index, actions, history)
            if not should_continue:
                stop_reason = "USER_INTERVENTION_REQUIRED"
                break
            continue

        action_handlers = {
            "click": lambda: handle_click_action(
                decider_response, device, img, screenshot_resize,
                grounder_prompt_template_bbox, grounder_prompt_template_no_bbox,
                bbox_flag, use_qwen3, use_e2e,
                data_dir, device_paths, current_image, image_index, actions, history, hierarchy
            ),
            "swipe": lambda: handle_swipe_action(
                decider_response, device, img, use_e2e, use_qwen3,
                data_dir, image_index, actions, history
            ),
            "click_input": lambda: handle_click_input_action(decider_response, device, img, data_dir, image_index, actions, history, hierarchy),
            "input": lambda: handle_input_action(decider_response, device, image_index, actions, history),
            "open_app": lambda: handle_open_app_action(decider_response, device, image_index, actions, history),
            "press_home": lambda: handle_press_home_action(decider_response, device, device_type, image_index, actions, history),
            "press_back": lambda: handle_press_back_action(decider_response, device, device_type, image_index, actions, history),
            "wait": lambda: handle_wait_action(decider_response, image_index, actions, history),
            "long_press": lambda: handle_long_press_action(decider_response, device, img, image_index, actions, history),
        }

        handler = action_handlers.get(action)
        if handler is None:
            raise ValueError(f"Unknown action: {action}")
        handler()
        
    
    from datetime import datetime
    
    # 获取当前日期、星期和时间
    now = datetime.now()
    weekdays = ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日']
    execution_timestamp = {
        "date": now.strftime("%Y-%m-%d"),
        "weekday": weekdays[now.weekday()],
        "time": now.strftime("%H:%M:%S")
    }
    
    data = {
        "app_name": app,
        "task_type": None,
        "old_task_description": old_task,
        "task_description": task,
        "execution_timestamp": execution_timestamp,
        "decider_protocol": decider_protocol,
        "stop_reason": stop_reason,
        "action_count": len(actions),
        "actions": actions
    }

    with open(os.path.join(data_dir, "actions.json"), "w", encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    with open(os.path.join(data_dir, "react.json"), "w", encoding='utf-8') as f:
        json.dump(reacts, f, ensure_ascii=False, indent=4)
    
    # 任务完成后，异步提取用户偏好
    if preference_extractor and should_extract_preferences(data):
        task_data = {
            'task_description': task,
            'actions': actions,
            'reacts': reacts,
            'app_name': app
        }
        preference_extractor.extract_async(task_data)
        logging.info("Submitted preference extraction task")


def parse_planner_response(response_str: str):
    """
    解析 Planner 模型响应，使用统一的 JSON 解析逻辑
    
    Args:
        response_str: 模型返回的响应字符串
    
    Returns:
        解析后的 JSON 对象，或 None 如果解析失败
    """
    parsed = _load_json_from_text(response_str)
    if parsed is None:
        logging.error(f"解析 Planner 响应失败")
        logging.error(f"原始内容: {str(response_str)[:300]}...")
    return parsed

def get_app_package_name(task_description, use_graphrag=False, device_type="Android", use_experience=False):
    """单阶段：本地检索经验，调用模型完成应用选择和任务描述生成。"""
    current_file_path = Path(__file__).resolve()
    current_dir = current_file_path.parent
    default_template_path = current_dir.parent.parent / "utils" /"experience" / "templates-new.json"
    logging.debug("Using template path: %s", default_template_path)

    # 本地检索经验
    experience_content = ""
    if use_experience:
        from utils.local_experience import PromptTemplateSearch as ExperienceSearch

        search_engine = ExperienceSearch(default_template_path)
        experience_content = search_engine.get_experience(task_description, 1)
        logging.debug("检索到的相关经验:\n%s", experience_content)
    else:
        logging.debug("经验检索已禁用")
        
    if device_type == "Android":
        planner_prompt_template = load_prompt("planner_oneshot.md")
    elif device_type == "Harmony":
        planner_prompt_template = load_prompt("planner_oneshot_harmony.md")
    
    # 检索用户偏好
    user_preferences = {}
    if preference_extractor and preference_extractor.mem:
        user_preferences = retrieve_user_preferences(
            task_description,
            preference_extractor.mem,
            use_graphrag=use_graphrag
        )
        if user_preferences:
            print(f"检索到的用户偏好 (使用{'GraphRAG' if use_graphrag else '向量检索'}):\n{user_preferences}")
        else:
            print("未找到相关用户偏好")
    if user_preferences:
        user_profile_content = "用户画像与偏好：\n" + "\n".join(f"- {item}" for item in user_preferences)
    else:
        user_profile_content = "无"

    # 结合上下文
    enhanced_context = combine_context(experience_content, user_preferences)
    # 构建Prompt
    prompt = planner_prompt_template.format(
        task_description=task_description,
        experience_content=experience_content,
        user_profile_content=user_profile_content
    )
    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": prompt}],
        }
    ]
    response_str = _requests_chat_completion(
        "Planner",
        planner_model,
        messages,
        INITIAL_TEMP,
        API_TIMEOUT,
        256,
    )
    if response_str is None:
        response_str = planner_client.chat.completions.create(
            model=planner_model,
            messages=messages,
        ).choices[0].message.content
    logging.info(f"Planner 响应: \n{response_str}")
    response_json = parse_planner_response(response_str)
    if response_json is None:
        logging.error("无法解析模型响应为 JSON。")
        logging.error(f"原始响应内容: {response_str}")
        raise ValueError("无法解析模型响应为 JSON。")
    app_name = response_json.get("app_name")
    package_name = response_json.get("package_name")
    final_desc = response_json.get("final_task_description", task_description)
    return app_name, package_name, final_desc


def resolve_task_description_with_user_confirmation(original_task_description, planner_task_description, auto_accept_planner_changes=False):
    """Resolve which task description should be executed."""
    original_text = (original_task_description or "").strip()
    planner_text = (planner_task_description or "").strip()

    if not planner_text or planner_text == original_text:
        logging.info("Planner task description matches the original task description.")
        return original_task_description

    if auto_accept_planner_changes:
        logging.info("Auto-accepting planner-rewritten task description.")
        return planner_task_description

    if not sys.stdin.isatty():
        logging.warning(
            "Planner rewrote the task description, but stdin is not interactive. "
            "Falling back to the original task description."
        )
        return original_task_description

    print("\nPlanner 根据 profile/experience 调整了任务描述，请选择要执行的版本：")
    print("[1] 原始任务")
    print(textwrap.indent(original_task_description, prefix="    "))
    print("[2] 修改后任务")
    print(textwrap.indent(planner_task_description, prefix="    "))

    while True:
        choice = input("请输入 1 或 2（直接回车默认使用原始任务）: ").strip()
        if choice in {"", "1"}:
            logging.info("Using original task description after terminal confirmation.")
            return original_task_description
        if choice == "2":
            logging.info("Using planner-rewritten task description after terminal confirmation.")
            return planner_task_description
        print("无效输入，请输入 1 或 2。")


def should_use_planner_rewritten_task(use_experience=False):
    """Only enable planner task rewriting when experience or user profile is active."""
    return bool(use_experience or (preference_extractor and getattr(preference_extractor, 'mem', None)))


def execute_single_task(task_description, device, data_dir, use_experience, use_graphrag, current_device_type, use_qwen3_model, use_e2e=False, auto_accept_planner_changes=False, decider_protocol=DECIDER_PROTOCOL_QWEN_JSON, forced_app_name=None, forced_package_name=None):
    """
    执行单个任务的通用函数
    
    Args:
        task_description: 任务描述
        device: 设备对象
        data_dir: 数据保存目录
        use_experience: 是否使用经验改写任务
        use_graphrag: 是否使用GraphRAG
        current_device_type: 设备类型
        use_qwen3_model: 是否使用Qwen3模型
        use_e2e: 是否使用e2e模式（skip grounder调用）
    """
    if forced_app_name and forced_package_name:
        app_name = forced_app_name
        package_name = forced_package_name
        planner_task_description = task_description
        logging.info(
            "Using frozen structured-task app mapping: %s (%s)",
            app_name,
            package_name,
        )
    else:
        logging.info(f"Calling planner to get app_name and package_name")
        app_name, package_name, planner_task_description = get_app_package_name(
            task_description, use_graphrag=use_graphrag, device_type=current_device_type, use_experience=use_experience
        )

    if should_use_planner_rewritten_task(use_experience=use_experience):
        new_task_description = resolve_task_description_with_user_confirmation(
            task_description,
            planner_task_description,
            auto_accept_planner_changes=auto_accept_planner_changes,
        )
    else:
        logging.info("Planner task rewriting is disabled; using original task description.")
        new_task_description = task_description
    logging.info(f"Final task description for execution: {new_task_description}")

    logging.info(f"Starting task in app: {app_name} (package: {package_name})")
    device.app_start(package_name)
    task_in_app(
        app_name,
        task_description,
        new_task_description,
        device,
        data_dir,
        True,
        use_qwen3_model,
        current_device_type,
        use_e2e,
        decider_protocol=decider_protocol,
    )
    time.sleep(APP_STOP_WAIT)  # 等待后再停止应用
    logging.info(f"Stopping app: {app_name} (package: {package_name})")
    device.app_stop(package_name)


# for testing purposes
if __name__ == "__main__":
    # 解析命令行参数
    parser = argparse.ArgumentParser(description="MobiMind Agent")
    parser.add_argument("--service_ip", type=str, default="localhost", help="Ip for the services (default: localhost)")
    parser.add_argument("--decider_port", type=int, default=8000, help="Port for decider service (default: 8000)")
    parser.add_argument("--grounder_port", type=int, default=8001, help="Port for grounder service (default: 8001)")
    parser.add_argument("--planner_port", type=int, default=8002, help="Port for planner service (default: 8002)")
    parser.add_argument("--user_profile", choices=["on", "off"], default="off", help="Enable user profile memory (default: off)")
    parser.add_argument("--use_graphrag", choices=["on", "off"], default="off", help="Use GraphRAG for user profile preference memory (default: off)")
    parser.add_argument("--clear_memory", action="store_true", help="Force clear all stored user memories and exit")
    parser.add_argument("--device", type=str, default="Android", choices=["Android", "Harmony"], help="Device type: Android or Harmony (default: Android)")
    parser.add_argument(
        "--device_serial",
        type=str,
        default=None,
        help="Explicit device serial. Required by packaged Harmony evaluation runs.",
    )
    parser.add_argument("--use_qwen3", choices=["on", "off"], default="on", help="Whether to use Qwen3VL-based model (default: on)")
    parser.add_argument("--use_experience", choices=["on", "off"], default="off", help="Whether to use experience (use planner for task rewriting) (default: off)")
    parser.add_argument(
        "--accept_planner_changes",
        choices=["on", "off"],
        default="off",
        help="Whether to automatically accept planner-rewritten task descriptions without terminal confirmation (default: off)",
    )
    parser.add_argument("--data_dir", type=str, default=None, help="Directory to save data (default: ./data relative to script location)")
    parser.add_argument("--task_file", type=str, default=None, help="Path to task.json file (default: ./task.json relative to script location)")
    parser.add_argument("--e2e", action="store_true", default=True, help="Enable e2e mode: use e2e_qwen3.md as decider prompt and return coordinates directly from decider (default: True)")
    parser.add_argument(
        "--decider_protocol",
        choices=SUPPORTED_DECIDER_PROTOCOLS,
        default=DECIDER_PROTOCOL_QWEN_JSON,
        help="Decider output protocol to use (default: qwen_json)",
    )
    parser.add_argument(
        "--coord_mode",
        choices=sorted(SUPPORTED_COORD_MODES),
        default=os.getenv("MOBIAGENT_COORD_MODE", COORD_MODE_RESIZED_PIXEL),
        help=(
            "Coordinate mode for model bbox/points. Use resized_pixel for "
            "pixel coordinates in the screenshot sent to the model; use "
            "qwen_normalized for 0-1000 normalized coordinates."
        ),
    )
    args = parser.parse_args()
    DECIDER_COORD_MODE = args.coord_mode.strip().lower()

    # 使用命令行参数初始化
    enable_user_profile = (args.user_profile == "on")
    use_graphrag = (args.use_graphrag == "on")
    init(
        args.service_ip,
        args.decider_port,
        args.grounder_port,
        args.planner_port,
        enable_user_profile=enable_user_profile,
        use_graphrag=use_graphrag,
    )

    # 如果需要清除记忆，优先执行并退出
    if args.clear_memory:
        if enable_user_profile and preference_extractor and getattr(preference_extractor, 'mem', None):
            try:
                count = preference_extractor.clear_all_memories()
                print(f"Memory cleared. Deleted {count} item(s).")
            except Exception as e:
                print(f"Failed to clear memory: {e}")
        else:
            print("User profile is disabled or memory client not initialized; nothing to clear.")
        raise SystemExit(0)
    # 根据 --device 参数选择设备类型
    if args.device == "Android":
        device = AndroidDevice()
        logging.info("Using AndroidDevice")
    elif args.device == "Harmony":
        device = HarmonyDevice(serial=args.device_serial)
        logging.info("Using HarmonyDevice")
    else:
        raise ValueError(f"Unknown device type: {args.device}")
    
    logging.info(f"Connected to device: {args.device}")
    use_qwen3_model = (args.use_qwen3 == "on")
    use_experience = (args.use_experience == "on")
    auto_accept_planner_changes = (args.accept_planner_changes == "on")
    current_device_type = args.device  # 保存设备类型用于后续使用
    logging.info(f"Use Qwen3 model: {use_qwen3_model}")
    logging.info(f"Use experience (planner task rewriting): {use_experience}")
    logging.info(f"Auto accept planner changes: {auto_accept_planner_changes}")
    logging.info(f"Device type: {current_device_type}")
    logging.info(f"Use E2E mode: {args.e2e}")
    logging.info(f"Decider protocol: {args.decider_protocol}")
    logging.info(f"Coordinate mode: {DECIDER_COORD_MODE}")
    # 配置数据保存目录
    if args.data_dir:
        data_base_dir = args.data_dir
        logging.info(f"Using custom data directory: {data_base_dir}")
    else:
        data_base_dir = os.path.join(os.path.dirname(__file__), 'data')
        logging.info(f"Using default data directory: {data_base_dir}")
    
    if not os.path.exists(data_base_dir):
        os.makedirs(data_base_dir)
        logging.info(f"Created data directory: {data_base_dir}")

    # 读取任务列表
    if args.task_file:
        task_json_path = args.task_file
        logging.info(f"Using custom task file: {task_json_path}")
    else:
        task_json_path = os.path.join(os.path.dirname(__file__), "task.json")
    with open(task_json_path, "r", encoding="utf-8") as f:
        task_list = json.load(f)
    
    # print(task_list)

    for task_item in task_list:
        # 支持两种格式：
        # 1. 简单字符串格式: ["task1", "task2", ...]
        # 2. 结构化格式: {"app": "app_name", "type": "type_name", "tasks": ["task1", "task2", ...]}
        
        if isinstance(task_item, dict):
            # 新格式：结构化任务
            app_name_from_file = task_item.get("app")
            task_type = task_item.get("type", "default")
            tasks_list = task_item.get("tasks", [])
            
            
            # 遍历该应用和类型下的所有任务
            for task_index, task_description in enumerate(tasks_list, 1):
                # 创建 data_dir: data_base_dir/app/type/task_index
                data_dir = os.path.join(data_base_dir, app_name_from_file, task_type, str(task_index))
                os.makedirs(data_dir, exist_ok=True)
                logging.info(f"Processing task {task_index} of {app_name_from_file}/{task_type}: {task_description}")

                package_name_from_file = device.app_package_names.get(app_name_from_file)
                if not package_name_from_file:
                    raise ValueError(
                        f"Structured task app '{app_name_from_file}' is not registered for {current_device_type}."
                    )

                
                execute_single_task(
                    task_description,
                    device,
                    data_dir,
                    use_experience,
                    use_graphrag,
                    current_device_type,
                    use_qwen3_model,
                    args.e2e,
                    auto_accept_planner_changes,
                    args.decider_protocol,
                    forced_app_name=app_name_from_file,
                    forced_package_name=package_name_from_file,
                )
        else:
            # 旧格式：简单任务列表
            existing_dirs = [d for d in os.listdir(data_base_dir) if os.path.isdir(os.path.join(data_base_dir, d)) and d.isdigit()]
            if existing_dirs:
                data_index = max(int(d) for d in existing_dirs) + 1
            else:
                data_index = 1
            data_dir = os.path.join(data_base_dir, str(data_index))
            os.makedirs(data_dir, exist_ok=True)
            task_description = task_item
            
            execute_single_task(
                task_description,
                device,
                data_dir,
                use_experience,
                use_graphrag,
                current_device_type,
                use_qwen3_model,
                args.e2e,
                auto_accept_planner_changes,
                args.decider_protocol,
            )
    
    # 等待所有偏好提取任务完成
    if preference_extractor and hasattr(preference_extractor, 'executor'):
        logging.info("Waiting for all preference extraction tasks to complete...")
        preference_extractor.executor.shutdown(wait=True)
        logging.info("All preference extraction tasks completed")
