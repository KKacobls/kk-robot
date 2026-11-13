import base64
import time

import cv2
import numpy as np
import pytest

from backend.engine import WorkflowEngine, verify_flow
from backend.vision import Screen


def image_url(arr):
    ok, encoded = cv2.imencode('.png', arr)
    assert ok
    return 'data:image/png;base64,' + base64.b64encode(encoded).decode()


class FakeDesktop:
    def __init__(self):
        self.screen = np.zeros((140, 210, 3), dtype=np.uint8)
        rng = np.random.default_rng(3)
        self.template = rng.integers(0, 256, (20, 35, 3), dtype=np.uint8)
        self.screen[25:45, 80:115] = self.template
        self.clicks = []
        self.keys = []

    def capture(self):
        return Screen(self.screen, left=500, top=1000)

    def click(self, x, y, button, count):
        self.clicks.append((x, y, button, count))

    def hotkey(self, keys):
        self.keys.append(keys)


def graph(image, threshold=.9, click=True):
    nodes = [
        {'id':'start', 'type':'start', 'title':'start'},
        {'id':'A', 'type':'condition', 'title':'A', 'image':image, 'threshold':threshold,
         'timeout':0, 'clickOnFound':click, 'clickX':.75,'clickY':.5},
        {'id':'yes', 'type':'end', 'title':'yes'},
        {'id':'no', 'type':'end', 'title':'no'},
    ]
    edges = [
        {'source':'start','sourceHandle':'next','target':'A'},
        {'source':'A','sourceHandle':'found','target':'yes'},
        {'source':'A','sourceHandle':'missing','target':'no'},
    ]
    return {'nodes':nodes,'edges':edges}


def wait_done(engine, timeout=3):
    limit=time.monotonic()+timeout
    while engine.state()['status']=='running' and time.monotonic()<limit:
        time.sleep(.02)
    return engine.state()


def test_found_branch_and_relative_click():
    d=FakeDesktop();eng=WorkflowEngine(d);eng._start_listener=lambda:None
    eng.start(graph(image_url(d.template)),dry_run=False)
    result=wait_done(eng)
    assert result['status']=='finished'
    assert result['history']==['start','A','yes']
    assert d.clicks == [(606,1035,'left',1)]


def test_dry_run_has_no_click():
    d=FakeDesktop();eng=WorkflowEngine(d);eng._start_listener=lambda:None
    eng.start(graph(image_url(d.template)),dry_run=True)
    result=wait_done(eng)
    assert result['history'][-1]=='yes'
    assert d.clicks==[]


def test_missing_branch():
    d=FakeDesktop();eng=WorkflowEngine(d);eng._start_listener=lambda:None
    image=np.full((12,12,3),255,dtype=np.uint8)
    eng.start(graph(image_url(image)),dry_run=False)
    result=wait_done(eng)
    assert result['history'][-1]=='no'
    assert d.clicks==[]


def test_stop_while_waiting():
    d=FakeDesktop();eng=WorkflowEngine(d);eng._start_listener=lambda:None
    flow={'nodes':[{'id':'s','type':'start'},{'id':'w','type':'wait','seconds':60},{'id':'e','type':'end'}],
          'edges':[{'source':'s','sourceHandle':'next','target':'w'},{'source':'w','sourceHandle':'next','target':'e'}]}
    eng.start(flow,dry_run=True)
    time.sleep(.06);eng.stop()
    result=wait_done(eng)
    assert result['status']=='stopped'


def test_missing_image_blocked():
    d=FakeDesktop()
    flow=graph('')
    with pytest.raises(ValueError,match='還沒有圖片'):
        verify_flow(flow)


def test_bad_edge_blocked():
    d=FakeDesktop()
    flow=graph(image_url(d.template))
    flow['edges'][1]['sourceHandle']='next'
    with pytest.raises(ValueError):
        verify_flow(flow)
