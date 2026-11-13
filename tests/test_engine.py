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


def test_engine_uses_selected_monitor(monkeypatch):
    class DualDesktop(FakeDesktop):
        def __init__(self):
            super().__init__()
            self.monitors_called = []
        def capture(self, monitor='all'):
            self.monitors_called.append(monitor)
            return super().capture()

    d = DualDesktop(); eng = WorkflowEngine(d); eng._start_listener = lambda: None
    f = graph(image_url(d.template))
    f['nodes'][1]['monitor'] = '2'
    eng.start(f, dry_run=True)
    result = wait_done(eng)
    assert result['status'] == 'finished'
    assert d.monitors_called == ['2']


def test_engine_invalid_monitor_blocked():
    d=FakeDesktop();f=graph(image_url(d.template));f['nodes'][1]['monitor']='broken'
    with pytest.raises(ValueError,match='螢幕編號不合法'):
        verify_flow(f)


def multi_if_graph(conditions):
    """A prioritized IF / ELSE IF / ELSE group in a single node."""
    ports = [item['id'] for item in conditions] + ['else']
    nodes = [
        {'id': 'start', 'type': 'start'},
        {'id': 'decision', 'type': 'multi_if', 'title': '條件', 'cases': conditions},
    ] + [{'id': p + '_end', 'type': 'end'} for p in ports]
    edges = [{'source': 'start', 'sourceHandle': 'next', 'target': 'decision'}] + [
        {'source': 'decision', 'sourceHandle': p, 'target': p + '_end'} for p in ports
    ]
    return {'nodes': nodes, 'edges': edges}


def case(cid, image, **kwargs):
    return {'id': cid, 'title': cid, 'image': image, 'threshold': .9, 'timeout': 0, **kwargs}


def test_multi_if_first_matching_case_has_priority():
    desktop = FakeDesktop(); engine = WorkflowEngine(desktop); engine._start_listener=lambda:None
    image = image_url(desktop.template)
    flow = multi_if_graph([case('if_aaa', image), case('if_bbb', image)])
    engine.start(flow, dry_run=True)
    state = wait_done(engine)
    assert state['status'] == 'finished'
    assert state['history'] == ['start', 'decision', 'if_aaa_end']
    assert state['lastEdge']['sourceHandle'] == 'if_aaa'
    assert not any('IF 2：' in log['message'] for log in state['logs'])


def test_multi_if_second_case_and_relative_click():
    desktop = FakeDesktop(); engine = WorkflowEngine(desktop); engine._start_listener=lambda:None
    missing_image = image_url(np.full((12,12,3),255,dtype=np.uint8))
    flow = multi_if_graph([
        case('if_aaa', missing_image),
        case('if_bbb', image_url(desktop.template), clickOnFound=True, clickX=.75, clickY=.5),
    ])
    engine.start(flow, dry_run=False)
    state = wait_done(engine)
    assert state['history'][-1] == 'if_bbb_end'
    assert desktop.clicks == [(606, 1035, 'left', 1)]


def test_multi_if_else_if_all_failed():
    desktop = FakeDesktop(); engine = WorkflowEngine(desktop); engine._start_listener=lambda:None
    image = image_url(np.full((12,12,3),255,dtype=np.uint8))
    flow = multi_if_graph([case('if_aaa', image), case('if_bbb', image)])
    engine.start(flow, dry_run=True)
    state = wait_done(engine)
    assert state['history'][-1] == 'else_end'
    assert state['lastEdge']['sourceHandle'] == 'else'


def test_multi_if_can_check_image_not_existing():
    desktop = FakeDesktop(); engine = WorkflowEngine(desktop); engine._start_listener=lambda:None
    missing_image = image_url(np.full((12,12,3),255,dtype=np.uint8))
    flow = multi_if_graph([
        case('if_missing', missing_image, expect='missing'),
        case('if_found', image_url(desktop.template)),
    ])
    engine.start(flow, dry_run=True)
    state = wait_done(engine)
    assert state['history'][-1] == 'if_missing_end'
    assert desktop.clicks == []


def test_multi_if_validation_rejects_duplicate_ports():
    desktop = FakeDesktop();image=image_url(desktop.template)
    flow=multi_if_graph([case('if_same',image),case('if_same',image)])
    with pytest.raises(ValueError,match='重複'):
        verify_flow(flow)


def test_multi_if_validation_blocks_incomplete_image():
    desktop = FakeDesktop();image=image_url(desktop.template)
    flow=multi_if_graph([case('if_aaa',image),case('if_bbb','')])
    with pytest.raises(ValueError,match='IF 2 還沒有圖片'):
        verify_flow(flow)


def test_multi_if_validation_rejects_removed_port():
    desktop = FakeDesktop();image=image_url(desktop.template)
    flow=multi_if_graph([case('if_aaa',image)])
    flow['edges'][1]['sourceHandle']='if_deleted'
    with pytest.raises(ValueError,match='輸出連接點'):
        verify_flow(flow)


def test_multi_if_validation_too_many_cases():
    desktop = FakeDesktop(); image=image_url(desktop.template)
    flow=multi_if_graph([case(f'if_case{i}',image) for i in range(11)])
    with pytest.raises(ValueError,match='1 到 10'):
        verify_flow(flow)
