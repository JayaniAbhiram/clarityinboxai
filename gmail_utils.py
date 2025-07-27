import os
import base64
import re
import tempfile
from email.message import EmailMessage
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

def get_gmail_service(credentials_json_path):
    """
    Authorize and build Gmail API service using uploaded credentials file.
    """
    token_path = os.path.join(tempfile.gettempdir(), 'token.json')

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_json_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, 'w') as token_file:
            token_file.write(creds.to_json())

    service = build('gmail', 'v1', credentials=creds)
    return service

def list_messages(service, max_results=50, label_ids=None, query=None, page_token=None):
    """
    Lists messages from Gmail API.
    Returns a list of messages and the nextPageToken if available.
    """
    request_params = {
        'userId': 'me',
        'maxResults': max_results # This will be our EMAILS_PER_PAGE now
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
    # Handle multipart messages
    if 'parts' in payload:
        for part in payload['parts']:
            if part.get('mimeType') == 'text/plain':
                data = part.get('body', {}).get('data')
                if data:
                    parts.append(base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore'))
            # Recursively check nested parts, e.g., for multipart/alternative
            elif 'parts' in part:
                parts.append(get_body_from_payload(part)) # Recurse into nested parts
    else: # Handle single-part messages
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
    # This regex looks for href attributes and then filters for 'unsubscribe' in the link.
    # It's a simplified approach and might not catch all unsubscribe links.
    # A more robust solution might involve parsing HTML with a library like BeautifulSoup.
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
