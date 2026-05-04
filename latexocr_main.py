#!/usr/bin/env python
"""
LaTeX-OCR GUI 独立入口点
"""
import sys
import os

# 添加 _internal 目录到路径，以便导入 pix2tex
if getattr(sys, 'frozen', False):
    # PyInstaller 打包后的路径
    application_path = sys._MEIPASS
    base_path = os.path.dirname(sys.executable)
    add_path = os.path.join(base_path, '_internal')
else:
    # 开发环境路径
    application_path = os.path.dirname(os.path.abspath(__file__))
    base_path = application_path
    add_path = application_path

if add_path not in sys.path:
    sys.path.insert(0, add_path)

# 确保在正确的目录
os.chdir(base_path)

def main():
    from argparse import ArgumentParser

    parser = ArgumentParser()
    parser.add_argument('-t', '--temperature', type=float, default=.333, help='Softmax sampling frequency')
    parser.add_argument('-c', '--config', type=str, default='settings/config.yaml', help='path to config file')
    parser.add_argument('-m', '--checkpoint', type=str, default='checkpoints/weights.pth', help='path to weights file')
    parser.add_argument('--no-cuda', action='store_true', help='Compute on CPU')
    parser.add_argument('--no-resize', action='store_true', help='Resize the image beforehand')
    parser.add_argument('-s', '--show', action='store_true', help='Show the rendered predicted latex code (cli only)')
    parser.add_argument('-k', '--katex', action='store_true', help='Render the latex code in the browser (cli only)')
    parser.add_argument('--gui', action='store_true', help='Use GUI (gui only)')
    parser.add_argument('file', nargs='*', type=str, default=None, help='Predict LaTeX code from image file instead of clipboard (cli only)')
    arguments = parser.parse_args()

    name = os.path.split(sys.argv[0])[-1]
    # 打包后强制使用 GUI
    if getattr(sys, 'frozen', False) or arguments.gui or name in ['pix2tex_gui', 'latexocr', 'LaTeX-OCR']:
        from pix2tex.gui import main as gui_main
        gui_main(arguments)
    else:
        from pix2tex.cli import main as cli_main
        cli_main(arguments)

if __name__ == '__main__':
    main()