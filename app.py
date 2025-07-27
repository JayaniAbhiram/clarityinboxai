import os
import tempfile
import json # To parse credentials_json
from flask import Flask, render_template, request, session, redirect, url_for, flash
from werkzeug.utils import secure_filename
import openai
from google.oauth2.credentials import Credentials # Import Credentials class
from google.auth.transport.requests import Request # Import Request for token refresh
from gmail_utils import (
    get_gmail_service_flow, get_gmail_credentials_from_callback, # New OAuth functions
    get_gmail_service, list_messages, get_message_payload,
    classify_message, modify_message_labels,
    parse_unsubscribe_links, send_message
)

app = Flask(__name__)
app.secret_key = os.urandom(24) # Ensure this is a strong, random key in production

UPLOAD_FOLDER = tempfile.gettempdir()
ALLOWED_EXTENSIONS = {'json'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

EMAILS_PER_PAGE = 45 # As per your request

# Define the OAuth callback route
OAUTH_CALLBACK_PATH = '/oauth2callback'

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/', methods=['GET', 'POST'])
def index():
    emails, logs = [], []
    form_data = {
        'openai_key': '',
        'summary_email': '',
        'vip_senders': '',
        'keywords': ''
    }

    filter_priority = request.args.get('filter', 'all').lower()
    page = request.args.get('page', 1)
    try:
        page = int(page)
        if page < 1:
            page = 1
    except ValueError:
        page = 1

    # Initialize pagination tokens and Gmail credentials in session
    if 'page_tokens' not in session:
        session['page_tokens'] = [None]
    if 'gmail_credentials_data' not in session:
        session['gmail_credentials_data'] = None

    if request.method == 'POST':
        # Handle form submission and credential upload
        # Read the file content as a string
        credentials_file = request.files.get('credentials')
        if not credentials_file or credentials_file.filename == '':
            flash('Google credentials.json file is required!', 'danger')
            return redirect(request.url)

        if not allowed_file(credentials_file.filename):
            flash('Invalid file. Please upload a valid credentials.json', 'danger')
            return redirect(request.url)

        # Store credentials JSON data (as string) in session
        # This is temporary and will be cleared once flow is complete or session expires
        credentials_data = credentials_file.read().decode('utf-8')
        session['uploaded_credentials_json_data'] = credentials_data

        # Save other form inputs to session
        session['openai_key'] = request.form.get('openai_key', '').strip()
        session['summary_email'] = request.form.get('summary_email', '').strip()
        vip_input = request.form.get('vip_senders', '')
        session['vip_senders'] = [v.strip() for v in vip_input.split(',') if v.strip()]
        keywords_input = request.form.get('keywords', '')
        session['keywords'] = [k.strip() for k in keywords_input.split(',') if k.strip()]

        # Validate required inputs
        if not session['openai_key'] or not session['summary_email']:
            flash('OpenAI API key and Summary Email are required!', 'danger')
            # Repopulate form_data before rendering
            form_data['openai_key'] = session['openai_key']
            form_data['summary_email'] = session['summary_email']
            form_data['vip_senders'] = vip_input
            form_data['keywords'] = keywords_input
            return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                                   filter_priority=filter_priority, page=page, has_next=False, has_prev=False)

        # Construct the redirect URI for the OAuth flow
        # Use request.url_root for the base URL, which works for both local and Render
        redirect_uri = request.url_root.rstrip('/') + OAUTH_CALLBACK_PATH

        try:
            # Initiate the OAuth flow
            authorization_url, flow_state = get_gmail_service_flow(credentials_data, redirect_uri)
            session['gmail_oauth_flow_state'] = flow_state # Store the flow state for callback
            session['page_tokens'] = [None] # Reset pagination tokens on new run
            session['gmail_credentials_data'] = None # Clear old creds

            flash('Redirecting to Google for authentication...', 'info')
            return redirect(authorization_url) # Redirect user to Google for authorization

        except Exception as e:
            flash(f'Error initiating Google authentication: {str(e)}. Make sure your credentials.json is for "Web application" type and redirect URIs are configured correctly.', 'danger')
            # Repopulate form_data on error
            form_data['openai_key'] = session['openai_key']
            form_data['summary_email'] = session['summary_email']
            form_data['vip_senders'] = vip_input
            form_data['keywords'] = keywords_input
            return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                                   filter_priority=filter_priority, page=page, has_next=False, has_prev=False)

    # For GET requests:
    # 1. Repopulate form data from session
    form_data['openai_key'] = session.get('openai_key', '')
    form_data['summary_email'] = session.get('summary_email', '')
    form_data['vip_senders'] = ', '.join(session.get('vip_senders', []))
    form_data['keywords'] = ', '.join(session.get('keywords', []))

    # 2. Check if Gmail credentials are authenticated
    if session.get('gmail_credentials_data') is None:
        flash('Please upload your Google credentials.json (Web Application type) and provide OpenAI API key to start.', 'info')
        return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                               filter_priority=filter_priority, page=page, has_next=False, has_prev=False)

    try:
        # Reconstruct credentials from stored data (JSON string -> Credentials object)
        creds_json = json.loads(session['gmail_credentials_data'])
        creds = Credentials.from_authorized_user_info(creds_json, SCOPES)

        # Refresh token if expired
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Save updated credentials back to session
            session['gmail_credentials_data'] = creds.to_json()
            session.modified = True

        service = get_gmail_service(creds)
        openai.api_key = session.get('openai_key', '')

        # Build query for Gmail API based on keywords if any
        keywords = session.get('keywords', [])
        query_string = None
        if keywords:
            def q_escape(w):
                if ' ' in w:
                    return f'"{w}"'
                return w
            query_string = ' OR '.join([q_escape(k) for k in keywords])

        label_ids = None if query_string else ['INBOX']

        # Determine the page token to use for the current request
        current_page_token = None
        if page > 1 and page <= len(session['page_tokens']):
            current_page_token = session['page_tokens'][page - 1]
        elif page > len(session['page_tokens']):
            # If user tries to jump to a page whose token we don't have yet,
            # we try to fetch from the last known token.
            current_page_token = session['page_tokens'][-1]


        messages, next_gmail_page_token = list_messages(
            service,
            max_results=EMAILS_PER_PAGE,
            label_ids=label_ids,
            query=query_string,
            page_token=current_page_token
        )

        if not messages:
            emails = []
            logs.append('No emails found for current criteria.')
            has_next = False
            has_prev = page > 1
            return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                                   filter_priority=filter_priority, page=page,
                                   has_next=has_next, has_prev=has_prev)

        # Update page_tokens list if we've moved to a new 'next' page
        if next_gmail_page_token and (page + 1) > len(session['page_tokens']):
            session['page_tokens'].append(next_gmail_page_token)
            session.modified = True

        detailed_emails = []
        for msg in messages:
            subject, sender, snippet, body, _ = get_message_payload(service, msg['id'])
            priority = classify_message(subject, sender, body, session.get('vip_senders', []), keywords or [])

            try:
                if priority == 'high':
                    modify_message_labels(service, msg['id'], labels_to_add=['IMPORTANT'])
                else:
                    modify_message_labels(service, msg['id'], labels_to_remove=['IMPORTANT'])
            except Exception as label_err:
                logs.append(f"Warning: Could not modify labels for message {msg['id']}: {label_err}")

            gmail_url = f"https://mail.google.com/mail/u/0/#all/{msg['id']}"
            links = parse_unsubscribe_links(body)

            detailed_emails.append({
                'id': msg['id'],
                'subject': subject,
                'sender': sender,
                'snippet': snippet,
                'priority': priority,
                'links': links,
                'gmail_url': gmail_url
            })

        if filter_priority in ('high', 'normal'):
            emails = [e for e in detailed_emails if e['priority'] == filter_priority]
        else:
            emails = detailed_emails

        has_prev = page > 1
        has_next = bool(next_gmail_page_token)

        logs.append(f"Showing page {page} - {len(emails)} emails (filter: {filter_priority.capitalize()})")
        if next_gmail_page_token:
            logs.append(f"Next Gmail page token received: {next_gmail_page_token[:10]}...")

    except openai.AuthenticationError:
        flash('Invalid OpenAI API Key. Please check your key.', 'danger')
        logs = []
        emails = []
        session.pop('openai_key', None)
        form_data['openai_key'] = ''
        has_next = False
        has_prev = False
    except Exception as e:
        flash(f'An error occurred: {str(e)}', 'danger')
        emails = []
        logs = []
        has_next = False
        has_prev = False

    return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                           filter_priority=filter_priority, page=page,
                           has_next=has_next, has_prev=has_prev)


@app.route(OAUTH_CALLBACK_PATH)
def oauth2callback():
    """
    Handles the redirect from Google after user authorization.
    Exchanges the authorization code for access tokens.
    """
    state = request.args.get('state')
    code = request.args.get('code')
    error = request.args.get('error')

    if error:
        flash(f'Google OAuth Error: {error}', 'danger')
        return redirect(url_for('index'))

    if 'gmail_oauth_flow_state' not in session or session['gmail_oauth_flow_state']['state'] != state:
        flash('OAuth state mismatch or session expired. Please try again.', 'danger')
        return redirect(url_for('index'))

    try:
        # Reconstruct the flow and fetch tokens
        credentials = get_gmail_credentials_from_callback(
            session.pop('gmail_oauth_flow_state'), # Pop to clear state after use
            request.url # Pass the full URL (contains code and state)
        )

        # Store the serialized credentials (including refresh token) in the session
        session['gmail_credentials_data'] = credentials.to_json()
        session.modified = True # Crucial for Flask to save changes to session

        flash('Successfully authenticated with Google!', 'success')
        return redirect(url_for('index', filter=request.args.get('filter', 'all'), page=1))

    except Exception as e:
        flash(f'Error exchanging authorization code: {str(e)}', 'danger')
        return redirect(url_for('index'))


if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5001)
