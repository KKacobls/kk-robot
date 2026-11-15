# Garden Plot Detector / 田地圖片即時辨識

A **local-only Python tool** that detects three kinds of garden plots shown in the provided game screenshot. It draws boxes and counts plots, and can optionally show a transparent **click-through overlay on Windows**. **It never clicks the game or sends screenshots to a server.**

## Classes

- **Green / E / 空地**: Brown plot without an icon.
- **Cyan / W / 水珠**: Plot with a blue droplet.
- **Red / R / 石塊**: Plot with gray rocks. This is provisionally treated as "不能種" based on the reference screenshot; verify the game's rules.
- **`*` after a label**: Plot touches the screenshot edge, so it may be incomplete and misclassified.

## Windows setup (Python 3.10+)

Download/extract the ZIP and double-click **`start.bat`**. This creates `.venv`, installs dependencies and opens the graphical application. Or use PowerShell:

```powershell
python -m pip install -r requirements.txt
python app.py
```

In the window, choose the monitor showing the game and press **開始辨識**. The preview continuously updates the boxes and counts. To limit false detections, drag a rectangle around the farmland directly in the preview (or click **清除框選範圍** to reset).

For **boxes directly on the game**, tick **在遊戲畫面上顯示透明即時框**. Windows attempts to make the topmost transparent overlay click-through, but this feature has not been hardware-tested here: if it doesn't work with your fullscreen game, use windowed/borderless mode or just the preview. Overlay windows are destroyed when you stop the capture or close the application.

Press **儲存辨識截圖** to save annotated screenshots in `captures/` (a local-only, gitignored directory). The screen contents are never uploaded.

### Offline check / test a screenshot

```powershell
python app.py --image examples/sample.png --output sample_result.png
```

The example screenshot supplied for this task gives **7 empty / 13 water / 11 rock plots = 31 visible regions** according to the first version of the detector, including **7 cropped at the image edge**. These numbers are image-based estimates, **not verified ground truth**. Some plots near the crop border or under other sprites may not be detected.

Select a subregion offline:

```powershell
python app.py --image examples/sample.png --roi 30 60 340 170 --output crop_check.png
```

### How it works and limitations

It uses **OpenCV HSV color segmentation and connected-component shapes** to find repeating brown soil plots, and cyan / low-saturation color pixel ratios to distinguish droplets and rocks. No neural network weights, account credentials, browser extensions, OCR, cloud APIs or training are required. The built-in thresholds are tailored to **this screenshot's game graphics**. New map styles, lighting, occlusion, moving animations, zoom, scale, themes, or game updates may affect accuracy. The image can't establish which plots are functionally plantable; the red class means "looks like a rock plot".

**Multi-monitor:** choose screen 1, 2, etc., or 全部螢幕. The tool uses screen offsets to draw boxes on the correct monitor, including negative Windows desktop coordinates. DPI awareness is set before the GUI opens. When possible, choose just the monitor containing the game for higher performance and accuracy. 

**Privacy and safety:** screenshot contents stay on your PC. Use only for your own game screen. This tool is observation-only; it never sends clicks or keys. Do not rely on it for unattended, high-stakes tasks.

## Dependencies

- `opencv-python`, `numpy`: local color/shape recognition and box drawing
- `mss`: screen capture on Windows / multi monitor
- `Pillow`: display annotated previews in Tkinter
- Tkinter is included with normal Windows Python installers.

No Node.js, GPU or extra model downloads required.

## Test

```powershell
python -m unittest discover -s tests -v
```