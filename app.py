import os
import tempfile
import json
from flask import Flask, render_template, request, session, redirect, url_for, flash
from werkzeug.utils import secure_filename
import openai
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from gmail_utils import (
    get_gmail_service_flow, get_gmail_credentials_from_callback,
    get_credentials_from_env_vars, # NEW: Import for env var handling
    get_gmail_service, list_messages, get_message_payload,
    classify_message, modify_message_labels,
    parse_unsubscribe_links, send_message,
    SCOPES
)

app = Flask(__name__)
app.secret_key = os.urandom(24) 

# UPLOAD_FOLDER and ALLOWED_EXTENSIONS are kept because form still accepts file upload
UPLOAD_FOLDER = tempfile.gettempdir()
ALLOWED_EXTENSIONS = {'json'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

EMAILS_PER_PAGE = 45

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

    if 'page_tokens' not in session:
        session['page_tokens'] = [None]
    # We no longer store full gmail_credentials_data in session long-term
    # Only for the immediate next request after OAuth callback
    if 'gmail_credentials_data' not in session:
        session['gmail_credentials_data'] = None
    if 'gmail_oauth_state' not in session:
        session['gmail_oauth_state'] = None
    
    # Store client ID/Secret from uploaded file in session for immediate use in flow
    if 'client_id_temp' not in session:
        session['client_id_temp'] = None
    if 'client_secret_temp' not in session:
        session['client_secret_temp'] = None

    # --- Start of Authentication Logic ---
    creds = None
    auth_needed_from_env_or_form = False # Flag for initial auth decision

    # Try to load credentials from environment variables first
    env_client_id = os.environ.get('GOOGLE_CLIENT_ID')
    env_client_secret = os.environ.get('GOOGLE_CLIENT_SECRET')
    env_refresh_token = os.environ.get('GOOGLE_REFRESH_TOKEN')

    if env_client_id and env_client_secret and env_refresh_token:
        try:
            creds = get_credentials_from_env_vars(env_client_id, env_client_secret, env_refresh_token)
            # Try to refresh access token using the refresh token
            if not creds.valid:
                if creds.refresh_token:
                    creds.refresh(Request())
                    flash("Authenticated using persistent Google credentials.", "success")
                else:
                    flash("Persistent Google refresh token invalid/expired. Please re-authenticate.", "warning")
                    auth_needed_from_env_or_form = True # Force full OAuth if refresh fails
            else:
                flash("Authenticated using persistent Google credentials.", "success")

            session['gmail_credentials_data'] = creds.to_json() # Store in session for immediate use
            session.modified = True

        except Exception as e:
            flash(f"Failed to use persistent Google credentials from environment: {str(e)}. Please re-authenticate.", "danger")
            auth_needed_from_env_or_form = True # Force full OAuth if env vars fail
    else:
        # If any env var is missing, authentication is needed via form/OAuth
        auth_needed_from_env_or_form = True

    # --- Handle POST request (Form submission) ---
    if request.method == 'POST':
        # Always try to process form data
        session['openai_key'] = request.form.get('openai_key', '').strip()
        session['summary_email'] = request.form.get('summary_email', '').strip()
        vip_input = request.form.get('vip_senders', '')
        session['vip_senders'] = [v.strip() for v in vip_input.split(',') if v.strip()]
        keywords_input = request.form.get('keywords', '')
        session['keywords'] = [k.strip() for k in keywords_input.split(',') if k.strip()]

        if not session['openai_key'] or not session['summary_email']:
            flash('OpenAI API key and Summary Email are required!', 'danger')
            form_data['openai_key'] = session['openai_key']
            form_data['summary_email'] = session['summary_email']
            form_data['vip_senders'] = vip_input
            form_data['keywords'] = keywords_input
            return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                                   filter_priority=filter_priority, page=page, has_next=False, has_prev=False)

        # Handle credentials.json upload ONLY if persistent env vars are NOT set up
        # or if the user is explicitly re-uploading (auth_needed_from_env_or_form is True).
        if auth_needed_from_env_or_form:
            credentials_file = request.files.get('credentials')
            if not credentials_file or credentials_file.filename == '':
                flash('Google credentials.json file is required to initiate authentication!', 'danger')
                return redirect(request.url) # Redirect to GET to show flash message

            if not allowed_file(credentials_file.filename):
                flash('Invalid file. Please upload a valid credentials.json', 'danger')
                return redirect(request.url)

            credentials_data = credentials_file.read().decode('utf-8')
            try:
                # Parse the JSON to extract client_id and client_secret
                parsed_creds = json.loads(credentials_data)
                client_id = parsed_creds['web']['client_id']
                client_secret = parsed_creds['web']['client_secret']
                
                # Store client_id and client_secret in session for the OAuth flow
                session['client_id_temp'] = client_id
                session['client_secret_temp'] = client_secret

                redirect_uri = request.url_root.rstrip('/') + OAUTH_CALLBACK_PATH
                
                # Initiate the OAuth flow using extracted client ID/Secret
                authorization_url, state = get_gmail_service_flow(client_id, client_secret, redirect_uri)
                session['gmail_oauth_state'] = state
                session['page_tokens'] = [None] # Reset pagination on new auth flow
                session['gmail_credentials_data'] = None # Clear old creds in session

                flash('Redirecting to Google for authentication...', 'info')
                return redirect(authorization_url)

            except Exception as e:
                flash(f'Error processing credentials.json or initiating Google authentication: {str(e)}. Ensure it\'s a valid Web Application JSON.', 'danger')
                # Repopulate form_data for re-render
                form_data['openai_key'] = session['openai_key']
                form_data['summary_email'] = session['summary_email']
                form_data['vip_senders'] = vip_input
                form_data['keywords'] = keywords_input
                return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                                       filter_priority=filter_priority, page=page, has_next=False, has_prev=False)
        else:
            # Persistent env vars are already set, no OAuth needed on POST
            flash("Using persistent Google authentication. No re-authentication needed.", "success")
            return redirect(url_for('index', filter=filter_priority, page=page))

    # --- End of POST Handling ---

    # --- For GET requests (initial page load or after redirects) ---
    form_data['openai_key'] = session.get('openai_key', '')
    form_data['summary_email'] = session.get('summary_email', '')
    form_data['vip_senders'] = ', '.join(session.get('vip_senders', []))
    form_data['keywords'] = ', '.join(session.get('keywords', []))

    # If auth_needed_from_env_or_form is True here, it means:
    # 1. Persistent env vars are missing/invalid.
    # 2. No OAuth flow is currently in progress (not coming from /oauth2callback).
    # So, we need to prompt the user to upload credentials and start the process.
    if auth_needed_from_env_or_form:
        flash('Google authentication is required. Please upload your credentials.json and submit the form.', 'warning')
        return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                               filter_priority=filter_priority, page=page, has_next=False, has_prev=False)

    # If we reached here, 'creds' should be available from the GOOGLE_REFRESH_TOKEN env vars
    # or from session['gmail_credentials_data'] after a successful oauth2callback.
    try:
        # Load credentials from session. This should be populated if auth_needed_from_env_or_form was False
        # (meaning env vars worked) or after a successful oauth2callback.
        if session.get('gmail_credentials_data') is None:
            # Fallback for unexpected state where credentials are not ready.
            flash('Authentication state lost. Please re-authenticate.', 'danger')
            return redirect(url_for('index'))

        creds_json = json.loads(session['gmail_credentials_data'])
        creds = Credentials.from_authorized_user_info(creds_json, SCOPES)
        
        # Final check if credentials are valid, refresh if needed
        if not creds.valid:
            if creds.refresh_token:
                creds.refresh(Request())
                session['gmail_credentials_data'] = creds.to_json()
                session.modified = True
            else:
                flash('Google credentials expired and no refresh token available. Please re-authenticate.', 'danger')
                session.pop('gmail_credentials_data', None)
                session.pop('gmail_oauth_state', None)
                session.pop('client_id_temp', None)
                session.pop('client_secret_temp', None)
                return redirect(url_for('index'))

        service = get_gmail_service(creds)
        openai.api_key = session.get('openai_key', '')

        # --- REST OF YOUR EMAIL PROCESSING LOGIC REMAINS THE SAME ---
        keywords = session.get('keywords', [])
        query_string = None
        if keywords:
            def q_escape(w):
                if ' ' in w:
                    return f'"{w}"'
                return w
            query_string = ' OR '.join([q_escape(k) for k in keywords])

        label_ids = None if query_string else ['INBOX']

        current_page_token = None
        if page > 1 and page <= len(session['page_tokens']):
            current_page_token = session['page_tokens'][page - 1]
        elif page > len(session['page_tokens']):
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
    state = request.args.get('state')
    error = request.args.get('error')

    if error:
        flash(f'Google OAuth Error: {error}', 'danger')
        return redirect(url_for('index'))

    stored_state = session.pop('gmail_oauth_state', None)
    # Get client ID and secret from session (stored from form upload)
    client_id = session.pop('client_id_temp', None)
    client_secret = session.pop('client_secret_temp', None)

    if stored_state is None or stored_state != state or client_id is None or client_secret is None:
        flash('OAuth state mismatch, client secrets missing from session, or session expired. Please try again (re-upload credentials).', 'danger')
        return redirect(url_for('index'))

    try:
        redirect_uri = request.url_root.rstrip('/') + OAUTH_CALLBACK_PATH
        
        credentials = get_gmail_credentials_from_callback(
            client_id, # Pass client_id
            client_secret, # Pass client_secret
            redirect_uri,
            request.url
        )

        session['gmail_credentials_data'] = credentials.to_json()
        session.modified = True

        flash('Successfully authenticated with Google! Please check your Render logs for the refresh token and client secrets to set as environment variables for permanent access.', 'success')
        
        # Don't pop client_id/secret from session here, but rather once user has configured them.
        # This will be handled by the next load logic.
        return redirect(url_for('index', filter=request.args.get('filter', 'all'), page=1))

    except Exception as e:
        flash(f'Error exchanging authorization code: {str(e)}', 'danger')
        return redirect(url_for('index'))


if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5001)
