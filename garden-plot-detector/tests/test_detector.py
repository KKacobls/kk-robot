import sys
from pathlib import Path
import unittest

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parents[1]))
from detector import detect_plots, draw_boxes, summarize

SAMPLE = Path(__file__).parents[1] / "examples" / "sample.png"


class TestDetector(unittest.TestCase):
    def setUp(self):
        if not SAMPLE.exists():
            self.skipTest("Screenshot sample is shipped in the local ZIP, not public source control")
        self.frame = cv2.imread(str(SAMPLE))
        self.assertIsNotNone(self.frame)

    def test_counts_of_reference_image(self):
        dets = detect_plots(self.frame)
        self.assertEqual(summarize(dets), {"empty": 7, "water": 13, "blocked": 11, "total": 31})

    def test_boxes_do_not_leave_image(self):
        h, w = self.frame.shape[:2]
        for p in detect_plots(self.frame):
            self.assertTrue(0 <= p.x < w)
            self.assertTrue(0 <= p.y < h)
            self.assertLessEqual(p.x + p.w, w)
            self.assertLessEqual(p.y + p.h, h)
            self.assertIn(p.kind, ("empty", "water", "blocked"))

    def test_partial_tiles_on_border(self):
        dets = detect_plots(self.frame)
        self.assertEqual(sum(p.partial for p in dets), 7)

    def test_empty_screenshot(self):
        black = np.zeros((260, 413, 3), dtype=np.uint8)
        self.assertEqual(detect_plots(black), [])

    def test_single_non_plot_rectangle_does_not_count(self):
        black = np.zeros((260, 413, 3), dtype=np.uint8)
        cv2.rectangle(black, (50, 60), (110, 93), (80, 135, 190), -1)
        self.assertEqual(detect_plots(black), [])

    def test_roi_coordinates_and_smaller_count(self):
        dets = detect_plots(self.frame, (25, 75, 155, 80))
        self.assertTrue(dets)
        self.assertLess(len(dets), 31)
        self.assertTrue(all(25 <= p.x and 75 <= p.y for p in dets))

    def test_roi_outside_returns_empty(self):
        self.assertEqual(detect_plots(self.frame, (1000, 1000, 20, 20)), [])

    def test_visual_output_has_summary_header_not_covering_source(self):
        dets = detect_plots(self.frame)
        visual = draw_boxes(self.frame, dets)
        self.assertEqual(visual.shape[:2], (self.frame.shape[0] + 50, self.frame.shape[1]))
        self.assertTrue(np.array_equal(visual[50 + 25, 15], self.frame[25, 15]))

    def test_without_summary_same_shape(self):
        dets = detect_plots(self.frame)
        out = draw_boxes(self.frame, dets, summary=False)
        self.assertEqual(out.shape, self.frame.shape)

    def test_scaled_double(self):
        bigger = cv2.resize(self.frame, None, fx=2, fy=2, interpolation=cv2.INTER_LINEAR)
        result = summarize(detect_plots(bigger))
        self.assertEqual(result['water'], 13)
        self.assertGreaterEqual(result['empty'], 7)
        self.assertGreaterEqual(result['blocked'], 10)


class TestSynthetic(unittest.TestCase):
    def make_farm(self):
        bgr = np.zeros((260, 413, 3), dtype=np.uint8)
        bgr[:] = (110, 190, 115)  # green grass in BGR
        for row in range(3):
            for col in range(3):
                x, y = 30 + col * 84, 35 + row * 55
                cv2.rectangle(bgr, (x, y), (x + 60, y + 32), (70, 140, 200), -1)
                if row == 1:
                    cv2.circle(bgr, (x + 30, y + 16), 7, (255, 170, 26), -1)
                elif row == 2:
                    cv2.circle(bgr, (x + 20, y + 15), 5, (120, 125, 128), -1)
                    cv2.circle(bgr, (x + 40, y + 15), 5, (120, 125, 128), -1)
        return bgr

    def test_generated_three_groups(self):
        counts = summarize(detect_plots(self.make_farm()))
        self.assertEqual(counts, {"empty": 3, "water": 3, "blocked": 3, "total": 9})

    def test_generated_plot_centers(self):
        plots = detect_plots(self.make_farm())
        self.assertTrue(all(10 < p.center[0] < 330 for p in plots))


if __name__ == "__main__":
    unittest.main()