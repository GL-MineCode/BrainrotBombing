"""BrainrotBombing — 在随机位置弹窗随机播放 assets 文件夹中的视频。

控制器可以设置最多同时播放的弹窗数量；每个弹窗只播放一个视频，播放结束后
自动关闭，手动关闭时也会自动补上新的弹窗以维持同时播放的数量上限。

实现要点：弹窗及其内部的 QMediaPlayer 全程**复用**，不反复销毁重建；解码器的
启动与释放也都**错开时间**逐个进行。原因是 Qt Multimedia 的 FFmpeg 后端在同一
个事件循环回合里同时拆掉多个正在播放的播放器时，会与解码/音频线程互相等待，
把主线程卡死（窗口无响应）。

命令行参数：
    python main.py                              正常启动，显示控制器
    python main.py -s                           静默启动：不显示控制器，直接开始轰炸
    python main.py -s -n 8 -d 30 -f              静默启动，8 个弹窗，每个视频最长 30 秒，弹窗乱飞
    python main.py -a D:\\视频                   指定视频目录（默认同级的 assets_light）

静默启动时会放一个托盘图标，双击它可以打开控制器，右键菜单可以停止轰炸或退出。
"""

import argparse
import os
import random
import sys
import time
from pathlib import Path

# Qt 的多媒体后端会把每个视频的解析信息打到控制台，这里静音以免刷屏
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*=false")

from PyQt6.QtCore import QRectF, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtMultimedia import QAudioOutput, QMediaMetaData, QMediaPlayer
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QSlider,
    QSpinBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

if getattr(sys, "frozen", False):
    # 打包成 exe 后 __file__ 指向解包出来的临时目录，视频得按 exe 自己所在的目录找
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent

ASSETS_DIR = APP_DIR / "assets_light"
VIDEO_SUFFIXES = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v"}

MIN_POPUP_WIDTH = 240
BASE_POPUP_WIDTH = 480
VIDEO_ASPECT = 16 / 9
ASPECT_TOLERANCE = 0.01
LOAD_TIMEOUT_MS = 8000
FLY_INTERVAL_MS = 30
FLY_SPEED_RANGE = (3.0, 8.0)
# 窗口还没显示时拿不到真实边框尺寸（标题栏高度随 DPI 变化），先用保守估计
FRAME_MARGIN_ESTIMATE = 48

MIN_PLAY_SECONDS = 0
MAX_PLAY_SECONDS = 300
DEFAULT_PLAY_SECONDS = 30
PLAY_SLIDER_STEP = 5

MAX_POPUPS = 20
DEFAULT_POPUPS = 3

# 补位启动 / 释放解码器的间隔：错开进行，避免同时操作多个播放器导致卡死
FILL_INTERVAL_MS = 150
RELEASE_INTERVAL_MS = 60
FAST_FAILURE_LIMIT = 5

_PLAYING_STATUSES = (
    QMediaPlayer.MediaStatus.BufferingMedia,
    QMediaPlayer.MediaStatus.BufferedMedia,
)


def format_duration(seconds: int) -> str:
    seconds = int(seconds)
    if seconds <= 0:
        return "不限制"
    minutes, secs = divmod(seconds, 60)
    if minutes:
        return f"{minutes} 分 {secs:02d} 秒"
    return f"{secs} 秒"


def ensure_std_streams() -> None:
    """用 pythonw 启动或以窗口模式打包成 exe 时 stdout / stderr 是 None。

    这时随便一句 print（连同 argparse 的 --help、异常的 traceback）都会因为往 None
    写东西而直接报错，所以先把它们指到空设备上，直接丢掉这些输出。
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def find_videos(directory: Path) -> list[Path]:
    """返回目录下所有可播放的视频文件（按名称排序，结果稳定）。"""
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
    )


class VideoPopup(QWidget):
    """只播放一个视频的弹窗，播放结束或出错后发出 finished 并等待被复用。"""

    finished = pyqtSignal(object)

    def __init__(self, always_on_top: bool = True) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.video_path: Path | None = None
        self._finished_reported = True
        self._failed = False
        self._aspect = VIDEO_ASPECT
        self._size_factor = 1.0
        self._velocity = (0.0, 0.0)
        self._flying = False
        self._max_duration = DEFAULT_PLAY_SECONDS
        self._play_started_at: float | None = None

        screen = self.screen() or QGuiApplication.primaryScreen()
        self._area = screen.availableGeometry() if screen else None

        self.setWindowTitle("BrainrotBombing")

        self._video_widget = QVideoWidget(self)
        self._video_widget.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self._video_widget.setStyleSheet("background-color: black;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._video_widget)

        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.setVideoOutput(self._video_widget)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.playbackStateChanged.connect(self._on_playback_state)
        self._player.metaDataChanged.connect(self._apply_video_aspect)
        self._player.errorOccurred.connect(self._on_error)

        # 防止视频卡在加载状态而永远不关窗
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.setInterval(LOAD_TIMEOUT_MS)
        self._watchdog.timeout.connect(self._on_load_timeout)

        # 单个视频最长播放时间，到点就收工，0 表示不限制
        self._limit_timer = QTimer(self)
        self._limit_timer.setSingleShot(True)
        self._limit_timer.timeout.connect(self.finish)

        # 乱飞：每个计时周期把弹窗移动一小段距离，碰到屏幕边缘就反弹
        self._fly_timer = QTimer(self)
        self._fly_timer.setInterval(FLY_INTERVAL_MS)
        self._fly_timer.timeout.connect(self._fly_step)

        self._resize_to_video()
        self._random_position()
        if always_on_top:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

    @property
    def was_successful(self) -> bool:
        """视频是否真的播放过（用于判断播放器/解码器故障）。"""
        return not self._failed

    @property
    def playing(self) -> bool:
        """是否正在播放（已开始且尚未结束/释放）。"""
        return not self._finished_reported

    # ------------------------------------------------------------ 播放生命周期

    def play(
        self,
        video_path: Path,
        max_duration: int = DEFAULT_PLAY_SECONDS,
        flying: bool = False,
        always_on_top: bool = True,
    ) -> None:
        """复用本弹窗播放一个新视频。"""
        self.video_path = video_path
        self._failed = False
        self._finished_reported = False
        self._aspect = VIDEO_ASPECT
        self._size_factor = random.uniform(0.6, 1.0)
        self._max_duration = max_duration
        self.setWindowTitle(video_path.stem)

        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, always_on_top)
        self._resize_to_video()
        self._random_position()
        self.show()
        # 显示后才能拿到真实边框尺寸，重算尺寸并校正位置，保证整窗在屏幕内
        self._resize_to_video()
        self._clamp_position()
        self.set_flying(flying)

        self._play_started_at = time.monotonic()
        self._player.setSource(QUrl.fromLocalFile(str(video_path)))
        self._player.play()
        self._watchdog.start()
        self._restart_limit_timer()

    def suspend(self) -> None:
        """立刻静音、停掉定时器并隐藏窗口（不拆解码器，交给错开的 release）。"""
        self._watchdog.stop()
        self._fly_timer.stop()
        self._limit_timer.stop()
        self._player.pause()
        self.hide()

    def release(self) -> None:
        """释放解码器并隐藏窗口；弹窗对象本身保留下来等待下次复用。"""
        self.suspend()
        self._player.setSource(QUrl())
        self.video_path = None
        self._finished_reported = True

    def finish(self) -> None:
        """结束当前视频（播完、超时、被关闭或出错），通知控制器可以补位。"""
        if self._finished_reported:
            return
        self._finished_reported = True
        self._watchdog.stop()
        self._fly_timer.stop()
        self._limit_timer.stop()
        self._player.stop()
        self.finished.emit(self)

    # ------------------------------------------------------------ 尺寸与位置

    def _frame_extra(self) -> tuple[int, int]:
        """窗口边框（标题栏、边框）比客户区多出的宽高。"""
        if self.isVisible():
            frame = self.frameGeometry()
            return max(0, frame.width() - self.width()), max(0, frame.height() - self.height())
        return 0, FRAME_MARGIN_ESTIMATE

    def _resize_to_video(self) -> None:
        """按当前视频宽高比随机取尺寸，含标题栏在内不超过半个屏幕。"""
        if self._area is None:
            self.resize(BASE_POPUP_WIDTH, int(BASE_POPUP_WIDTH / self._aspect))
            return
        extra_width, extra_height = self._frame_extra()
        half_width = max(MIN_POPUP_WIDTH, self._area.width() // 2 - extra_width)
        half_height = max(1, self._area.height() // 2 - extra_height)

        width = max(
            MIN_POPUP_WIDTH,
            int(min(BASE_POPUP_WIDTH, half_width) * self._size_factor),
        )
        height = max(1, round(width / self._aspect))
        if height > half_height:
            height = half_height
            width = max(1, round(height * self._aspect))
        width = max(1, min(width, half_width))
        height = max(1, min(height, half_height))
        self.resize(width, height)

    def _position_limits(self) -> tuple[int, int, int, int]:
        """返回 pos() 允许的 (min_x, min_y, max_x, max_y)。

        顶层窗口的 pos() 是包含边框（标题栏）的左上角，所以要按边框尺寸限制，
        这样弹窗的标题栏与关闭按钮才始终留在屏幕内。
        """
        if self._area is None:
            return 0, 0, 0, 0
        frame = self.frameGeometry()
        extra_width = max(0, frame.width() - self.width())
        extra_height = max(0, frame.height() - self.height())
        if not self.isVisible():
            extra_width = max(extra_width, FRAME_MARGIN_ESTIMATE)
            extra_height = max(extra_height, FRAME_MARGIN_ESTIMATE)
        min_x, min_y = self._area.left(), self._area.top()
        return (
            min_x,
            min_y,
            max(min_x, self._area.right() - self.width() - extra_width + 1),
            max(min_y, self._area.bottom() - self.height() - extra_height + 1),
        )

    def _random_position(self) -> None:
        """把弹窗随机摆放在屏幕内（完整可见）。"""
        if self._area is None:
            return
        min_x, min_y, max_x, max_y = self._position_limits()
        self.move(random.randint(min_x, max_x), random.randint(min_y, max_y))

    def _clamp_position(self) -> None:
        if self._area is None:
            return
        min_x, min_y, max_x, max_y = self._position_limits()
        self.move(
            min(max(self.x(), min_x), max_x),
            min(max(self.y(), min_y), max_y),
        )

    def _apply_video_aspect(self) -> None:
        """视频元数据加载后，把弹窗比例改成视频本身的比例。"""
        size = self._player.metaData().value(QMediaMetaData.Key.Resolution)
        if not isinstance(size, QSize) or size.width() <= 0 or size.height() <= 0:
            return
        aspect = size.width() / size.height()
        if abs(aspect - self._aspect) < ASPECT_TOLERANCE:
            return
        self._aspect = aspect
        self._resize_to_video()
        self._clamp_position()

    # ------------------------------------------------------------ 乱飞

    def set_flying(self, enabled: bool) -> None:
        self._flying = enabled
        if not enabled:
            self._fly_timer.stop()
            return
        sign_x = random.choice((-1.0, 1.0))
        sign_y = random.choice((-1.0, 1.0))
        self._velocity = (
            sign_x * random.uniform(*FLY_SPEED_RANGE),
            sign_y * random.uniform(*FLY_SPEED_RANGE),
        )
        self._fly_timer.start()

    def _fly_step(self) -> None:
        if self._area is None:
            return
        min_x, min_y, max_x, max_y = self._position_limits()
        vx, vy = self._velocity
        x = self.x() + round(vx)
        y = self.y() + round(vy)
        if x <= min_x or x >= max_x:
            vx = -vx
            x = min(max(x, min_x), max_x)
        if y <= min_y or y >= max_y:
            vy = -vy
            y = min(max(y, min_y), max_y)
        self._velocity = (vx, vy)
        self.move(x, y)

    # ------------------------------------------------------------ 播放时长限制

    def set_max_duration(self, seconds: int) -> None:
        """更新单个视频的最长播放时间，对已开始播放的弹窗同样生效；0 表示不限制。"""
        self._max_duration = seconds
        self._restart_limit_timer()

    def _elapsed(self) -> float:
        if self._play_started_at is None:
            return 0.0
        return time.monotonic() - self._play_started_at

    def _restart_limit_timer(self) -> None:
        self._limit_timer.stop()
        if self._max_duration <= 0 or self._play_started_at is None:
            return
        remaining = self._max_duration - self._elapsed()
        if remaining <= 0:
            self.finish()
            return
        self._limit_timer.start(int(remaining * 1000))

    # ------------------------------------------------------------ 播放器回调

    def _on_media_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.finish()
            return
        if status in _PLAYING_STATUSES:
            self._watchdog.stop()

    def _on_playback_state(self, state: QMediaPlayer.PlaybackState) -> None:
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._watchdog.stop()

    def _on_error(self, error: QMediaPlayer.Error, message: str) -> None:
        if error == QMediaPlayer.Error.NoError:
            return
        self._failed = True
        name = self.video_path.name if self.video_path else "?"
        print(f"[BrainrotBombing] 无法播放 {name}: {message}")
        self.finish()

    def _on_load_timeout(self) -> None:
        self._failed = True
        name = self.video_path.name if self.video_path else "?"
        print(f"[BrainrotBombing] 加载超时 {name}")
        self.finish()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名约定
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名约定
        super().closeEvent(event)
        self.finish()


class ControllerWindow(QMainWindow):
    """控制器：设置同时播放上限、启动/停止轰炸。"""

    def __init__(
        self,
        videos: list[Path],
        max_playing: int = DEFAULT_POPUPS,
        max_duration: int = DEFAULT_PLAY_SECONDS,
        flying: bool = False,
        assets_dir: Path = ASSETS_DIR,
    ) -> None:
        super().__init__()
        self._videos = videos
        self._assets_dir = assets_dir
        self._pool: list[VideoPopup] = []  # 全部弹窗实例（长期复用，不销毁）
        self._active: list[VideoPopup] = []  # 正在播放的弹窗
        self._idle: list[VideoPopup] = []  # 空闲可复用的弹窗
        self._pending_release: list[VideoPopup] = []  # 等待逐个释放解码器的弹窗
        self._play_counts: dict[Path, int] = {}
        self._running = False
        self._fast_failures = 0

        self.setWindowTitle("BrainrotBombing 控制器")
        self.setFixedWidth(380)

        self._max_spin = QSpinBox()
        self._max_spin.setRange(1, MAX_POPUPS)
        self._max_spin.setValue(max_playing)
        self._max_spin.setSuffix(" 个")
        self._max_spin.valueChanged.connect(self._on_max_changed)

        self._top_check = QCheckBox("弹窗置顶显示")
        self._top_check.setChecked(True)

        self._fly_check = QCheckBox("弹窗乱飞")
        self._fly_check.setChecked(flying)
        self._fly_check.toggled.connect(self._on_fly_toggled)

        self._duration_slider = QSlider(Qt.Orientation.Horizontal)
        self._duration_slider.setRange(MIN_PLAY_SECONDS, MAX_PLAY_SECONDS)
        self._duration_slider.setSingleStep(PLAY_SLIDER_STEP)
        self._duration_slider.setPageStep(30)
        self._duration_slider.setValue(max_duration)
        self._duration_slider.valueChanged.connect(self._on_duration_changed)

        self._duration_label = QLabel()

        warning = QLabel("弹窗数量太高可能导致死机！")
        warning.setStyleSheet("color: #c62828; font-weight: bold;")
        warning.setWordWrap(True)

        self._toggle_button = QPushButton("开始轰炸")
        self._toggle_button.clicked.connect(self.toggle)

        self._status_label = QLabel()
        self._status_label.setWordWrap(True)

        settings = QGroupBox("设置")
        settings_layout = QVBoxLayout(settings)
        max_row = QHBoxLayout()
        max_row.addWidget(QLabel("最多同时播放："))
        max_row.addWidget(self._max_spin)
        max_row.addStretch(1)
        settings_layout.addLayout(max_row)
        settings_layout.addWidget(warning)
        settings_layout.addWidget(self._duration_label)
        settings_layout.addWidget(self._duration_slider)
        settings_layout.addWidget(self._top_check)
        settings_layout.addWidget(self._fly_check)

        note = QLabel("每个弹窗播放一个视频，播放完毕、超过最长播放时间或手动关闭后会自动补上新的弹窗。按 Esc 可关闭弹窗。")
        note.setWordWrap(True)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(settings)
        layout.addWidget(self._toggle_button)
        layout.addWidget(self._status_label)
        layout.addWidget(note)
        self.setCentralWidget(central)

        # 补位与释放在计时器里逐个进行，避免同一回合内批量操作播放器
        self._spawn_timer = QTimer(self)
        self._spawn_timer.setSingleShot(True)
        self._spawn_timer.timeout.connect(self._fill_popups)

        self._release_timer = QTimer(self)
        self._release_timer.setSingleShot(True)
        self._release_timer.timeout.connect(self._release_next)

        self._update_duration_label()
        self._update_status()

    # ------------------------------------------------------------------ 状态

    @property
    def max_playing(self) -> int:
        return self._max_spin.value()

    @property
    def running(self) -> bool:
        return self._running

    def _update_status(self) -> None:
        if not self._videos:
            self._status_label.setText(f"未在 {self._assets_dir} 中找到视频文件，请把视频放进该文件夹。")
            self._toggle_button.setEnabled(False)
            return
        state = "运行中" if self._running else "已停止"
        played = sum(self._play_counts.values())
        self._status_label.setText(
            f"状态：{state}\n"
            f"正在播放：{len(self._active)} / {self.max_playing} 个\n"
            f"可用视频：{len(self._videos)} 个\n"
            f"本次已播放：{played} 个，覆盖 {len(self._play_counts)} / {len(self._videos)} 种"
        )

    # ------------------------------------------------------------------ 控制

    def shutdown(self) -> None:
        """停止轰炸，并把剩下的解码器逐个释放掉。

        退出前必须这样收尾，否则一次性拆掉多个播放器会和解码/音频线程互相等待，
        把进程卡死（见模块开头的说明）。控制器关闭与托盘退出都走这里。
        """
        self.stop()
        self._release_timer.stop()
        while self._pending_release:
            self._release_next()
            QApplication.processEvents()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名约定
        self.shutdown()
        super().closeEvent(event)

    def show_controller(self) -> None:
        """把控制器窗口显示到最前面（静默模式下由托盘图标调用）。"""
        self.show()
        self.raise_()
        self.activateWindow()

    def toggle(self) -> None:
        """开始 / 停止轰炸（控制器按钮与托盘菜单共用）。"""
        if self._running:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        if self._running or not self._videos:
            return
        self._running = True
        self._fast_failures = 0
        self._toggle_button.setText("停止轰炸")
        self._fill_popups()

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._spawn_timer.stop()
        self._toggle_button.setText("开始轰炸")

        finishing = list(self._active)
        self._active.clear()
        for popup in finishing:
            popup.suspend()  # 先静音、隐藏，界面立刻响应
        for popup in finishing:
            self._queue_release(popup)  # 解码器随后逐个释放
        self._update_status()

    def _on_max_changed(self, value: int) -> None:
        if self._running:
            while len(self._active) > value:
                self._active[-1].finish()
            self._fill_popups()
        self._update_status()

    def _on_fly_toggled(self, checked: bool) -> None:
        for popup in list(self._active):
            popup.set_flying(checked)

    def _on_duration_changed(self, value: int) -> None:
        self._update_duration_label()
        # 复制一份：调整时长可能立刻结束某些弹窗，链表会在迭代中被修改
        for popup in list(self._active):
            popup.set_max_duration(value)

    def _update_duration_label(self) -> None:
        self._duration_label.setText(
            f"单个视频最长播放：{format_duration(self._duration_slider.value())}"
        )

    # ------------------------------------------------------------------ 弹窗

    def _pick_video(self) -> Path:
        """随机挑一个视频：优先从未播放次数最少的那批里选，避免老是重复。

        正在播放的视频会被排除（视频种类不够时才允许重复）。
        """
        playing = {popup.video_path for popup in self._active}
        candidates = [video for video in self._videos if video not in playing]
        if not candidates:
            candidates = list(self._videos)
        fewest = min(self._play_counts.get(video, 0) for video in candidates)
        least_played = [video for video in candidates if self._play_counts.get(video, 0) == fewest]
        chosen = random.choice(least_played)
        self._play_counts[chosen] = self._play_counts.get(chosen, 0) + 1
        return chosen

    def _take_idle_popup(self) -> VideoPopup:
        """取一个空闲弹窗复用；池子不够时才新建（运行中只在调高上限时发生）。"""
        if self._idle:
            return self._idle.pop()
        popup = VideoPopup(self._top_check.isChecked())
        popup.finished.connect(self._on_popup_finished)
        self._pool.append(popup)
        return popup

    def _fill_popups(self) -> None:
        """把播放中的弹窗补齐到上限；每次只补一个，避免同时启动多个播放器。"""
        if not self._running:
            return
        if len(self._active) >= self.max_playing:
            self._update_status()
            return

        popup = self._take_idle_popup()
        popup.play(
            self._pick_video(),
            max_duration=self._duration_slider.value(),
            flying=self._fly_check.isChecked(),
            always_on_top=self._top_check.isChecked(),
        )
        self._active.append(popup)
        self._update_status()

        if len(self._active) < self.max_playing:
            self._spawn_timer.start(FILL_INTERVAL_MS)

    def _on_popup_finished(self, popup: VideoPopup) -> None:
        if popup in self._active:
            self._active.remove(popup)
        self._queue_release(popup)

        if not self._running:
            self._update_status()
            return

        if popup.was_successful:
            self._fast_failures = 0
        else:
            self._fast_failures += 1
            if self._fast_failures >= FAST_FAILURE_LIMIT:
                # 连续多个视频无法播放，避免无限重试
                self._running = False
                self._spawn_timer.stop()
                self._toggle_button.setText("开始轰炸")
                self._status_label.setText("连续多个视频无法播放，已停止。请检查视频文件或解码器。")
                return

        self._spawn_timer.start(FILL_INTERVAL_MS)
        self._update_status()

    # ------------------------------------------------------ 解码器逐个释放

    def _queue_release(self, popup: VideoPopup) -> None:
        if popup in self._pending_release:
            return
        self._pending_release.append(popup)
        if not self._release_timer.isActive():
            self._release_timer.start(RELEASE_INTERVAL_MS)

    def _release_next(self) -> None:
        if not self._pending_release:
            return
        popup = self._pending_release.pop(0)
        popup.release()
        if popup not in self._idle:
            self._idle.append(popup)
        if self._pending_release:
            self._release_timer.start(RELEASE_INTERVAL_MS)
        self._update_status()


def create_app_icon() -> QIcon:
    """画一个简单的炸弹图标（省得额外带一个图片文件）。"""
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#212121"))
    painter.drawEllipse(QRectF(8.0, 20.0, 48.0, 42.0))
    pen = QPen(QColor("#ff7043"), 8.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawLine(38, 22, 54, 6)
    painter.end()
    return QIcon(pixmap)


def create_tray_icon(app: QApplication, controller: ControllerWindow) -> QSystemTrayIcon:
    """静默模式下的托盘图标：双击显示控制器，右键菜单可启停轰炸、退出程序。"""
    tray = QSystemTrayIcon(create_app_icon(), app)
    tray.setToolTip("BrainrotBombing")

    menu = QMenu()
    show_action = menu.addAction("显示控制器")
    show_action.triggered.connect(lambda: controller.show_controller())
    toggle_action = menu.addAction("开始轰炸")
    toggle_action.triggered.connect(lambda: controller.toggle())
    menu.addSeparator()
    quit_action = menu.addAction("退出")
    quit_action.triggered.connect(lambda: quit_from_tray(app, controller))

    def sync_toggle_text() -> None:
        toggle_action.setText("停止轰炸" if controller.running else "开始轰炸")

    menu.aboutToShow.connect(sync_toggle_text)
    sync_toggle_text()

    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: controller.show_controller()
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick
        else None
    )
    tray.show()
    return tray


def quit_from_tray(app: QApplication, controller: ControllerWindow) -> None:
    """托盘退出：同样要逐个释放解码器，不能直接 app.quit()。"""
    controller.shutdown()
    app.quit()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="BrainrotBombing —— 在随机位置弹窗随机播放 assets 文件夹中的视频。",
    )
    parser.add_argument(
        "-s",
        "--silent",
        action="store_true",
        help="静默启动：不显示控制器窗口并立即开始轰炸，用托盘图标退出",
    )
    parser.add_argument(
        "-n",
        "--count",
        type=int,
        default=DEFAULT_POPUPS,
        help=f"最多同时播放的弹窗数量（1 ~ {MAX_POPUPS}，默认 {DEFAULT_POPUPS}）",
    )
    parser.add_argument(
        "-d",
        "--duration",
        type=int,
        default=DEFAULT_PLAY_SECONDS,
        help=f"单个视频最长播放秒数（0 ~ {MAX_PLAY_SECONDS}，0 表示不限制，默认 {DEFAULT_PLAY_SECONDS}）",
    )
    parser.add_argument(
        "-f",
        "--fly",
        action="store_true",
        help="弹窗乱飞（一边播放一边在屏幕上弹来弹去，碰到边缘会反弹）",
    )
    parser.add_argument(
        "-a",
        "--assets",
        type=Path,
        default=ASSETS_DIR,
        help=f"视频所在目录（默认 {ASSETS_DIR.name}，即本程序同级的 {ASSETS_DIR.name} 文件夹）",
    )
    args = parser.parse_args(argv)
    # 超出范围的取值直接夹到边界，免得静默启动时因为参数写错而什么都没发生
    args.count = max(1, min(MAX_POPUPS, args.count))
    args.duration = max(MIN_PLAY_SECONDS, min(MAX_PLAY_SECONDS, args.duration))
    args.assets = args.assets.expanduser()
    return args


def main(argv: list[str] | None = None) -> int:
    ensure_std_streams()
    argv = list(sys.argv if argv is None else argv)
    args = parse_args(argv[1:])

    app = QApplication(argv)
    videos = find_videos(args.assets)
    controller = ControllerWindow(
        videos,
        max_playing=args.count,
        max_duration=args.duration,
        flying=args.fly,
        assets_dir=args.assets,
    )

    if not args.silent:
        controller.show()
        return app.exec()

    if QSystemTrayIcon.isSystemTrayAvailable():
        # 控制器窗口不显示，所以最后关掉弹窗时不能连带退出程序
        app.setQuitOnLastWindowClosed(False)
        tray = create_tray_icon(app, controller)
        if not videos:
            tray.showMessage("BrainrotBombing", f"未在 {args.assets} 中找到视频文件。")
    else:
        # 没有托盘图标就没有退出的入口，退回显示控制器让用户自己关
        controller.show()
    controller.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
