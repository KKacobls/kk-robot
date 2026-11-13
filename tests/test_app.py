import json

from fastapi.testclient import TestClient

from backend import app as app_module


def test_local_api_has_csrf_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module,'DATA',tmp_path)
    client=TestClient(app_module.app,base_url="http://localhost:4317")
    token=client.get('/api/bootstrap').json()['token']
    assert client.post('/api/stop').status_code==403
    assert client.post('/api/stop',headers={'X-ImageFlow-Token':token}).status_code==200


def test_save_load_with_incomplete_image_template(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module,'DATA',tmp_path)
    client=TestClient(app_module.app,base_url="http://localhost:4317")
    token=client.get('/api/bootstrap').json()['token']
    flow=app_module.sample_flow()
    result=client.post('/api/workflows',json=flow,headers={'X-ImageFlow-Token':token})
    assert result.status_code==200
    saved=client.get('/api/workflows/starter')
    assert saved.status_code==200
    assert saved.json()['nodes'][1]['image']==''
    assert len(client.get('/api/workflows').json())==1


def test_directory_traversal_blocked():
    assert '..' not in app_module.flow_path('starter').name
    from fastapi import HTTPException
    try: app_module.flow_path('../secret')
    except HTTPException as exc: assert exc.status_code==400
    else: assert False


def test_monitor_list_and_independent_test_api(monkeypatch):
    monkeypatch.setattr(app_module, 'list_monitors', lambda: [
        {'id':'0','name':'全部螢幕','left':-500,'top':0,'width':1000,'height':700},
        {'id':'1','name':'螢幕 1','left':-500,'top':0,'width':500,'height':700},
        {'id':'2','name':'螢幕 2','left':0,'top':0,'width':500,'height':700},
    ])
    captured={}
    def fake_detect(image, threshold, screen=None, monitor='all'):
        captured.update({'image':image,'threshold':threshold,'monitor':monitor})
        return {'found':True, 'score':1.,'monitor':'2','rect':{'x':4,'y':5,'w':20,'h':20},'candidates':[]}
    monkeypatch.setattr(app_module,'detect',fake_detect)
    client=TestClient(app_module.app,base_url='http://localhost:4317')
    token=client.get('/api/bootstrap').json()['token']
    assert client.get('/api/monitors').json()[2]['id']=='2'
    r=client.post('/api/test',json={'image':'some image','threshold':.9,'monitor':'2'},headers={'X-ImageFlow-Token':token})
    assert r.status_code==200 and r.json()['monitor']=='2'
    assert captured['monitor']=='2' and captured['threshold']==.9
