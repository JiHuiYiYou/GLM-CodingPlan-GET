"""
GLM Coding Plan 自动抢购 — GUI 入口。

布局:
  ┌─ Cookie 配置 ─────────────────────────────────┐
  │ [路径: _______________________________] [选择] [从剪贴板导入] │
  │ 状态: ✓ 已加载 (5 cookies, 12 localStorage)     │
  └────────────────────────────────────────────────┘
  ┌─ Plan 类型 ───────────────────────────────────┐
  │ ☑ personal  ☑ team  ☐ (更多自动抓)  [🔄 刷新] │
  └────────────────────────────────────────────────┘
  ┌─ 定时 ────────────────────────────────────────┐
  │ 每天 [HH]:[MM]:[SS] 触发  [☑ 启用] [立即抢购] │
  └────────────────────────────────────────────────┘
  ┌─ 状态 ────────────────────────────────────────┐
  │ 下次执行: 2026-07-07 10:00:00 (剩余 23h 59m)  │
  │ 最近结果: ✅ 2026-07-06 10:00:01 抢到 personal │
  │ ── 日志 ────────────────────────────────       │
  │ 10:00:00.123 启动浏览器                       │
  │ 10:00:01.456 打开 plan 页                     │
  │ 10:00:02.789 已到支付页,等待用户…             │
  └────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import Optional

import ttkbootstrap as ttk # pyright: ignore[reportMissingImports]
from ttkbootstrap.constants import ( # pyright: ignore[reportMissingImports]
    BOTH, BOTTOM, CHECKBUTTON, DANGER, DISABLED, END, FALSE, HORIZONTAL,
    INFO, LEFT, NORMAL, PRIMARY, RIGHT, ROUND, SECONDARY, SUCCESS, TOP, TRUE,
    VERTICAL, WARNING, X, Y,
)

import buyer
from buyer import BuyResult, BuyState, Credential, list_plantypes, TIER_NAMES


# ---------- 路径 ----------

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.json"
LOG_PATH = PROJECT_DIR / "buyer.log"

# 每个套餐的价格 (来自 bigmodel.cn 页面 2026-07-06 实测)
# 月 = 原价(无折扣), 季 = 9折, 年 = 8折
# 格式: plantype → tier → (monthly, quarterly, yearly)
TIER_PRICES = {
    "personal": {
        "Lite":   ("￥49/月",    "￥44.1/月",  "￥39.2/月"),
        "Pro":    ("￥149/月",   "￥134.1/月", "￥119.2/月"),
        "Max":    ("￥469/月",   "￥422.1/月", "￥375.2/月"),
    },
    "team": {
        "标准版": ("￥598/席/月",  "￥538.2/席/月", "￥478.4/席/月"),
        "高级版": ("￥1198/席/月", "￥1078.2/席/月", "￥958.4/席/月"),
    },
}
# 索引: 0=月, 1=季, 2=年
TERM_IDX = {"monthly": 0, "quarterly": 1, "yearly": 2}

DEFAULT_CONFIG = {
    "cookie_path": "",
    "active_plantype": "personal",  # 当前选中的 plantype (单选)
    "tiers_personal": ["Pro"],      # personal 下的 tier (单选)
    "tiers_team": ["标准版"],        # team 下的 tier (单选)
    "billing_term": "monthly",      # monthly / quarterly / yearly (单选)
    # 库存固定 10:00 上线:脚本会在 9:59 提前唤醒,等按钮变 enabled 后立刻点
    "schedule_enabled": True,
}


# ---------- 日志 ----------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("glm-buyer")


# ---------- 配置加载 / 保存 ----------

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            # 迁移: 旧 config 是多选,单选模型下每个 plantype 只能保留 1 个 tier
            for plantype in TIER_NAMES:
                key = f"tiers_{plantype}"
                tiers = cfg.get(key) or []
                if len(tiers) > 1:
                    log.warning("config.json 迁移: %s 原本 %d 个 tier,只保留第一个 %s",
                                key, len(tiers), tiers[0])
                    cfg[key] = tiers[:1]
            # 迁移: 旧字段 plantypes → active_plantype (取第一个)
            legacy = cfg.get("plantypes")
            if legacy and isinstance(legacy, list) and legacy:
                cfg.setdefault("active_plantype", legacy[0])
            return cfg
        except Exception as e:
            log.warning("config.json 解析失败,使用默认: %s", e)
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------- 主窗口 ----------

class App:
    POLL_INTERVAL_MS = 1000  # 状态刷新频率

    def __init__(self) -> None:
        self.cfg = load_config()
        self.root = ttk.Window(themename="darkly")
        self.root.title("GLM Coding Plan 自动抢购")
        self.root.geometry("1000x900")
        self.root.minsize(900, 820)

        # 状态
        self.scheduler_thread: Optional[threading.Thread] = None
        self.stop_flag = threading.Event()
        self.last_result: Optional[BuyResult] = None

        self._build_ui()
        self._bind_close()

        # 启动调度 (在刷新循环之前,避免首屏误判"未启用")
        if self.cfg["schedule_enabled"]:
            self._start_scheduler()

        self._refresh_status_loop()

    # ---- UI 构建 ----

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill=BOTH, expand=True)

        # 顶部 tab 导航
        self.notebook = ttk.Notebook(outer, bootstyle=PRIMARY)
        self.notebook.pack(fill=BOTH, expand=True)

        # Tab 1: 抢购 — 中间内容可滚动,状态栏钉死窗口底部永远可见
        self.tab_snipe = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_snipe, text="  抢购  ")

        # 状态栏先 pack 到 BOTTOM,占据底部固定位置
        self._build_status_section(self.tab_snipe)
        self.status_frame.pack(side=BOTTOM, fill=X, pady=(10, 0))

        # 中间用 Canvas + Scrollbar 让 cookie + plan 可滚动
        self.snipe_canvas = tk.Canvas(self.tab_snipe, highlightthickness=0)
        self.snipe_scroll = ttk.Scrollbar(
            self.tab_snipe, orient=VERTICAL, command=self.snipe_canvas.yview,
            bootstyle=(ROUND, SECONDARY),
        )
        self.snipe_scroll.pack(side=RIGHT, fill=Y)
        self.snipe_canvas.pack(side=LEFT, fill=BOTH, expand=True)
        self.snipe_canvas.configure(yscrollcommand=self.snipe_scroll.set)

        self.snipe_inner = ttk.Frame(self.snipe_canvas)
        self._snipe_window = self.snipe_canvas.create_window(
            (0, 0), window=self.snipe_inner, anchor="nw",
        )

        def _on_canvas_resize(e):
            self.snipe_canvas.itemconfig(self._snipe_window, width=e.width)
        self.snipe_canvas.bind("<Configure>", _on_canvas_resize)

        def _on_inner_resize(e):
            self.snipe_canvas.configure(
                scrollregion=self.snipe_canvas.bbox("all")
            )
        self.snipe_inner.bind("<Configure>", _on_inner_resize)

        # cookie + plan 放进 inner (可滚动)
        self._build_cookie_section(self.snipe_inner)
        self.cookie_frame.pack(side=TOP, fill=X, padx=6, pady=(6, 0))

        ttk.Separator(self.snipe_inner, orient=HORIZONTAL).pack(
            side=TOP, fill=X, padx=6, pady=12,
        )

        self._build_plan_section(self.snipe_inner)
        self.plan_frame.pack(side=TOP, fill=X, padx=6, pady=(0, 6))

        # 鼠标滚轮支持 (只在这个 tab 激活时生效,避免影响日志 tab)
        def _on_mousewheel(e):
            if str(self.notebook.select()) == str(self.tab_snipe):
                self.snipe_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        self.snipe_canvas.bind("<Enter>",
            lambda _: self.snipe_canvas.bind_all("<MouseWheel>", _on_mousewheel))
        self.snipe_canvas.bind("<Leave>",
            lambda _: self.snipe_canvas.unbind_all("<MouseWheel>"))

        # Tab 2: 日志
        self.tab_log = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(self.tab_log, text="  日志  ")
        self._build_log_section(self.tab_log)

    def _build_cookie_section(self, parent: ttk.Frame) -> None:
        self.cookie_frame = ttk.Labelframe(parent, text="Cookie 配置", padding=16)
        frm = self.cookie_frame
        frm.pack(fill=X)

        row = ttk.Frame(frm)
        row.pack(fill=X)

        ttk.Label(row, text="路径:", font=("Segoe UI", 11)).pack(
            side=LEFT, padx=(0, 8)
        )
        self.cookie_path_var = ttk.StringVar(value=self.cfg["cookie_path"])
        ttk.Entry(row, textvariable=self.cookie_path_var).pack(
            side=LEFT, fill=X, expand=True, padx=(0, 12)
        )
        ttk.Button(row, text="选择…", command=self._pick_cookie_file,
                   bootstyle=SECONDARY).pack(side=LEFT, padx=4)
        ttk.Button(row, text="📋 从剪贴板导入", command=self._import_from_clipboard,
                   bootstyle=INFO).pack(side=LEFT, padx=4)
        ttk.Button(row, text="✓ 验证", command=self._verify_cookie,
                   bootstyle=SUCCESS).pack(side=LEFT, padx=4)

        self.cookie_status_var = ttk.StringVar(value="未加载")
        ttk.Label(frm, textvariable=self.cookie_status_var,
                  font=("Segoe UI", 10),
                  bootstyle=SECONDARY).pack(anchor="w", pady=(10, 0))

    def _build_plan_section(self, parent: ttk.Frame) -> None:
        """套餐选择 — 多级导航: ① 类型 → ② 套餐 → ③ 周期。

        单选模型: 一次只能选 1 个 plantype × 1 个 tier × 1 个 term。
        切换 plantype 时只显示对应 tier 卡片,避免一屏塞满。
        """
        self.plan_frame = ttk.Labelframe(parent, text="选择套餐", padding=14)
        frm = self.plan_frame
        frm.pack(fill=X)

        self.tier_vars: dict[str, dict[str, ttk.BooleanVar]] = {}
        self.tier_buttons: dict[tuple[str, str], ttk.Button] = {}
        self.tier_card_frames: dict[tuple[str, str], ttk.Frame] = {}
        # (label_widget, base_name) — 用于单选高亮时切换前缀/颜色
        self.tier_name_labels: dict[tuple[str, str], tuple[ttk.Label, str]] = {}
        self.tier_groups: dict[str, ttk.Frame] = {}   # 每个 plantype 一个容器 (用于切换显示)
        self.price_labels: dict[tuple[str, str], ttk.Label] = {}
        self.term_price_labels: dict[str, ttk.Label] = {}

        # ---- 第 1 级: 计划类型 (单选) ----
        level1 = ttk.Frame(frm)
        level1.pack(fill=X, pady=(0, 4))
        ttk.Label(level1, text="① 类型",
                  font=("Segoe UI", 12, "bold"), bootstyle=PRIMARY
                  ).pack(side=LEFT, padx=(0, 14))
        self.plantype_var = ttk.StringVar(
            value=self.cfg.get("active_plantype", "personal")
        )
        for key, label in (("personal", "👤 个人套餐"), ("team", "👥 团队套餐")):
            ttk.Radiobutton(
                level1, text=label, variable=self.plantype_var, value=key,
                command=self._on_plantype_change, bootstyle=PRIMARY,
            ).pack(side=LEFT, padx=10)

        ttk.Separator(frm, orient=HORIZONTAL).pack(fill=X, pady=8)

        # ---- 第 2 级: tier 卡片 (单选,只显示当前 plantype) ----
        self.tier_section = ttk.Frame(frm)
        self.tier_section.pack(fill=X)
        # 两个 plantype 都建好,运行时根据单选切换可见性
        for plantype in TIER_NAMES:
            group = ttk.Frame(self.tier_section)
            self._build_tier_group(group, plantype)
            self.tier_groups[plantype] = group

        ttk.Separator(frm, orient=HORIZONTAL).pack(fill=X, pady=8)

        # ---- 第 3 级: 计费周期 ----
        self._build_term_row(frm)

        # 初始化 plantype 显示 + 立即抢购按钮状态
        self._on_plantype_change()

    def _build_term_row(self, parent: ttk.Frame) -> None:
        """计费周期选择:连续包月(推荐/灵活) / 连续包季(9折) / 连续包年(8折)。
        月付和季/年付是不同价值的两个选择 — 月付可随时退订最灵活,
        季/年付享折扣但要锁定周期。"""
        ttk.Label(parent, text="计费周期",
                  font=("Segoe UI", 12, "bold"),
                  bootstyle=PRIMARY).pack(anchor="w", pady=(0, 8))

        row = ttk.Frame(parent)
        row.pack(fill=X)

        self.term_var = ttk.StringVar(
            value=self.cfg.get("billing_term", "monthly")
        )
        self.term_buttons: dict[str, ttk.Button] = {}

        # (key, 显示文字, 标签) — 月付和季/年付是平等的两个选项
        terms = (
            ("monthly",   "连续包月", "推荐·灵活"),
            ("quarterly", "连续包季", "9折"),
            ("yearly",    "连续包年", "8折"),
        )
        for key, label, badge in terms:
            card = ttk.Frame(row, padding=(20, 12), relief="solid", borderwidth=1)
            card.pack(side=LEFT, padx=(0, 12), fill=BOTH, expand=True)

            ttk.Label(card, text=label,
                      font=("Segoe UI", 14, "bold")).pack(anchor="w")
            ttk.Label(card, text=badge,
                      font=("Segoe UI", 10),
                      bootstyle=(SUCCESS if key == "monthly" else INFO)
                      ).pack(anchor="w", pady=(4, 10))

            # 价格(随 term 切换;占位,真值由 _refresh_all_prices 注入)
            self.term_price_labels[key] = ttk.Label(
                card, text="",
                font=("Segoe UI", 12), bootstyle=SECONDARY,
            )
            self.term_price_labels[key].pack(anchor="w", pady=(0, 10))

            btn = ttk.Button(
                card, text="选择此周期",
                command=lambda k=key: self._select_term(k),
            )
            btn.pack(fill=X)
            self.term_buttons[key] = btn

        ttk.Label(parent,
                  text="💡 月付可随时退订最灵活;季/年付享折扣但要锁定周期",
                  font=("Segoe UI", 10), bootstyle=SECONDARY
                  ).pack(anchor="w", pady=(6, 0))

        self._refresh_term_buttons()
        self._refresh_all_prices()
        self.term_var.trace_add(
            "write", lambda *_: (
                self._refresh_term_buttons(),
                self._refresh_all_prices(),
            )
        )

    def _select_term(self, key: str) -> None:
        self.term_var.set(key)

    def _refresh_term_buttons(self) -> None:
        cur = self.term_var.get()
        for k, btn in self.term_buttons.items():
            if k == cur:
                btn.configure(text="✓ 当前选择", bootstyle=SUCCESS)
            else:
                btn.configure(text="选择此周期", bootstyle=SECONDARY)

    def _refresh_all_prices(self) -> None:
        """计费周期变了 → 更新所有 tier 卡片的价格显示,以及 term-row 的起价。"""
        term_idx = TERM_IDX.get(self.term_var.get(), 0)
        # tier 卡片
        for (plantype, tier), label in self.price_labels.items():
            prices = TIER_PRICES.get(plantype, {}).get(tier)
            if prices:
                label.configure(text=prices[term_idx])
        # term-row 起价 (用 personal/Lite 作为锚)
        anchor = TIER_PRICES.get("personal", {}).get("Lite", ("", "", ""))
        for term_key, label in self.term_price_labels.items():
            idx = TERM_IDX.get(term_key, 0)
            label.configure(text=f"起价 {anchor[idx]}")

    def _build_tier_group(self, parent: ttk.Frame, plantype: str):
        """给一个 plantype 建一组 tier 卡片 (单选)。parent 由调用方控制可见性。"""
        cards_row = ttk.Frame(parent)
        cards_row.pack(fill=X)

        self.tier_vars[plantype] = {}
        tiers = TIER_NAMES[plantype]

        for tier in tiers:
            card = ttk.Frame(cards_row, padding=14, relief="solid", borderwidth=1)
            card.pack(side=LEFT, padx=(0, 10), fill=BOTH, expand=True)
            self.tier_card_frames[(plantype, tier)] = card

            # Tier 名 (大粗体) — Pro 加 🔥 标记
            base_name = f"{tier} 🔥" if tier == "Pro" else tier
            name_label = ttk.Label(
                card, text=base_name,
                font=("Segoe UI", 16, "bold"),
            )
            name_label.pack(pady=(0, 4))
            self.tier_name_labels[(plantype, tier)] = (name_label, base_name)

            # 价格 (随计费周期变化)
            prices = TIER_PRICES.get(plantype, {}).get(tier, ("", "", ""))
            term_idx = TERM_IDX.get(self.cfg.get("billing_term", "monthly"), 0)
            self.price_labels[(plantype, tier)] = ttk.Label(
                card, text=prices[term_idx],
                font=("Segoe UI", 13), bootstyle=SECONDARY,
            )
            self.price_labels[(plantype, tier)].pack(pady=(0, 12))

            # 切换按钮 (单选)
            var = ttk.BooleanVar(
                value=tier in self.cfg.get(f"tiers_{plantype}", [])
            )
            self.tier_vars[plantype][tier] = var
            btn = ttk.Button(
                card,
                command=lambda p=plantype, t=tier: self._toggle_tier(p, t),
            )
            self.tier_buttons[(plantype, tier)] = btn
            btn.pack(fill=X, ipady=3)

            # 让整个 card 也可点击 (点非按钮区域 = 点 card)
            def _on_card_click(event, p=plantype, t=tier, b=btn):
                widget = event.widget.winfo_containing(
                    event.x_root, event.y_root
                )
                if widget is not b:
                    self._toggle_tier(p, t)
            card.bind("<Button-1>", _on_card_click)
            for child in card.winfo_children():
                child.bind("<Button-1>", _on_card_click)

            # 初始化按钮视觉 + 监听 BooleanVar 变化
            self._refresh_tier_button(plantype, tier)
            var.trace_add(
                "write",
                lambda *_, p=plantype, t=tier: self._refresh_tier_button(p, t),
            )

    def _toggle_tier(self, plantype: str, tier: str) -> None:
        """单选: 点 tier → 自动取消同 plantype 的其他 tier;再点一次取消自身。"""
        var = self.tier_vars[plantype][tier]
        new_state = not var.get()
        # 取消同组其他 tier (单选)
        for t, v in self.tier_vars[plantype].items():
            if t != tier:
                v.set(False)
        var.set(new_state)
        self._refresh_buy_btn_state()

    def _refresh_tier_button(self, plantype: str, tier: str) -> None:
        """单选视觉 (强反馈):
        - 选中: 卡片粗边框 + 名字/价格变绿色加 ● + 按钮绿色"✓ 当前选择"
        - 未选: 细灰边框 + 名字/价格暗色 + 按钮"选择此套餐"
        """
        var = self.tier_vars[plantype][tier]
        btn = self.tier_buttons[(plantype, tier)]
        card = self.tier_card_frames[(plantype, tier)]
        name_label, base_name = self.tier_name_labels[(plantype, tier)]
        price_label = self.price_labels[(plantype, tier)]
        if var.get():
            btn.configure(text="✓ 当前选择", bootstyle=SUCCESS)
            name_label.configure(text=f"● {base_name}", bootstyle=SUCCESS)
            price_label.configure(bootstyle=SUCCESS)
            try:
                card.configure(relief="solid", borderwidth=3)
            except Exception:
                pass
        else:
            btn.configure(text="选择此套餐", bootstyle=SECONDARY)
            name_label.configure(text=base_name, bootstyle=SECONDARY)
            price_label.configure(bootstyle=SECONDARY)
            try:
                card.configure(relief="solid", borderwidth=1)
            except Exception:
                pass

    def _on_plantype_change(self) -> None:
        """plantype 切换 → 只显示对应 tier 组,刷新立即抢购按钮状态。"""
        cur = self.plantype_var.get()
        for p, frame in self.tier_groups.items():
            if p == cur:
                frame.pack(fill=X, pady=(0, 4))
            else:
                frame.pack_forget()
        self._refresh_buy_btn_state()

    def _refresh_buy_btn_state(self) -> None:
        """没选 tier 时禁用立即抢购按钮,避免点完才发现没选。"""
        if not hasattr(self, "buy_btn"):
            return
        if self._selected_combo():
            self.buy_btn.configure(state=NORMAL, text="⚡ 立即抢购", bootstyle=PRIMARY)
        else:
            self.buy_btn.configure(state=DISABLED, text="⚡ 请先选套餐",
                                   bootstyle=SECONDARY)

    def _build_status_section(self, parent: ttk.Frame) -> None:
        self.status_frame = ttk.Labelframe(parent, text="状态", padding=12)
        frm = self.status_frame
        frm.pack(fill=X)

        # 顶部一行:调度信息 + 立即抢购按钮
        top = ttk.Frame(frm)
        top.pack(fill=X)
        self.next_run_var = ttk.StringVar(value="—")
        ttk.Label(top, textvariable=self.next_run_var,
                  font=("Segoe UI", 11)).pack(side=LEFT, pady=(0, 2))
        self.buy_btn = ttk.Button(top, text="⚡ 立即抢购",
                                  command=self._buy_now,
                                  bootstyle=PRIMARY)
        self.buy_btn.pack(side=RIGHT)
        self.last_result_var = ttk.StringVar(value="—")
        ttk.Label(frm, textvariable=self.last_result_var,
                  font=("Segoe UI", 11)).pack(anchor="w", pady=(6, 0))

    def _build_log_section(self, parent: ttk.Frame) -> None:
        """日志 tab:全屏 log text widget + 清空按钮。"""
        # 顶部工具栏
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill=X, pady=(0, 8))
        ttk.Label(toolbar, text="运行日志 (实时刷新)",
                  font=("Segoe UI", 11, "bold")).pack(side=LEFT)
        ttk.Button(toolbar, text="清空",
                   command=self._clear_log,
                   bootstyle=(SECONDARY, "outline-toolbutton")).pack(side=RIGHT, padx=4)
        ttk.Button(toolbar, text="打开日志文件",
                   command=self._open_log_file,
                   bootstyle=(INFO, "outline-toolbutton")).pack(side=RIGHT, padx=4)

        # 日志 text
        log_frame = ttk.Frame(parent)
        log_frame.pack(fill=BOTH, expand=True)

        self.log_text = ttk.Text(log_frame, height=10, wrap="word",
                                 font=("Consolas", 10))
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side=LEFT, fill=BOTH, expand=True)
        scroll.pack(side=RIGHT, fill="y")

        # redirect logging → GUI text widget
        gui_handler = _GuiLogHandler(self._append_log)
        gui_handler.setFormatter(
            logging.Formatter("%(asctime)s.%(msecs)03d  %(message)s",
                              datefmt="%H:%M:%S")
        )
        log.addHandler(gui_handler)

    def _clear_log(self) -> None:
        if self.log_text:
            self.log_text.delete("1.0", END)

    def _open_log_file(self) -> None:
        """在资源管理器/默认应用中打开 buyer.log"""
        import os
        try:
            if sys.platform == "win32":
                os.startfile(str(LOG_PATH))
            elif sys.platform == "darwin":
                os.system(f"open '{LOG_PATH}'")
            else:
                os.system(f"xdg-open '{LOG_PATH}'")
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    # ---- Cookie 操作 ----

    def _pick_cookie_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 cookie.json",
            filetypes=[("JSON", "*.json"), ("All", "*.*")],
            initialdir=str(PROJECT_DIR),
        )
        if path:
            self.cookie_path_var.set(path)
            self._persist()

    def _import_from_clipboard(self) -> None:
        try:
            content = self.root.clipboard_get()
        except Exception as e:
            messagebox.showerror("剪贴板读取失败", str(e))
            return
        try:
            data = json.loads(content)
            # 验证最小字段
            if "cookies" not in data and "localStorage" not in data:
                raise ValueError("剪贴板内容不是有效的 cookie JSON (需要 cookies "
                                 "或 localStorage 字段)")
            target = PROJECT_DIR / "cookie.json"
            target.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                              encoding="utf-8")
            self.cookie_path_var.set(str(target))
            self._persist()
            messagebox.showinfo("已导入", f"已写入 {target}")
        except Exception as e:
            messagebox.showerror("解析失败", str(e))

    def _verify_cookie(self) -> None:
        path = self.cookie_path_var.get().strip()
        if not path or not Path(path).exists():
            messagebox.showwarning("文件不存在", "请先选择或导入 cookie 文件")
            return
        try:
            cred = Credential.from_json_file(path)
            self.cookie_status_var.set(
                f"✓ 已加载: {len(cred.cookies)} cookies, "
                f"{len(cred.local_storage)} localStorage 项"
            )
            self._persist()
        except Exception as e:
            self.cookie_status_var.set(f"✗ 解析失败: {e}")

    # ---- Plan 类型 ----
    # UI 改用 tier 卡片后,不再需要刷 plantype 列表 — 保留为 no-op 防万一调用
    def _render_plantypes(self, types: list[str]) -> None:
        pass

    # ---- 定时调度 ----

    def _start_scheduler(self) -> None:
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            return
        self.stop_flag.clear()
        self.scheduler_thread = threading.Thread(target=self._scheduler_loop,
                                                 daemon=True)
        self.scheduler_thread.start()
        log.info("调度已启动")

    def _stop_scheduler(self) -> None:
        self.stop_flag.set()
        log.info("调度已停止")

    def _next_run_time(self) -> datetime:
        """明天 10:00:00 (库存固定 10 点上线,提前 1 分钟唤醒)。"""
        from datetime import datetime, timedelta
        now = datetime.now()
        target = now.replace(hour=10, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    def _wake_up_time(self) -> datetime:
        """提前 1 分钟 (9:59:00) 唤醒,给浏览器加载和页面就绪留时间。"""
        from datetime import datetime, timedelta
        now = datetime.now()
        target = now.replace(hour=9, minute=59, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    def _scheduler_loop(self) -> None:
        while not self.stop_flag.is_set():
            wake = self._wake_up_time()
            restock = self._next_run_time()
            log.info("下次唤醒: %s, 库存上线: %s (剩余 %s)",
                     wake.strftime("%H:%M:%S"),
                     restock.strftime("%H:%M:%S"),
                     wake - datetime.now())

            # sleep until 9:59 with stop check
            while not self.stop_flag.is_set() and datetime.now() < wake:
                self.stop_flag.wait(timeout=0.5)

            if self.stop_flag.is_set():
                return

            # 触发抢购 (buyer 内部会等到按钮 enable,覆盖 10:00:00 整点)
            self._buy_now_threaded(scheduled=True)
            # 等到下一天 (避免 1s 内连续触发)
            self.stop_flag.wait(timeout=2)

    # ---- 抢购入口 ----

    def _buy_now(self) -> None:
        """GUI 按钮触发,起后台线程跑抢购。单选模型: 一次只抢一个 (plantype, tier)。"""
        combo = self._selected_combo()
        if not combo:
            messagebox.showwarning("未选套餐", "请先选择一个 tier")
            return
        self._buy_now_threaded()

    def _buy_now_threaded(self, scheduled: bool = False) -> None:
        path = self.cookie_path_var.get().strip()
        if not path or not Path(path).exists():
            messagebox.showwarning("需要 cookie", "先选 cookie 文件")
            return

        combo = self._selected_combo()
        if not combo:
            return  # 已在 _buy_now 拦过
        plantype, tier = combo
        term = self.term_var.get()
        # 定时模式下等到按钮 enable (覆盖 10:00:00 整点);手动模式立即失败
        wait_sec = 90.0 if scheduled else 0.0

        def worker() -> None:
            log.info("▶ 抢购 %s / %s (term=%s, wait_for_enable=%.0fs)",
                     plantype, tier, term, wait_sec)
            result = buyer.buy_once(
                path,
                plantype,
                tier=tier, # pyright: ignore[reportCallIssue]
                term=term, # pyright: ignore[reportCallIssue]
                wait_for_enable_sec=wait_sec, # pyright: ignore[reportCallIssue]
                on_state=lambda s, m: log.info("[%s] %s", s.name, m),
                on_payment_alert=self._on_payment_alert,
            )
            self.last_result = result
            self.root.after(0, self._update_last_result_label)

        threading.Thread(target=worker, daemon=True).start()

    def _selected_tiers(self, plantype: str) -> list[str]:
        """给定 plantype,返回选中的 tier 列表(单选下要么 0 个要么 1 个)。"""
        return [t for t, v in self.tier_vars.get(plantype, {}).items()
                if v.get()]

    def _selected_combo(self) -> Optional[tuple[str, str]]:
        """单选模型: 返回当前 (plantype, tier) 组合,或 None 表示没选。"""
        plantype = self.plantype_var.get()
        tiers = self._selected_tiers(plantype)
        if not tiers:
            return None
        return (plantype, tiers[0])

    # ---- 支付提醒 ----

    def _on_payment_alert(self, url: str) -> None:
        """buyer 到达支付页时,在主线程弹出提醒。"""
        self.root.after(0, lambda: self._show_payment_alert(url))

    def _show_payment_alert(self, url: str) -> None:
        # 1. 打开默认浏览器到付款页 (用户可以直接在浏览器里扫码)
        try:
            webbrowser.open(url)
        except Exception as e:
            log.warning("打开浏览器失败: %s", e)

        # 2. 系统通知 + 声音 (winsound 简单 Beep)
        try:
            import winsound
            for _ in range(3):
                winsound.Beep(1200, 200)
        except Exception:
            pass

        # 3. 模态弹窗
        modal = ttk.Toplevel(self.root)
        modal.title("💰 已到支付页")
        modal.geometry("520x200")
        modal.transient(self.root)
        modal.grab_set()

        ttk.Label(modal,
                  text="已到支付页,请手动完成支付!",
                  font=("Segoe UI", 14, "bold"),
                  bootstyle=SUCCESS).pack(pady=(20, 8))

        ttk.Label(modal, text=url, wraplength=480,
                  bootstyle=SECONDARY).pack(pady=(0, 12))

        row = ttk.Frame(modal)
        row.pack()

        ttk.Button(row, text="✓ 我已支付,完成",
                   command=lambda: self._on_payment_done(modal, True),
                   bootstyle=SUCCESS).pack(side=LEFT, padx=4)

        ttk.Button(row, text="打开浏览器",
                   command=lambda: webbrowser.open(url),
                   bootstyle=INFO).pack(side=LEFT, padx=4)

        ttk.Button(row, text="放弃",
                   command=lambda: self._on_payment_done(modal, False),
                   bootstyle=DANGER).pack(side=LEFT, padx=4)

    def _on_payment_done(self, modal: ttk.Toplevel, success: bool) -> None:
        modal.destroy()
        if success:
            log.info("✅ 用户确认支付完成")
        else:
            log.warning("⛔ 用户放弃本次抢购")

    # ---- 状态刷新 ----

    def _refresh_status_loop(self) -> None:
        try:
            if not self.cfg["schedule_enabled"]:
                self.next_run_var.set("调度未启用")
            elif not self.scheduler_thread:
                self.next_run_var.set("调度启动中…")
            else:
                target = self._next_run_time()
                remain = target - datetime.now()
                if remain.total_seconds() > 0:
                    self.next_run_var.set(
                        f"下次执行: {target.strftime('%Y-%m-%d %H:%M:%S')} "
                        f"(剩余 {self._fmt_duration(remain)})"
                    )
                else:
                    self.next_run_var.set("下次执行: 即将触发…")
        finally:
            self.root.after(self.POLL_INTERVAL_MS, self._refresh_status_loop)

    @staticmethod
    def _fmt_duration(td: timedelta) -> str:
        s = int(td.total_seconds())
        h, rem = divmod(s, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m}m"
        return f"{m}m {s}s"

    def _update_last_result_label(self) -> None:
        r = self.last_result
        if not r:
            self.last_result_var.set("最近结果: —")
            return
        emoji = {
            BuyState.SUCCESS: "✅",
            BuyState.PAYMENT_PAGE_REACHED: "📝",
            BuyState.CAPTCHA_REQUIRED: "🧩",
            BuyState.PAYMENT_QR_READY: "📱",
            BuyState.LOGGED_OUT: "🔒",
            BuyState.FAILED: "❌",
        }.get(r.state, "❔")
        self.last_result_var.set(f"最近结果: {emoji} {r.state.name} — {r.message}")

    # ---- 日志到 GUI ----

    def _append_log(self, line: str) -> None:
        self.log_text.insert(END, line + "\n")
        self.log_text.see(END)

    # ---- 关闭 ----

    def _bind_close(self) -> None:
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
        self._persist()
        self._stop_scheduler()
        self.root.destroy()

    def _persist(self) -> None:
        self.cfg["cookie_path"] = self.cookie_path_var.get()
        self.cfg["active_plantype"] = self.plantype_var.get()
        # 单选下每个 plantype 的 tiers_* 列表只有 0 或 1 个元素
        for plantype in TIER_NAMES:
            self.cfg[f"tiers_{plantype}"] = self._selected_tiers(plantype)
        self.cfg["billing_term"] = self.term_var.get()
        # schedule_enabled 仅在启动时读 config.json;此处不再写回
        try:
            save_config(self.cfg)
        except Exception as e:
            log.warning("保存 config.json 失败: %s", e)

    def run(self) -> None:
        self.root.mainloop()


# ---------- 日志重定向到 Text widget ----------

class _GuiLogHandler(logging.Handler):
    def __init__(self, callback) -> None:
        super().__init__()
        self.callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self.callback(msg)
        except Exception:
            pass


if __name__ == "__main__":
    App().run()