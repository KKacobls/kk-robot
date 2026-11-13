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
