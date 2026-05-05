from shutil import which
import io
import subprocess
import sys
import os
import re
import tempfile
import threading
import queue
from PyQt6 import QtCore, QtGui
from PyQt6.QtCore import Qt, pyqtSlot, pyqtSignal, QThread, QTimer, QEvent
from PyQt6.QtGui import QGuiApplication, QAction
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QMainWindow, QApplication, QMessageBox, QVBoxLayout, QWidget, \
    QPushButton, QTextEdit, QFormLayout, QHBoxLayout, QDoubleSpinBox, QLabel, QRadioButton, \
    QSystemTrayIcon, QMenu
from pynput.mouse import Controller
from pynput import keyboard

from PIL import ImageGrab, Image, ImageEnhance
import numpy as np
from screeninfo import get_monitors
from pix2tex import cli
from pix2tex.utils import in_model_path
from latex2sympy2 import latex2sympy

import pix2tex.resources.resources

# 全局热键监听线程
hotkey_queue = queue.Queue()

ACCEPTED_IMAGE_SUFFIX = ['png', 'jpg', 'jpeg']

def to_sympy(latex):
    normalized = re.sub(r'operatorname\*{(\w+)}', '\g<1>', latex)
    sympy_expr = latex2sympy(f'${normalized}$')
    return sympy_expr


class WebView(QWebEngineView):
    def __init__(self, app) -> None:
        super().__init__()
        self.setAcceptDrops(True)
        self._app = app

    def dragEnterEvent(self, event):
        if event.mimeData().urls():
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        self._app.returnFromMimeData(urls)

class App(QMainWindow):
    isProcessing = False

    def __init__(self, args=None):
        super().__init__()
        self.args = args
        self.model = None
        self.model_loaded = False
        self.tray_icon = None
        self.hotkey_thread = None
        self.running = True
        self.initUI()
        self.initTray()
        self.snipWidget = SnipWidget(self)
        self.show()
        # 窗口显示后异步加载模型，避免启动时卡顿
        QTimer.singleShot(100, self.load_model)
        # 启动全局热键监听线程
        self.startHotkeyListener()

    def initTray(self):
        """初始化系统托盘"""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(QtGui.QIcon(':/icons/icon.svg'))

        # 创建托盘菜单
        tray_menu = QMenu()
        show_action = QAction("显示窗口", self)
        show_action.triggered.connect(self.showFromTray)
        quit_action = QAction("完全退出", self)
        quit_action.triggered.connect(self.quitApp)
        tray_menu.addAction(show_action)
        tray_menu.addAction(quit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.onTrayActivated)
        self.tray_icon.show()

    def onTrayActivated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.showFromTray()

    def showFromTray(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def quitApp(self):
        """完全退出应用"""
        self.running = False
        if self.tray_icon:
            self.tray_icon.hide()
        QApplication.quit()

    def closeEvent(self, event):
        """关闭窗口时最小化到托盘"""
        if self.running:
            event.ignore()
            self.hide()
        else:
            event.accept()

    def startHotkeyListener(self):
        """启动全局热键监听线程"""
        def hotkey_listener():
            pressed_keys = set()

            def on_press(key):
                try:
                    if key == keyboard.Key.alt_l or key == keyboard.Key.alt_r:
                        pressed_keys.add('alt')
                    elif hasattr(key, 'char') and key.char == 's':
                        pressed_keys.add('s')
                    elif hasattr(key, 'vk') and key.vk == ord('S'):
                        pressed_keys.add('s')
                    elif key == keyboard.Key.esc:
                        pressed_keys.add('esc')

                    if 'alt' in pressed_keys and 's' in pressed_keys:
                        hotkey_queue.put(('snip', None))
                    if 'esc' in pressed_keys and self.snipWidget.isSnipping:
                        hotkey_queue.put(('cancel_snip', None))
                except Exception as e:
                    print(f"Hotkey press error: {e}")

            def on_release(key):
                try:
                    if key == keyboard.Key.alt_l or key == keyboard.Key.alt_r:
                        pressed_keys.discard('alt')
                    elif hasattr(key, 'char') and key.char == 's':
                        pressed_keys.discard('s')
                    elif hasattr(key, 'vk') and key.vk == ord('S'):
                        pressed_keys.discard('s')
                    elif key == keyboard.Key.esc:
                        pressed_keys.discard('esc')
                except Exception as e:
                    print(f"Hotkey release error: {e}")

            try:
                with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
                    listener.join()
            except Exception as e:
                print(f"Hotkey listener error: {e}")

        self.hotkey_thread = threading.Thread(target=hotkey_listener, daemon=True)
        self.hotkey_thread.start()
        # 定期检查热键队列
        self.hotkey_timer = QTimer(self)
        self.hotkey_timer.timeout.connect(self.checkHotkeyQueue)
        self.hotkey_timer.start(100)

    def checkHotkeyQueue(self):
        """检查热键队列"""
        try:
            while True:
                msg = hotkey_queue.get_nowait()
                if msg[0] == 'snip':
                    self.onSnipHotkey()
                elif msg[0] == 'cancel_snip':
                    self.cancelSnip()
        except queue.Empty:
            pass

    def cancelSnip(self):
        """取消当前截图"""
        if self.snipWidget.isSnipping:
            self.snipWidget.cancelSnip()

    def onSnipHotkey(self):
        """热键触发的截图"""
        # 如果窗口隐藏，先显示
        if not self.isVisible():
            self.showFromTray()
        # 使用现有的截图逻辑
        self.onClick()

    def load_model(self):
        """异步加载模型"""
        self.toggleProcessing(True)
        try:
            self.model = cli.LatexOCR(self.args)
            self.model_loaded = True
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.webView.setHtml("<html><body><center><h2 style='color:red'>模型加载失败</h2></center></body></html>")
            QMessageBox.critical(self, "错误", f"模型加载失败: {e}")
        finally:
            self.toggleProcessing(False)

    def initUI(self):
        self.setWindowTitle("LaTeX OCR")
        QApplication.setWindowIcon(QtGui.QIcon(':/icons/icon.svg'))
        self.left = 300
        self.top = 300
        self.width = 500
        self.height = 300
        self.setGeometry(self.left, self.top, self.width, self.height)
        self.format_type = 'LaTeX-$'
        self.raw_prediction = ''

        # Create LaTeX display
        self.webView = WebView(self)
        self.webView.setHtml("")
        self.webView.setMinimumHeight(80)

        # Create textbox
        self.textbox = QTextEdit(self)
        # self.textbox.textChanged.connect(self.displayPrediction)
        self.textbox.textChanged.connect(self.onTextboxChange)
        self.textbox.setMinimumHeight(40)
        self.format_textbox = QTextEdit(self)
        # self.textbox.textChanged.connect(self.displayPrediction)
        self.format_textbox.textChanged.connect(self.onFormatTextboxChange)
        self.format_textbox.setMinimumHeight(40)

        # format types
        format_types = QHBoxLayout()
        self.format_label = QLabel('Format:', self)
        self.format_type0 = QRadioButton('Raw', self)
        self.format_type0.toggled.connect(self.onFormatChange)
        self.format_type1 = QRadioButton('LaTeX-$', self)
        self.format_type1.setChecked(True)
        self.format_type1.toggled.connect(self.onFormatChange)
        self.format_type2 = QRadioButton('LaTeX-$$', self)
        self.format_type2.toggled.connect(self.onFormatChange)
        self.format_type3 = QRadioButton('Sympy', self)
        self.format_type3.toggled.connect(self.onFormatChange)
        format_types.addWidget(self.format_label)
        format_types.addWidget(self.format_type0)
        format_types.addWidget(self.format_type1)
        format_types.addWidget(self.format_type2)
        format_types.addWidget(self.format_type3)

        # error output
        self.error = QTextEdit(self)
        self.error.setReadOnly(True)
        self.error.setTextColor(Qt.GlobalColor.red)
        self.error.setMinimumHeight(12)

        # Create temperature text input
        self.tempField = QDoubleSpinBox(self)
        self.tempField.setValue(self.args.temperature)
        self.tempField.setRange(0, 1)
        self.tempField.setSingleStep(0.1)

        # Create snip button
        if sys.platform == "darwin":
            self.snipButton = QPushButton('Snip [Option+S]', self)
            self.snipButton.clicked.connect(self.onClick)
        else:
            self.snipButton = QPushButton('Snip [Alt+S]', self)
            self.snipButton.clicked.connect(self.onClick)

        self.shortcut = QtGui.QShortcut(QtGui.QKeySequence('Alt+S'), self)
        self.shortcut.activated.connect(self.onClick)

        # Create retry button
        self.retryButton = QPushButton('Retry', self)
        self.retryButton.setEnabled(False)
        self.retryButton.clicked.connect(self.returnSnip)

        # Create copy button
        self.copyButton = QPushButton('Copy to Clipboard', self)
        self.copyButton.clicked.connect(self.copyToClipboard)

        # Create layout
        centralWidget = QWidget()
        centralWidget.setMinimumWidth(200)
        self.setCentralWidget(centralWidget)

        lay = QVBoxLayout(centralWidget)
        lay.addWidget(self.webView, stretch=4)
        lay.addWidget(self.textbox, stretch=2)
        lay.addLayout(format_types)
        lay.addWidget(self.format_textbox, stretch=2)
        lay.addWidget(self.error, stretch=1)
        buttons = QHBoxLayout()
        buttons.addWidget(self.snipButton)
        buttons.addWidget(self.retryButton)
        lay.addLayout(buttons)
        lay.addWidget(self.copyButton)  # 长长的复制按钮
        settings = QFormLayout()
        settings.addRow('Temperature:', self.tempField)
        lay.addLayout(settings)

        self.installEventFilter(self)

    def toggleProcessing(self, value=None):
        if value is None:
            self.isProcessing = not self.isProcessing
        else:
            self.isProcessing = value
        if self.isProcessing:
            text = 'Interrupt'
            func = self.interrupt
        else:
            if sys.platform == "darwin":
                text = 'Snip [Option+S]'
            else:
                text = 'Snip [Alt+S]'
            func = self.onClick
            self.retryButton.setEnabled(True)
        self.shortcut.setEnabled(not self.isProcessing)
        self.snipButton.setText(text)
        self.snipButton.clicked.disconnect()
        self.snipButton.clicked.connect(func)
        self.displayPrediction()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.KeyRelease:
            if event.key() == Qt.Key.Key_V and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
                clipboard = QApplication.clipboard()
                img = clipboard.image()
                if not img.isNull():
                    self.returnSnip(Image.fromqimage(img))
                else:
                    self.returnFromMimeData(clipboard.mimeData().urls())

        return super().eventFilter(obj, event)

    @pyqtSlot()
    def onClick(self):
        self.close()
        if os.environ.get('SCREENSHOT_TOOL') == "gnome-screenshot":
            self.snip_using_gnome_screenshot()
        elif os.environ.get('SCREENSHOT_TOOL') == "spectacle":
            self.snip_using_spectacle()
        elif os.environ.get('SCREENSHOT_TOOL') == "grim":
            self.snip_using_grim()
        elif os.environ.get('SCREENSHOT_TOOL') == "pil":
            self.snipWidget.snip()
        elif which('gnome-screenshot'):
            self.snip_using_gnome_screenshot()
        elif which('grim') and which('slurp'):
            self.snip_using_grim()
        else:
            self.snipWidget.snip()

    @pyqtSlot()
    def interrupt(self):
        if hasattr(self, 'thread'):
            self.thread.terminate()
            self.thread.wait()
            self.toggleProcessing(False)

    def snip_using_gnome_screenshot(self):
        try:
            with tempfile.NamedTemporaryFile() as tmp:
                subprocess.run(["gnome-screenshot", "--area", f"--file={tmp.name}"])
                # Use `tmp.name` instead of `tmp.file` due to compatability issues between Pillow and tempfile
                self.returnSnip(Image.open(tmp.name))
        except:
            print(f"Failed to load saved screenshot! Did you cancel the screenshot?")
            print("If you don't have gnome-screenshot installed, please install it.")
            self.returnSnip()

    def snip_using_spectacle(self):
        try:
            with tempfile.NamedTemporaryFile() as tmp:
                subprocess.run(["spectacle", "-r", "-b", "-n", "-o", f"{tmp.name}"])
                self.returnSnip(Image.open(tmp.name))
        except:
            print(f"Failed to load saved screenshot! Did you cancel the screenshot?")
            print("If you don't have spectacle installed, please install it.")
            self.returnSnip()

    def snip_using_grim(self):
        try:
            p = subprocess.run('slurp',
                               check=True,
                               capture_output=True,
                               text=True)
            geometry = p.stdout.strip()

            p = subprocess.run(['grim', '-g', geometry, '-'],
                               check=True,
                               capture_output=True)
            self.returnSnip(Image.open(io.BytesIO(p.stdout)))
        except:
            print(f"Failed to load saved screenshot! Did you cancel the screenshot?")
            print("If you don't have slurp and grim installed, please install them.")
            self.returnSnip()

    def returnFromMimeData(self, urls):
        if not urls or not urls[0]:
            return

        image_url = urls[0]
        if image_url and image_url.scheme() == 'file' and image_url.fileName().split('.')[-1] in ACCEPTED_IMAGE_SUFFIX:
            image_path = image_url.toLocalFile()
            return self.returnSnip(Image.open(image_path))

    def returnSnip(self, img=None):
        if not self.model_loaded:
            QMessageBox.warning(self, "提示", "模型正在加载中，请稍候...")
            return

        self.toggleProcessing(True)
        self.retryButton.setEnabled(False)

        if img:
            width, height = img.size
            if width <= 0 or height <= 0:
                self.toggleProcessing(False)
                self.retryButton.setEnabled(True)
                self.show()
                return

            if width < 100 or height < 100: # too small size will make OCR wrong
                scale_factor = max(100 / width, 100 / height)
                new_width = int(width * scale_factor)
                new_height = int(height * scale_factor)
                img = img.resize((new_width,new_height), Image.Resampling.LANCZOS)
                contrast = ImageEnhance.Contrast(img)
                img = contrast.enhance(1.5)
                sharpness = ImageEnhance.Sharpness(img)
                img = sharpness.enhance(1.5)

        self.show()
        try:
            self.model.args.temperature = self.tempField.value()
            if self.model.args.temperature == 0:
                self.model.args.temperature = 1e-8
        except:
            pass
        # Run the model in a separate thread
        self.thread = ModelThread(img=img, model=self.model)
        self.thread.finished.connect(self.returnPrediction)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def returnPrediction(self, result):
        self.toggleProcessing(False)
        success, prediction = result["success"], result["prediction"]

        if success:
            self.raw_prediction = prediction
            self.textbox.setText(prediction)
            self.format_textbox.setText(self.formatPrediction(prediction))
            self.displayPrediction(prediction)
            self.retryButton.setEnabled(True)
        else:
            self.webView.setHtml("")
            msg = QMessageBox()
            msg.setWindowTitle(" ")
            msg.setText("Prediction failed.")
            msg.exec()

    def onFormatChange(self):
        rb = self.sender()

        if rb.isChecked():
            self.format_type = rb.text()
            self.format_textbox.setText(self.formatPrediction(self.raw_prediction))

    def formatPrediction(self, prediction, format_type=None):
        self.error.setText("")
        prediction = prediction or self.format_textbox.toPlainText()

        raw = prediction.strip('$')
        if len(raw) == 0:
            return ''

        format_type = format_type or self.format_type
        if format_type == "Raw":
            formatted = raw
        elif format_type == "LaTeX-$":
            formatted = f"${raw}$"
        elif format_type == "LaTeX-$$":
            formatted = f"$${raw}$$"
        elif format_type == "MathJax":
            formatted = raw
        elif format_type == "Sympy":
            try:
                formatted = str(to_sympy(raw))
            except Exception as e:
                print(e)
                formatted = raw
                self.error.setText("Failed to parse Sympy expr.")
        else:
            return raw

        return formatted

    def onTextboxChange(self):
        text = self.textbox.toPlainText()
        new_raw_prediction = self.formatPrediction(text, "Raw")
        if new_raw_prediction != self.raw_prediction:
            self.raw_prediction = new_raw_prediction
            self.format_textbox.setText(self.formatPrediction(self.raw_prediction))
            self.displayPrediction()

    def onFormatTextboxChange(self):
        text = self.format_textbox.toPlainText()
        clipboard = QApplication.clipboard()
        clipboard.setText(text)

    def copyToClipboard(self):
        """复制 LaTeX 代码到剪贴板"""
        text = self.format_textbox.toPlainText()
        clipboard = QApplication.clipboard()
        clipboard.setText(text)
        # 显示短暂提示
        self.copyButton.setText("Copied!")
        QTimer.singleShot(1500, lambda: self.copyButton.setText("Copy to Clipboard"))

    def displayPrediction(self, prediction=None):
        if self.isProcessing:
            pageSource = """<center>
            <img src="qrc:/icons/processing-icon-anim.svg" width="50", height="50">
            </center>"""
        else:
            if prediction is None:
                prediction = self.textbox.toPlainText().strip('$')
            pageSource = """
            <html>
            <head><script id="MathJax-script" src="qrc:MathJax.js"></script>
            <script>
            MathJax.Hub.Config({messageStyle: 'none',tex2jax: {preview: 'none'}});
            MathJax.Hub.Queue(
                function () {
                    document.getElementById("equation").style.visibility = "";
                }
                );
            </script>
            </head> """ + """
            <body>
            <div id="equation" style="font-size:1em; visibility:hidden">$${equation}$$</div>
            </body>
            </html>
                """.format(equation=prediction)
        self.webView.setHtml(pageSource)


class ModelThread(QThread):
    finished = pyqtSignal(dict)

    def __init__(self, img, model):
        super().__init__()
        self.img = img
        self.model = model

    def run(self):
        try:
            prediction = self.model(self.img)
            # replace <, > with \lt, \gt so it won't be interpreted as html code
            prediction = prediction.replace('<', '\\lt ').replace('>', '\\gt ')
            self.finished.emit({"success": True, "prediction": prediction})
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.finished.emit({"success": False, "prediction": None})


class SnipWidget(QMainWindow):
    isSnipping = False

    def __init__(self, parent):
        super().__init__()
        self.parent = parent

        monitos = get_monitors()
        bboxes = np.array([[m.x, m.y, m.width, m.height] for m in monitos])
        x, y, _, _ = bboxes.min(0)
        w, h = bboxes[:, [0, 2]].sum(1).max(), bboxes[:, [1, 3]].sum(1).max()
        self.setGeometry(x, y, w-x, h-y)

        self.begin = QtCore.QPoint()
        self.end = QtCore.QPoint()

        self.mouse = Controller()

        # Create and start the timer
        self.factor = QGuiApplication.primaryScreen().devicePixelRatio()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_geometry_based_on_cursor_position)
        self.timer.start(500)

    def update_geometry_based_on_cursor_position(self):
        if not self.isSnipping:
            return

        # Update the geometry of the SnipWidget based on the current screen
        mouse_pos = QtGui.QCursor.pos()
        screen = QGuiApplication.screenAt(mouse_pos)
        if screen:
            self.factor = screen.devicePixelRatio()
            screen_geometry = screen.geometry()
            self.setGeometry(screen_geometry)


    def snip(self):
        self.isSnipping = True
        self.setWindowFlags(QtCore.Qt.WindowType.WindowStaysOnTopHint)
        QApplication.setOverrideCursor(QtGui.QCursor(QtCore.Qt.CursorShape.CrossCursor))

        self.show()

    def cancelSnip(self):
        """取消截图（由全局热键调用）"""
        if self.isSnipping:
            self.isSnipping = False
            QApplication.restoreOverrideCursor()
            self.close()
            self.parent.show()

    def paintEvent(self, event):
        if self.isSnipping:
            brushColor = (0, 180, 255, 100)
            opacity = 0.3
        else:
            brushColor = (255, 255, 255, 0)
            opacity = 0

        self.setWindowOpacity(opacity)
        qp = QtGui.QPainter(self)
        qp.setPen(QtGui.QPen(QtGui.QColor('black'), 2))
        qp.setBrush(QtGui.QColor(*brushColor))
        qp.drawRect(QtCore.QRect(self.begin, self.end))

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Escape.value:
            QApplication.restoreOverrideCursor()
            self.close()
            self.parent.show()
        event.accept()

    def mousePressEvent(self, event):
        self.startPos = self.mouse.position

        self.begin = event.pos()
        self.end = self.begin
        self.update()

    def mouseMoveEvent(self, event):
        self.end = event.pos()
        self.update()

    def mouseReleaseEvent(self, event):
        self.isSnipping = False
        QApplication.restoreOverrideCursor()

        startPos = self.startPos
        endPos = self.mouse.position

        x1 = int(min(startPos[0], endPos[0]))
        y1 = int(min(startPos[1], endPos[1]))
        x2 = int(max(startPos[0], endPos[0]))
        y2 = int(max(startPos[1], endPos[1]))

        self.repaint()
        QApplication.processEvents()
        try:
            img = ImageGrab.grab(bbox=(x1, y1, x2, y2), all_screens=True)
        except Exception as e:
            if sys.platform == "darwin":
                img = ImageGrab.grab(bbox=(x1//self.factor, y1//self.factor,
                                           x2//self.factor, y2//self.factor), all_screens=True)
            else:
                raise e
        QApplication.processEvents()

        self.close()
        self.begin = QtCore.QPoint()
        self.end = QtCore.QPoint()
        self.parent.returnSnip(img)

def main(arguments):
    with in_model_path():
        if os.name != 'nt':
            os.environ['QTWEBENGINE_DISABLE_SANDBOX'] = '1'
        app = QApplication(sys.argv)
        ex = App(arguments)
        sys.exit(app.exec())