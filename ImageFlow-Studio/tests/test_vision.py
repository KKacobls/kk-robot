import base64

import cv2
import numpy as np
import pytest

from backend.vision import Screen, decode_template, match_image


def to_data_url(image):
    ok, encoded = cv2.imencode('.png', image)
    assert ok
    return 'data:image/png;base64,' + base64.b64encode(encoded).decode()


def test_unique_template_matches_at_correct_physical_position():
    rng = np.random.default_rng(42)
    background = rng.integers(0, 256, (140, 200, 3), dtype=np.uint8)
    tile = background[42:68, 74:113].copy()
    template = decode_template(to_data_url(tile))
    result = match_image(Screen(background, left=-1920, top=0), template, .95)
    assert result['found'] is True
    assert result['rect'] == {'x': -1846, 'y': 42, 'w': 39, 'h': 26}
    assert result['score'] > .99


def test_constant_color_template_avoids_trivial_one_score():
    screen = np.zeros((120, 120, 3), dtype=np.uint8)
    template = np.full((12, 12, 3), 255, dtype=np.uint8)
    result = match_image(Screen(screen, 0, 0), template, .8)
    assert result['found'] is False


def test_invalid_image_rejected():
    with pytest.raises(ValueError):
        decode_template('data:text/html;base64,PGgxPg==')
    with pytest.raises(ValueError):
        decode_template('data:image/png;base64,not base64')
