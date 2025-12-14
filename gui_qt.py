from __future__ import annotations

import asyncio
import hashlib
import io
import re
import sys
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, NoReturn, TYPE_CHECKING

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QFont,
    QIcon,
    QPalette,
    QColor,
    QPixmap,
    QPainter,
    QFontMetrics,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QProgressBar,
    QSizePolicy,
    QStyle,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QListWidget,
    QListWidgetItem,
    QHeaderView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from translate import _
from utils import resource_path, webopen, Game
from constants import (
    WINDOW_TITLE,
    WS_TOPICS_LIMIT,
    MAX_WEBSOCKETS,
    OUTPUT_FORMATTER,
    State,
    PriorityMode,
    CACHE_PATH,
)


if TYPE_CHECKING:
    from twitch import Twitch
    from channel import Channel
    from inventory import DropsCampaign, TimedDrop


logger = logging.getLogger("TwitchDrops")


@dataclass
class LoginData:
    username: str
    password: str
    token: str


def _qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    # Required for tray apps: keep running when window is closed
    app.setQuitOnLastWindowClosed(False)
    return app


def _apply_dark_palette(app: QApplication) -> None:
    # Minimal, readable dark palette (Fusion style works best for consistency)
    app.setStyle("Fusion")
    palette = QPalette()

    bg = QColor(30, 30, 30)
    surface = QColor(37, 37, 37)
    fg = QColor(230, 230, 230)
    muted = QColor(160, 160, 160)
    accent = QColor(13, 153, 255)

    palette.setColor(QPalette.Window, bg)
    palette.setColor(QPalette.WindowText, fg)
    palette.setColor(QPalette.Base, surface)
    palette.setColor(QPalette.AlternateBase, bg)
    palette.setColor(QPalette.ToolTipBase, fg)
    palette.setColor(QPalette.ToolTipText, fg)
    palette.setColor(QPalette.Text, fg)
    palette.setColor(QPalette.Button, surface)
    palette.setColor(QPalette.ButtonText, fg)
    palette.setColor(QPalette.BrightText, QColor(255, 0, 0))
    palette.setColor(QPalette.Link, accent)
    palette.setColor(QPalette.Highlight, QColor(9, 71, 113))
    palette.setColor(QPalette.HighlightedText, QColor(255, 255, 255))

    # Disabled
    palette.setColor(QPalette.Disabled, QPalette.Text, muted)
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, muted)
    palette.setColor(QPalette.Disabled, QPalette.WindowText, muted)

    app.setPalette(palette)


def _apply_light_palette(app: QApplication) -> None:
    # Prefer system palette, but set Fusion for consistency with dark
    app.setStyle("Fusion")
    app.setPalette(QApplication.style().standardPalette())


def _is_os_dark(app: QApplication) -> bool:
    # Qt 6 exposes system color scheme on most platforms.
    hints = app.styleHints()
    if hasattr(hints, "colorScheme"):
        try:
            return hints.colorScheme() == Qt.ColorScheme.Dark  # type: ignore[attr-defined]
        except Exception:
            pass
    # Fallback: estimate by window background luminance.
    c = app.palette().color(QPalette.Window)
    return c.lightness() < 128


class StatusBar:
    def __init__(self, label: QLabel) -> None:
        self._label = label

    def update(self, text: str) -> None:
        self._label.setText(text)


class WebsocketStatus:
    def __init__(self, table: QTableWidget) -> None:
        self._table = table
        self._items: dict[int, dict[str, Any]] = {}

        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels([
            "#",
            _("gui", "websocket", "websocket"),
            "Topics",
        ])
        self._table.setRowCount(MAX_WEBSOCKETS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._table.setAlternatingRowColors(True)

        # Column sizing: keep index/topics tight and let the websocket/status column fill.
        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        for idx in range(MAX_WEBSOCKETS):
            self._table.setItem(idx, 0, QTableWidgetItem(str(idx)))
            self._table.setItem(idx, 1, QTableWidgetItem(""))
            self._table.setItem(idx, 2, QTableWidgetItem(""))

    def update(self, idx: int, *, status: str | None = None, topics: int | None = None) -> None:
        if idx < 0 or idx >= MAX_WEBSOCKETS:
            return
        item = self._items.get(idx, {"status": "", "topics": 0})
        if status is not None:
            item["status"] = status
        if topics is not None:
            item["topics"] = topics
        self._items[idx] = item
        self._table.item(idx, 1).setText(item["status"])
        self._table.item(idx, 2).setText(f"{item['topics']}/{WS_TOPICS_LIMIT}")

    def remove(self, idx: int) -> None:
        self._items.pop(idx, None)
        if 0 <= idx < MAX_WEBSOCKETS:
            self._table.item(idx, 1).setText("")
            self._table.item(idx, 2).setText("")


class ConsoleOutput:
    def __init__(self, output: QPlainTextEdit) -> None:
        self._output = output
        self._output.setReadOnly(True)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._output.setFont(mono)

    def print(self, message: str) -> None:
        stamp = datetime.now().strftime("%X")
        if "\n" in message:
            message = message.replace("\n", f"\n{stamp}: ")
        self._output.appendPlainText(f"{stamp}: {message}")


class QtImageCache:
    def __init__(self, twitch: Twitch):
        self._twitch = twitch
        self._mem: dict[tuple[str, int, int], QIcon] = {}
        self._dir = CACHE_PATH / "qt"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, url: str, *, w: int, h: int) -> str:
        hsh = hashlib.sha1(url.encode("utf-8"), usedforsecurity=False).hexdigest()
        return str(self._dir / f"{hsh}_{w}x{h}.png")

    async def get_icon(self, url: str, *, size: tuple[int, int]) -> QIcon:
        w, h = size
        key = (url, w, h)
        if key in self._mem:
            return self._mem[key]

        path = self._path_for(url, w=w, h=h)
        pix = QPixmap()
        if pix.load(path):
            icon = QIcon(pix)
            self._mem[key] = icon
            return icon

        data: bytes | None = None
        try:
            async with self._twitch.request("GET", url) as resp:
                if resp.status == 200:
                    data = await resp.read()
        except Exception:
            data = None

        if data:
            pix.loadFromData(data)
        if pix.isNull():
            pix = QPixmap(w, h)
            pix.fill(Qt.GlobalColor.transparent)
        else:
            pix = pix.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)

        # Persist best-effort (ignore failures)
        try:
            pix.save(path, "PNG")
        except Exception:
            pass

        icon = QIcon(pix)
        self._mem[key] = icon
        return icon


class CampaignProgress:
    ALMOST_DONE_SECONDS = 10

    def __init__(self, campaign_bar: QProgressBar, drop_bar: QProgressBar, campaign_label: QLabel, drop_label: QLabel) -> None:
        self._campaign_bar = campaign_bar
        self._drop_bar = drop_bar
        self._campaign_label = campaign_label
        self._drop_label = drop_label
        self._drop: TimedDrop | None = None
        self._next_minute_deadline: datetime | None = None
        self._last_deadline_fired: datetime | None = None

        for bar in (self._campaign_bar, self._drop_bar):
            bar.setRange(0, 1000)
            bar.setTextVisible(True)

        self.display(None)

    def stop_timer(self) -> None:
        # Qt updates are driven by incoming events; no separate timer needed.
        pass

    def display(self, drop: TimedDrop | None, *, countdown: bool = True, subone: bool = False) -> None:
        self._drop = drop
        now = datetime.now(timezone.utc)
        if drop is None:
            self._campaign_label.setText("")
            self._drop_label.setText("")
            self._campaign_bar.setValue(0)
            self._drop_bar.setValue(0)
            self._next_minute_deadline = None
            self._last_deadline_fired = None
            return

        campaign = drop.campaign
        # Campaign
        c_pct = max(0.0, min(1.0, campaign.progress))
        self._campaign_bar.setValue(int(c_pct * 1000))
        self._campaign_bar.setFormat(f"{c_pct:.1%} ({campaign.claimed_drops}/{campaign.total_drops})")
        self._campaign_label.setText(f"{campaign.game.name} — {campaign.name}")

        # Drop
        d_pct = max(0.0, min(1.0, drop.progress))
        self._drop_bar.setValue(int(d_pct * 1000))
        self._drop_bar.setFormat(f"{d_pct:.1%} ({drop.current_minutes}/{drop.required_minutes} min)")
        self._drop_label.setText(drop.rewards_text())

        if countdown:
            # Next minute boundary from now.
            seconds_left = 60 - now.second
            if subone:
                seconds_left = max(0, seconds_left - 1)
            self._next_minute_deadline = now.replace(microsecond=0) + timedelta(seconds=seconds_left)
        else:
            self._next_minute_deadline = None
            self._last_deadline_fired = None

    def minute_almost_done(self) -> bool:
        deadline = self._next_minute_deadline
        if deadline is None:
            return False
        now = datetime.now(timezone.utc)
        remaining = (deadline - now).total_seconds()
        if remaining > self.ALMOST_DONE_SECONDS:
            return False
        # Fire once per deadline
        if self._last_deadline_fired == deadline:
            return False
        self._last_deadline_fired = deadline
        return True


class ChannelList:
    def __init__(self, twitch: Twitch, table: QTableWidget, switch_button: QPushButton) -> None:
        self._twitch = twitch
        self._table = table
        self._switch_button = switch_button
        self._rows: dict[str, int] = {}
        self._watching_iid: str | None = None

        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels([
            _("gui", "channels", "headings", "channel"),
            _("gui", "channels", "headings", "status"),
            _("gui", "channels", "headings", "game"),
            "🎁",
            _("gui", "channels", "headings", "viewers"),
            "📋",
        ])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setIconSize(QSize(24, 24))
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)  # channel
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)  # game
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._on_selected)
        self._switch_button.clicked.connect(lambda: self._twitch.change_state(State.CHANNEL_SWITCH))
        self._switch_button.setEnabled(False)

    def _on_selected(self) -> None:
        self._switch_button.setEnabled(bool(self._table.selectionModel().selectedRows()))

    def clear_watching(self) -> None:
        if self._watching_iid is None:
            return
        # Clear any reward icon shown for the watching channel
        row = self._rows.get(self._watching_iid)
        if row is not None:
            item0 = self._table.item(row, 0)
            if item0 is not None:
                item0.setIcon(QIcon())
        row = self._rows.get(self._watching_iid)
        if row is not None:
            for c in range(self._table.columnCount()):
                item = self._table.item(row, c)
                if item is not None:
                    item.setBackground(QColor())
        self._watching_iid = None

    def set_watching(self, channel: Channel) -> None:
        self.clear_watching()
        iid = channel.iid
        row = self._rows.get(iid)
        if row is None:
            return
        for c in range(self._table.columnCount()):
            item = self._table.item(row, c)
            if item is not None:
                item.setBackground(QColor(128, 128, 128, 80))
        self._watching_iid = iid
        self._table.scrollToItem(self._table.item(row, 0))

    def set_watching_drop_icon(self, icon: QIcon | None) -> None:
        if self._watching_iid is None:
            return
        row = self._rows.get(self._watching_iid)
        if row is None:
            return
        item0 = self._table.item(row, 0)
        if item0 is None:
            return
        item0.setIcon(icon or QIcon())

    def get_selection(self) -> Channel | None:
        selected = self._table.selectionModel().selectedRows()
        if not selected:
            return None
        row = selected[0].row()
        iid_item = self._table.item(row, 0)
        if iid_item is None:
            return None
        # stored in UserRole
        iid = iid_item.data(Qt.ItemDataRole.UserRole)
        if not iid:
            return None
        try:
            return self._twitch.channels[int(iid)]
        except Exception:
            return None

    def clear_selection(self) -> None:
        self._table.clearSelection()

    def clear(self) -> None:
        self._table.setRowCount(0)
        self._rows.clear()
        self.clear_watching()

    def display(self, channel: Channel, *, add: bool = False) -> None:
        iid = channel.iid
        if not add and iid not in self._rows:
            return

        # ACL-based
        acl_based = "✔" if channel.acl_based else "❌"
        # status
        if channel.online:
            status = _("gui", "channels", "online")
        elif channel.pending_online:
            status = _("gui", "channels", "pending")
        else:
            status = _("gui", "channels", "offline")
        # game
        game = str(channel.game or "")
        # drops
        drops = "✔" if channel.drops_enabled else "❌"
        # viewers
        viewers = str(channel.viewers) if channel.viewers is not None else ""

        if iid not in self._rows:
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._rows[iid] = row

            name_item = QTableWidgetItem(channel.name)
            name_item.setData(Qt.ItemDataRole.UserRole, iid)
            self._table.setItem(row, 0, name_item)
            self._table.setItem(row, 1, QTableWidgetItem(status))
            self._table.setItem(row, 2, QTableWidgetItem(game))
            self._table.setItem(row, 3, QTableWidgetItem(drops))
            self._table.setItem(row, 4, QTableWidgetItem(viewers))
            self._table.setItem(row, 5, QTableWidgetItem(acl_based))
        else:
            row = self._rows[iid]
            self._table.item(row, 1).setText(status)
            self._table.item(row, 2).setText(game)
            self._table.item(row, 3).setText(drops)
            self._table.item(row, 4).setText(viewers)
            self._table.item(row, 5).setText(acl_based)

    def remove(self, channel: Channel) -> None:
        iid = channel.iid
        row = self._rows.pop(iid, None)
        if row is None:
            return
        self._table.removeRow(row)
        # Rebuild row mapping (simple and safe)
        self._rows = {
            self._table.item(r, 0).data(Qt.ItemDataRole.UserRole): r
            for r in range(self._table.rowCount())
            if self._table.item(r, 0) is not None
        }


class _InventoryGrid(QListWidget):
    def __init__(self) -> None:
        super().__init__()
        self._tile_h = 150
        self._preferred_tile_w = 260
        self._min_tile_w = 260
        self._max_tile_w = 420

    def set_tile_height(self, h: int) -> None:
        self._tile_h = max(80, int(h))
        self._update_grid()

    def set_preferred_tile_width(self, w: int) -> None:
        self._preferred_tile_w = max(120, int(w))
        self._update_grid()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._update_grid()

    def _update_grid(self) -> None:
        vw = self.viewport().width()
        if vw <= 0:
            return

        spacing = max(0, int(self.spacing()))
        best: tuple[int, int] | None = None  # (leftover_px, tile_w)
        for cols in range(1, 20):
            tile_w = (vw - spacing * (cols - 1) - 2) // cols
            if tile_w < self._min_tile_w:
                break
            if tile_w > self._max_tile_w:
                continue
            used = tile_w * cols + spacing * (cols - 1)
            leftover = max(0, vw - used)
            if best is None or leftover < best[0]:
                best = (leftover, tile_w)
        if best is None:
            tile_w = max(self._min_tile_w, min(self._max_tile_w, self._preferred_tile_w))
        else:
            tile_w = best[1]
        self.setGridSize(QSize(int(tile_w), int(self._tile_h)))


class InventoryOverview:
    _ROLE_SEARCH = Qt.ItemDataRole.UserRole + 1
    _ROLE_STATS = Qt.ItemDataRole.UserRole + 2

    class _GridDelegate(QStyledItemDelegate):
        def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:  # type: ignore[override]
            opt = QStyleOptionViewItem(option)
            self.initStyleOption(opt, index)

            # Draw selection/background like a normal item, but we draw icon/text ourselves
            # to avoid Qt's text eliding in IconMode.
            style = opt.widget.style() if opt.widget is not None else QApplication.style()
            style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, opt, painter, opt.widget)

            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

            rect = opt.rect
            stats: str = index.data(InventoryOverview._ROLE_STATS) or ""

            # Layout: icon at top, name below.
            icon_size = opt.decorationSize
            icon_rect = rect
            icon_rect.setLeft(rect.left() + (rect.width() - icon_size.width()) // 2)
            icon_rect.setTop(rect.top() + 8)
            icon_rect.setWidth(icon_size.width())
            icon_rect.setHeight(icon_size.height())

            if not opt.icon.isNull():
                opt.icon.paint(painter, icon_rect, Qt.AlignmentFlag.AlignCenter)

            # Badge: translucent pill over the icon (bottom-left). Elide if too long.
            if stats:
                fm = QFontMetrics(opt.font)
                padding_x, padding_y = 6, 3
                badge_h = fm.height() + padding_y * 2
                badge_rect = icon_rect.adjusted(4, 0, -4, 0)
                badge_rect.setTop(icon_rect.bottom() - badge_h - 4)
                badge_rect.setHeight(badge_h)

                bg = opt.palette.color(QPalette.ColorRole.Base)
                bg.setAlpha(190)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(bg)
                painter.drawRoundedRect(badge_rect, 6, 6)

                fg = opt.palette.color(QPalette.ColorRole.Text)
                painter.setPen(fg)
                inner = badge_rect.adjusted(padding_x, padding_y, -padding_x, -padding_y)
                elided = fm.elidedText(stats, Qt.TextElideMode.ElideRight, max(0, inner.width()))
                painter.drawText(inner, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, elided)

            # Name text: wrapped, centered, no elide.
            text = index.data(Qt.ItemDataRole.DisplayRole) or ""
            text_rect = rect.adjusted(6, icon_rect.bottom() - rect.top() + 10, -6, -6)
            painter.setPen(opt.palette.color(QPalette.ColorRole.Text))
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
                text,
            )

            painter.restore()

    def __init__(
        self,
        view: QListWidget,
        search: QLineEdit,
        *,
        image_cache: QtImageCache,
    ) -> None:
        self._view = view
        self._search = search
        self._img = image_cache
        self._drop_items: dict[str, QListWidgetItem] = {}

        self._search.setPlaceholderText("Search drops…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self.apply_filter)

        self._view.setViewMode(QListView.ViewMode.IconMode)
        self._view.setFlow(QListView.Flow.LeftToRight)
        self._view.setWrapping(True)
        self._view.setResizeMode(QListView.ResizeMode.Adjust)
        self._view.setMovement(QListView.Movement.Static)
        self._view.setSpacing(10)
        self._view.setIconSize(QSize(96, 96))
        self._view.setTextElideMode(Qt.TextElideMode.ElideNone)
        self._view.setGridSize(QSize(260, 150))
        self._view.setWordWrap(True)
        self._view.setUniformItemSizes(True)
        self._view.setItemDelegate(self._GridDelegate(self._view))

    def clear(self) -> None:
        self._drop_items.clear()
        self._view.clear()

    async def add_campaign(self, campaign: DropsCampaign) -> None:
        for drop in campaign.drops:
            li = QListWidgetItem()
            li.setData(Qt.ItemDataRole.UserRole, drop.id)
            # used for search
            li.setData(self._ROLE_SEARCH, f"{drop.name} {campaign.name}".lower())
            li.setData(self._ROLE_STATS, self._format_drop_stats(drop))

            # Icon: reward image if available, otherwise campaign box art.
            try:
                if drop.benefits:
                    icon = await self._img.get_icon(str(drop.benefits[0].image_url), size=(96, 96))
                else:
                    icon = await self._img.get_icon(str(campaign.image_url), size=(96, 96))
                li.setIcon(icon)
            except Exception:
                pass

            li.setText(drop.name)
            li.setToolTip(self._format_drop_tooltip(drop, campaign_name=campaign.name))
            self._view.addItem(li)
            self._drop_items[drop.id] = li

        # Re-apply current filter after adding new items.
        self.apply_filter(self._search.text())

    def update_drop(self, drop: TimedDrop) -> None:
        di = self._drop_items.get(drop.id)
        if di is not None:
            di.setData(self._ROLE_STATS, self._format_drop_stats(drop))
            di.setText(drop.name)
            di.setToolTip(self._format_drop_tooltip(drop, campaign_name=drop.campaign.name))

    def apply_filter(self, text: str) -> None:
        q = (text or "").strip().lower()
        if not q:
            for i in range(self._view.count()):
                self._view.item(i).setHidden(False)
            return
        for i in range(self._view.count()):
            it = self._view.item(i)
            hay = it.data(self._ROLE_SEARCH) or ""
            it.setHidden(q not in hay)

    @staticmethod
    def _format_drop_stats(drop: TimedDrop) -> str:
        mins = (
            f"{drop.current_minutes}/{drop.required_minutes}m"
            if drop.required_minutes
            else ""
        )
        pct = f"{drop.progress:.0%}"
        return " • ".join([p for p in (pct, mins) if p])

    @staticmethod
    def _format_drop_tooltip(drop: TimedDrop, *, campaign_name: str) -> str:
        lines: list[str] = [
            f"{drop.name}",
            f"Campaign: {campaign_name}",
            f"Progress: {drop.progress:.1%}",
            f"Minutes: {drop.current_minutes}/{drop.required_minutes}",
        ]
        if drop.is_claimed:
            lines.append("Status: Claimed")
        return "\n".join(lines)


class TrayIcon:
    TITLE = "Twitch Drops Miner"

    def __init__(self, manager: "GUIManager") -> None:
        self._manager = manager
        self._tray = None
        self._icon_state: str = "pickaxe"

        self._icons: dict[str, QIcon] = {
            "pickaxe": QIcon(str(resource_path("icons/pickaxe.ico"))),
            "active": QIcon(str(resource_path("icons/active.ico"))),
            "idle": QIcon(str(resource_path("icons/idle.ico"))),
            "error": QIcon(str(resource_path("icons/error.ico"))),
            "maint": QIcon(str(resource_path("icons/maint.ico"))),
        }

        from PySide6.QtWidgets import QSystemTrayIcon

        self._tray = QSystemTrayIcon(self._icons["pickaxe"])
        self._tray.setToolTip(self.TITLE)

        menu = self._tray.contextMenu() or None
        if menu is None:
            from PySide6.QtWidgets import QMenu

            menu = QMenu()
            self._tray.setContextMenu(menu)

        act_show = QAction(_("gui", "tray", "show"), menu)
        act_show.triggered.connect(self.restore)
        menu.addAction(act_show)

        act_quit = QAction(_("gui", "tray", "quit"), menu)
        act_quit.triggered.connect(self._manager.close)
        menu.addAction(act_quit)

        self._tray.activated.connect(self._on_activated)
        self._tray.show()

    def _on_activated(self, reason) -> None:
        # Left click toggles show/hide
        try:
            from PySide6.QtWidgets import QSystemTrayIcon

            if reason == QSystemTrayIcon.ActivationReason.Trigger:
                if self._manager._window.isVisible():
                    self.minimize()
                else:
                    self.restore()
        except Exception:
            return

    def stop(self) -> None:
        if self._tray is not None:
            self._tray.hide()

    def minimize(self) -> None:
        self._manager._window.hide()

    def restore(self) -> None:
        w = self._manager._window
        w.show()
        w.raise_()
        w.activateWindow()

    def change_icon(self, name: str) -> None:
        self._icon_state = name
        icon = self._icons.get(name)
        if icon is not None and self._tray is not None:
            self._tray.setIcon(icon)

    def update_title(self, drop: TimedDrop | None) -> None:
        title = self.TITLE
        if drop is not None:
            campaign = drop.campaign
            title = (
                f"{self.TITLE}\n{campaign.game.name}\n{drop.rewards_text()} {drop.progress:.1%}"
                f" ({campaign.claimed_drops}/{campaign.total_drops})"
            )
        if self._tray is not None:
            self._tray.setToolTip(title)

    def notify(self, message: str, title: str) -> None:
        if self._tray is not None:
            self._tray.showMessage(title, message)


class SettingsPanel:
    def __init__(
        self,
        twitch: Twitch,
        *,
        priority_mode: QComboBox,
        priority_combo: QComboBox,
        exclude_combo: QComboBox,
        priority_list: QListWidget,
        exclude_list: QListWidget,
    ) -> None:
        self._twitch = twitch
        self._settings = twitch.settings
        self._priority_mode = priority_mode
        self._priority_combo = priority_combo
        self._exclude_combo = exclude_combo
        self._priority_list = priority_list
        self._exclude_list = exclude_list

        # Initialize lists
        self._priority_list.addItems(self._settings.priority)
        for game in sorted(self._settings.exclude):
            self._exclude_list.addItem(game)

    def clear_selection(self) -> None:
        self._priority_list.clearSelection()
        self._exclude_list.clearSelection()

    def set_games(self, games: set[Game]) -> None:
        # Populate add-combos with available game names
        names = sorted({g.name for g in games if g and getattr(g, "name", None)})
        self._priority_combo.clear()
        self._exclude_combo.clear()
        self._priority_combo.addItems(names)
        self._exclude_combo.addItems(names)

    def priority_add(self) -> None:
        name = self._priority_combo.currentText().strip()
        if not name or name in self._settings.priority:
            return
        self._settings.priority.append(name)
        self._settings.alter()
        self._priority_list.addItem(name)

    def priority_delete(self) -> None:
        row = self._priority_list.currentRow()
        if row < 0:
            return
        item = self._priority_list.takeItem(row)
        if item is None:
            return
        name = item.text()
        if name in self._settings.priority:
            self._settings.priority.remove(name)
            self._settings.alter()

    def priority_move(self, up: bool) -> None:
        row = self._priority_list.currentRow()
        if row < 0:
            return
        new_row = row - 1 if up else row + 1
        if new_row < 0 or new_row >= self._priority_list.count():
            return
        item = self._priority_list.takeItem(row)
        assert item is not None
        self._priority_list.insertItem(new_row, item)
        self._priority_list.setCurrentRow(new_row)

        # Sync settings list
        name = item.text()
        self._settings.priority.pop(row)
        self._settings.priority.insert(new_row, name)
        self._settings.alter()

    def exclude_add(self) -> None:
        name = self._exclude_combo.currentText().strip()
        if not name or name in self._settings.exclude:
            return
        self._settings.exclude.add(name)
        self._settings.alter()
        self._exclude_list.addItem(name)

    def exclude_delete(self) -> None:
        row = self._exclude_list.currentRow()
        if row < 0:
            return
        item = self._exclude_list.takeItem(row)
        if item is None:
            return
        name = item.text()
        if name in self._settings.exclude:
            self._settings.exclude.remove(name)
            self._settings.alter()


class LoginForm:
    def __init__(self, manager: "GUIManager", status_label: QLabel, user_label: QLabel, login: QLineEdit, password: QLineEdit, token: QLineEdit, confirm_button: QPushButton, logout_button: QPushButton) -> None:
        self._manager = manager
        self._status_label = status_label
        self._user_label = user_label
        self._login = login
        self._password = password
        self._token = token
        self._confirm = confirm_button
        self._logout = logout_button
        self._confirm.setEnabled(False)
        self._logout.setEnabled(False)
        self._confirm.clicked.connect(self._on_confirm)
        self._logout.clicked.connect(self._on_logout)
        self._confirm_future: asyncio.Future[None] | None = None
        self.update(_("gui", "login", "logged_out"), None)

    def _on_confirm(self) -> None:
        fut = self._confirm_future
        if fut is not None and not fut.done():
            fut.set_result(None)

    def _on_logout(self) -> None:
        from exceptions import ReloadRequest

        self._manager._twitch.logout()
        self.update(_("gui", "login", "logged_out"), None)
        self._manager.print("Logged out. Restarting application...")
        raise ReloadRequest()

    def clear(self, login: bool = False, password: bool = False, token: bool = False) -> None:
        clear_all = not login and not password and not token
        if login or clear_all:
            self._login.clear()
        if password or clear_all:
            self._password.clear()
        if token or clear_all:
            self._token.clear()

    async def _wait_for_confirm(self) -> None:
        self._confirm_future = asyncio.get_running_loop().create_future()
        self._confirm.setEnabled(True)
        try:
            await self._manager.coro_unless_closed(self._confirm_future)
        finally:
            self._confirm.setEnabled(False)
            self._confirm_future = None

    async def ask_login(self) -> LoginData:
        self.update(_("gui", "login", "required"), None)
        self._manager.grab_attention(sound=False)
        while True:
            self._manager.print(_("gui", "login", "request"))
            await self._wait_for_confirm()
            login_data = LoginData(
                self._login.text().strip(),
                self._password.text(),
                self._token.text().strip(),
            )
            # basic validation
            if (not 3 <= len(login_data.username) <= 25) or not re.match(r"^[a-zA-Z0-9_]+$", login_data.username):
                self.clear(login=True)
                continue
            if len(login_data.password) < 8:
                self.clear(password=True)
                continue
            if login_data.token and len(login_data.token) < 6:
                self.clear(token=True)
                continue
            return login_data

    async def ask_enter_code(self, page_url, user_code: str) -> None:
        self.update(_("gui", "login", "required"), None)
        self._manager.grab_attention(sound=False)
        self._manager.print(_("gui", "login", "request"))
        await self._wait_for_confirm()
        self._manager.print(f"Enter this code on the Twitch's device activation page: {user_code}")
        await asyncio.sleep(1)
        webopen(page_url)

    def update(self, status: str, user_id: int | None) -> None:
        self._status_label.setText(status)
        self._user_label.setText(str(user_id) if user_id is not None else "-")
        self._logout.setEnabled(user_id is not None)


class _QtOutputHandler(logging.Handler):
    def __init__(self, output: "GUIManager") -> None:
        super().__init__()
        self._output = output

    def emit(self, record: logging.LogRecord) -> None:
        self._output.print(self.format(record))


class _MainWindow(QMainWindow):
    def __init__(self, manager: "GUIManager") -> None:
        super().__init__()
        self._manager = manager

    def closeEvent(self, event: QCloseEvent) -> None:
        # User close requests miner shutdown; actual window close happens later.
        if getattr(self._manager, "_allow_window_close", False):
            event.accept()
            return
        self._manager.close()
        event.ignore()


class GUIManager:
    def __init__(self, twitch: Twitch):
        self._twitch = twitch
        self._poll_task: asyncio.Task[NoReturn] | None = None
        self._close_requested = asyncio.Event()
        self._allow_window_close: bool = False

        app = _qapp()
        self._window = _MainWindow(self)
        self._window.setWindowTitle(WINDOW_TITLE)
        self._window.setWindowIcon(QIcon(str(resource_path("icons/pickaxe.ico"))))

        # ESC clears selections (mirrors old Tk behavior)
        act_unfocus = QAction(self._window)
        act_unfocus.setShortcut(Qt.Key.Key_Escape)
        act_unfocus.triggered.connect(self.unfocus)
        self._window.addAction(act_unfocus)

        # Auto theme: follow OS
        self._apply_auto_theme()
        try:
            app.styleHints().colorSchemeChanged.connect(lambda *_: self._apply_auto_theme())  # type: ignore[attr-defined]
        except Exception:
            pass

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(10, 10, 10, 10)
        self._window.setCentralWidget(root)

        self.tabs = QTabWidget()
        # Make tab widths fill the available space (avoids narrow/truncated tab look)
        try:
            self.tabs.tabBar().setExpanding(True)
        except Exception:
            pass
        layout.addWidget(self.tabs)

        # Main tab
        main = QWidget()
        main_layout = QGridLayout(main)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setHorizontalSpacing(10)
        main_layout.setVerticalSpacing(10)
        main_layout.setColumnStretch(0, 1)
        main_layout.setColumnStretch(1, 1)
        main_layout.setRowStretch(3, 1)

        # Tray
        self.tray = TrayIcon(self)

        # Image cache (Qt)
        self._img_cache = QtImageCache(twitch)
        self._drop_icon_task: asyncio.Task[None] | None = None
        self._last_drop_icon_url: str | None = None

        # Status + websockets
        status_box = QGroupBox(_("gui", "status", "name"))
        status_l = QVBoxLayout(status_box)
        status_label = QLabel("")
        status_label.setWordWrap(True)
        status_l.addWidget(status_label)
        self.status = StatusBar(status_label)

        ws_box = QGroupBox(_("gui", "websocket", "name"))
        ws_l = QVBoxLayout(ws_box)
        ws_table = QTableWidget()
        ws_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        ws_table.setToolTip("Live websocket connections used for mining.\nIf this stays disconnected, drops won't progress.")
        ws_l.addWidget(ws_table)
        self.websockets = WebsocketStatus(ws_table)

        main_layout.addWidget(status_box, 0, 0)
        main_layout.addWidget(ws_box, 0, 1)

        # Login
        login_box = QGroupBox(_("gui", "login", "name"))
        login_layout = QGridLayout(login_box)
        login_layout.addWidget(QLabel(_("gui", "login", "labels")), 0, 0)
        login_status = QLabel("")
        login_user = QLabel("")
        login_layout.addWidget(login_status, 0, 1)
        login_layout.addWidget(login_user, 0, 2)

        le_user = QLineEdit()
        le_user.setPlaceholderText(_("gui", "login", "username"))
        le_pass = QLineEdit()
        le_pass.setPlaceholderText(_("gui", "login", "password"))
        le_pass.setEchoMode(QLineEdit.EchoMode.Password)
        le_token = QLineEdit()
        le_token.setPlaceholderText(_("gui", "login", "twofa_code"))

        btn_confirm = QPushButton(_("gui", "login", "button"))
        btn_confirm.setToolTip("Submit the login details above.")
        btn_logout = QPushButton(_("gui", "login", "logout_button") if _("gui", "login", "logout_button") else "Logout")
        btn_logout.setToolTip("Log out and restart the miner.")

        login_layout.addWidget(le_user, 1, 0, 1, 3)
        login_layout.addWidget(le_pass, 2, 0, 1, 3)
        login_layout.addWidget(le_token, 3, 0, 1, 3)
        login_layout.addWidget(btn_confirm, 4, 0, 1, 3)
        login_layout.addWidget(btn_logout, 5, 0, 1, 3)

        self.login = LoginForm(self, login_status, login_user, le_user, le_pass, le_token, btn_confirm, btn_logout)

        # Progress
        progress_box = QGroupBox(_("gui", "progress", "name"))
        progress_layout = QVBoxLayout(progress_box)
        lbl_campaign = QLabel("")
        lbl_campaign.setWordWrap(True)
        bar_campaign = QProgressBar()
        lbl_drop = QLabel("")
        lbl_drop.setWordWrap(True)
        bar_drop = QProgressBar()
        progress_layout.addWidget(QLabel(_("gui", "progress", "campaign")))
        progress_layout.addWidget(lbl_campaign)
        progress_layout.addWidget(bar_campaign)
        progress_layout.addSpacing(6)
        progress_layout.addWidget(QLabel(_("gui", "progress", "drop")))
        progress_layout.addWidget(lbl_drop)
        progress_layout.addWidget(bar_drop)
        self.progress = CampaignProgress(bar_campaign, bar_drop, lbl_campaign, lbl_drop)

        # Output
        output_box = QGroupBox(_("gui", "output"))
        output_layout = QVBoxLayout(output_box)
        out = QPlainTextEdit()
        output_layout.addWidget(out)
        self.output = ConsoleOutput(out)

        # Channels
        channels_box = QGroupBox(_("gui", "channels", "name"))
        channels_layout = QVBoxLayout(channels_box)
        btn_switch = QPushButton(_("gui", "channels", "switch"))
        btn_switch.setToolTip("Switch to the selected channel.")
        channels_table = QTableWidget()
        channels_layout.addWidget(btn_switch)
        channels_layout.addWidget(channels_table)
        self.channels = ChannelList(twitch, channels_table, btn_switch)

        main_layout.addWidget(login_box, 1, 0)
        main_layout.addWidget(channels_box, 1, 1)
        main_layout.addWidget(progress_box, 2, 0, 1, 2)
        main_layout.addWidget(output_box, 3, 0, 1, 2)

        self.tabs.addTab(main, _("gui", "tabs", "main"))

        # Inventory tab
        inv = QWidget()
        inv_layout = QVBoxLayout(inv)

        inv_search = QLineEdit()
        inv_layout.addWidget(inv_search)

        inv_list = _InventoryGrid()
        inv_list.set_preferred_tile_width(260)
        inv_list.set_tile_height(150)
        inv_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        inv_layout.addWidget(inv_list)

        self.inv = InventoryOverview(inv_list, inv_search, image_cache=self._img_cache)
        self.tabs.addTab(inv, _("gui", "tabs", "inventory"))

        # Settings tab (minimal)
        settings = QWidget()
        settings_layout = QVBoxLayout(settings)
        settings_layout.setContentsMargins(10, 10, 10, 10)
        settings_layout.setSpacing(12)

        general = QGroupBox(_("gui", "settings", "general", "name"))
        gl = QFormLayout(general)
        gl.setContentsMargins(10, 10, 10, 10)
        gl.setSpacing(6)

        self._cb_tray = QCheckBox(_("gui", "settings", "general", "tray"))
        self._cb_tray_notifications = QCheckBox(_("gui", "settings", "general", "tray_notifications"))
        self._cb_auto_claim = QCheckBox(_("gui", "settings", "general", "auto_claim"))
        self._cb_auto_restart = QCheckBox(_("gui", "settings", "advanced", "auto_restart_on_error"))
        self._cb_bypass_link = QCheckBox(_("gui", "settings", "advanced", "bypass_account_linking"))
        self._cb_available_check = QCheckBox(_("gui", "settings", "advanced", "available_drops_check"))
        self._cb_enable_badges = QCheckBox(_("gui", "settings", "advanced", "enable_badges_emotes"))
        self._cb_ignore_badge = QCheckBox(_("gui", "settings", "advanced", "ignore_badge_emote"))

        gl.addRow(self._cb_tray)
        gl.addRow(self._cb_tray_notifications)
        gl.addRow(self._cb_auto_claim)
        gl.addRow(self._cb_auto_restart)
        gl.addRow(self._cb_bypass_link)
        gl.addRow(self._cb_available_check)
        gl.addRow(self._cb_enable_badges)
        gl.addRow(self._cb_ignore_badge)

        priority_box = QGroupBox(_("gui", "settings", "priority"))
        pl = QVBoxLayout(priority_box)
        pl.setContentsMargins(10, 10, 10, 10)
        pl.setSpacing(8)
        self._priority_mode = QComboBox()
        priority_box.setTitle("Watch order")
        self._priority_mode.addItem("Use watch list only", PriorityMode.PRIORITY_ONLY)
        self._priority_mode.addItem("Prefer drops ending sooner", PriorityMode.ENDING_SOONEST)
        self._priority_mode.addItem("Prefer low availability", PriorityMode.LOW_AVBL_FIRST)
        pl.addWidget(QLabel("Watch mode:"))
        pl.addWidget(self._priority_mode)

        # Priority list controls
        pr_row = QHBoxLayout()
        self._priority_combo = QComboBox()
        self._priority_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        btn_pr_add = QPushButton("+")
        btn_pr_del = QPushButton("Remove")
        btn_pr_up = QPushButton("Up")
        btn_pr_down = QPushButton("Down")
        btn_pr_add.setToolTip("Add the selected game to the priority list.")
        btn_pr_del.setToolTip("Remove the selected game from the priority list.")
        btn_pr_up.setToolTip("Move the selected game up.")
        btn_pr_down.setToolTip("Move the selected game down.")
        btn_pr_del.setEnabled(False)
        btn_pr_up.setEnabled(False)
        btn_pr_down.setEnabled(False)
        pr_row.addWidget(self._priority_combo)
        pr_row.addWidget(btn_pr_add)
        pr_row.addWidget(btn_pr_del)
        pr_row.addWidget(btn_pr_up)
        pr_row.addWidget(btn_pr_down)
        pl.addLayout(pr_row)

        self._priority_list = QListWidget()
        pl.addWidget(self._priority_list)

        # Exclude list controls
        ex_box = QGroupBox(_("gui", "settings", "exclude"))
        ex_l = QVBoxLayout(ex_box)
        ex_l.setContentsMargins(10, 10, 10, 10)
        ex_l.setSpacing(8)
        ex_row = QHBoxLayout()
        self._exclude_combo = QComboBox()
        self._exclude_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        btn_ex_add = QPushButton("+")
        btn_ex_del = QPushButton("Remove")
        btn_ex_add.setToolTip("Add the selected game to the exclude list.")
        btn_ex_del.setToolTip("Remove the selected game from the exclude list.")
        btn_ex_del.setEnabled(False)
        ex_row.addWidget(self._exclude_combo)
        ex_row.addWidget(btn_ex_add)
        ex_row.addWidget(btn_ex_del)
        ex_l.addLayout(ex_row)
        self._exclude_list = QListWidget()
        ex_l.addWidget(self._exclude_list)

        # Reload
        reload_row = QHBoxLayout()
        reload_lbl = QLabel(_("gui", "settings", "reload_text"))
        reload_lbl.setWordWrap(True)
        reload_row.addWidget(reload_lbl)
        btn_reload = QPushButton(_("gui", "settings", "reload"))
        btn_reload.setToolTip("Reload drops and apply settings.")
        btn_reload.clicked.connect(lambda: self._twitch.change_state(State.INVENTORY_FETCH))
        reload_row.addWidget(btn_reload)
        reload_row.addStretch(1)

        settings_layout.addWidget(general)
        settings_layout.addWidget(priority_box)
        settings_layout.addWidget(ex_box)
        settings_layout.addLayout(reload_row)
        settings_layout.addStretch(1)

        self.settings = SettingsPanel(
            twitch,
            priority_mode=self._priority_mode,
            priority_combo=self._priority_combo,
            exclude_combo=self._exclude_combo,
            priority_list=self._priority_list,
            exclude_list=self._exclude_list,
        )
        btn_pr_add.clicked.connect(self.settings.priority_add)
        btn_pr_del.clicked.connect(self.settings.priority_delete)
        btn_pr_up.clicked.connect(lambda: self.settings.priority_move(True))
        btn_pr_down.clicked.connect(lambda: self.settings.priority_move(False))
        btn_ex_add.clicked.connect(self.settings.exclude_add)
        btn_ex_del.clicked.connect(self.settings.exclude_delete)

        def _sync_priority_buttons(_: int = -1) -> None:
            row = self._priority_list.currentRow()
            has = row >= 0
            btn_pr_del.setEnabled(has)
            btn_pr_up.setEnabled(has and row > 0)
            btn_pr_down.setEnabled(has and row < self._priority_list.count() - 1)

        def _sync_exclude_buttons(_: int = -1) -> None:
            btn_ex_del.setEnabled(self._exclude_list.currentRow() >= 0)

        self._priority_list.currentRowChanged.connect(_sync_priority_buttons)
        self._exclude_list.currentRowChanged.connect(_sync_exclude_buttons)
        _sync_priority_buttons()
        _sync_exclude_buttons()
        self.tabs.addTab(settings, _("gui", "tabs", "settings"))

        # Help tab
        help_tab = QWidget()
        help_layout = QVBoxLayout(help_tab)
        help_layout.addWidget(QLabel(_("gui", "help", "how_it_works")))
        help_text = QLabel(_("gui", "help", "how_it_works_text"))
        help_text.setWordWrap(True)
        help_layout.addWidget(help_text)
        help_layout.addSpacing(10)
        help_layout.addWidget(QLabel(_("gui", "help", "getting_started")))
        help_text2 = QLabel(_("gui", "help", "getting_started_text"))
        help_text2.setWordWrap(True)
        help_layout.addWidget(help_text2)
        help_layout.addStretch(1)
        self.tabs.addTab(help_tab, _("gui", "tabs", "help"))

        self._bind_settings()

        self._window.resize(QSize(1024, 720))
        if self._twitch.settings.tray:
            self._window.hide()
        else:
            self._window.show()

        # logging handler
        self._handler = _QtOutputHandler(self)
        self._handler.setFormatter(OUTPUT_FORMATTER)
        logging.getLogger("TwitchDrops").addHandler(self._handler)

    def _apply_auto_theme(self) -> None:
        app = _qapp()
        if _is_os_dark(app):
            _apply_dark_palette(app)
        else:
            _apply_light_palette(app)

    def _bind_settings(self) -> None:
        s = self._twitch.settings

        # initialize
        self._cb_tray.setChecked(bool(s.tray))
        self._cb_tray_notifications.setChecked(bool(s.tray_notifications))
        self._cb_auto_claim.setChecked(bool(s.auto_claim))
        self._cb_auto_restart.setChecked(bool(s.auto_restart_on_error))
        self._cb_bypass_link.setChecked(bool(s.bypass_account_linking))
        self._cb_available_check.setChecked(bool(s.available_drops_check))
        self._cb_enable_badges.setChecked(bool(s.enable_badges_emotes))
        self._cb_ignore_badge.setChecked(bool(s.ignore_badge_emote))

        # bind
        self._cb_tray.stateChanged.connect(lambda v: setattr(s, "tray", bool(v)))
        self._cb_tray_notifications.stateChanged.connect(lambda v: setattr(s, "tray_notifications", bool(v)))
        self._cb_auto_claim.stateChanged.connect(lambda v: setattr(s, "auto_claim", bool(v)))
        self._cb_auto_restart.stateChanged.connect(lambda v: setattr(s, "auto_restart_on_error", bool(v)))
        self._cb_bypass_link.stateChanged.connect(lambda v: setattr(s, "bypass_account_linking", bool(v)))
        self._cb_available_check.stateChanged.connect(lambda v: setattr(s, "available_drops_check", bool(v)))
        self._cb_enable_badges.stateChanged.connect(lambda v: setattr(s, "enable_badges_emotes", bool(v)))
        self._cb_ignore_badge.stateChanged.connect(lambda v: setattr(s, "ignore_badge_emote", bool(v)))

        idx = self._priority_mode.findData(s.priority_mode)
        if idx >= 0:
            self._priority_mode.setCurrentIndex(idx)
        self._priority_mode.currentIndexChanged.connect(
            lambda _: setattr(s, "priority_mode", self._priority_mode.currentData())
        )

    @property
    def running(self) -> bool:
        return self._poll_task is not None

    @property
    def close_requested(self) -> bool:
        return self._close_requested.is_set()

    async def wait_until_closed(self) -> None:
        await self._close_requested.wait()

    async def coro_unless_closed(self, coro: Any) -> Any:
        tasks = [asyncio.ensure_future(coro), asyncio.ensure_future(self._close_requested.wait())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if self._close_requested.is_set():
            from exceptions import ExitRequest

            raise ExitRequest()
        return await next(iter(done))

    def prevent_close(self) -> None:
        self._close_requested.clear()

    def start(self) -> None:
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll())

    def stop(self) -> None:
        self.progress.stop_timer()
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None

    async def _poll(self) -> NoReturn:
        app = _qapp()
        while True:
            app.processEvents()
            await asyncio.sleep(0.02)

    def close(self, *args: Any) -> int:
        self._close_requested.set()
        self._twitch.close()
        return 0

    def close_window(self) -> None:
        self.tray.stop()
        logging.getLogger("TwitchDrops").removeHandler(self._handler)
        self._allow_window_close = True
        self._window.close()

    def unfocus(self, *args: Any) -> None:
        self.channels.clear_selection()
        if hasattr(self, "settings") and hasattr(self.settings, "clear_selection"):
            self.settings.clear_selection()

    def save(self, *, force: bool = False) -> None:
        # No-op: Tk image cache is not used in Qt UI.
        return

    def grab_attention(self, *, sound: bool = True) -> None:
        self.tray.restore()
        if sound:
            QApplication.beep()

    def set_games(self, games: set[Game]) -> None:
        self.settings.set_games(games)

    def display_drop(self, drop: TimedDrop, *, countdown: bool = True, subone: bool = False) -> None:
        self.progress.display(drop, countdown=countdown, subone=subone)
        self.tray.update_title(drop)

        # Show the reward image for the currently watched drop in the Channels table.
        url: str | None = None
        try:
            if drop.benefits:
                url = str(drop.benefits[0].image_url)
            else:
                url = str(drop.campaign.image_url)
        except Exception:
            url = None
        self._schedule_watching_drop_icon(url)

    def clear_drop(self) -> None:
        self.progress.display(None)
        self.tray.update_title(None)
        self._schedule_watching_drop_icon(None)

    def _schedule_watching_drop_icon(self, url: str | None) -> None:
        if url == self._last_drop_icon_url:
            return
        self._last_drop_icon_url = url
        if self._drop_icon_task is not None:
            self._drop_icon_task.cancel()
            self._drop_icon_task = None
        if url is None:
            self.channels.set_watching_drop_icon(None)
            return
        self._drop_icon_task = asyncio.create_task(self._load_and_set_watching_drop_icon(url))

    async def _load_and_set_watching_drop_icon(self, url: str) -> None:
        try:
            icon = await self._img_cache.get_icon(url, size=(24, 24))
        except Exception:
            icon = None
        self.channels.set_watching_drop_icon(icon)

    def print(self, message: str) -> None:
        self.output.print(message)
