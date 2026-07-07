"""
GLM Coding Plan 抢购自动化核心。

流程:
  1. 从 cookie.json 读取凭证 (document.cookie + localStorage)
  2. 用系统 Edge 打开浏览器,注入 cookies
  3. 访问 /glm-coding?plantype=X
  4. 模拟人手点击"开通"
  5. 到达支付页 → 触发 on_payment_alert 回调 → 等待用户手动支付

所有拟人化处理 (随机延迟 / 缓慢滚动) 都在这里做,
上层 app.py 只负责调度 + UI 展示。
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Callable, Optional

from DrissionPage import ChromiumOptions, ChromiumPage
from DrissionPage.errors import ElementNotFoundError


# ---------- 常量 ----------

PLAN_BASE_URL = "https://www.bigmodel.cn/glm-coding"
SUPPORTED_PLANTYPES = ("personal", "team")  # 其他类型启动时再从页面动态抓

# 拟人化: 每次操作的延迟范围 (秒)
JITTER_PAGE_LOAD = (1.0, 3.0)   # 页面加载后停留
JITTER_BEFORE_CLICK = (0.05, 0.20)
JITTER_AFTER_CLICK = (0.3, 0.8)

# 拟人化: 视口尺寸候选
VIEWPORTS = [(1920, 1080), (1536, 864), (1440, 900), (1680, 1050)]

# 支付页 URL 关键词 (任一命中即视为到达支付页)
# 注意:智谱用 /team-coding-detail?tier=XXX 这种路径
PAYMENT_URL_HINTS = ("pay", "order", "checkout", "payment",
                     "alipay", "wechat", "cashier", "coding-detail")

# 支付页文本关键词 — 订单页有 "订单支付" 面包屑
PAYMENT_TEXT_HINTS = ("扫码支付", "确认支付", "支付方式", "选择支付",
                      "立即支付", "去支付", "订单支付")

# 库存售罄的按钮文本 — 看到这些就跳过
# 实测:智谱显示 "暂时售罄 ｜07月07日 10:00 补货" (全角竖线)
SOLD_OUT_TEXT = ("暂时售罄", "抢购人数过多", "已售罄", "暂时缺货", "暂无库存")

# 每个 plantype 对应的 tier 名称 (页面显示)
TIER_NAMES = {
    "personal": ("Lite", "Pro", "Max"),
    "team": ("标准版", "高级版"),
}

# 计费周期 (连续包月/季/年) — 页面 tab 用
# 内部 key → 页面显示文本 (前段用于匹配 .switch-tab-item)
BILLING_TERMS: tuple[tuple[str, str], ...] = (
    ("monthly",   "连续包月"),
    ("quarterly", "连续包季"),
    ("yearly",    "连续包年"),
)
BILLING_LABELS = dict(BILLING_TERMS)
DEFAULT_TERM = "monthly"

# 候选购买按钮文本 (按出现优先级排序)
# 实测 2026-07-06:页面按钮已从 "立即订阅" 改为 "特惠订阅"
BUY_BUTTON_TEXTS = ("特惠订阅", "立即订阅", "立即购买", "立即开通", "购买", "开通", "订阅")


# ---------- 状态机 ----------

class BuyState(Enum):
    INIT = auto()
    COOKIES_LOADED = auto()
    PLAN_PAGE_OPENED = auto()
    LOGGED_OUT = auto()
    PURCHASE_BUTTON_FOUND = auto()
    CLICKED = auto()
    PAYMENT_PAGE_REACHED = auto()      # 订单页已就绪 (term 已选)
    CAPTCHA_REQUIRED = auto()          # 出现拖动验证码,需用户手动解
    PAYMENT_QR_READY = auto()          # 扫码页已打开 (Alipay/WeChat QR)
    SUCCESS = auto()
    FAILED = auto()


@dataclass
class BuyResult:
    state: BuyState
    message: str
    payment_url: Optional[str] = None


# ---------- Cookie 加载 ----------

@dataclass
class Credential:
    """从浏览器导出的 cookie + localStorage 包。"""
    domain: str
    cookies: list[dict]
    local_storage: dict[str, str]

    @classmethod
    def from_json_file(cls, path: str | Path) -> "Credential":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(
            domain=data.get("domain", "bigmodel.cn"),
            cookies=data.get("cookies", []),
            local_storage=data.get("localStorage", {}),
        )


# ---------- 拟人化辅助 ----------

def _sleep_jitter(rng: tuple[float, float]) -> None:
    time.sleep(random.uniform(*rng))


def _scroll_humanly(page: ChromiumPage) -> None:
    """模拟人滚动浏览页面 (300-900px 区间, 多次小幅滚动)。"""
    h = page.run_js("return document.body.scrollHeight") or 1000
    target = random.randint(300, min(900, max(300, h // 4)))
    steps = random.randint(3, 6)
    for i in range(steps):
        y = int(target * (i + 1) / steps)
        page.run_js(f"window.scrollTo({{top: {y}, behavior: 'smooth'}})")
        time.sleep(random.uniform(0.05, 0.15))
    page.run_js("window.scrollTo({top: 0, behavior: 'smooth'})")
    time.sleep(random.uniform(0.2, 0.5))


# ---------- Term 选择 ----------

def _select_billing_term(page: ChromiumPage, term: str) -> None:
    """
    点击计费周期 tab (连续包月/季/年)。
    页面是 Vue 内部状态,不反映到 URL,所以必须点击 DOM 元素。
    注意:页面里 "连续包季" 这个文本会出现在 tab 标题 AND 卡片描述里
    (例如 "连续包季 9 折"),所以必须用 .switch-tab-item 容器限定。
    """
    label = BILLING_LABELS.get(term, term)
    # 找包含 label 文本的 .switch-tab-item 容器
    tab = page.ele(
        f"xpath://div[contains(@class,'switch-tab-item') and "
        f".//span[normalize-space(text())='{label}']]",
        timeout=2,
    )
    if not tab:
        # 不抛错,默认月付(页面初始就是月付)也能跑
        print(f"[term] 找不到 tab: {label} — 用页面默认", flush=True)
        return
    tab.click()
    # 切 tab 后页面会重新渲染价格和按钮
    time.sleep(0.5)


# 订单页 term 选项标签 (在 /coding-detail 或 /team-coding-detail)
ORDER_TERM_LABELS = {
    "monthly":   "连续包月",
    "quarterly": "连续包季",
    "yearly":    "连续包年",
}


def _select_term_on_order_page(page: ChromiumPage, term: str) -> bool:
    """
    在订单页 (/coding-detail, /team-coding-detail) 点选 term。
    返回 True 表示成功点击,False 表示没找到对应选项。

    订单页的 term 选项是 3 个 listitem (class 'select-item'),文本:
      - 连续包月 ￥X/月
      - 连续包季 9折 ￥X/月   (personal 才有)
      - 连续包年 9折或8折 ￥X/月
    team 页面没有"连续包季",只有月/年/单次采购 — quarterly 会返回 False。
    """
    label = ORDER_TERM_LABELS.get(term)
    if not label:
        return False

    # 优先: class=select-item 的 li (订单页专属)
    candidates = page.eles("css:li.select-item", timeout=2) or []
    for li in candidates:
        text = li.text or ""
        if label in text and "￥" in text:
            # 已经是 active 就别点 (避免无意义重渲染)
            if "is-active" in (li.attr("class") or ""):
                return True
            li.click()
            time.sleep(0.6)
            return True

    # 兜底: 任何 li 含 label + ￥ (万一 class 改了)
    for li in page.eles("css:li", timeout=1) or []:
        text = li.text or ""
        if label in text and "￥" in text and "订阅价" not in text:
            li.click()
            time.sleep(0.6)
            return True

    return False


# 订单页 "立即购买" 按钮文本
BUY_NOW_TEXT = "立即购买"

# 验证码 / 扫码页 DOM 特征
CAPTCHA_HINTS = ("captcha", "puzzle", "verify", "安全验证", "拖动")
# 扫码页 URL 关键词 (Alipay/WeChat/聚合收银台)
QR_URL_HINTS = ("alipay", "alipay.com", "weixin", "wx.tenpay", "cashier",
                "qrcode", "pay-channel", "pay.zhifu")
QR_TEXT_HINTS = ("扫码支付", "请扫码", "打开支付宝", "打开微信")


def _click_buy_now_on_order_page(
    page: ChromiumPage, on_state: Optional[Callable[[BuyState, str], None]] = None,
) -> "tuple[BuyState, str]":
    """
    订单页上点 "立即购买",然后轮询直到出现 验证码 OR 扫码页 OR 超时。
    返回 (state, message) — 让调用者据此决定后续动作。
    """
    def emit(state: BuyState, msg: str = "") -> None:
        if on_state:
            on_state(state, msg)

    # 找 立即购买 按钮 (订单页底部那个)
    btn = page.ele(f"text={BUY_NOW_TEXT}", timeout=3)
    if not btn:
        return BuyState.FAILED, f"订单页找不到 '{BUY_NOW_TEXT}' 按钮"

    btn.click()
    emit(BuyState.CLICKED, f"已点 '{BUY_NOW_TEXT}',等待跳转…")

    # 轮询 8s,看是验证码还是扫码页
    deadline = time.time() + 8
    captcha_seen = False
    while time.time() < deadline:
        time.sleep(0.3)
        cur_url = page.url.lower()

        # 1) 验证码: 安全验证/拖动文字 或 captcha 元素
        if any(page.ele(f"text={h}", timeout=0.2) for h in CAPTCHA_HINTS):
            captcha_seen = True
            break
        if page.ele("css:.captcha-component, [class*=tencent-captcha], "
                    "[class*=puzzle]", timeout=0.2):
            captcha_seen = True
            break

        # 2) 扫码页: URL 含支付网关 OR 页面有扫码字样
        if any(h in cur_url for h in QR_URL_HINTS):
            return BuyState.PAYMENT_QR_READY, f"扫码页: {page.url}"
        if any(page.ele(f"text={h}", timeout=0.2) for h in QR_TEXT_HINTS):
            return BuyState.PAYMENT_QR_READY, f"扫码页: {page.url}"

    if captcha_seen:
        return (BuyState.CAPTCHA_REQUIRED,
                "出现验证码,需手动拖动完成;完成后回 GUI 即可")

    return BuyState.FAILED, f"点 '{BUY_NOW_TEXT}' 后 8s 内未跳到验证码或扫码页 (URL: {page.url})"


# ---------- 按钮查找 ----------

def _is_sold_out(text: str) -> bool:
    return any(s in text for s in SOLD_OUT_TEXT)


def _find_buy_button_raw(page: ChromiumPage, tier: Optional[str] = None,
                         include_disabled: bool = False):
    """
    找 .buy-btn 元素,可选是否包含 disabled。
      - tier 给定: 找该 tier 对应的 .buy-btn
      - tier 为 None: 找第一个 .buy-btn
      - include_disabled=False (默认): 跳过 disabled 和售罄
      - include_disabled=True: 返回所有 (用于轮询)
    """
    if tier:
        name_ele = page.ele(f"text={tier}", timeout=2)
        if not name_ele:
            return None
        el = name_ele
        for _ in range(8):
            try:
                el = el.parent()
                if not el or not hasattr(el, "ele"):
                    break
            except Exception:
                break
            try:
                sel = "css:.buy-btn" if include_disabled else "css:.buy-btn:not([disabled])"
                btn = el.ele(sel, timeout=0.3)
                if btn:
                    if not include_disabled:
                        if _is_sold_out(btn.text):
                            return None
                        return btn
                    if not _is_sold_out(btn.text):
                        return btn
            except Exception:
                continue
        return None

    btns = page.eles("css:.buy-btn", timeout=2) or []
    for btn in btns:
        if not include_disabled:
            try:
                disabled = bool(btn.run_js("el => el.disabled"))
            except Exception:
                disabled = False
            if disabled or _is_sold_out(btn.text):
                continue
            return btn
        else:
            if _is_sold_out(btn.text):
                continue
            return btn
    return None


def _wait_for_enabled_button(page: ChromiumPage, tier: Optional[str],
                             timeout_sec: float):
    """
    轮询等按钮变 enabled (从 "暂时售罄" → "立即订阅")。
    每 50ms 查一次,返回第一个 enabled 的按钮,或超时返回 None。
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        btn = _find_buy_button_raw(page, tier, include_disabled=True)
        if btn:
            try:
                disabled = bool(btn.run_js("el => el.disabled"))
            except Exception:
                disabled = False
            if not disabled:
                return btn
        time.sleep(0.05)
    return None


def _find_buy_button(page: ChromiumPage, tier: Optional[str] = None):
    """兼容旧调用 — 等同于 _find_buy_button_raw(..., include_disabled=False)。"""
    return _find_buy_button_raw(page, tier, include_disabled=False)


# ---------- 核心: 抢一次 ----------

def buy_once(
    cookie_path: str | Path,
    plantype: str,
    tier: Optional[str] = None,
    *,
    term: str = DEFAULT_TERM,
    on_state: Optional[Callable[[BuyState, str], None]] = None,
    on_payment_alert: Optional[Callable[[str], None]] = None,
    retry: int = 1,
    wait_for_enable_sec: float = 0.0,
) -> BuyResult:
    """
    抢一次。返回 BuyResult。

    参数:
      cookie_path: 导出的 cookie JSON 文件
      plantype: 例如 "personal" / "team"
      tier: 例如 "Pro" / "标准版" / None (None = 第一个可用的)
      term: 计费周期, "monthly" / "quarterly" / "yearly"
      on_state: 状态变化回调 (state, message) — 用于更新 GUI
      on_payment_alert: 到达支付页时回调 (payment_url) — 用于提醒用户
      retry: 失败后最大重试次数
      wait_for_enable_sec: 等按钮变 enabled 的秒数 (用于定时抢购,
        提前唤醒后等库存上线);0 = 立即失败 (手动模式用)
    """

    def emit(state: BuyState, msg: str = "") -> None:
        if on_state:
            on_state(state, msg)

    # ---- 加载 cookie ----
    try:
        cred = Credential.from_json_file(cookie_path)
    except Exception as e:
        return BuyResult(BuyState.FAILED, f"cookie 文件读取失败: {e}")

    emit(BuyState.COOKIES_LOADED, f"已加载 {len(cred.cookies)} 个 cookie, "
                                   f"{len(cred.local_storage)} 个 localStorage")

    # ---- 启动浏览器 (用系统 Edge) ----
    co = ChromiumOptions()
    co.set_browser_path(
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    )
    vw, vh = random.choice(VIEWPORTS)
    co.set_argument(f"--window-size={vw},{vh}")
    # 隐藏 webdriver 标记 (DrissionPage 默认应该处理, 兜底再加一个)
    co.set_argument("--disable-blink-features=AutomationControlled")

    last_err = None
    for attempt in range(1, retry + 2):  # 总共 retry+1 次
        page = ChromiumPage(co)
        try:
            # ---- 打开首页 + 注入 cookie + localStorage ----
            page.get("https://www.bigmodel.cn/")

            # DrissionPage 的 CookiesSetter.__call__ 只接 cookies 列表,
            # 每个 cookie 需要自带 name/value/domain/path,缺 domain 时补 .bigmodel.cn
            norm_cookies = []
            for c in cred.cookies:
                cc = dict(c)
                cc.setdefault("domain", "." + cred.domain)
                cc.setdefault("path", "/")
                norm_cookies.append(cc)
            page.set.cookies(norm_cookies)

            # localStorage 需要先访问同源页面才能 set
            page.run_js(
                f"Object.entries({json.dumps(cred.local_storage)}).forEach("
                f"([k,v]) => localStorage.setItem(k, v))"
            )

            # ---- 打开 plan 页 ----
            url = f"{PLAN_BASE_URL}?plantype={plantype}"
            page.get(url)
            _sleep_jitter(JITTER_PAGE_LOAD)
            _scroll_humanly(page)

            current_url = page.url
            emit(BuyState.PLAN_PAGE_OPENED, f"已打开 {url}")

            # ---- 检查是否登录 (未登录会被重定向到 /login 或出现登录弹窗) ----
            if "/login" in current_url or page.ele(
                "text=登录", timeout=0.5
            ):
                emit(BuyState.LOGGED_OUT, "cookie 已失效,需要重新导出")
                return BuyResult(BuyState.LOGGED_OUT, "cookie 失效")

            # ---- 选计费周期 (月/季/年) ----
            _select_billing_term(page, term)
            emit(BuyState.PLAN_PAGE_OPENED, f"已选计费: {BILLING_LABELS.get(term, term)}")

            # ---- 找"开通/购买"按钮 ----
            # 实测:智谱的按钮 class 是 `buy-btn el-button--primary`
            btn = _find_buy_button(page, tier)

            # 找不到 → 如果配置了 wait_for_enable_sec,进入轮询等待
            if not btn and wait_for_enable_sec > 0:
                emit(BuyState.PLAN_PAGE_OPENED,
                     f"按钮暂未开放,等待最多 {wait_for_enable_sec:.0f}s…")
                btn = _wait_for_enabled_button(page, tier, wait_for_enable_sec)
                if btn:
                    log_ctx = f"[{tier}] 按钮已开放" if tier else "按钮已开放"
                    log.info("%s", log_ctx)  # 通过 log 记录,emit 由上层 GUI 决定

            if not btn:
                target = tier or "任何"
                hint = "等待超时,未上线" if wait_for_enable_sec > 0 else "可能已售罄"
                return BuyResult(
                    BuyState.FAILED,
                    f"找不到 [{target}] 的可点击购买按钮 — {hint}",
                )

            emit(BuyState.PURCHASE_BUTTON_FOUND,
                 f"找到按钮: {btn.text.strip()[:30]}")
            _sleep_jitter(JITTER_BEFORE_CLICK)

            # ---- 点击 ----
            btn.click()
            _sleep_jitter(JITTER_AFTER_CLICK)
            emit(BuyState.CLICKED, "已点击,等待跳转…")

            # ---- 等跳转 / 检查是否到支付页 ----
            deadline = time.time() + 10
            while time.time() < deadline:
                time.sleep(0.3)
                cur = page.url.lower()
                # URL 含支付关键词 OR 页面文本含支付关键词
                if any(h in cur for h in PAYMENT_URL_HINTS):
                    break
                if page.ele(
                    f"text={PAYMENT_TEXT_HINTS[0]}", timeout=0.3
                ) or any(
                    page.ele(f"text={h}", timeout=0.2)
                    for h in PAYMENT_TEXT_HINTS[1:]
                ):
                    break
            else:
                raise TimeoutError("点击后 10s 内未跳转到支付页")

            # ---- 在订单页选 term (team 没有 pricing 页 term,只在这里选) ----
            ok = _select_term_on_order_page(page, term)
            if ok:
                emit(BuyState.PAYMENT_PAGE_REACHED,
                     f"订单页已选 {ORDER_TERM_LABELS.get(term, term)}")
            else:
                if term != "monthly":
                    emit(BuyState.PAYMENT_PAGE_REACHED,
                         f"⚠ 订单页没找到 {ORDER_TERM_LABELS.get(term, term)} 选项,"
                         f" 当前默认月付;请手动切换")

            # ---- 点 "立即购买" → 验证码 / 扫码页 分流 ----
            qr_state, qr_msg = _click_buy_now_on_order_page(page, on_state)

            # 验证码 → 停在这,等用户拖完,然后点一下确认才会继续
            if qr_state == BuyState.CAPTCHA_REQUIRED:
                page.set.window.max()
                if on_payment_alert:
                    # 复用回调,URL 传订单页 (用户解完要回到这里)
                    on_payment_alert(page.url)
                return BuyResult(qr_state, qr_msg, payment_url=page.url)

            # 扫码页 → 弹窗 + 声音 + 系统通知
            if qr_state == BuyState.PAYMENT_QR_READY:
                page.set.window.max()
                emit(qr_state, qr_msg)
                if on_payment_alert:
                    on_payment_alert(page.url)
                return BuyResult(qr_state, qr_msg, payment_url=page.url)

            # 其他 (FAILED) — 也通知用户,订单页留着
            emit(qr_state, qr_msg)
            if on_payment_alert:
                on_payment_alert(page.url)
            return BuyResult(qr_state, qr_msg, payment_url=page.url)

        except Exception as e:
            last_err = e
            if attempt <= retry:
                emit(BuyState.FAILED,
                     f"第 {attempt} 次失败: {e} — {3}s 后重试")
                time.sleep(3)
            else:
                emit(BuyState.FAILED, f"已重试 {retry} 次,放弃: {e}")
        finally:
            # 注意: 不到支付页的情况下关闭浏览器; 到支付页时留给用户看
            try:
                if page and not on_payment_alert:
                    page.quit()
            except Exception:
                pass

    return BuyResult(BuyState.FAILED, str(last_err) if last_err else "未知失败")


# ---------- 工具: 列出页面所有 plantype ----------

def list_plantypes(
    cookie_path: str | Path,
    *,
    on_state: Optional[Callable[[BuyState, str], None]] = None,
) -> list[str]:
    """访问 /glm-coding 并从页面解析所有 plantype 选项。"""
    try:
        cred = Credential.from_json_file(cookie_path)
    except Exception as e:
        if on_state:
            on_state(BuyState.FAILED, f"cookie 读取失败: {e}")
        return list(SUPPORTED_PLANTYPES)

    co = ChromiumOptions()
    co.set_browser_path(
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    )
    page = ChromiumPage(co)
    try:
        page.get("https://www.bigmodel.cn/")
        norm_cookies = []
        for c in cred.cookies:
            cc = dict(c)
            cc.setdefault("domain", "." + cred.domain)
            cc.setdefault("path", "/")
            norm_cookies.append(cc)
        page.set.cookies(norm_cookies)
        page.run_js(
            f"Object.entries({json.dumps(cred.local_storage)}).forEach("
            f"([k,v]) => localStorage.setItem(k, v))"
        )
        page.get(PLAN_BASE_URL)
        _sleep_jitter(JITTER_PAGE_LOAD)
        # 抓所有 plantype 参数
        urls = page.run_js("""
            () => Array.from(document.querySelectorAll('a[href*="plantype="]'))
                .map(a => {
                    const m = a.href.match(/plantype=([^&]+)/);
                    return m ? decodeURIComponent(m[1]) : null;
                }).filter(Boolean)
        """) or []
        return sorted(set(urls)) if urls else list(SUPPORTED_PLANTYPES)
    finally:
        try:
            page.quit()
        except Exception:
            pass