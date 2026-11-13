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


def test_two_monitors_match_only_physical_screen_and_keep_negative_coordinates():
    rng = np.random.default_rng(211)
    virtual = rng.integers(0, 256, (130, 240, 3), dtype=np.uint8)
    template = rng.integers(0, 256, (20, 26, 3), dtype=np.uint8)
    virtual[37:57, 30:56] = template  # left monitor, virtual desktop x=-120
    monitors = [
        {'id':'0','left':-120,'top':-30,'width':240,'height':130},
        {'id':'1','left':-120,'top':-30,'width':120,'height':130},
        {'id':'2','left':0,'top':-30,'width':120,'height':130},
    ]
    all_screen = Screen(virtual, -120, -30, monitors, 'all')
    r = match_image(all_screen, template, .95)
    assert r['found'] and r['monitor'] == '1'
    assert r['rect'] == {'x':-90, 'y':7, 'w':26, 'h':20}
    assert len(r['candidates']) == 2
    right_screen = Screen(virtual[:,120:,:].copy(), 0, -30, [monitors[2]], '2')
    right = match_image(right_screen, template, .95)
    assert not right['found']
    assert right['monitor'] == '2'


def test_monitor_selection_validation():
    from backend.vision import normalize_monitor
    assert normalize_monitor(0) == 'all'
    assert normalize_monitor('2') == '2'
    with pytest.raises(ValueError): normalize_monitor('33')
    with pytest.raises(ValueError): normalize_monitor('../x')
