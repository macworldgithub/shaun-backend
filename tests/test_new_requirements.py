"""Integration route tests are isolated from live databases and accounts."""
from unittest.mock import patch, AsyncMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from auth import get_current_user
from models import User
from routers.contractor import router
from tests.contractor_fixtures import mock_database, contractor_environment


def test_integration_poll_and_config():
    app = FastAPI()
    app.include_router(router)
    admin = User(id='admin', email='admin@example.com', name='Admin', role='admin')
    app.dependency_overrides[get_current_user] = lambda: admin
    db = mock_database()
    client = TestClient(app)
    with patch('routers.contractor.get_db', return_value=db), patch('routers.contractor.poll_delivery_centre_integration', new=AsyncMock(return_value={'status': 'success'})):
        assert client.get('/api/contractor/integrations/config').status_code == 403
        admin.role = 'super_admin'
        assert client.get('/api/contractor/integrations/config').status_code == 200
        response = client.post('/api/contractor/integrations/config', json={'polling_interval_minutes': 15, 'api_key': 'private-test-key'})
        assert response.status_code == 200
        assert response.json()['config']['api_key'] == ''
        assert client.post('/api/contractor/integrations/poll').json()['status'] == 'success'
        assert client.post('/api/contractor/integrations/webhook', json={'event': 'job.updated'}).status_code == 401
        db.contractor_jobs.update_one.assert_not_awaited()
