# slack.py
import json
import secrets
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse
import httpx
import asyncio
import base64
import requests
from integrations.integration_item import IntegrationItem
import os

from redis_client import add_key_value_redis, get_value_redis, delete_key_redis

APP_ID = os.getenv('HUBSPOT_APP_ID')
CLIENT_ID = os.getenv('HUBSPOT_CLIENT_ID')
CLIENT_SECRET = os.getenv('HUBSPOT_CLIENT_SECRET')
REDIRECT_URI = 'http://localhost:8000/integrations/hubspot/oauth2callback'
authorization_url = f'https://app-na2.hubspot.com/oauth/authorize?client_id={CLIENT_ID}&response_type=code&owner=user&scope=oauth%20crm.objects.contacts.read%20crm.objects.contacts.write&redirect_uri=http%3A%2F%2Flocalhost%3A8000%2Fintegrations%2Fhubspot%2Foauth2callback'


async def authorize_hubspot(user_id, org_id):
    state_data = {
        'state': secrets.token_urlsafe(32),
        'user_id': user_id,
        'org_id': org_id
    }
    encoded_state = json.dumps(state_data)
    await add_key_value_redis(f'hubspot_state:{org_id}:{user_id}', encoded_state, expire=600)

    return f'{authorization_url}&state={encoded_state}'

async def oauth2callback_hubspot(request: Request):
    if request.query_params.get('error'):
        raise HTTPException(status_code=400, detail=request.query_params.get('error'))
    code = request.query_params.get('code')
    encoded_state = request.query_params.get('state')
    if not code or not encoded_state:
        raise HTTPException(status_code=400, detail='Missing code or state in the callback.')
    state_data = json.loads(encoded_state)

    original_state = state_data.get('state')
    user_id = state_data.get('user_id')
    org_id = state_data.get('org_id')

    saved_state_json = await get_value_redis(f'hubspot_state:{org_id}:{user_id}')
    
    saved_state_obj = json.loads(saved_state_json)
    saved_state = saved_state_obj.get('state')
    
    if not saved_state or saved_state != original_state:
        raise HTTPException(status_code=400, detail='State does not match.')

    # Exchange the authorization code for an access token
    async with httpx.AsyncClient() as client:
        response, _ = await asyncio.gather(
            client.post(
                'https://api.hubapi.com/oauth/v1/token', 
                data={
                    'grant_type': 'authorization_code',
                    'client_id': CLIENT_ID,
                    'client_secret': CLIENT_SECRET,
                    'redirect_uri': REDIRECT_URI,
                    'code': code
                },
                headers={'Content-Type': 'application/x-www-form-urlencoded'}
            ),
            delete_key_redis(f'hubspot_state:{org_id}:{user_id}')
        )
    response_data = response.json()
    # print("HubSpot Token Response:", response_data)
    # if response.status_code != 200:
    #     raise HTTPException(status_code=400, detail=response_data.get('error_description'))

    # Save the response in Redis
    await add_key_value_redis(f'hubspot_credentials:{org_id}:{user_id}', json.dumps(response_data), expire=600)

    close_window_script = """
    <html>
        <script>
            window.close();
        </script>
    </html>
    """

    return HTMLResponse(content=close_window_script)

async def get_hubspot_credentials(user_id, org_id):
    credentials = await get_value_redis(f'hubspot_credentials:{org_id}:{user_id}')
    if not credentials:
        raise HTTPException(status_code=400, detail='No credentials found.')
    credentials = json.loads(credentials)
    if not credentials:
        raise HTTPException(status_code=400, detail='No credentials found.')
    await delete_key_redis(f'hubspot_credentials:{org_id}:{user_id}')

    return credentials

# def recurs
def _recursive_dict_search(data, target_key):
    """Recursively search for a key in a dictionary of dictionaries."""
    if target_key in data:
        return data[target_key]

    for value in data.values():
        if isinstance(value, dict):
            result = _recursive_dict_search(value, target_key)
            if result is not None:
                return result
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    result = _recursive_dict_search(item, target_key)
                    if result is not None:
                        return result
    return None

def create_integration_item_metadata_object(
    response_json_str: str
):
    """Creates an integration metadata object from the response"""
    response_json=json.loads(json.dumps(response_json_str))
    name = _recursive_dict_search(response_json['properties'], 'firstname') or "No Name"
    creation_time = _recursive_dict_search(response_json, 'createdAt')
    last_modified_time = _recursive_dict_search(response_json, 'updatedAt')
    integration_item_metadata = IntegrationItem(
        id=response_json['id'],
        type='contact',
        name=name,
        creation_time=creation_time,
        last_modified_time=last_modified_time,
        visibility=response_json['archived']
    )

    return integration_item_metadata

async def get_items_hubspot(credentials):
    """Aggregate all metadata relevant for a HubSpot data fetching logic here"""
    credentials = json.loads(credentials)
    access_token = credentials.get('access_token')
    if not access_token:
        raise HTTPException(status_code=400, detail='Invalid HubSpot credentials.')

    response = requests.get(
        'https://api.hubapi.com/crm/v3/objects/contacts',
        headers={
            'Authorization': f'Bearer {access_token}'
        }
    )

    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    
    response_data = response.json()
    # print("HubSpot Contacts Response Data:", response_data)
    results = response_data.get('results', [])
    list_of_integration_items = []
    for result in results:
        list_of_integration_items.append(
            create_integration_item_metadata_object(result)
        )
    print(list_of_integration_items)
    return list_of_integration_items