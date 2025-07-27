import os
import base64
import re
import tempfile
import json
from email.message import EmailMessage
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

def get_gmail_service_flow(credentials_json_data, redirect_uri):
    """
    Initializes the OAuth flow for a web application.
    Returns the authorization URL and the state string.
    """
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as temp_cred_file:
        temp_cred_file.write(credentials_json_data)
        temp_cred_file_path = temp_cred_file.name

    flow = Flow.from_client_secrets_file(temp_cred_file_path, SCOPES)
    flow.redirect_uri = redirect_uri

    os.remove(temp_cred_file_path) # Delete temp file immediately

    authorization_url, state = flow.authorization_url(
        access_type='offline',  # Request a refresh token
        include_granted_scopes='true'
    )
    
    return authorization_url, state

def get_gmail_credentials_from_callback(credentials_json_data, redirect_uri, authorization_response):
    """
    Exchanges the authorization code for credentials by re-initializing the Flow.
    """
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as temp_cred_file:
        temp_cred_file.write(credentials_json_data)
        temp_cred_file_path = temp_cred_file.name

    flow = Flow.from_client_secrets_file(temp_cred_file_path, SCOPES)
    flow.redirect_uri = redirect_uri

    os.remove(temp_cred_file_path)

    flow.fetch_token(authorization_response=authorization_response)
    
    # CRITICAL: Print the refresh token for manual copy
    print(f"--- IMPORTANT: Copy this refresh token and set it as GOOGLE_REFRESH_TOKEN env var in Render ---")
    print(f"REFRESH_TOKEN_FOR_RENDER_ENV: {flow.credentials.refresh_token}")
    print(f"-------------------------------------------------------------------------------------------------")

    return flow.credentials

def get_credentials_from_refresh_token(refresh_token, credentials_json_data):
    """
    Rebuilds credentials using a stored refresh token and client secrets.
    """
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as temp_cred_file:
        temp_cred_file.write(credentials_json_data)
        temp_cred_file_path = temp_cred_file.name

    # Load client config from file
    client_config = json.loads(credentials_json_data)['web'] # Assuming 'web' client

    creds = Credentials.from_authorized_user_info(
        info={
            'client_id': client_config['client_id'],
            'client_secret': client_config['client_secret'],
            'refresh_token': refresh_token,
            'token_uri': client_config['token_uri'],
            'scopes': SCOPES # Ensure scopes are included here
        },
        scopes=SCOPES
    )
    os.remove(temp_cred_file_path)
    return creds


def get_gmail_service(credentials):
    """
    Builds Gmail API service using existing credentials.
    """
    service = build('gmail', 'v1', credentials=credentials)
    return service

# --- Existing functions (list_messages, find_header, get_body_from_payload, etc.) remain unchanged ---

def list_messages(service, max_results=50, label_ids=None, query=None, page_token=None):
    request_params = {
        'userId': 'me',
        'maxResults': max_results
    }
    if label_ids:
        request_params['labelIds'] = label_ids
    if query:
        request_params['q'] = query
    if page_token:
        request_params['pageToken'] = page_token

    results = service.users().messages().list(**request_params).execute()
    messages = results.get('messages', [])
    next_page_token = results.get('nextPageToken', None)
    return messages, next_page_token

def find_header(headers, header_name):
    for h in headers:
        if h['name'].lower() == header_name.lower():
            return h['value']
    return ''

def get_body_from_payload(payload):
    if not payload:
        return ''
    parts = []
    if 'parts' in payload:
        for part in payload['parts']:
            if part.get('mimeType') == 'text/plain':
                data = part.get('body', {}).get('data')
                if data:
                    parts.append(base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore'))
            elif 'parts' in part:
                parts.append(get_body_from_payload(part))
    else:
        data = payload.get('body', {}).get('data')
        if data:
            parts.append(base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore'))
    return '\n'.join(parts)

def get_message_payload(service, msg_id):
    msg = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
    headers = msg.get('payload', {}).get('headers', [])
    subject = find_header(headers, 'Subject')
    sender = find_header(headers, 'From')
    snippet = msg.get('snippet', '')
    body = get_body_from_payload(msg.get('payload'))
    return subject, sender, snippet, body, msg

def classify_message(subject, sender, body, vip_senders, keywords_important):
    sender_lower = (sender or '').lower()
    subject_lower = (subject or '').lower()
    body_lower = (body or '').lower()

    for vip in vip_senders:
        vip_lower = vip.lower()
        if vip_lower in sender_lower:
            return 'high'

    for keyword in keywords_important:
        keyword_lower = keyword.lower()
        if (keyword_lower in sender_lower) or (keyword_lower in subject_lower) or (keyword_lower in body_lower):
            return 'high'

    return 'normal'

def modify_message_labels(service, msg_id, labels_to_add=None, labels_to_remove=None):
    body = {}
    if labels_to_add:
        body['addLabelIds'] = labels_to_add
    if labels_to_remove:
        body['removeLabelIds'] = labels_to_remove
    return service.users().messages().modify(userId='me', id=msg_id, body=body).execute()

def parse_unsubscribe_links(body):
    unsubscribe_links = re.findall(r'href=[\'"]([^\'" >]+)[\'"]?', body, re.IGNORECASE)
    unsubscribe_links = [link for link in unsubscribe_links if 'unsubscribe' in link.lower()]
    return unsubscribe_links

def send_message(service, to, subject, body):
    message = EmailMessage()
    message.set_content(body)
    message['To'] = to
    message['From'] = 'me'
    message['Subject'] = subject
    encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
    create_message = {'raw': encoded_message}
    send = service.users().messages().send(userId='me', body=create_message).execute()
    return send
